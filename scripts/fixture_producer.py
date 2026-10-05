#!/usr/bin/env python3
"""Trusted synthetic upstream producer; the complete ledger precedes database writes."""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import secrets
import uuid

from ops_common import Ops, OpsError, atomic_bytes, atomic_json, strict_object, utc_now


def append_event(path, tenant, event):
    from restore import validate_ledger
    path = pathlib.Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    try: descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError: raise OpsError('Independent ledger producer overlap or stale lock') from None
    try:
        os.close(descriptor)
        if path.exists():
            raw = path.read_text(encoding='utf-8'); validate_ledger(raw)
            events = json.loads(raw, object_pairs_hook=strict_object)['events']
        else: events = []
        events.append({'tenant': tenant, **event})
        document = {'format_version': 1, 'complete': True, 'events_sha256': hashlib.sha256(
            json.dumps(events, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest(), 'events': events}
        encoded = json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2)
        validate_ledger(encoded)
        atomic_bytes(path, (encoded + '\n').encode('utf-8'), mode=0o600)
    finally: lock.unlink()


def record_event(ops, event, tenant='acme', ledger_path=None, instance='api-a'):
    ledger_path = ledger_path or ops.root / '.local/recovery/ledger.json'
    append_event(ledger_path, tenant, event)
    reply = ops.internal_http(instance, 'POST', '/v1/source-events', event, (tenant, 'writer'))
    if reply['status'] != 200 or reply['body'].get('outcome') != 'APPLIED': raise OpsError('Trusted synthetic source event was not applied')
    return reply['body']


def fresh_deadline(): return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=240)).isoformat()


def expect(ops, instance, path, payload=None, principal=('acme', 'alice'), status=200, code=None, markers=()):
    reply = ops.internal_http(instance, 'POST' if payload is not None else 'GET', path, payload, principal)
    body = reply['body']
    if reply['status'] != status or (code is not None and body.get('code', body.get('outcome')) != code): raise OpsError('Synthetic recovery safety decision failed')
    if status >= 400 and (body.get('items') or any(marker in json.dumps(body) for marker in markers)): raise OpsError('Denied recovery response exposed a body')
    if 'no-store' not in reply.get('headers', {}).get('cache-control', ''): raise OpsError('Recovery response lacks no-store')
    return body


def create_source(ops, source_id, fixture, instance='api-a'):
    handle = expect(ops, instance, '/v1/contexts/source', {'source_id': source_id, 'ttl_seconds': 300}, status=201)
    fixture['committed_contexts'].append({'id': handle['id'], 'committed_at': utc_now()})
    return handle['id']


def remember_source_event(fixture,event,tenant='acme'):
    fixture.setdefault('committed_source_events',[]).append({'id':tenant+'/'+event['source_id']+'/'+str(event['sequence']),'committed_at':utc_now()})


def remember_receipt(fixture,decision):
    fixture.setdefault('committed_receipts',[]).append({'id':decision['receipt']['id'],'committed_at':utc_now()})


def prepare_fixture(ops, ledger_path, fixture_path):
    from restore import validate_ledger
    latest = validate_ledger(pathlib.Path(ledger_path).read_text()) if pathlib.Path(ledger_path).exists() else {}
    keys = json.loads(ops.database('backup', "select coalesce(json_agg(json_build_array(tenant,source_id)), '[]') from source_state;"))
    if set(map(tuple, keys)) != set(latest): raise OpsError('Use a clean recovery project or a complete independent producer ledger')
    prefix = 'recovery.' + uuid.uuid4().hex[:12]
    fixture = {'format_version': 1, 'prefix': prefix, 'sources': {}, 'old_context_ids': [], 'committed_contexts': [], 'committed_receipts':[], 'committed_source_events':[],
               'markers': ['SYNTHETIC-RECOVERY-' + uuid.uuid4().hex, 'SYNTHETIC-RECOVERY-DERIVED-' + uuid.uuid4().hex]}
    for kind in ('keep', 'revoke', 'delete'):
        source_id = prefix + '.' + kind
        event = {'source_id': source_id, 'sequence': 1, 'content': fixture['markers'][0] + '-' + kind, 'readers': ['alice', 'bob'], 'state': 'ACTIVE', 'fresh_until': fresh_deadline()}
        record_event(ops, event, ledger_path=ledger_path)
        remember_source_event(fixture,event)
        fixture['sources'][kind] = source_id
        handle = create_source(ops, source_id, fixture)
        fixture['old_context_ids'].append(handle)
        if kind == 'keep': fixture['old_source'] = handle
    derived = expect(ops, 'api-a', '/v1/contexts/derived', {'content': fixture['markers'][1], 'parent_ids': [fixture['old_source']], 'ttl_seconds': 300}, status=201)
    fixture['old_derived'] = derived['id']; fixture['old_context_ids'].append(derived['id'])
    fixture['committed_contexts'].append({'id': derived['id'], 'committed_at': utc_now()})
    for instance in ('api-a', 'api-b'):
        for _ in range(2):
            decision = expect(ops, instance, '/v1/contexts/assemble', {'context_ids': fixture['old_context_ids']}, code='ALLOWED')
            remember_receipt(fixture,decision)
            if instance == 'api-a': fixture['old_receipt'] = decision['receipt']['id']
    atomic_bytes(fixture_path, (json.dumps(fixture, sort_keys=True, indent=2) + '\n').encode(), mode=0o600)
    return fixture


