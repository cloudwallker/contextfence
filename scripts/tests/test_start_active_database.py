"""Startup follows the persistent database target at a controlled Docker boundary."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import start
from ops_common import Ops, OpsError, atomic_bytes, atomic_json, gate_is_open

IMAGE_ID = 'sha256:' + 'b' * 64
RECOVERED_SERVICE = 'recovery-db-abcdef'


class DockerCLI:
    """Model stopped database services without invoking Docker or any network."""
    def __init__(self, ops, active_service, fail_database_up=False):
        self.ops = ops
        self.active_service = active_service
        self.fail_database_up = fail_database_up
        self.running = set()
        self.calls = []

    def __call__(self, command, **kwargs):
        command = list(command)
        call = {'command': command,
                'proxy_open': gate_is_open(self.ops.proxy_gate),
                'app_open': gate_is_open(self.ops.app_gate)}
        self.calls.append(call)
        code, output = 0, ''
        if command[1:3] == ['image', 'inspect']:
            output = json.dumps([{'Id': IMAGE_ID, 'RepoDigests': []}])
        elif 'up' in command:
            arguments = iter(command[command.index('up') + 1:])
            services = []
            for argument in arguments:
                if argument == '--wait-timeout':
                    next(arguments)
                elif not argument.startswith('--'):
                    services.append(argument)
            call['services'] = services
            if self.fail_database_up and self.active_service in services:
                code = 8
            else:
                self.running.update(services)
        elif 'run' in command and 'migrate' in command:
            call['migration'] = True
            if self.active_service not in self.running:
                code = 9
        elif 'exec' in command:
            if '/ops/grants.sh' in command or '/ops/client.sh' in command:
                if self.active_service not in self.running:
                    code = 9
                elif '/ops/client.sh' in command:
                    output = 'OK\n'
            elif '--write-out' in command:
                output = '503'
            elif '--config' in command:
                output = 'HTTP/1.1 200 OK\nCache-Control: no-store\n\n{"status":"UP"}\n200'
        return subprocess.CompletedProcess(command, code, output, '')


class ActiveDatabaseStartupTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        (self.root / '.env').write_text('OPS_PROJECT_NAME=contextfence-start-test\n')
        (self.root / 'compose.yaml').write_text('name: contextfence\nservices: {}\n')
        self.ops = Ops(self.root)
        atomic_bytes(self.ops.runtime / 'images.env',
                     ('API_A_IMAGE=contextfence:local\nGRAFANA_IMAGE=' + IMAGE_ID + '\n').encode())
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def target(self, host, recovered=False):
        atomic_bytes(self.ops.runtime / 'database.env', ('DB_HOST=' + host + '\n').encode())
        if recovered:
            atomic_json(self.ops.runtime / 'recovery.compose.yaml', {
                'services': {RECOVERED_SERVICE: {
                    'image': 'postgres-synthetic@sha256:' + 'c' * 64,
                    'volumes': ['recovery-data-abcdef:/var/lib/postgresql/data']}},
                'volumes': {'recovery-data-abcdef': {}}})

    def launch(self, cli):
        checks = []
        def smoke(instance):
            self.assertFalse(gate_is_open(self.ops.proxy_gate),
                             'Ingress must remain closed throughout both internal safety checks')
            self.assertTrue(gate_is_open(self.ops.app_gate))
            checks.append(instance)
        with patch('ops_common.subprocess.run', side_effect=cli), \
                patch('ops_common.shutil.which', return_value=None), \
                patch.object(self.ops, 'smoke', side_effect=smoke):
            try:
                start.start_stack(self.ops)
                error = None
            except OpsError as caught:
                error = caught
        return checks, error

    def ups(self, cli):
        return [call for call in cli.calls if 'services' in call]

    def assert_waited_for_database_before_migration(self, cli, service):
        first_up = self.ups(cli)[0]
        self.assertEqual([service], first_up['services'])
        self.assertIn('--no-deps', first_up['command'])
        self.assertIn('--wait', first_up['command'])
        self.assertFalse(first_up['proxy_open'])
        self.assertFalse(first_up['app_open'])
        migration = next(call for call in cli.calls if call.get('migration'))
        self.assertLess(cli.calls.index(first_up), cli.calls.index(migration))
        self.assertFalse(migration['proxy_open'])
        self.assertFalse(migration['app_open'])

    def test_normal_host_starts_and_waits_for_postgres_before_migration(self):
        self.target('postgres')
        cli = DockerCLI(self.ops, 'postgres')
        checks, error = self.launch(cli)
        self.assertIsNone(error)
        self.assert_waited_for_database_before_migration(cli, 'postgres')
        self.assertEqual(['api-a', 'api-b'], checks)
        self.assertTrue(gate_is_open(self.ops.proxy_gate))

    def test_recovered_host_restarts_active_service_and_preserves_host_and_volume(self):
        self.target(RECOVERED_SERVICE, recovered=True)
        database_before = (self.ops.runtime / 'database.env').read_bytes()
        overlay_before = (self.ops.runtime / 'recovery.compose.yaml').read_bytes()
        cli = DockerCLI(self.ops, RECOVERED_SERVICE)
        checks, error = self.launch(cli)
        self.assert_waited_for_database_before_migration(cli, RECOVERED_SERVICE)
        self.assertIsNone(error, 'The stopped recovered service must be available for migration')
        self.assertEqual(['api-a', 'api-b'], checks)
        self.assertTrue(gate_is_open(self.ops.proxy_gate))
        self.assertTrue(gate_is_open(self.ops.app_gate))
        self.assertIn(str(self.ops.runtime / 'recovery.compose.yaml'), self.ups(cli)[0]['command'])
        self.assertFalse(any('postgres' in call['services'] for call in self.ups(cli)))
        self.assertEqual(database_before, (self.ops.runtime / 'database.env').read_bytes())
        self.assertEqual(overlay_before, (self.ops.runtime / 'recovery.compose.yaml').read_bytes())

    def test_fault_proxy_host_keeps_postgres_container_mapping_and_connection_target(self):
        self.target('db-fault-proxy')
        database_before = (self.ops.runtime / 'database.env').read_bytes()
        cli = DockerCLI(self.ops, 'postgres')
        checks, error = self.launch(cli)
        self.assertIsNone(error)
        self.assert_waited_for_database_before_migration(cli, 'postgres')
        grants = next(call['command'] for call in cli.calls if '/ops/grants.sh' in call['command'])
        self.assertIn('PGHOST=db-fault-proxy', grants)
        self.assertEqual('postgres', grants[-3])
        self.assertEqual(['api-a', 'api-b'], checks)
        self.assertEqual(database_before, (self.ops.runtime / 'database.env').read_bytes())

    def test_failed_active_database_start_closes_both_gates_and_skips_migration(self):
        self.target(RECOVERED_SERVICE, recovered=True)
        for gate in (self.ops.app_gate, self.ops.proxy_gate):
            atomic_json(gate, {'format_version': 1, 'state': 'OPEN'})
        cli = DockerCLI(self.ops, RECOVERED_SERVICE, fail_database_up=True)
        checks, error = self.launch(cli)
        self.assertIsInstance(error, OpsError)
        self.assertEqual([RECOVERED_SERVICE], self.ups(cli)[0]['services'])
        self.assertFalse(gate_is_open(self.ops.app_gate))
        self.assertFalse(gate_is_open(self.ops.proxy_gate))
        self.assertEqual([], checks)
        self.assertEqual(1, len(self.ups(cli)))
        self.assertFalse(any(call.get('migration') for call in cli.calls))


if __name__ == '__main__':
    unittest.main()
