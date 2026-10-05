import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import backup


class BackupPublicationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.store = backup.BackupStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def candidate(self, name='20261004T120000Z-abcdef12'):
        candidate = self.store.prepare(name)
        (candidate / 'database.dump').write_bytes(b'PGDMP\x00synthetic-test-archive')
        return candidate

    def metadata(self):
        return {'snapshot_id': '00000003-0000001A-1',
                'snapshot_at': '2026-10-04T12:00:00+00:00',
                'schema_version': '2', 'postgres_version': '17.11',
                'context_count': 3, 'source_count': 2,
                'application_images': {'api-a': 'sha256:' + 'a' * 64,
                                       'api-b': 'sha256:' + 'a' * 64}}

    def test_failed_archive_never_replaces_last_verified_backup(self):
        first = self.candidate()
        accepted = self.store.publish(first, self.metadata())
        latest = (self.root / 'latest.json').read_bytes()
        broken = self.candidate('20261004T130000Z-abcdef12')
        (broken / 'database.dump').write_bytes(b'partial, not pg_dump archive')
        with self.assertRaises(backup.BackupError):
            self.store.publish(broken, self.metadata())
        self.assertEqual(latest, (self.root / 'latest.json').read_bytes())
        self.assertEqual(3, backup.validate_backup(accepted)['context_count'])

    def test_restore_preflight_rejects_modified_archive_and_path_escape(self):
        accepted = self.store.publish(self.candidate(), self.metadata())
        (accepted / 'database.dump').write_bytes(b'PGDMP\x00changed-after-publication')
        with self.assertRaises(backup.BackupError):
            backup.validate_backup(accepted)
        document = json.loads((accepted / 'manifest.json').read_text())
        document['archive'] = '../elsewhere.dump'
        (accepted / 'manifest.json').write_text(json.dumps(document))
        with self.assertRaises(backup.BackupError):
            backup.validate_backup(accepted)

    def test_invalid_snapshot_metadata_cannot_be_published(self):
        metadata = self.metadata()
        metadata['snapshot_id'] = 'current snapshot guessed after dump'
        with self.assertRaises(backup.BackupError):
            self.store.publish(self.candidate(), metadata)
        self.assertFalse((self.root / 'latest.json').exists())

    def test_backup_lock_blocks_overlapping_jobs_and_releases_after_failure(self):
        with backup.BackupLock(self.root):
            with self.assertRaises(backup.BackupError):
                with backup.BackupLock(self.root):
                    pass
        with self.assertRaises(RuntimeError):
            with backup.BackupLock(self.root):
                raise RuntimeError('synthetic failure')
        with backup.BackupLock(self.root):
            pass

    def test_metrics_keep_last_success_while_reporting_new_failure(self):
        accepted = self.store.publish(self.candidate(), self.metadata())
        metric = self.root / 'backup.prom'
        backup.write_metrics(metric, accepted, success=False)
        text = metric.read_text()
        self.assertIn('contextfence_backup_last_attempt_success 0', text)
        self.assertIn('contextfence_backup_snapshot_timestamp_seconds 1791115200', text)
        self.assertNotIn('token', text)

    def test_backup_after_recovery_uses_new_database_while_source_is_stopped(self):
        recovered = 'recovery-db-abcdef12'
        events = []
        metadata = self.metadata()
        class Snapshot:
            def __enter__(self): return metadata.copy()
            def __exit__(self, *args): pass
        class RecoveredOps:
            root = self.root
            def database_service(self): return recovered
            def database_args(self, role):
                return ['docker', 'compose', 'exec', '-T', '-e', 'PGHOST=' + recovered,
                        recovered, 'sh', '/ops/client.sh', role]
            def image_manifest(self):
                return {'services': {node: {'image_digest': 'sha256:' + 'a' * 64}
                                     for node in ('api-a', 'api-b')}}
            def compose(self, *args, **kwargs):
                events.append(args)
                if args[0] == 'exec':
                    if recovered not in args:
                        raise backup.OpsError('Source database is stopped')
                elif args[0] == 'cp':
                    if not args[1].startswith(recovered + ':'):
                        raise backup.OpsError('Source database is stopped')
                    pathlib.Path(args[2]).write_bytes(b'PGDMP\x00synthetic-restored-db-archive')
                return SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(backup, 'ExportedSnapshot', return_value=Snapshot()):
            document = backup.run_backup(RecoveredOps())
        self.assertEqual(3, document['context_count'])
        self.assertTrue(any(row[0] == 'cp' for row in events))
        self.assertTrue(all(recovered in row or row[1].startswith(recovered + ':')
                            for row in events))


if __name__ == '__main__':
    unittest.main()