def mutate_fixture(ops, ledger_path, fixture_path):
    from restore import validate_ledger
    fixture = json.loads(pathlib.Path(fixture_path).read_text(), object_pairs_hook=strict_object)
    latest = validate_ledger(pathlib.Path(ledger_path).read_text())
    for kind in ('revoke', 'delete'):
        previous = latest[('acme', fixture['sources'][kind])]
        event = {name: value for name, value in previous.items() if name != 'tenant'}
        event.update(sequence=event['sequence'] + 1, fresh_until=fresh_deadline())
        if kind == 'revoke': event['readers'] = ['bob']
        else: event.update(state='DELETED', content='', readers=[])
        record_event(ops, event, ledger_path=ledger_path)
        remember_source_event(fixture,event)
    fixture['sources']['new'] = fixture['prefix'] + '.new'
    new_event={'source_id': fixture['sources']['new'], 'sequence': 1, 'content': fixture['markers'][0] + '-new', 'readers': ['alice'], 'state': 'ACTIVE', 'fresh_until': fresh_deadline()}
    record_event(ops,new_event,ledger_path=ledger_path); remember_source_event(fixture,new_event)
    fixture['post_snapshot_context'] = create_source(ops, fixture['sources']['new'], fixture)
    path = ops.root / '.local/identities.json'
    identities = json.loads(path.read_text(), object_pairs_hook=strict_object)
    principal = next(row for row in identities['principals'] if (row['tenant'], row['subject']) == ('acme', 'alice'))
    fixture['old_token'] = principal['token']; principal['token'] = secrets.token_hex(32)
    ops.close_maintenance('recovery-token-rotation')
    atomic_bytes(path, (json.dumps(identities, sort_keys=True, indent=2) + '\n').encode(), mode=0o644)
    ops.compose('up', '--detach', '--no-deps', '--no-build', '--force-recreate', 'api-a', 'api-b', timeout=180)
    ops.open_app_gate('recovery-token-rotation-checks')
    for instance in ('api-a', 'api-b'):
        ops.wait_ready(instance)
        if old_token_status(ops,instance,fixture['old_token'])!='401': raise OpsError('Credential rotation did not reject the old token')
        for _ in range(2):
            decision=expect(ops,instance,'/v1/contexts/assemble',{'context_ids':[fixture['old_source'],fixture['old_derived']]},code='ALLOWED')
            remember_receipt(fixture,decision)
    ops.open_maintenance('recovery-token-rotation-verified', verified=True)
    atomic_bytes(fixture_path, (json.dumps(fixture, sort_keys=True, indent=2) + '\n').encode(), mode=0o600)
    return fixture


def old_token_status(ops, instance, token):
    if not isinstance(token, str) or not re_token(token): raise OpsError('Invalid private old token fixture')
    configuration = 'silent\nmax-time = 15\noutput = "/dev/null"\nwrite-out = "%{http_code}"\nurl = "http://127.0.0.1:8080/v1/receipts/00000000-0000-0000-0000-000000000000"\nheader = "Authorization: Bearer ' + token + '"\n'
    result = ops.compose('exec', '-T', instance, 'curl', '--config', '-', input=configuration, timeout=25)
    return result.stdout.strip()


