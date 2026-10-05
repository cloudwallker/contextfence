import datetime as dt
import hashlib
import importlib
import json
import pathlib
import sys
import tempfile
import unittest
import types
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import backup
from ops_common import Ops, OpsError, atomic_json, gate_is_open


def envelope(events):
    return {'format_version': 1, 'complete': True, 'events_sha256': hashlib.sha256(
        json.dumps(events, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(), 'events': events}


def event(sequence=1, state='ACTIVE', **changes):
    return dict({'tenant': 'acme', 'source_id': 'policy', 'sequence': sequence,
                 'content': '' if state == 'DELETED' else 'SYNTHETIC',
                 'readers': [] if state == 'DELETED' else ['alice'], 'state': state,
                 'fresh_until': '2026-10-04T00:00:00Z'}, **changes)


class RecoveryValidationTest(unittest.TestCase):
    def test_post_recovery_migration_grants_use_the_active_service(self):
        with tempfile.TemporaryDirectory() as directory:
            client=Ops(directory); client.runtime.mkdir(parents=True)
            (client.runtime/'database.env').write_text('DB_HOST=recovery-db-abcdef\n')
            calls=[]; client.compose=lambda *args,**kwargs: calls.append(args)
            client.verify_database_roles=lambda: {'runtime':'PASS'}
            client.migrate('sha256:'+'a'*64)
            self.assertEqual('recovery-db-abcdef',calls[-1][-3])

    def test_environment_manifest_reports_actual_engine_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            client=Ops(directory); (client.root/'compose.yaml').write_text(''); (client.root/'Dockerfile').write_text('')
            client.compose=lambda *args,**kwargs: types.SimpleNamespace(stdout='container' if args[-1] in ('api-a','api-b') else '')
            inspect=[{'Image':'sha256:'+'a'*64,'HostConfig':{'NanoCpus':750000000,'Memory':1073741824}}]
            client.run=lambda args,**kwargs: types.SimpleNamespace(stdout=json.dumps(inspect if args[1]=='inspect' else {'ServerVersion':'29.1.3','NCPU':20,'MemTotal':8589934592,'OperatingSystem':'Ubuntu'}))
            client.database=lambda *args,**kwargs: '2'
            record=client.image_manifest()
            self.assertEqual(20,record['baseline']['vm_cpus'])
            self.assertEqual(8,record['baseline']['vm_memory_gib'])
            self.assertEqual(record['docker']['cpus'],record['baseline']['vm_cpus'])

    def test_environment_manifest_inspects_active_recovery_database(self):
        with tempfile.TemporaryDirectory() as directory:
            client=Ops(directory); client.runtime.mkdir(parents=True)
            (client.runtime/'database.env').write_text('DB_HOST=recovery-db-abcdef\n')
            (client.root/'compose.yaml').write_text(''); (client.root/'Dockerfile').write_text('')
            observed=[]
            def compose(*args,**kwargs):
                observed.append(args[-1])
                identity={'api-a':'api-a-container','api-b':'api-b-container',
                          'recovery-db-abcdef':'active-database-container'}.get(args[-1],'')
                return types.SimpleNamespace(stdout=identity)
            def run(args,**kwargs):
                if args[1]=='inspect':
                    database=args[-1]=='active-database-container'
                    value=[{'Image':'sha256:'+('b' if database else 'a')*64,
                            'HostConfig':{'NanoCpus':1000000000 if database else 750000000,
                                          'Memory':2147483648 if database else 1073741824}}]
                else:
                    value={'ServerVersion':'29.1.3','NCPU':20,'MemTotal':8589934592,'OperatingSystem':'Ubuntu'}
                return types.SimpleNamespace(stdout=json.dumps(value))
            client.compose=compose; client.run=run; client.database=lambda *args,**kwargs:'2'
            record=client.image_manifest()
            self.assertEqual('sha256:'+'b'*64,record['infrastructure'].get('postgres',{}).get('image_digest'))
            self.assertEqual(2147483648,record['infrastructure']['postgres']['memory_bytes'])
            self.assertNotIn('postgres',observed,'A stopped source must not stand in for the active recovered database')

    def test_database_client_runs_inside_recovery_service_when_source_server_is_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            client=Ops(directory); client.runtime.mkdir(parents=True)
            (client.root/'.env').write_text('OPS_PROJECT_NAME=contextfence\n')
            (client.runtime/'database.env').write_text('DB_HOST=recovery-db-abcdef\n')
            command=client.database_args('migration')
            self.assertEqual('recovery-db-abcdef',command[-4])
            self.assertIn('PGHOST=recovery-db-abcdef',command)

    def module(self):
        self.assertTrue((ROOT / 'scripts/restore.py').exists(), 'Recovery orchestration is missing')
        return importlib.import_module('restore')

    def test_ledger_preserves_expired_time_and_exact_reader_order_for_checksum(self):
        restore = self.module()
        document = envelope([event(readers=['bob', 'alice']), event(2, content='SYNTHETIC-NEW')])
        state = restore.validate_ledger(json.dumps(document))
        self.assertEqual('2026-10-04T00:00:00Z', state[('acme', 'policy')]['fresh_until'])
        self.assertEqual(2, state[('acme', 'policy')]['sequence'])

    def test_ledger_rejects_gaps_revive_bool_unknown_and_duplicate_keys(self):
        restore = self.module()
        candidates = [envelope([event(), event(3)]), envelope([event(state='DELETED'), event(2)]),
                      envelope([event(sequence=True)]), envelope([event(extra='secret')]),
                      envelope([event(readers=['alice', 'alice'])]), envelope([event(content=None)]),
                      envelope([event(readers=['alice'], state='DELETED')])]
        for document in candidates:
            with self.subTest(document=document):
                with self.assertRaises(restore.RestoreError): restore.validate_ledger(json.dumps(document))
        with self.assertRaises(restore.RestoreError):
            restore.validate_ledger(json.dumps(envelope([event()])).replace('"complete": true', '"complete": true, "complete": true'))

    def test_ledger_rejects_checksum_mutation_without_echoing_body(self):
        restore = self.module()
        document = envelope([event()]); document['events'][0]['content'] = 'PRIVATE-SYNTHETIC-MARKER'
        with self.assertRaises(restore.RestoreError) as failure: restore.validate_ledger(json.dumps(document))
        self.assertNotIn('PRIVATE-SYNTHETIC-MARKER', str(failure.exception))

    def test_isolated_overlay_uses_new_volume_and_current_secrets(self):
        restore = self.module()
        document = restore.isolated_overlay('recovery-db-abc123', 'recovery-data-abc123', 'postgres:17.11-alpine', 'contextfence')
        service = document['services']['recovery-db-abc123']
        self.assertIn('recovery-data-abc123:/var/lib/postgresql/data', service['volumes'])
        self.assertNotIn('postgres-data:/var/lib/postgresql/data', service['volumes'])
        self.assertIn('db-admin', service['secrets'])
        self.assertNotIn('ports', service)
        self.assertEqual('contextfence', service['environment']['POSTGRES_DB'])

    def test_rpo_distinguishes_actual_snapshot_age_and_missing_contexts(self):
        restore = self.module()
        report = restore.loss_report('2026-10-04T00:00:00Z', '2026-10-04T00:00:40Z',
             [{'id': 'old', 'committed_at': '2026-10-04T00:00:01Z'},
              {'id': 'lost', 'committed_at': '2026-10-04T00:00:15Z'}], ['old'])
        self.assertEqual(40, report['snapshot_age_seconds'])
        self.assertEqual(['lost'], report['missing_context_ids'])
        self.assertEqual(25, report['missing_commit_age_seconds'])


class RecoveryFailClosedTest(unittest.TestCase):
    def scenario(self, directory, failure=None):
        import restore
        root = pathlib.Path(directory)
        current = root / '.local/identities.json'; current.parent.mkdir(parents=True)
        current.write_text('{"principals": [{"tenant":"acme","subject":"alice","token":"SYNTHETIC-CURRENT-TOKEN-123456"}]}')
        ledger = root / '.local/ledger.json'
        events = [event(source_id='keep'), event(source_id='revoke'), event(2, source_id='revoke', readers=['bob']),
                  event(source_id='delete'), event(2, 'DELETED', source_id='delete'), event(source_id='new')]
        atomic_json(ledger, envelope(events))
        fixture = root / '.local/fixture.json'
        ids = [str(uuid.uuid4()) for _ in range(3)]
        atomic_json(fixture, {'sources': {'keep':'keep','revoke':'revoke','delete':'delete','new':'new'},
           'old_source':ids[0], 'old_derived':ids[1], 'old_receipt':ids[2], 'markers':['SYNTHETIC-PRIVATE-MARKER'],
           'old_token':'SYNTHETIC-OLD-TOKEN-1234567890', 'committed_contexts':[{'id':ids[0],'committed_at':'2026-10-04T00:00:01Z'}],
           'committed_receipts':[{'id':ids[2],'committed_at':'2026-10-04T00:00:01Z'},{'id':'lost-receipt','committed_at':'2026-10-04T00:00:20Z'}],
           'committed_source_events':[{'id':'acme/'+source+'/'+str(seq),'committed_at':'2026-10-04T00:00:15Z'} for source,seq in [('keep',1),('revoke',1),('delete',1),('revoke',2),('delete',2),('new',1)]]})
        store = backup.BackupStore(root / '.local/backups')
        staging = store.prepare('20261004T000000Z-abcdef12')
        (staging / 'database.dump').write_bytes(b'PGDMP\x00synthetic-test-archive')
        archive = store.publish(staging, {'snapshot_id':'00000003-0000001A-1','snapshot_at':'2026-10-04T00:00:00Z',
           'schema_version':'2','postgres_version':'17.11','context_count':1,'source_count':3,
           'application_images': {'api-a':'sha256:'+'a'*64,'api-b':'sha256:'+'a'*64}})
        atomic_json(root / 'compose.yaml', {})
        (root / '.env').write_text('POSTGRES_DB=contextfence\n')
        class ExternalServices(Ops):
            def __init__(self):
                super().__init__(root); self.calls=[]; self.body={}; self.next=0; self.reconciled=False
            def verify_proxy_closed(self):
                if gate_is_open(self.proxy_gate): raise OpsError('proxy unexpectedly open')
            def compose(self, *args, **kwargs):
                self.calls.append(args)
                if failure == 'restore' and '/ops/restore.sh' in args: raise OpsError('synthetic pg_restore failure')
                if args[-2:] == ('--reconcile-source-ledger','/recovery/ledger.json'):
                    self.reconciled=True
                    return types.SimpleNamespace(stdout=json.dumps({'code':'RECOVERY_COMPLETE','report':{'retired_contexts':1,
                        'sources':[{'tenant':'acme','source_id':source,'sequence':2 if source in ('revoke','delete') else 1,
                        'content_version':2 if source=='delete' else 1,'rebuilt_epoch':2 if source in ('revoke','delete') else 1,
                        'previous_epoch':0 if source=='new' else 1,'maximum_context_epoch':0 if source=='new' else 1,
                        'restored_epoch':3 if source in ('revoke','delete') else 2} for source in ('keep','revoke','delete','new')]}}))
                if args[-2:] == ('--config','-'): return types.SimpleNamespace(stdout='401')
                return types.SimpleNamespace(stdout='')
            def database(self, role, sql, database=None):
                if 'from source_state' in sql and 'json_agg' in sql:
                    rows=list(restore.validate_ledger(ledger.read_text()).values())
                    for row in rows:
                        row['content_version']=2 if row['source_id']=='delete' else 1
                        row['auth_epoch']=3 if row['source_id'] in ('delete','revoke') else 2
                        if failure=='projection-version': row['content_version']=99
                    return json.dumps(rows)
                if 'unretired' in sql: return '{"unretired":0,"matching_epochs":0}'
                if 'json_agg(id' in sql: return json.dumps([ids[0]])
                if 'admission_receipts' in sql: return json.dumps([ids[2]])
                if 'source_events' in sql:
                    identifiers=['acme/keep/1','acme/revoke/1','acme/delete/1']
                    if self.reconciled:
                        identifiers+=['acme/revoke/2','acme/delete/2','acme/new/1']
                        if failure=='rebuild-missing': identifiers[-1]='acme/unrelated/1'
                    return json.dumps(identifiers)
                raise AssertionError('Unexpected external database request')
            def verify_database_roles(self): return {'runtime':'PASS'}
            def wait_ready(self, instance, timeout=120): pass
            def resume(self, instance): self.calls.append(('resume',instance))
            def internal_http(self, instance, method, path, payload=None, principal=('acme','alice')):
                status=200; body={}
                if path == '/v1/source-events': body={'outcome':'APPLIED'}
                elif path.startswith('/v1/receipts/'):
                    body={'id':ids[2],'decision':'ALLOWED'}
                    if failure == 'receipt-body': body['items']=[{'content':'SYNTHETIC-PRIVATE-MARKER'}]
                elif path == '/v1/contexts/source':
                    source=payload['source_id']
                    if source=='revoke': status=403; body={'code':'SOURCE_ACCESS_DENIED'}
                    elif source=='delete': status=410; body={'code':'SOURCE_DELETED'}
                    else:
                        status=201; self.next+=1; identifier=str(uuid.UUID(int=self.next)); body={'id':identifier}; self.body[identifier]='unexpected' if failure=='fresh-body' else 'SYNTHETIC'
                elif path == '/v1/contexts/derived':
                    if ids[1] in payload['parent_ids']: status=410; body={'code':'CONTEXT_RETIRED'}
                    else:
                        status=201; self.next+=1; identifier=str(uuid.UUID(int=self.next)); body={'id':identifier}; self.body[identifier]=payload['content']
                elif path == '/v1/contexts/assemble':
                    requested=payload['context_ids']
                    if principal[0]=='beta': status=404; body={'code':'NOT_FOUND'}
                    elif any(identifier in ids[:2] for identifier in requested): status=410; body={'code':'CONTEXT_RETIRED','items':[]}
                    else: body={'code':'ALLOWED','items':[{'id':identifier,'content':self.body[identifier]} for identifier in requested]}
                else: raise AssertionError('Unexpected external HTTP request')
                return {'status':status,'body':body,'headers':{'cache-control':'no-store'}}
        return ExternalServices(), archive, ledger, fixture, current

    def test_all_checks_open_only_after_both_instances_and_preserve_current_identities(self):
        import restore
        with tempfile.TemporaryDirectory() as directory:
            client, archive, ledger, fixture, current = self.scenario(directory)
            original=current.read_bytes()
            report=restore.run_restore(client, archive, ledger, fixture, '2026-10-04T00:00:40Z')
            self.assertEqual('PASS',report['status']); self.assertTrue(report['safety']['all_passed'])
            self.assertEqual(20,len(report['safety']['observations']))
            self.assertTrue(gate_is_open(client.proxy_gate)); self.assertTrue(gate_is_open(client.app_gate))
            self.assertEqual(original,current.read_bytes())
            overlay=json.loads((client.runtime/'recovery.compose.yaml').read_text())
            self.assertEqual(1,len(overlay['volumes'])); self.assertNotIn('postgres-data',overlay['volumes'])
            self.assertIn(('resume','api-a'),client.calls); self.assertIn(('resume','api-b'),client.calls)
            self.assertFalse(any('down' in call or '--volumes' in call for call in client.calls))
            self.assertIn('receipt_history',report['rpo'],'Confirmed missing receipts are not measured')
            self.assertEqual(1,report['rpo']['receipt_history']['missing_receipt_count'])
            self.assertEqual(3,report['rpo']['source_event_history']['missing_source_event_count'])

    def test_restore_error_or_receipt_body_leak_never_opens_proxy(self):
        import restore
        for failure in ('restore','receipt-body','projection-version','fresh-body'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                client, archive, ledger, fixture, current = self.scenario(directory,failure)
                with self.assertRaises(restore.RestoreError):
                    restore.run_restore(client, archive, ledger, fixture, '2026-10-04T00:00:40Z')
                self.assertFalse(gate_is_open(client.proxy_gate)); self.assertFalse(gate_is_open(client.app_gate))
                self.assertNotIn(('resume','api-a'),client.calls)

    def test_ledger_missing_confirmed_source_tail_is_rejected_before_creating_a_database(self):
        import restore
        with tempfile.TemporaryDirectory() as directory:
            client, archive, ledger, fixture, current = self.scenario(directory)
            document=json.loads(fixture.read_text())
            document['committed_source_events'].append({'id':'acme/keep/3','committed_at':'2026-10-04T00:00:30Z'})
            atomic_json(fixture,document)
            with self.assertRaises(restore.RestoreError):
                restore.run_restore(client,archive,ledger,fixture,'2026-10-04T00:00:40Z')
            self.assertFalse(gate_is_open(client.proxy_gate)); self.assertFalse(gate_is_open(client.app_gate))
            self.assertFalse((client.runtime/'recovery.compose.yaml').exists())
            self.assertFalse(any(call[0]=='up' for call in client.calls))

    def test_missing_rebuilt_event_cannot_be_replaced_by_an_equal_count_of_other_events(self):
        import restore
        with tempfile.TemporaryDirectory() as directory:
            client, archive, ledger, fixture, current = self.scenario(directory,'rebuild-missing')
            with self.assertRaises(restore.RestoreError):
                restore.run_restore(client,archive,ledger,fixture,'2026-10-04T00:00:40Z')
            self.assertFalse(gate_is_open(client.proxy_gate)); self.assertFalse(gate_is_open(client.app_gate))
            self.assertNotIn(('resume','api-a'),client.calls)
            report=json.loads((client.root/'artifacts/local/recovery.json').read_text())
            self.assertEqual('FAIL_CLOSED',report['status'])
            self.assertNotIn('rebuilt_missing_records',report.get('rpo',{}).get('source_event_history',{}))

    def test_verified_rebuild_preserves_actual_pre_reconciliation_history_loss(self):
        import restore
        with tempfile.TemporaryDirectory() as directory:
            client, archive, ledger, fixture, current = self.scenario(directory)
            report=restore.run_restore(client,archive,ledger,fixture,'2026-10-04T00:00:40Z')
            history=report['rpo']['source_event_history']
            self.assertEqual('PASS',report['status'])
            self.assertEqual(['acme/revoke/2','acme/delete/2','acme/new/1'],history['missing_source_event_ids'])
            self.assertEqual(3,history['missing_source_event_count'])
            self.assertEqual(3,history['rebuilt_missing_records'])
            self.assertFalse(history['original_application_timestamps_recovered'])

    def test_missing_confirmed_history_never_reports_zero_loss_or_creates_a_database(self):
        import restore
        for field in ('committed_contexts','committed_receipts','committed_source_events'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                client, archive, ledger, fixture, current = self.scenario(directory)
                document=json.loads(fixture.read_text()); document.pop(field); atomic_json(fixture,document)
                with self.assertRaises(restore.RestoreError):
                    restore.run_restore(client,archive,ledger,fixture,'2026-10-04T00:00:40Z')
                self.assertFalse(gate_is_open(client.proxy_gate)); self.assertFalse(gate_is_open(client.app_gate))
                self.assertFalse((client.runtime/'recovery.compose.yaml').exists())

    def test_invalid_backup_closes_both_gates_and_stops_apps_before_validation(self):
        self.assertTrue((ROOT / 'scripts/restore.py').exists(), 'Recovery orchestration is missing')
        restore = importlib.import_module('restore')
        with tempfile.TemporaryDirectory() as directory:
            client = Ops(directory); client.verify_proxy_closed = lambda: None
            calls = []
            client.compose = lambda *args, **kwargs: calls.append(args)
            client.open_maintenance('fixture', verified=True)
            identities = pathlib.Path(directory) / '.local/identities.json'
            identities.write_text('{"current": "synthetic-current-token"}')
            original = identities.read_bytes()
            with self.assertRaises(restore.RestoreError):
                restore.run_restore(client, pathlib.Path(directory) / 'missing', pathlib.Path(directory) / 'ledger.json',
                                    pathlib.Path(directory) / 'fixture.json', '2026-10-04T00:00:40Z')
            self.assertFalse(gate_is_open(client.app_gate)); self.assertFalse(gate_is_open(client.proxy_gate))
            self.assertIn(('stop', 'api-a', 'api-b'), calls)
            self.assertEqual(original, identities.read_bytes())
            self.assertFalse((client.runtime / 'recovery.compose.yaml').exists())


if __name__ == '__main__': unittest.main()
