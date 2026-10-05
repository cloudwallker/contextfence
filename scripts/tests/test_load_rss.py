"""Pure checks for init-child Java RSS evidence and the five-second sampler cadence."""
import contextlib
import io
import json
import pathlib
import sys
import threading
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from load import runner


def status(rss='2048 kB', name='java', pid='7', parent='1', marker='7'):
    return f'ContextFenceJavaPid:{marker}\nName:\t{name}\nPid:\t{pid}\nPPid:\t{parent}\nVmRSS:\t{rss}\n'


class ProcessStatusRssTest(unittest.TestCase):
    def parse(self, raw):
        self.assertTrue(callable(getattr(runner, 'process_status_rss', None)),
                        'Actual Java RSS needs a strict process status parser')
        return runner.process_status_rss(raw)

    def test_java_child_of_docker_init_is_accepted_with_bound_identity(self):
        raw = 'ContextFenceJavaPid:7\nName:\tjava\nPid:\t7\nPPid:\t1\nVmRSS:\t316324 kB\n'
        try:
            value = self.parse(raw)
        except runner.LoadError:
            self.fail('Compose init:true requires RSS from the verified Java child rather than PID 1')
        self.assertEqual(323915776, value)

    def test_actual_kibibytes_are_returned_as_integer_bytes(self):
        value = self.parse(status())
        self.assertIs(type(value), int)
        self.assertEqual(2097152, value)
        self.assertEqual(1024, self.parse(status('1 kB')))

    def test_non_string_input_is_rejected(self):
        for raw in (None, b'Name: java', {}, [], 1, 1.0, True):
            with self.subTest(kind=type(raw).__name__), self.assertRaises(runner.LoadError):
                self.parse(raw)

    def test_rss_requires_bounded_positive_ascii_integer_and_exact_unit(self):
        for value in ('0 kB', '-1 kB', '+1 kB', '01 kB', '1.5 kB', '1e3 kB',
                      'NaN kB', 'inf kB', '1', '1 KB', '1 kb', '1 MB', '1 kB extra',
                      '１ kB', '9007199254740992 kB', '9' * 5000 + ' kB'):
            with self.subTest(value=value[:30]), self.assertRaises(runner.LoadError):
                self.parse(status(value))

    def test_duplicate_required_fields_are_rejected(self):
        for extra in ('ContextFenceJavaPid:7\n', 'Name:\tjava\n', 'Pid:\t7\n',
                      'PPid:\t1\n', 'VmRSS:\t2048 kB\n', ' VmRSS:\t2048 kB\n'):
            with self.subTest(field=extra.split(':')[0]), self.assertRaises(runner.LoadError):
                self.parse(status() + extra)

    def test_non_java_or_process_identity_mismatch_cannot_be_mislabeled(self):
        for name, pid, parent, marker in (('docker-init', '1', '0', '1'), ('sh', '7', '1', '7'),
                ('Java', '7', '1', '7'), ('java worker', '7', '1', '7'), ('java', '8', '1', '7'),
                ('java', '7', '2', '7'), ('java', '7', '01', '7'), ('java', '1', '1', '1'),
                ('java', '07', '1', '07'), ('java', '-1', '1', '-1'),
                ('java', '2147483648', '1', '2147483648')):
            with self.subTest(name=name, pid=pid, parent=parent), self.assertRaises(runner.LoadError):
                self.parse(status(name=name, pid=pid, parent=parent, marker=marker))

    def test_missing_duplicate_or_malformed_child_marker_is_rejected(self):
        for marker in ('', '0', '-7', '+7', '07', '7 8', '7.0', '７', '9' * 100):
            with self.subTest(marker=marker), self.assertRaises(runner.LoadError):
                self.parse(status(marker=marker))
        for extra in ('ContextFenceJavaPid 7\n', 'Pid 7\n', 'PPid 1\n', 'VmRSS 2048 kB\n'):
            with self.subTest(field=extra.split()[0]), self.assertRaises(runner.LoadError):
                self.parse(status() + extra)

    def test_missing_fields_and_oversized_status_are_rejected(self):
        for raw in ('', *(status().replace(line, '') for line in status().splitlines(keepends=True)),
                    'Name: java\nPid: 1\nVmRSS: 2048 kB\n', status() + 'Unknown:' + 'x' * 65536):
            with self.subTest(size=len(raw)), self.assertRaises(runner.LoadError):
                self.parse(raw)

    def test_unknown_status_fields_are_ignored_and_never_returned(self):
        value = self.parse(status() + 'Unknown: synthetic-private-value\nUid: 10001 10001 10001 10001\n')
        self.assertEqual(2097152, value)
        self.assertIs(type(value), int)