def re_token(token):
    import re
    return re.fullmatch(r'[A-Za-z0-9._~-]{24,256}', token)


def verify_recovered_fixture(ops, fixture, latest, ledger_path):
    observations = []
    # Explicit trusted producer renewal is outside the recovery CLI and preserves current ACL/state.
    for (tenant, source_id), previous in latest.items():
        if previous['state'] == 'ACTIVE':
            event = {name: value for name, value in previous.items() if name != 'tenant'}
            event.update(sequence=previous['sequence'] + 1, fresh_until=fresh_deadline())
            record_event(ops, event, tenant, ledger_path)
    for instance in ('api-a', 'api-b'):
        if old_token_status(ops, instance, fixture['old_token']) != '401': raise OpsError('Rotated old credential remains valid after restore')
        for phase, identifier in (('old-source', fixture['old_source']), ('old-derived', fixture['old_derived'])):
            expect(ops, instance, '/v1/contexts/assemble', {'context_ids': [identifier]}, status=410, code='CONTEXT_RETIRED', markers=fixture['markers'])
            observations.append({'instance': instance, 'phase': phase, 'passed': True})
        receipt = expect(ops, instance, '/v1/receipts/' + fixture['old_receipt'])
        if 'items' in receipt or any(marker in json.dumps(receipt) for marker in fixture['markers']): raise OpsError('Historical receipt exposed old body')
        expect(ops, instance, '/v1/contexts/derived', {'content': 'SYNTHETIC-REPLACEMENT', 'parent_ids': [fixture['old_derived']], 'ttl_seconds': 180}, status=410, code='CONTEXT_RETIRED', markers=fixture['markers'])
        expect(ops, instance, '/v1/contexts/source', {'source_id': fixture['sources']['revoke'], 'ttl_seconds': 180}, status=403, code='SOURCE_ACCESS_DENIED', markers=fixture['markers'])
        expect(ops, instance, '/v1/contexts/source', {'source_id': fixture['sources']['delete'], 'ttl_seconds': 180}, status=410, code='SOURCE_DELETED', markers=fixture['markers'])
        handles = [create_source(ops, fixture['sources'][kind], fixture, instance) for kind in ('keep', 'new')]
        derived = expect(ops, instance, '/v1/contexts/derived', {'content': 'SYNTHETIC-NEW-RECOVERY-DERIVED', 'parent_ids': handles, 'ttl_seconds': 180}, status=201)
        decision = expect(ops, instance, '/v1/contexts/assemble', {'context_ids': handles + [derived['id']]}, code='ALLOWED')
        expected_contents=[latest[('acme',fixture['sources'][kind])]['content'] for kind in ('keep','new')]+['SYNTHETIC-NEW-RECOVERY-DERIVED']
        if [item.get('content') for item in decision.get('items',[])]!=expected_contents: raise OpsError('Current authorized handles returned unexpected contents')
        expect(ops, instance, '/v1/contexts/assemble', {'context_ids': [handles[0], fixture['old_source']]}, status=410, code='CONTEXT_RETIRED', markers=fixture['markers'])
        expect(ops, instance, '/v1/contexts/assemble', {'context_ids': [handles[0]]}, principal=('beta', 'alice'), status=404, code='NOT_FOUND', markers=fixture['markers'])
        observations.extend({'instance': instance, 'phase': phase, 'passed': True} for phase in ('receipt-metadata-only', 'old-parent', 'revoked', 'deleted', 'new-authorized', 'mixed-batch', 'cross-tenant', 'old-token'))
    return {'all_passed': True, 'observations': observations, 'source_renewal': 'explicit trusted producer events after reconciliation'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['seed', 'mutate'])
    parser.add_argument('--root', default=str(pathlib.Path(__file__).resolve().parents[1]))
    args = parser.parse_args(); ops = Ops(args.root)
    try:
        directory = ops.root / '.local/recovery'; directory.mkdir(parents=True, exist_ok=True)
        action = prepare_fixture if args.action == 'seed' else mutate_fixture
        action(ops, directory / 'ledger.json', directory / 'fixture.json')
        print('PASS: independent synthetic recovery fixture ' + args.action + '; private tokens and bodies omitted')
        return 0
    except (OpsError, OSError, ValueError, StopIteration):
        print('FAIL: synthetic recovery producer; complete private ledger and gate state require inspection')
        return 1


if __name__ == '__main__': raise SystemExit(main())
