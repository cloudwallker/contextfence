"""Completion evidence must share the load boundary, excluding raw analysis time."""
import json
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from load import runner


class LoadCompletionTest(unittest.TestCase):
    def test_raw_analysis_delay_does_not_change_the_shared_load_completion_time(self):
        # Taking another end timestamp after analysis would split these persisted records.
        clock = types.SimpleNamespace(now=1000, reads=[])
        stopped = threading.Event()

        def read_clock():
            clock.reads.append(clock.now)
            return clock.now

        class Feeder:
            reserved_writes = 0

            def __init__(self, client, tenants):
                pass

            def tick(self):
                pass

        class FixtureServer:
            url = 'http://127.0.0.1:58096'
            capability = 'synthetic-capability'
            failure = None
            aux_requests = 0

            def __init__(self, feeder, tokens):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class RenewalThread:
            def __init__(self, target, daemon):
                pass

            def start(self):
                pass

            def join(self, timeout):
                if not stopped.is_set() or timeout != 40:
                    raise AssertionError('Renewal must stop within the unchanged shutdown grace')
                clock.now += 2

        class ResourceSampler:
            errors = 0

            def __init__(self, ops, pid, path):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                clock.now += 3

        class RawLoadExporter:
            failure = False
            failure_details = []

            def __init__(self, raw, scenario, counters):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                clock.now += 5

        class Process:
            pid = 123

            def wait(self, timeout):
                clock.now += 90
                return 0

        def start_process(command, **kwargs):
            raw = pathlib.Path(command[command.index('--out') + 1].removeprefix('json='))
            tags = {'phase': 'measure', 'operation': 'read'}
            points = [
                {'type': 'Point', 'metric': 'cf_business_started',
                 'data': {'value': 1, 'tags': tags}},
                {'type': 'Point', 'metric': 'http_reqs',
                 'data': {'value': 1, 'tags': dict(tags, traffic='business')}},
                {'type': 'Point', 'metric': 'cf_e2e_ms',
                 'data': {'value': 2, 'tags': dict(tags, category='success', issued='1')}},
            ]
            raw.write_text(''.join(json.dumps(point) + '\n' for _ in range(600) for point in points),
                           encoding='utf-8')
            return Process()

        real_summarize = runner.summarize

        def delayed_summarize(*args):
            # Analyze actual raw points, then advance time without sleeping or service traffic.
            report = real_summarize(*args)
            clock.now += 17
            return report

        job = {'kind': 'probe', 'scenario': 'hot', 'rps': 10, 'repeat': 1,
               'warmup_seconds': 30, 'measure_seconds': 60}
        identities = ({}, [{'tenant': name, 'reader': 'reader', 'writer': 'writer'}
                            for name in ('alpha', 'beta', 'gamma', 'delta')])
        with tempfile.TemporaryDirectory() as directory:
            ops = types.SimpleNamespace(root=pathlib.Path(directory), env=lambda: {})
            output = ops.root / 'run'
            with patch.object(runner, 'load_identities', return_value=identities), \
                 patch.object(runner, 'Feeder', Feeder), \
                 patch.object(runner, 'FixtureServer', FixtureServer), \
                 patch.object(runner, 'ResourceSampler', ResourceSampler), \
                 patch.object(runner, 'RawLoadExporter', RawLoadExporter), \
                 patch.object(runner, 'threading', types.SimpleNamespace(
                     Event=lambda: stopped, Thread=RenewalThread)), \
                 patch.object(runner, 'time', types.SimpleNamespace(time=read_clock)), \
                 patch.object(runner.subprocess, 'Popen', side_effect=start_process), \
                 patch.object(runner, 'summarize', side_effect=delayed_summarize):
                report = runner.run_job(ops, job, output, 'synthetic-k6', 512, 2048)
            execution = json.loads((output / 'execution.json').read_text(encoding='utf-8'))
            summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))

        self.assertEqual('1970-01-01T00:16:40+00:00', execution['started_at'])
        self.assertEqual(execution['started_at'], summary['started_at'])
        self.assertEqual(execution['started_at'], report['started_at'])
        self.assertEqual('1970-01-01T00:18:20+00:00', execution['ended_at'])
        self.assertEqual(execution['ended_at'], summary['ended_at'])
        self.assertEqual(execution['ended_at'], report['ended_at'])
        self.assertEqual([1000, 1100], clock.reads, 'Read one start and one shared load completion time')
        self.assertEqual(1117, clock.now, 'Analysis delay must occur after the recorded load boundary')
        self.assertEqual(600, summary['issued_requests'])
        self.assertTrue(summary['capacity_eligible'])


if __name__ == '__main__':
    unittest.main()