class OneRoundStop:
    def __init__(self):
        self.waits = []

    def is_set(self):
        return bool(self.waits)

    def wait(self, seconds):
        self.waits.append(seconds)
        return True


class OutputSink:
    def __init__(self):
        self.stream = io.StringIO()

    def open(self, mode, encoding):
        if (mode, encoding) != ('a', 'utf-8'):
            raise AssertionError('Resource evidence must append UTF-8 rows')
        return contextlib.nullcontext(self.stream)


class ObservationOps:
    def __init__(self, statuses):
        self.statuses = statuses
        self.proc_calls = []
        self.metadata_timeout = None

    def env(self):
        return {'OPS_PROJECT_NAME': 'contextfence-bench', 'ENTRY_PORT': '58095', 'PROMETHEUS_PORT': '59095'}

    def database_service(self):
        return 'db'

    def compose(self, *args, **kwargs):
        if args == ('ps', '--quiet', 'api-a', 'api-b', 'db'):
            self.metadata_timeout = kwargs.get('timeout')
            return types.SimpleNamespace(stdout='a b c')
        expected_script = ('set -euf; children=$(cat /proc/1/task/1/children); set -- $children; '
            '[ "$#" -eq 1 ]; pid=$1; case "$pid" in \'\'|*[!0-9]*) exit 1;; esac; '
            '[ "$pid" -gt 1 ]; printf \'ContextFenceJavaPid:%s\\n\' "$pid"; cat "/proc/$pid/status"')
        if len(args) != 6 or args[:2] != ('exec', '-T') or args[2] not in ('api-a', 'api-b') or \
           args[3:] != ('sh', '-c', expected_script):
            # Fail on command scope drift instead of silently accepting arbitrary Docker commands.
            raise AssertionError('Expected project-scoped unique Java init-child status observation')
        timeout = kwargs.get('timeout')
        if type(timeout) not in (int, float) or not 0 < timeout <= 5:
            raise AssertionError('Each RSS command must have a bounded five-second deadline')
        self.proc_calls.append(args[2])
        raw = self.statuses[args[2]]
        if isinstance(raw, Exception):
            raise raw
        return types.SimpleNamespace(stdout=raw)

    def run(self, args, timeout):
        if args != ['docker', 'stats', '--no-stream', '--format', '{{json .}}', 'a', 'b', 'c'] or timeout != 15:
            raise AssertionError('Unexpected service observation command')
        return types.SimpleNamespace(stdout='{"Name":"api-a"}\n{"Name":"api-b"}\n{"Name":"db"}\n')

    def database(self, role, query):
        if role != 'monitor' or not query.startswith('select json_build_object('):
            raise AssertionError('Expected aggregate-only database observation')
        return '{"connections":2,"waiting":0,"active":0,"oldest_wait_seconds":0}'


class ReadApiProcessRssTest(unittest.TestCase):
    def read(self, ops, node):
        self.assertTrue(callable(getattr(runner, 'read_api_process_rss', None)),
                        'Preflight and sampler need one shared Java child observation')
        return runner.read_api_process_rss(ops, node)

    def test_project_scoped_single_exec_returns_java_child_rss_only(self):
        ops = ObservationOps({'api-a': status('316324 kB') + 'Unknown: synthetic-private-value\n'})
        value = self.read(ops, 'api-a')
        self.assertEqual(323915776, value)
        self.assertIs(type(value), int)
        self.assertEqual(['api-a'], ops.proc_calls)

    def test_non_single_child_or_disappeared_child_command_failure_is_propagated(self):
        for reason in ('non-single-child', 'child-disappeared'):
            with self.subTest(reason=reason), self.assertRaises(runner.OpsError):
                self.read(ObservationOps({'api-a': runner.OpsError(reason)}), 'api-a')

    def test_only_fixed_api_nodes_are_allowed_before_any_command(self):
        class NoCommands:
            def compose(self, *args, **kwargs):
                raise AssertionError('Unapproved nodes must be rejected before command execution')
        for node in ('proxy', 'db', '', 'api-a; env', None, 1, ['api-a']):
            with self.subTest(kind=type(node).__name__), self.assertRaises(runner.LoadError):
                self.read(NoCommands(), node)

    def test_controlled_three_second_read_fits_fixed_five_second_deadline_without_sleep(self):
        class ThreeSecondRead(ObservationOps):
            def __init__(self):
                super().__init__({'api-a': status('316324 kB')})
                self.observed_timeout = None

            def compose(self, *args, **kwargs):
                self.observed_timeout = kwargs.get('timeout')
                if self.observed_timeout < 3:
                    raise runner.OpsError('synthetic-controlled-deadline-exceeded')
                return super().compose(*args, **kwargs)

        ops = ThreeSecondRead()
        try:
            value = self.read(ops, 'api-a')
        except runner.OpsError:
            self.fail('A controlled three-second read must fit the authorized fixed five-second CLI bound')
        self.assertEqual(323915776, value)
        self.assertEqual(5, ops.observed_timeout)


