#!/usr/bin/env python3
"""Restore into a new PostgreSQL service and open traffic only after authority verification."""
import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import time
import uuid

from backup import validate_backup, timestamp
from ops_common import Ops, OpsError, atomic_bytes, atomic_json, strict_object, utc_now


class RestoreError(OpsError): pass


EVENT_FIELDS = {'tenant', 'source_id', 'sequence', 'content', 'readers', 'state', 'fresh_until'}
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}')


def validate_ledger(raw):
    try:
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 16 * 1024 * 1024: raise ValueError()
        document = json.loads(raw, object_pairs_hook=strict_object)
        if set(document) != {'format_version', 'complete', 'events_sha256', 'events'}: raise ValueError()
        if type(document['format_version']) is not int or document['format_version'] != 1 or document['complete'] is not True: raise ValueError()
        events = document['events']
        if not isinstance(events, list) or not 1 <= len(events) <= 10000: raise ValueError()
        digest = hashlib.sha256(json.dumps(events, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        if document['events_sha256'] != digest: raise ValueError()
        state = {}
        for event in events:
            if not isinstance(event, dict) or set(event) != EVENT_FIELDS: raise ValueError()
            for name in ('tenant', 'source_id'):
                if not isinstance(event[name], str) or not IDENTIFIER.fullmatch(event[name]): raise ValueError()
            if type(event['sequence']) is not int or not 1 <= event['sequence'] <= 9223372036854775807: raise ValueError()
            readers = event['readers']; content = event['content']
            if not isinstance(readers, list) or len(readers) > 100: raise ValueError()
            if any(not isinstance(reader, str) or not IDENTIFIER.fullmatch(reader) for reader in readers) or len(set(readers)) != len(readers): raise ValueError()
            if not isinstance(content, str) or '\x00' in content or len(content.encode('utf-8')) > 65536: raise ValueError()
            if event['state'] not in ('ACTIVE', 'DELETED'): raise ValueError()
            if event['state'] == 'DELETED' and (content or readers): raise ValueError()
            deadline = timestamp(event['fresh_until'])
            if not dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) <= deadline <= dt.datetime(2100, 1, 1, tzinfo=dt.timezone.utc): raise ValueError()
            key = (event['tenant'], event['source_id']); previous = state.get(key)
            if event['sequence'] != (previous['sequence'] + 1 if previous else 1): raise ValueError()
            if previous and previous['state'] == 'DELETED': raise ValueError()
            state[key] = event.copy()
        return state
    except (ValueError, TypeError, KeyError, UnicodeError, OpsError):
        raise RestoreError('Independent source ledger is invalid or incomplete') from None


def isolated_overlay(host, volume, image, database):
    if not re.fullmatch(r'recovery-db-[0-9a-f]{6,32}', host) or not re.fullmatch(r'recovery-data-[0-9a-f]{6,32}', volume): raise RestoreError('Invalid isolated recovery identity')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,62}', database): raise RestoreError('Invalid recovery database')
    if not re.fullmatch(r'[A-Za-z0-9_./:@-]+', image): raise RestoreError('Invalid PostgreSQL image')
    return {'services': {host: {'image': image, 'restart': 'unless-stopped', 'cpus': 1.0, 'mem_limit': '2g',
             'environment': {'POSTGRES_DB': database, 'POSTGRES_USER': 'cf_admin', 'POSTGRES_PASSWORD_FILE': '/run/secrets/db-admin', 'POSTGRES_INITDB_ARGS': '--auth-host=scram-sha-256'},
             'command': ['postgres', '-c', 'max_connections=80', '-c', 'shared_buffers=512MB', '-c', 'log_statement=none', '-c', 'log_min_error_statement=panic'],
             'secrets': ['db-admin', 'db-migration', 'db-runtime', 'db-backup', 'db-monitor'],
             'volumes': [volume + ':/var/lib/postgresql/data', './ops/postgres:/ops:ro', './ops/postgres/init.sh:/docker-entrypoint-initdb.d/10-contextfence.sh:ro'],
             'networks': ['backend'], 'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}},
             'healthcheck': {'test': ['CMD-SHELL', 'pg_isready -U cf_admin -d "$POSTGRES_DB"'], 'interval': '3s', 'timeout': '3s', 'retries': 30}}},
             'volumes': {volume: {}}}


