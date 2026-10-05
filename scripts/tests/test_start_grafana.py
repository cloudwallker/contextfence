"""Start binds Grafana to an available content identity before starting services."""
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
from ops_common import Ops, OpsError, atomic_bytes, gate_is_open, read_env

LOCAL_TAG = 'contextfence-grafana:12.2.0-official-archive'
IMAGE_ID = 'sha256:' + 'b' * 64


class RecordingOps(Ops):
    def __init__(self, root, inspect=None):
        super().__init__(root)
        self.calls = []
        self.inspect = inspect if inspect is not None else [{'Id': IMAGE_ID, 'RepoDigests': []}]

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ['docker', 'image', 'inspect']:
            if self.inspect == 'missing':
                raise OpsError('Operational command failed; captured output remains private')
            return subprocess.CompletedProcess(args, 0, json.dumps(self.inspect), '')
        return subprocess.CompletedProcess(args, 0, '', '')

    def verify_proxy_closed(self):
        self.calls.append(['proxy-closed', str(gate_is_open(self.proxy_gate))])

    def migrate(self, image):
        self.calls.append(['migrate', image])

    def wait_ready(self, *args, **kwargs):
        pass

    def smoke(self, *args):
        pass

    def resume(self, *args):
        pass


class GrafanaStartupTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.env_content = '# private config must remain byte-identical\nPOSTGRES_PASSWORD=SYNTHETIC-PRIVATE-MARKER\n'
        (self.root / '.env').write_text(self.env_content)
        atomic_bytes(self.root / '.local/runtime/images.env', b'API_A_IMAGE=contextfence:local\nAPI_B_IMAGE=contextfence:local\nAPI_A_VERSION=retained\n')
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def prepare(self, ops):
        self.assertTrue(hasattr(start, 'prepare_grafana_image'), 'Startup must prepare an immutable Grafana image')
        return start.prepare_grafana_image(ops)

    def test_default_build_saves_inspected_image_id_and_preserves_private_configuration(self):
        ops = RecordingOps(self.root)
        with patch('start.ensure_archive', create=True) as fetch:
            digest = self.prepare(ops)
        self.assertEqual(IMAGE_ID, digest)
        self.assertEqual(IMAGE_ID, read_env(ops.runtime / 'images.env')['GRAFANA_IMAGE'])
        self.assertEqual('retained', read_env(ops.runtime / 'images.env')['API_A_VERSION'])
        self.assertEqual(self.env_content, (self.root / '.env').read_text())
        fetch.assert_called_once_with(self.root)
        self.assertTrue(any(call[-2:] == ['build', 'grafana-build'] for call in ops.calls))
        self.assertIn(['docker', 'image', 'inspect', LOCAL_TAG], ops.calls)

    def test_existing_content_id_is_verified_without_build_or_archive_download(self):
        ops = RecordingOps(self.root)
        atomic_bytes(ops.runtime / 'images.env', ('API_A_IMAGE=contextfence:local\nGRAFANA_IMAGE=' + IMAGE_ID + '\n').encode())
        with patch('start.ensure_archive', create=True) as fetch:
            self.assertEqual(IMAGE_ID, self.prepare(ops))
        fetch.assert_not_called()
        self.assertEqual([['docker', 'image', 'inspect', IMAGE_ID]], ops.calls)

    def test_runtime_preserves_unrelated_bytes_and_original_line_endings(self):
        ops = RecordingOps(self.root)
        before = b'\xef\xbb\xbf# local private configuration\r\nAPI_A_IMAGE=contextfence:local\r\nAPI_B_IMAGE=contextfence:local\r\nOTHER_SECRET="SYNTHETIC-PRIVATE-MARKER"\r\n'
        atomic_bytes(ops.runtime / 'images.env', before)
        with patch('start.ensure_archive'):
            self.prepare(ops)
        self.assertEqual(before + ('GRAFANA_IMAGE=' + IMAGE_ID + '\n').encode(),
                         (ops.runtime / 'images.env').read_bytes())

    def test_registry_digest_config_is_verified_without_archive_download(self):
        reference = 'registry.example.invalid/team/grafana@sha256:' + 'c' * 64
        (self.root / '.env').write_text(self.env_content + 'GRAFANA_IMAGE=' + reference + '\n')
        ops = RecordingOps(self.root, [{'Id': IMAGE_ID, 'RepoDigests': [reference]}])
        with patch('start.ensure_archive', create=True) as fetch:
            self.assertEqual(reference, self.prepare(ops))
        fetch.assert_not_called()
        self.assertEqual(reference, read_env(ops.runtime / 'images.env')['GRAFANA_IMAGE'])

    def test_mutable_external_tag_is_rejected_without_echoing_configured_value(self):
        marker = 'private-registry-marker/grafana:latest'
        (self.root / '.env').write_text(self.env_content + 'GRAFANA_IMAGE=' + marker + '\n')
        ops = RecordingOps(self.root)
        with self.assertRaises(OpsError) as error:
            self.prepare(ops)
        self.assertNotIn(marker, str(error.exception))
        self.assertEqual([], ops.calls)

    def test_missing_or_mismatched_content_identity_never_overwrites_runtime_settings(self):
        for inspect in ('missing', [{'Id': 'sha256:invalid'}], [{'Id': 'sha256:' + 'e' * 64}]):
            with self.subTest(inspect=inspect):
                ops = RecordingOps(self.root, inspect)
                atomic_bytes(ops.runtime / 'images.env', ('API_A_IMAGE=contextfence:local\nGRAFANA_IMAGE=' + IMAGE_ID + '\n').encode())
                before = (ops.runtime / 'images.env').read_bytes()
                with self.assertRaises(OpsError):
                    self.prepare(ops)
                self.assertEqual(before, (ops.runtime / 'images.env').read_bytes())

    def test_shell_digest_wins_over_runtime_and_env_then_is_saved_for_repeat_start(self):
        (self.root / '.env').write_text(self.env_content + 'GRAFANA_IMAGE=mutable-env-tag\n')
        atomic_bytes(self.root / '.local/runtime/images.env', b'API_A_IMAGE=contextfence:local\nGRAFANA_IMAGE=mutable-runtime-tag\n')
        ops = RecordingOps(self.root)
        with patch.dict(os.environ, {'GRAFANA_IMAGE': IMAGE_ID}):
            self.assertEqual(IMAGE_ID, self.prepare(ops))
        self.assertEqual(IMAGE_ID, read_env(ops.runtime / 'images.env')['GRAFANA_IMAGE'])

    def test_registry_reference_requires_exact_digest_membership_in_inspected_image(self):
        reference = 'registry.example.invalid/team/grafana@sha256:' + 'c' * 64
        (self.root / '.env').write_text(self.env_content + 'GRAFANA_IMAGE=' + reference + '\n')
        for repo_digests in ([], None, reference, [reference + '-invalid']):
            with self.subTest(repo_digests=repo_digests):
                ops = RecordingOps(self.root, [{'Id': IMAGE_ID, 'RepoDigests': repo_digests}])
                before = (ops.runtime / 'images.env').read_bytes()
                with self.assertRaises(OpsError):
                    self.prepare(ops)
                self.assertEqual(before, (ops.runtime / 'images.env').read_bytes())

    def test_shell_local_build_tag_is_overridden_by_digest_in_every_service_up(self):
        ops = RecordingOps(self.root)
        original = ops.run
        def observed(args, **kwargs):
            if 'up' in args:
                self.assertEqual({'GRAFANA_IMAGE': IMAGE_ID}, kwargs.get('env'))
            return original(args, **kwargs)
        ops.run = observed
        with patch.dict(os.environ, {'GRAFANA_IMAGE': LOCAL_TAG}), patch('start.ensure_archive'):
            start.start_stack(ops)

    def test_archive_failure_keeps_both_gates_closed_and_prevents_every_service_up(self):
        ops = RecordingOps(self.root)
        with patch('start.ensure_archive', create=True, side_effect=OpsError('Grafana archive verification failed')):
            with self.assertRaises(OpsError):
                start.start_stack(ops)
        self.assertFalse(gate_is_open(ops.proxy_gate))
        self.assertFalse(gate_is_open(ops.app_gate))
        self.assertFalse(any('up' in call for call in ops.calls))
        self.assertEqual(['proxy-closed', 'False'], ops.calls[0])

    def test_grafana_content_identity_is_persisted_before_first_service_up(self):
        ops = RecordingOps(self.root)
        original = ops.run
        def observed(args, **kwargs):
            if 'up' in args:
                self.assertFalse(gate_is_open(ops.proxy_gate))
                self.assertEqual(IMAGE_ID, read_env(ops.runtime / 'images.env').get('GRAFANA_IMAGE'))
            return original(args, **kwargs)
        ops.run = observed
        with patch('start.ensure_archive', create=True):
            start.start_stack(ops)
        self.assertTrue(gate_is_open(ops.proxy_gate))


if __name__ == '__main__':
    unittest.main()