class ResourceSamplerRssTest(unittest.TestCase):
    def sample(self, statuses, elapsed=1.25, ops=None, metric_error=None, client_error=None):
        ops = ops or ObservationOps(statuses)
        sink = OutputSink()
        sampler = runner.ResourceSampler(ops, 123, sink)
        sampler.stop = OneRoundStop()
        metrics = {'status': 'success', 'data': {'result': [
            {'metric': {'__name__': 'process_cpu_usage', 'instance': 'api-a'}, 'value': [1, '0.1']},
            {'metric': {'__name__': 'process_cpu_usage', 'instance': 'api-b'}, 'value': [1, '0.2']}]}}
        with patch.object(runner, 'client_sample', return_value={
                'pid': 123, 'cpu_seconds': 1, 'rss_bytes': 4096, 'host_memory_available_bytes': 8192},
                side_effect=client_error), \
             patch.object(runner.urllib.request, 'urlopen', return_value=io.StringIO(json.dumps(metrics)),
                          side_effect=metric_error), \
             patch.object(runner.time, 'monotonic', side_effect=[100.0, 100.0 + elapsed]):
            sampler.loop()
        rows = [json.loads(line) for line in sink.stream.getvalue().splitlines()]
        self.assertEqual(1, len(rows), 'Long sampling work must not manufacture backfilled rows')
        return rows[0], sampler, ops

    def test_both_actual_rss_values_are_stored_without_raw_status(self):
        row, sampler, ops = self.sample({
            'api-a': status() + 'Unknown: synthetic-private-value\n', 'api-b': status('3072 kB')})
        self.assertEqual({
            'api-a': {'rss_bytes': 2097152, 'source': 'java-init-child-proc-status'},
            'api-b': {'rss_bytes': 3145728, 'source': 'java-init-child-proc-status'}}, row.get('api_process'))
        self.assertNotIn('synthetic-private-value', json.dumps(row))
        self.assertEqual(0, sampler.errors)
        self.assertNotIn('api_process_observation_failed', row)
        self.assertNotIn('api_process_failure_kinds', row)
        self.assertEqual(['api-a', 'api-b'], sorted(ops.proc_calls))

    def test_failed_node_is_missing_without_zero_or_exception_text(self):
        for failed in (runner.OpsError('synthetic-private-value'), status('invalid-private-value')):
            with self.subTest(kind=type(failed).__name__):
                row, sampler, ops = self.sample({'api-a': status(), 'api-b': failed})
                self.assertEqual({'api-a': {'rss_bytes': 2097152, 'source': 'java-init-child-proc-status'}},
                                 row.get('api_process'))
                self.assertIs(row.get('api_process_observation_failed'), True)
                self.assertEqual(['api-b'], row.get('api_process_failed_instances'))
                self.assertEqual(1, sampler.errors)
                self.assertNotIn('private-value', json.dumps(row))
                self.assertEqual(['api-a', 'api-b'], sorted(ops.proc_calls))

    def test_both_failed_nodes_increment_subsystem_error_only_once(self):
        row, sampler, _ = self.sample({'api-a': status(name='sh'), 'api-b': status('0 kB')})
        self.assertEqual({}, row.get('api_process'))
        self.assertIs(row.get('api_process_observation_failed'), True)
        self.assertEqual(['api-a', 'api-b'], row.get('api_process_failed_instances'))
        self.assertEqual(1, sampler.errors)

    def test_failure_kind_is_one_fixed_enum_per_failed_node_without_exception_text(self):
        private = 'synthetic-private-value pid=7 /proc/7/status VmRSS: private'
        for failure, expected in ((runner.OpsError(private), 'ops-error'),
                (runner.LoadError(private), 'load-error'), (OSError(private), 'os-error'),
                (ValueError(private), 'value-error'), (status('invalid-private-value'), 'load-error')):
            with self.subTest(expected=expected, kind=type(failure).__name__):
                row, sampler, _ = self.sample({'api-a': status(), 'api-b': failure})
                self.assertEqual({'api-b': expected}, row.get('api_process_failure_kinds'))
                self.assertEqual(['api-b'], row['api_process_failed_instances'])
                self.assertEqual({'api-a': {'rss_bytes': 2097152, 'source': 'java-init-child-proc-status'}},
                                 row['api_process'])
                self.assertIs(row['api_process_observation_failed'], True)
                self.assertEqual(1, sampler.errors)
                self.assertNotIn('private-value', json.dumps(row))
                self.assertNotIn('/proc/7/status', json.dumps(row))

    def test_two_failure_kinds_keep_one_subsystem_error_and_actual_over_budget_gap(self):
        row, sampler, _ = self.sample({'api-a': runner.OpsError('private-value'),
            'api-b': runner.LoadError('private-value')}, elapsed=7.5)
        self.assertEqual({'api-a': 'ops-error', 'api-b': 'load-error'}, row.get('api_process_failure_kinds'))
        self.assertEqual({}, row['api_process'])
        self.assertEqual(['api-a', 'api-b'], row['api_process_failed_instances'])
        self.assertEqual(1, sampler.errors)
        self.assertEqual([0], sampler.stop.waits)
        self.assertNotIn('private-value', json.dumps(row))

    def test_work_elapsed_is_subtracted_from_five_second_wait(self):
        _, sampler, _ = self.sample({'api-a': status(), 'api-b': status()}, elapsed=1.25)
        self.assertEqual([3.75], sampler.stop.waits)

    def test_over_budget_work_waits_zero_and_keeps_only_actual_row(self):
        _, sampler, _ = self.sample({'api-a': status(), 'api-b': status()}, elapsed=6.5)
        self.assertEqual([0], sampler.stop.waits)

    def test_container_metadata_read_has_explicit_bound_before_stats(self):
        row, sampler, ops = self.sample({'api-a': status(), 'api-b': status()})
        self.assertEqual(15, ops.metadata_timeout)
        self.assertIn('containers', row)
        self.assertEqual(0, sampler.errors)

    def test_independent_service_success_is_preserved_when_other_group_fails(self):
        class ServiceFailures(ObservationOps):
            def __init__(self, failures):
                super().__init__({'api-a': status(), 'api-b': status()})
                self.failures = failures

            def run(self, *args, **kwargs):
                if 'containers' in self.failures:
                    raise runner.OpsError('synthetic-private-value')
                return super().run(*args, **kwargs)

            def database(self, *args, **kwargs):
                if 'database' in self.failures:
                    raise runner.OpsError('synthetic-private-value')
                return super().database(*args, **kwargs)

        for failures in (('containers',), ('database',), ('containers', 'database')):
            with self.subTest(failures=failures):
                row, sampler, _ = self.sample({}, ops=ServiceFailures(failures))
                self.assertIs(row.get('service_observation_failed'), True)
                self.assertEqual(1, sampler.errors)
                for field in ('containers', 'database'):
                    self.assertEqual(field not in failures, field in row)
                self.assertNotIn('synthetic-private-value', json.dumps(row))
                self.assertEqual(['api-a', 'api-b'], sorted(row['api_process']))

    def test_error_total_counts_subsystems_once_when_all_groups_fail(self):
        class FailedServices(ObservationOps):
            def run(self, *args, **kwargs):
                raise runner.OpsError('synthetic-private-value')

            def database(self, *args, **kwargs):
                raise runner.OpsError('synthetic-private-value')

        ops = FailedServices({'api-a': status('0 kB'), 'api-b': runner.OpsError('synthetic-private-value')})
        row, sampler, _ = self.sample({}, elapsed=7.5, ops=ops,
            metric_error=OSError('synthetic-private-value'), client_error=OSError('synthetic-private-value'))
        self.assertEqual(4, sampler.errors)
        self.assertEqual(4, sum(row.get(field) is True for field in (
            'client_observation_failed', 'api_process_observation_failed',
            'service_observation_failed', 'metric_observation_failed')))
        self.assertEqual({}, row['api_process'])
        self.assertEqual(['api-a', 'api-b'], row['api_process_failed_instances'])
        for field in ('client', 'containers', 'database', 'metrics'):
            self.assertNotIn(field, row)
        self.assertEqual([0], sampler.stop.waits)
        self.assertNotIn('synthetic-private-value', json.dumps(row))

    def test_five_io_groups_overlap_and_finish_before_coordinator_writes(self):
        coordinator = threading.get_ident()
        barrier = threading.Barrier(5, timeout=1)
        lock = threading.Lock()
        started = {}
        completed = set()

        def enter(group):
            with lock:
                started[group] = threading.get_ident()
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                raise AssertionError('All five independent I/O groups must overlap') from None

        def done(group):
            with lock:
                completed.add(group)

        class ConcurrentOps(ObservationOps):
            def compose(self, *args, **kwargs):
                group = 'containers' if args[0] == 'ps' else args[2]
                enter(group)
                value = super().compose(*args, **kwargs)
                if group != 'containers':
                    done(group)
                return value

            def run(self, *args, **kwargs):
                with lock:
                    if 'containers' not in started:
                        raise AssertionError('Container IDs must be read before docker stats')
                value = super().run(*args, **kwargs)
                done('containers')
                return value

            def database(self, *args, **kwargs):
                enter('database')
                value = super().database(*args, **kwargs)
                done('database')
                return value

        class CoordinatorSink(OutputSink):
            def open(self, *args, **kwargs):
                if threading.get_ident() != coordinator or completed != {
                        'api-a', 'api-b', 'containers', 'database', 'metrics'}:
                    raise AssertionError('Only the coordinator may write after every I/O group finishes')
                return super().open(*args, **kwargs)

        def client_sample(pid):
            if threading.get_ident() != coordinator or started:
                raise AssertionError('Client resource sampling must run immediately on the coordinator')
            return {'pid': pid, 'cpu_seconds': 1, 'rss_bytes': 4096, 'host_memory_available_bytes': 8192}

        metrics = {'status': 'success', 'data': {'result': [
            {'metric': {'__name__': 'process_cpu_usage', 'instance': 'api-a'}, 'value': [1, '0.1']},
            {'metric': {'__name__': 'process_cpu_usage', 'instance': 'api-b'}, 'value': [1, '0.2']}]}}

        def metric_query(*args, **kwargs):
            enter('metrics')
            done('metrics')
            return io.StringIO(json.dumps(metrics))

        sink = CoordinatorSink()
        ops = ConcurrentOps({'api-a': status(), 'api-b': status()})
        sampler = runner.ResourceSampler(ops, 123, sink)
        sampler.stop = OneRoundStop()
        with patch.object(runner, 'client_sample', side_effect=client_sample), \
             patch.object(runner.urllib.request, 'urlopen', side_effect=metric_query), \
             patch.object(runner.time, 'monotonic', side_effect=[100.0, 103.5]):
            sampler.loop()
        self.assertEqual({'api-a', 'api-b', 'containers', 'database', 'metrics'}, set(started))
        self.assertEqual(5, len(set(started.values())))
        self.assertNotIn(coordinator, started.values())
        self.assertEqual(1, len(sink.stream.getvalue().splitlines()))
        self.assertEqual(0, sampler.errors)
        self.assertEqual([1.5], sampler.stop.waits)

    def test_context_exit_drains_owned_sampling_thread_without_join_timeout(self):
        class PendingSamplingThread:
            def __init__(self):
                self.drained = False

            def join(self, timeout=None):
                # A bounded join may return while an existing bounded command still owns a future.
                self.drained = timeout is None

        sampler = runner.ResourceSampler(ObservationOps({}), 123, OutputSink())
        pending = PendingSamplingThread()
        sampler.thread = pending
        sampler.__exit__(None, None, None)
        self.assertTrue(sampler.stop.is_set())
        self.assertTrue(pending.drained, 'Context exit must wait for owned I/O rather than abandon background sampling')


if __name__ == '__main__':
    unittest.main()
