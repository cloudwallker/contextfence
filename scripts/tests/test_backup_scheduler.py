import importlib
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))


class BackupSchedulerTest(unittest.TestCase):
    def test_failure_is_reported_and_next_hour_still_runs(self):
        self.assertTrue((ROOT / 'scripts/backup_scheduler.py').exists(), 'Hourly scheduler is missing')
        scheduler = importlib.import_module('backup_scheduler')
        calls, waits = [], []
        def job():
            calls.append(True)
            if len(calls) == 1: raise RuntimeError('synthetic inaccessible storage')
        result = scheduler.schedule(job, interval=3600, iterations=2, sleep=lambda seconds: waits.append(seconds), monotonic=lambda: 10)
        self.assertEqual({'attempts': 2, 'failures': 1}, result)
        self.assertEqual([3600], waits)


if __name__ == '__main__': unittest.main()
