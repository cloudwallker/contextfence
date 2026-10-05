import importlib
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
from ops_common import Ops,OpsError,gate_is_open


class RecoveryDemoTest(unittest.TestCase):
    def test_pre_fault_producer_or_backup_failure_closes_both_gates_without_echoing(self):
        demo=importlib.import_module('restore_demo')
        for stage in ('prepare_fixture','run_backup','mutate_fixture'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as directory:
                client=Ops(directory); client.verify_proxy_closed=lambda: None
                client.open_maintenance('test',verified=True)
                with patch.object(demo,'prepare_fixture',return_value={}), \
                     patch.object(demo,'run_backup',return_value={'backup_id':'synthetic'}), \
                     patch.object(demo,'mutate_fixture',return_value={}), \
                     patch.object(demo,stage,side_effect=KeyError('PRIVATE-FAILURE-MARKER')):
                    try: demo.run_demo(client)
                    except Exception as error: failure=error
                    else: self.fail('Injected pre-fault failure was not rejected')
                self.assertFalse(gate_is_open(client.app_gate)); self.assertFalse(gate_is_open(client.proxy_gate))
                self.assertIsInstance(failure,OpsError); self.assertNotIn('PRIVATE-FAILURE-MARKER',str(failure))

    def test_failure_clock_precedes_fault_and_restore_receives_the_actual_backup(self):
        self.assertTrue((ROOT/'scripts/restore_demo.py').exists(),'Measured recovery demo is missing')
        demo=importlib.import_module('restore_demo')
        with tempfile.TemporaryDirectory() as directory:
            class ExternalEnvironment:
                root=pathlib.Path(directory)
                def database_host(self): return 'postgres'
                def compose(self,*args,**kwargs): events.append(('fault',args))
            events=[]; client=ExternalEnvironment()
            def restore(ops,archive,ledger,fixture,failure_at):
                self.assertEqual('2026-10-04T00:00:00Z',failure_at)
                self.assertEqual(client.root/'.local/backups/20261004T000000Z-abcdef12',archive)
                events.append(('restore',)); return {'status':'PASS'}
            with patch.object(demo,'prepare_fixture',lambda *args: events.append(('seed',))), \
                 patch.object(demo,'mutate_fixture',lambda *args: events.append(('mutate',))), \
                 patch.object(demo,'run_backup',lambda *args: events.append(('backup',)) or {'backup_id':'20261004T000000Z-abcdef12'}), \
                 patch.object(demo,'utc_now',lambda: events.append(('clock',)) or '2026-10-04T00:00:00Z'), \
                 patch.object(demo,'run_restore',restore):
                self.assertEqual('PASS',demo.run_demo(client)['status'])
            self.assertEqual(['seed','backup','mutate','clock','fault','restore'],[row[0] for row in events])


if __name__=='__main__': unittest.main()