def loss_report(snapshot_at, failure_at, committed_contexts, restored_context_ids, entity='context'):
    if entity not in ('context','receipt','source_event'): raise RestoreError('Invalid measured history entity')
    snapshot = timestamp(snapshot_at); failure = timestamp(failure_at)
    restored = set(restored_context_ids)
    missing = [row for row in committed_contexts if row['id'] not in restored]
    return {'snapshot_at': snapshot_at, 'snapshot_age_seconds': max(0, (failure - snapshot).total_seconds()),
            'confirmed_'+entity+'_count': len(committed_contexts), 'missing_'+entity+'_ids': [row['id'] for row in missing],
            'missing_'+entity+'_count': len(missing),
            'missing_commit_age_seconds': max([max(0, (failure - timestamp(row['committed_at'])).total_seconds()) for row in missing] or [0]),
            'scope': 'confirmed synthetic '+entity+' history at backup snapshot; source and ACL projection rebuilt separately'}


def validate_confirmed_history(fixture, failure_at):
    failure=timestamp(failure_at)
    for name in ('committed_contexts','committed_receipts','committed_source_events'):
        rows=fixture.get(name)
        if not isinstance(rows,list) or len(rows)>100000: raise RestoreError('Confirmed recovery history is incomplete')
        identifiers=set()
        for row in rows:
            if not isinstance(row,dict) or set(row)!={'id','committed_at'}: raise RestoreError('Confirmed recovery history is invalid')
            identifier=row['id']
            if not isinstance(identifier,str) or not 1<=len(identifier)<=192 or identifier in identifiers: raise RestoreError('Confirmed recovery history is invalid')
            if timestamp(row['committed_at'])>failure: raise RestoreError('Confirmed recovery commit occurs after fault injection')
            identifiers.add(identifier)


def verify_projection(ops, latest, events, reconciliation):
    versions = {}; previous_events = {}
    for event in events:
        key=(event['tenant'],event['source_id']); previous=previous_events.get(key)
        old_version,old_epoch=versions.get(key,(0,0))
        versions[key]=(old_version + int(previous is None or previous['content']!=event['content']),
                       old_epoch + int(previous is None or sorted(previous['readers'])!=sorted(event['readers']) or previous['state']!=event['state']))
        previous_events[key]=event
    reports={(row['tenant'],row['source_id']):row for row in reconciliation['sources']}
    if len(reports)!=len(reconciliation['sources']) or set(reports)!=set(latest): raise RestoreError('Recovery epoch report is incomplete')
    raw = ops.database('migration', "select coalesce(json_agg(json_build_object('tenant',tenant,'source_id',source_id,'sequence',last_sequence,'content_version',content_version,'auth_epoch',auth_epoch,'content',content,'readers',readers,'state',state,'fresh_until',fresh_until)), '[]') from source_state;")
    rows = json.loads(raw, object_pairs_hook=strict_object)
    actual = {(row['tenant'], row['source_id']): row for row in rows}
    if set(actual) != set(latest): raise RestoreError('Recovered source watermarks are incomplete')
    for key, expected in latest.items():
        row = actual[key]
        if any(row[name] != expected[name] for name in ('sequence', 'content', 'state')) or sorted(row['readers']) != sorted(expected['readers']): raise RestoreError('Recovered source authority differs from complete ledger')
        if timestamp(row['fresh_until']) != timestamp(expected['fresh_until']): raise RestoreError('Recovery modified source freshness')
        record=reports[key]; version,epoch=versions[key]
        if set(record)!={'tenant','source_id','sequence','content_version','rebuilt_epoch','previous_epoch','maximum_context_epoch','restored_epoch'}: raise RestoreError('Recovery epoch report is invalid')
        if any(type(record[name]) is not int or record[name]<0 for name in ('sequence','content_version','rebuilt_epoch','previous_epoch','maximum_context_epoch','restored_epoch')): raise RestoreError('Recovery epoch report is invalid')
        if record['sequence']!=expected['sequence'] or record['content_version']!=version or record['rebuilt_epoch']!=epoch: raise RestoreError('Recovery history projection differs from independent ledger')
        if record['restored_epoch']!=max(epoch,record['previous_epoch'],record['maximum_context_epoch'])+1 or record['restored_epoch']>9223372036854775807: raise RestoreError('Recovered authorization generation is unsafe')
        if row['content_version']!=version or row['auth_epoch']!=record['restored_epoch']: raise RestoreError('Recovered version or epoch is inconsistent')
    counts = ops.database('migration', "select json_build_object('unretired',(select count(*) from context_items where retired_at is null or content<>''),'matching_epochs',(select count(*) from context_sources cs join source_state s using(tenant,source_id) where cs.auth_epoch=s.auth_epoch));")
    if json.loads(counts) != {'unretired': 0, 'matching_epochs': 0}: raise RestoreError('Restored context or epoch retirement is incomplete')
    return {'source_watermarks_verified': len(latest), 'all_old_contexts_retired': True, 'old_bindings_have_different_epochs': True}


