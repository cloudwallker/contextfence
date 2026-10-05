import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))


class OpsContractTest(unittest.TestCase):
    def module(self, name):
        path = ROOT / 'scripts' / (name + '.py')
        self.assertTrue(path.exists(), 'Missing operational implementation: ' + name)
        return importlib.import_module(name)

    def test_atomic_gate_is_exact_and_missing_or_corrupt_gate_is_closed(self):
        ops = self.module('ops_common')
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'runtime' / 'gate.json'
            self.assertFalse(ops.gate_is_open(path))
            ops.atomic_json(path, {'format_version': 1, 'state': 'OPEN'})
            self.assertTrue(ops.gate_is_open(path))
            path.write_text('{"format_version":1,"state":"OPEN","extra":true}')
            self.assertFalse(ops.gate_is_open(path))
            path.write_text('{"format_version":1,"state":"OPEN","state":"CLOSED"}')
            self.assertFalse(ops.gate_is_open(path))

    def test_env_rejects_duplicate_or_expansion_without_echoing_value(self):
        ops = self.module('ops_common')
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / '.env'
            marker = 'SYNTHETIC-SECRET-NOT-FOR-OUTPUT'
            for body in ('PASSWORD=' + marker + '\nPASSWORD=other\n', 'PASSWORD=$(echo ' + marker + ')\n'):
                path.write_text(body)
                with self.assertRaises(ops.OpsError) as failure:
                    ops.read_env(path)
                self.assertNotIn(marker, str(failure.exception))

    def test_compose_uses_arrays_scoped_root_and_no_api_or_database_host_ports(self):
        ops = self.module('ops_common')
        command = ops.Ops(ROOT).compose_args('ps', '--format', 'json')
        self.assertIsInstance(command, list)
        self.assertIn(str(ROOT / 'compose.yaml'), command)
        compose = (ROOT / 'compose.yaml').read_text()
        self.assertNotIn('API_A_PORT', compose)
        self.assertNotIn('API_B_PORT', compose)
        self.assertNotIn('PG_PORT', compose)
        self.assertIn('./.local/runtime', compose)
        self.assertIn('/runtime:ro', compose)
        self.assertIn('max-size:', compose)
        self.assertIn('max-file:', compose)

    def test_command_failure_never_echoes_captured_secrets(self):
        ops = self.module('ops_common')
        marker = 'SYNTHETIC-SECRET-NOT-FOR-OUTPUT'
        with self.assertRaises(ops.OpsError) as failure:
            ops.Ops(ROOT).run([sys.executable, '-c', 'import sys; print(sys.argv[1]); sys.exit(1)', marker])
        self.assertNotIn(marker, str(failure.exception))

    def test_close_persists_proxy_before_app_and_open_verifies_before_proxy(self):
        ops = self.module('ops_common')
        with tempfile.TemporaryDirectory() as directory:
            client = ops.Ops(directory)
            observations = []
            client.verify_proxy_closed = lambda: observations.append(('proxy-check', ops.gate_is_open(client.proxy_gate), ops.gate_is_open(client.app_gate)))
            client.close_maintenance('test')
            self.assertEqual(observations, [('proxy-check', False, False)])
            client.open_app_gate('controlled-internal-smoke')
            self.assertTrue(ops.gate_is_open(client.app_gate))
            self.assertFalse(ops.gate_is_open(client.proxy_gate))
            with self.assertRaises(ops.OpsError):
                client.open_maintenance('test', verified=False)
            self.assertFalse(ops.gate_is_open(client.proxy_gate))
            client.open_maintenance('test', verified=True)
            self.assertTrue(ops.gate_is_open(client.proxy_gate))

    def test_drain_timeout_leaves_server_drained(self):
        ops = self.module('ops_common')
        client = ops.Ops(ROOT)
        commands = []
        def proxy(command):
            commands.append(command)
            return '# pxname,svname,scur\ncontextfence,api-a,1\n'
        client.proxy_command = proxy
        with self.assertRaises(ops.OpsError):
            client.drain('api-a', timeout=0)
        self.assertEqual(commands[0], 'set server contextfence/api-a state drain')
        self.assertNotIn('set server contextfence/api-a state ready', commands)

    def test_failed_candidate_rolls_back_images_without_database_rollback(self):
        deployment = self.module('deploy')
        class Fake:
            def __init__(self): self.events = []
            def close_maintenance(self, reason): self.events.append('close')
            def open_app_gate(self, reason): self.events.append('app-open')
            def open_maintenance(self, reason, verified=False): self.events.append('proxy-open')
            def migrate(self, image): self.events.append('migrate')
            def drain(self, instance, timeout=30): self.events.append('drain-' + instance)
            def switch_image(self, instance, image): self.events.append('switch-' + instance + '-' + image)
            def wait_ready(self, instance, timeout=120): self.events.append('ready-' + instance)
            def resume(self, instance): self.events.append('resume-' + instance)
            def smoke(self, instance):
                self.events.append('smoke-' + instance)
                if sum(event.startswith('smoke-') for event in self.events) == 1: raise RuntimeError('candidate unhealthy')
        fake = Fake()
        report = deployment.deploy_release(fake, 'sha256:candidate', {'api-a': 'sha256:old-a', 'api-b': 'sha256:old-b'})
        self.assertEqual(report['outcome'], 'ROLLED_BACK')
        self.assertIn('switch-api-a-sha256:old-a', fake.events)
        self.assertIn('switch-api-b-sha256:old-b', fake.events)
        self.assertEqual(fake.events.count('migrate'), 1)
        self.assertEqual(fake.events[-1], 'proxy-open')

    def test_forward_deadline_exhaustion_preserves_time_to_restore_previous_images(self):
        deployment = self.module('deploy')
        common = self.module('ops_common')
        now = [1000.0]
        class Fake:
            def __init__(self): self.deadline = None; self.restored = []; self.opened = False
            def check(self):
                if self.deadline is not None and now[0] >= self.deadline:
                    raise common.OpsError('Operational deadline exceeded')
            def migrate(self, image):
                now[0] = self.deadline + 0.01
                raise common.OpsError('Candidate timed out')
            def close_maintenance(self, reason): self.check()
            def open_app_gate(self, reason): self.check()
            def switch_image(self, node, image): self.check(); self.restored.append(node)
            def wait_ready(self, node, **kwargs): self.check()
            def smoke(self, node): self.check()
            def resume(self, node): self.check()
            def open_maintenance(self, reason, verified=False): self.check(); self.opened = True
        fake = Fake()
        with patch.object(deployment.time, 'monotonic', side_effect=lambda: now[0]):
            report = deployment.deploy_release(fake, 'candidate', {'api-a':'previous','api-b':'previous'})
        self.assertEqual('ROLLED_BACK', report['outcome'])
        self.assertEqual(['api-a','api-b'], fake.restored)
        self.assertTrue(fake.opened)
        self.assertLessEqual(now[0] - 1000, 300)

    def test_readiness_rollback_records_final_acceptance_within_total_budget(self):
        deployment = self.module('deploy'); common = self.module('ops_common')
        now = [1000.0]
        class Boundary:
            def __init__(self): self.candidate = False; self.opened = False; self.closed = False
            def migrate(self, image): now[0] += 2
            def open_app_gate(self, reason): pass
            def wait_ready(self, node, **kwargs):
                if self.candidate: now[0] = 1120.0; raise common.OpsError('Unavailable candidate')
            def smoke(self, node): now[0] += 2
            def drain(self, node, **kwargs): pass
            def switch_image(self, node, image): self.candidate = image == 'candidate'
            def resume(self, node): pass
            def close_maintenance(self, reason): self.closed = True
            def open_maintenance(self, reason, verified=False): self.opened = True; now[0] += 1
        fake = Boundary()
        with patch.object(deployment.time, 'monotonic', side_effect=lambda: now[0]):
            report = deployment.deploy_release(fake, 'candidate', {'api-a':'previous','api-b':'previous'})
        phases = [(row['phase'], row['instance']) for row in report['observations']]
        self.assertIn(('candidate-readiness-failed','api-a'), phases)
        self.assertEqual(('rollback-accepted',None), phases[-1])
        self.assertEqual(125.0, report['duration_seconds'])
        self.assertTrue(fake.opened)

    def test_readiness_poll_does_not_swallow_shared_deployment_deadline(self):
        common=self.module('ops_common');now=[1046.0]
        client=common.Ops(ROOT);client.deadline=1120.0
        def internal(*args,**kwargs):
            if now[0]>=client.deadline:raise common.OpsError('Operational deadline exceeded')
            return {'status':503}
        client.internal_http=internal
        with patch.object(common.time,'monotonic',side_effect=lambda:now[0]), \
             patch.object(common.time,'sleep',side_effect=lambda duration:now.__setitem__(0,now[0]+duration)):
            with self.assertRaises(common.OpsError):client.wait_ready('api-a',timeout=90)
        self.assertLessEqual(now[0],1120.0)

    def test_maven_verification_summary_rejects_stale_or_failed_xml_reports(self):
        deployment = self.module('deploy')
        self.assertTrue(hasattr(deployment,'maven_test_summary'),'Missing actual fresh test-report summary')
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            for name in ('surefire-reports','failsafe-reports'):
                path=root/'target'/name;path.mkdir(parents=True)
                (path/'TEST-example.xml').write_text('<testsuite tests="2" failures="0" errors="0" skipped="0"/>')
            summary=deployment.maven_test_summary(root,0)
            self.assertEqual(2,summary['unit']['tests']);self.assertEqual(2,summary['integration']['tests'])
            with self.assertRaises(deployment.OpsError):deployment.maven_test_summary(root,10**12)
            (root/'target/failsafe-reports/TEST-example.xml').write_text('<testsuite tests="2" failures="1" errors="0" skipped="0"/>')
            with self.assertRaises(deployment.OpsError):deployment.maven_test_summary(root,0)

    def test_new_maven_verification_removes_only_generated_test_xml(self):
        deployment = self.module('deploy')
        self.assertTrue(hasattr(deployment,'clear_maven_test_reports'),'Missing independent verification report reset')
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            for name in ('surefire-reports','failsafe-reports'):
                path=root/'target'/name;path.mkdir(parents=True)
                (path/'TEST-old.xml').write_text('<testsuite/>');(path/'diagnostic.txt').write_text('retained')
            deployment.clear_maven_test_reports(root)
            self.assertFalse(list((root/'target').rglob('TEST-*.xml')))
            self.assertEqual(2,len(list((root/'target').rglob('diagnostic.txt'))))

    def test_python_verification_summary_requires_actual_completed_success(self):
        import subprocess
        deployment = self.module('deploy')
        self.assertTrue(hasattr(deployment,'python_test_summary'),'Missing actual unittest completion summary')
        result=subprocess.CompletedProcess([],0,'','Ran 9 tests in 1.2s\n\nOK (skipped=2)\n')
        self.assertEqual({'exit_code':0,'tests':9,'skipped':2,'failures':0,'errors':0},deployment.python_test_summary(result))
        for output in ('Ran 9 tests in 1.2s\nFAILED (failures=1)\n','test.example ... ok\n',
                       'Ran 9 tests in 1.2s\n\nOK (skipped=9)\n'):
            result=subprocess.CompletedProcess([],0,'',output)
            with self.assertRaises(deployment.OpsError):deployment.python_test_summary(result)

    def test_alert_receiver_rejects_secret_bearing_labels_and_keeps_firing_resolved(self):
        receiver = self.module('alert_receiver')
        safe = {'status':'firing','alerts':[{'status':'firing','labels':{'alertname':'ApiUnavailable','instance':'api-a'},'annotations':{'summary':'safe'},'startsAt':'now','endsAt':'later'}]}
        result = receiver.sanitize(safe)
        self.assertEqual(result['status'], 'firing')
        safe['status'] = 'resolved'
        self.assertEqual(receiver.sanitize(safe)['status'], 'resolved')
        safe['alerts'][0]['labels']['token'] = 'SYNTHETIC-SECRET'
        result = receiver.sanitize(safe)
        self.assertNotIn('SYNTHETIC-SECRET', json.dumps(result))

    def test_startup_opens_proxy_only_after_both_internal_permission_smokes(self):
        start = self.module('start')
        class Fake:
            def __init__(self): self.events = []
            def compose(self,*args,**kwargs): self.events.append(tuple(args))
            def startup_image(self): return 'contextfence:local'
            def database_service(self): return 'postgres'
            def close_maintenance(self,reason): self.events.append('closed')
            def open_app_gate(self,reason): self.events.append('app-open')
            def migrate(self,image): self.events.append('migration')
            def wait_ready(self,node,**kwargs): self.events.append('ready-' + node)
            def smoke(self,node): self.events.append('smoke-' + node)
            def resume(self,node): self.events.append('resume-' + node)
            def open_maintenance(self,reason,verified=False): self.events.append('proxy-open')
        fake = Fake()
        # The immutable Grafana prerequisite has its own real configuration tests.
        with patch.object(start, 'prepare_grafana_image', return_value='sha256:' + 'a'*64):
            start.start_stack(fake)
        self.assertLess(fake.events.index('migration'),fake.events.index('app-open'))
        self.assertLess(fake.events.index('smoke-api-a'),fake.events.index('proxy-open'))
        self.assertLess(fake.events.index('smoke-api-b'),fake.events.index('proxy-open'))

    def test_database_commands_follow_restored_host_without_host_port(self):
        ops = self.module('ops_common')
        with tempfile.TemporaryDirectory() as directory:
            client = ops.Ops(directory)
            ops.atomic_bytes(client.runtime / 'database.env',b'DB_HOST=restore-isolated\n')
            command = client.database_args('backup')
            self.assertIn('PGHOST=restore-isolated',command)
            self.assertIn('/ops/client.sh',command)

    def test_rollback_smoke_failure_keeps_ingress_closed(self):
        deployment = self.module('deploy')
        class Fake:
            def __init__(self): self.opened = False; self.closed = False
            def migrate(self,image): raise RuntimeError('candidate migration failed')
            def close_maintenance(self,reason): self.closed = True
            def open_app_gate(self,reason): pass
            def switch_image(self,node,image): pass
            def wait_ready(self,node,timeout=90): pass
            def smoke(self,node): raise RuntimeError('old image incompatible')
            def open_maintenance(self,*args,**kwargs): self.opened = True
        fake = Fake()
        report = deployment.deploy_release(fake,'candidate',{'api-a':'previous','api-b':'previous'})
        self.assertEqual('FAILED_CLOSED',report['outcome'])
        self.assertTrue(fake.closed)
        self.assertFalse(fake.opened)

    def test_proxy_restarts_with_backends_disabled_until_controlled_smoke(self):
        config = (ROOT / 'ops/haproxy.cfg').read_text()
        self.assertIn('server api-a api-a:8080 disabled',config)
        self.assertIn('server api-b api-b:8080 disabled',config)
        lua = (ROOT / 'ops/proxy/gate.lua').read_text()
        self.assertNotIn('gsub',lua,'Do not erase whitespace inside the quoted state string')

    def test_internal_smoke_requires_no_store_before_trusting_response(self):
        common = self.module('ops_common')
        smoke = self.module('ops_smoke')
        class MissingHeader:
            calls = 0
            def internal_http(self,*args):
                self.calls += 1
                if self.calls > 1: raise AssertionError('Missing no-store response must stop immediately')
                return {'status':200,'body':{'status':'UP'},'headers':{}}
        with self.assertRaises(common.OpsError): smoke.smoke_instance(MissingHeader(),'api-a')

    def test_project_and_recovery_overlay_are_scoped_without_volume_cleanup(self):
        common = self.module('ops_common')
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root/'.env').write_text('OPS_PROJECT_NAME=contextfence-recovery-test\n')
            client = common.Ops(root)
            common.atomic_bytes(client.runtime/'recovery.compose.yaml',b'services: {}\n')
            args = client.compose_args('ps')
            self.assertEqual('contextfence-recovery-test',args[args.index('--project-name')+1])
            self.assertIn(str(client.runtime/'recovery.compose.yaml'),args)
            (root/'.env').write_text('OPS_PROJECT_NAME=invalid/project\n')
            with self.assertRaises(common.OpsError): client.compose_args('ps')

    def test_initialize_adds_four_complete_synthetic_tenants_without_rotating_tokens(self):
        common = self.module('ops_common')
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            common.initialize_ops(root)
            path = root/'.local/identities.json'
            first = path.read_bytes()
            identities = json.loads(first)['principals']
            shapes = {(p['tenant'],p['subject']):p['roles'] for p in identities}
            for tenant in ('acme','beta','gamma','delta'):
                self.assertEqual(['SOURCE_WRITER'],shapes[tenant,'writer'])
                self.assertEqual({'READER','PRODUCER'},set(shapes[tenant,'alice']))
            common.initialize_ops(root)
            self.assertTrue(first == path.read_bytes(),'Existing identity bytes and tokens must be preserved')

    def test_conflicting_synthetic_identity_roles_fail_without_overwriting_identity_file(self):
        common = self.module('ops_common')
        import bootstrap
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            bootstrap.bootstrap(root)
            path = root/'.local/identities.json'
            document = json.loads(path.read_text())
            document['principals'].append({'tenant':'gamma','subject':'writer','roles':['READER'],'token':'f'*64})
            common.atomic_json(path,document)
            before = path.read_bytes()
            with self.assertRaises(common.OpsError): common.initialize_ops(root)
            self.assertTrue(before == path.read_bytes(),'Conflicting identity configuration must remain unchanged')

    def test_role_verification_rejects_runtime_with_unexpected_database_privileges(self):
        common = self.module('ops_common')
        client = common.Ops(ROOT)
        client.database = lambda role,sql: 'OK\n' if role != 'runtime' else 'DENIED\n'
        self.assertTrue(hasattr(client,'verify_database_roles'),'Database role acceptance is required')
        with self.assertRaises(common.OpsError): client.verify_database_roles()

    def test_role_verification_rejects_membership_without_effective_monitor_access(self):
        common = self.module('ops_common')
        client = common.Ops(ROOT)
        def membership_only(role, sql):
            if role == 'monitor' and "'pg_monitor','USAGE'" in sql:
                return 'DENIED\n'
            return 'OK\n'
        client.database = membership_only
        with self.assertRaises(common.OpsError):
            client.verify_database_roles()


if __name__ == '__main__': unittest.main()
