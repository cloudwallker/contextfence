"""Pure failure diagnostics and exit ordering for original client-verdict export."""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from load.telemetry import EvidenceError,LoadCounters,RawLoadExporter


class PublicationBoundary(LoadCounters):
    def __init__(self,directory,failures=None):
        super().__init__(directory)
        self.failures=failures or {};self.publish_attempts=0
    def publish(self):
        self.publish_attempts+=1
        if self.publish_attempts in self.failures:raise self.failures[self.publish_attempts]
        super().publish()


class OnePassStop:
    def __init__(self):self.stopped=False;self.waits=0
    def wait(self,timeout):
        self.waits+=1
        return self.stopped or self.waits>1
    def set(self):self.stopped=True


class ThreadBoundary:
    def __init__(self,alive=False):self.alive=alive
    def join(self,timeout=None):pass
    def is_alive(self):return self.alive


class ExportFailureDiagnosticsTest(unittest.TestCase):
    def exporter(self,directory,failures=None,raw=None):
        root=pathlib.Path(directory);path=root/'raw.jsonl'
        if raw is not None:path.write_text(raw,encoding='utf-8')
        counters=PublicationBoundary(root/'metrics',failures)
        exporter=RawLoadExporter(path,'hot',counters);exporter.stop=OnePassStop()
        exporter.thread=ThreadBoundary()
        return exporter,counters

    def point(self,category='success'):
        return json.dumps({'type':'Point','metric':'cf_e2e_ms','data':{'value':10,
            'tags':{'issued':'1','category':category,'phase':'measure','operation':'read'}}})+'\n'

    def details(self,exporter):
        self.assertTrue(hasattr(exporter,'failure_details'),'Export failures must retain bounded safe diagnostics')
        return exporter.failure_details

    def test_initial_publish_error_keeps_stage_reason_and_bounded_errno_without_message(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter,_=self.exporter(directory,{1:OSError(11,'synthetic-private-message/path')})
            exporter.loop()
            self.assertTrue(exporter.failure)
            self.assertEqual([{'stage':'init-publish','reason':'OSError','errno':11}],self.details(exporter))
            self.assertNotIn('synthetic-private',json.dumps(self.details(exporter)))

    def test_raw_json_failure_records_scan_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter,counters=self.exporter(directory,raw='{"incomplete"\n')
            exporter.loop()
            self.assertEqual(0,counters.totals['hot']['requests'])
            self.assertTrue(exporter.failure)
            self.assertEqual([{'stage':'raw-scan','reason':'ValueError','errno':None}],self.details(exporter))

    def test_invalid_issued_verdict_keeps_fixed_evidence_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter,_=self.exporter(directory,raw=self.point('invalid'))
            exporter.loop()
            self.assertTrue(exporter.failure)
            self.assertEqual([{'stage':'raw-scan','reason':'EvidenceError','errno':None}],self.details(exporter))

    def test_periodic_publish_failure_preserves_already_counted_original_request(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter,counters=self.exporter(directory,{2:OSError(13,'synthetic-private-message')},self.point())
            exporter.loop()
            self.assertEqual(1,counters.totals['hot']['requests'])
            self.assertTrue(exporter.failure)
            self.assertEqual([{'stage':'periodic-publish','reason':'OSError','errno':13}],self.details(exporter))

    def test_incomplete_final_raw_records_drain_failure_and_skips_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter,counters=self.exporter(directory,raw='{"incomplete":')
            exporter.__exit__(None,None,None)
            self.assertTrue(exporter.failure)
            self.assertFalse(counters.state.exists())
            self.assertEqual([{'stage':'final-drain','reason':'EvidenceError','errno':None}],self.details(exporter))

    def test_final_publish_error_has_its_own_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter,_=self.exporter(directory,{1:TypeError('synthetic-private-message')})
            exporter.__exit__(None,None,None)
            self.assertTrue(exporter.failure)
            self.assertEqual([{'stage':'final-publish','reason':'TypeError','errno':None}],self.details(exporter))

    def test_alive_background_thread_rejects_exit_without_final_drain_or_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter,counters=self.exporter(directory,raw=self.point())
            exporter.thread=ThreadBoundary(alive=True)
            exporter.__exit__(None,None,None)
            self.assertTrue(exporter.failure)
            self.assertEqual(0,counters.totals['hot']['requests'])
            self.assertFalse(counters.state.exists())
            self.assertEqual([{'stage':'thread-not-stopped','reason':'ThreadNotStopped','errno':None}],
                             self.details(exporter))

    def test_final_recovery_does_not_clear_first_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter,counters=self.exporter(directory,{1:OSError(11,'synthetic-private-message')},self.point())
            exporter.loop();exporter.__exit__(None,None,None)
            self.assertEqual(1,counters.totals['hot']['requests'])
            self.assertEqual(1,json.loads(counters.state.read_text())['hot']['requests'])
            self.assertTrue(exporter.failure)
            self.assertEqual([{'stage':'init-publish','reason':'OSError','errno':11}],self.details(exporter))

    def test_failure_history_is_bounded_retains_first_and_is_defensively_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            failures={index:OSError(index,'synthetic-private-message') for index in range(1,6)}
            exporter,_=self.exporter(directory,failures)
            for _ in range(5):exporter.loop()
            details=self.details(exporter)
            self.assertEqual(3,len(details))
            self.assertEqual([1,2,3],[detail['errno'] for detail in details])
            details[0]['reason']='altered';details.append({'stage':'altered'})
            self.assertEqual('OSError',self.details(exporter)[0]['reason'])
            self.assertEqual(3,len(self.details(exporter)))
            self.assertTrue(exporter.failure)

    def test_errno_outside_integer_bounds_and_exception_text_are_not_exported(self):
        for error,reason in ((OSError(-1,'synthetic-private-message'),'OSError'),
                             (OSError(4096,'synthetic-private-message'),'OSError'),
                             (OSError(True,'synthetic-private-message'),'OSError'),
                             (ValueError('synthetic-private-message'),'ValueError')):
            with self.subTest(reason=reason,errno=getattr(error,'errno',None)),tempfile.TemporaryDirectory() as directory:
                exporter,_=self.exporter(directory,{1:error})
                exporter.loop()
                self.assertEqual([{'stage':'init-publish','reason':reason,'errno':None}],self.details(exporter))

    def test_healthy_scan_and_final_publish_keep_counts_without_failure_details(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter,counters=self.exporter(directory,raw=self.point())
            exporter.loop();exporter.__exit__(None,None,None)
            self.assertEqual(1,counters.totals['hot']['requests'])
            self.assertEqual(1,json.loads(counters.state.read_text())['hot']['requests'])
            self.assertFalse(exporter.failure)
            self.assertEqual([],self.details(exporter))


if __name__=='__main__':unittest.main()