def run_restore(ops, backup_directory, ledger_path, fixture_path, failure_at):
    started = time.monotonic(); report = {'format_version': 1, 'status': 'CLOSED', 'failure_at': failure_at}
    evidence = ops.root / 'artifacts/local/recovery.json'
    try:
        ops.close_maintenance('database-recovery')
        ops.compose('stop', 'api-a', 'api-b')
        metadata = validate_backup(backup_directory)
        ledger_path = pathlib.Path(ledger_path).resolve()
        if ledger_path.is_symlink() or ledger_path.stat().st_size > 16 * 1024 * 1024: raise RestoreError('Independent source ledger is unavailable')
        ledger_raw=ledger_path.read_text(encoding='utf-8')
        latest = validate_ledger(ledger_raw)
        events=json.loads(ledger_raw,object_pairs_hook=strict_object)['events']
        fixture = json.loads(pathlib.Path(fixture_path).read_text(encoding='utf-8'), object_pairs_hook=strict_object)
        validate_confirmed_history(fixture,failure_at)
        ledger_event_ids = {event['tenant']+'/'+event['source_id']+'/'+str(event['sequence']) for event in events}
        confirmed_event_ids = {row['id'] for row in fixture['committed_source_events']}
        if not confirmed_event_ids.issubset(ledger_event_ids): raise RestoreError('Independent source ledger omits confirmed source history')
        current_identity_hash = hashlib.sha256((ops.root / '.local/identities.json').read_bytes()).hexdigest()
        suffix = uuid.uuid4().hex[:12]; host = 'recovery-db-' + suffix; volume = 'recovery-data-' + suffix
        environment = ops.env(); database = environment.get('POSTGRES_DB', 'contextfence')
        overlay = isolated_overlay(host, volume, environment.get('POSTGRES_IMAGE', 'public.ecr.aws/docker/library/postgres@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24'), database)
        overlay_path = ops.runtime / 'recovery.compose.yaml'
        if overlay_path.exists():
            previous = json.loads(overlay_path.read_text(), object_pairs_hook=strict_object)
            for name in ('services', 'volumes'): overlay[name] = {**previous.get(name, {}), **overlay[name]}
        atomic_json(overlay_path, overlay)
        ops.compose('up', '--detach', '--wait', '--wait-timeout', '120', '--no-build', host, timeout=180)
        remote = '/tmp/contextfence-recovery.dump'
        ops.compose('cp', str(pathlib.Path(backup_directory).resolve() / 'database.dump'), host + ':' + remote, timeout=600)
        ops.compose('exec', '-T', host, 'sh', '/ops/restore.sh', remote, timeout=900)
        atomic_bytes(ops.runtime / 'database.env', ('DB_HOST=' + host + '\n').encode())
        ops.compose('run', '--rm', '--no-deps', 'migrate', '--migrate-only', timeout=180)
        ops.compose('exec', '-T', host, 'sh', '/ops/grants.sh', timeout=30)
        ops.verify_database_roles()
        restored_ids = json.loads(ops.database('migration', "select coalesce(json_agg(id::text),'[]') from context_items;"))
        restored_receipts = json.loads(ops.database('migration', "select coalesce(json_agg(r.id::text),'[]') from admission_receipts r;"))
        restored_events = json.loads(ops.database('migration', "select coalesce(json_agg(tenant||'/'||source_id||'/'||sequence::text),'[]') from source_events;"))
        result = ops.compose('run', '--rm', '--no-deps', '--user', '0:0', '--volume', str(ledger_path) + ':/recovery/ledger.json:ro',
              '-e', 'CONTEXTFENCE_GATE_FILE=/runtime/app-gate.json', 'migrate', '--reconcile-source-ledger', '/recovery/ledger.json', timeout=180)
        reconciliation = json.loads(result.stdout, object_pairs_hook=strict_object)
        if reconciliation.get('code') != 'RECOVERY_COMPLETE': raise RestoreError('Source reconciliation did not complete')
        report.update(verify_projection(ops, latest, events, reconciliation['report']))
        report['rpo'] = loss_report(metadata['snapshot_at'], failure_at, fixture['committed_contexts'], restored_ids)
        report['rpo']['receipt_history']=loss_report(metadata['snapshot_at'],failure_at,fixture['committed_receipts'],restored_receipts,entity='receipt')
        report['rpo']['source_event_history']=loss_report(metadata['snapshot_at'],failure_at,fixture['committed_source_events'],restored_events,entity='source_event')
        source_history=report['rpo']['source_event_history']
        reconciled_event_ids = set(json.loads(ops.database('migration', "select coalesce(json_agg(tenant||'/'||source_id||'/'||sequence::text),'[]') from source_events;")))
        missing_event_ids = set(source_history['missing_source_event_ids'])
        rebuilt_missing_ids = missing_event_ids.intersection(reconciled_event_ids)
        if rebuilt_missing_ids != missing_event_ids: raise RestoreError('Source reconciliation omitted confirmed source history')
        source_history['rebuilt_missing_records']=len(rebuilt_missing_ids)
        source_history['original_application_timestamps_recovered']=source_history['missing_source_event_count']==0
        report['rpo']['oldest_missing_confirmed_commit_age_seconds']=max(report['rpo']['missing_commit_age_seconds'],report['rpo']['receipt_history']['missing_commit_age_seconds'],source_history['missing_commit_age_seconds'])
        report['reconciliation'] = reconciliation['report']
        report.update({'backup_id': metadata['backup_id'], 'source_database_preserved': True, 'restored_host': host, 'restored_volume': volume})
        if hashlib.sha256((ops.root / '.local/identities.json').read_bytes()).hexdigest() != current_identity_hash: raise RestoreError('Current identity mapping changed during database restore')
        ops.compose('up', '--detach', '--no-deps', '--no-build', '--force-recreate', 'api-a', 'api-b', 'postgres-exporter', timeout=180)
        ops.open_app_gate('recovery-internal-checks')
        for instance in ('api-a', 'api-b'): ops.wait_ready(instance)
        from fixture_producer import verify_recovered_fixture
        report['safety'] = verify_recovered_fixture(ops, fixture, latest, ledger_path)
        if hashlib.sha256((ops.root / '.local/identities.json').read_bytes()).hexdigest() != current_identity_hash: raise RestoreError('Current identity mapping changed during safety verification')
        ops.verify_proxy_closed()
        for instance in ('api-a', 'api-b'): ops.resume(instance)
        ops.open_maintenance('database-recovery-verified', verified=True)
        report['safe_open_at'] = utc_now()
        report['rto_seconds'] = max(0, (timestamp(report['safe_open_at']) - timestamp(failure_at)).total_seconds())
        report['execution_seconds'] = time.monotonic() - started
        report['status'] = 'PASS'
        atomic_json(evidence, report)
        return report
    except Exception:
        try: ops.close_maintenance('database-recovery-failed')
        finally:
            report['status'] = 'FAIL_CLOSED'; atomic_json(evidence, report)
        raise RestoreError('Recovery failed; both traffic gates remain closed and source volumes were preserved') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument('--backup', required=True, type=pathlib.Path)
    parser.add_argument('--ledger', required=True, type=pathlib.Path)
    parser.add_argument('--fixture', required=True, type=pathlib.Path)
    parser.add_argument('--failure-at', required=True)
    args = parser.parse_args()
    try:
        result = run_restore(Ops(args.root), args.backup, args.ledger, args.fixture, args.failure_at)
        print(json.dumps({'status': result['status'], 'rto_seconds': result['rto_seconds'], 'snapshot_age_seconds': result['rpo']['snapshot_age_seconds'], 'missing_context_count': result['rpo']['missing_context_count']}))
        return 0
    except (OpsError, OSError, ValueError):
        print('FAIL: recovery remains closed; inspect private structured recovery evidence')
        return 1


if __name__ == '__main__': raise SystemExit(main())
