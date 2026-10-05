"""Supplemental probe evidence stays reproducible without approving capacity."""
import copy
import contextlib
import datetime as dt
import hashlib
import json
import io
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from load import runner
try:
    import probe_step
except ImportError:
    # The absent entry point has no effects and does not enforce evidence boundaries.
    probe_step = types.SimpleNamespace(main=lambda argv: 0)


def save(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document) + '\n', encoding='utf-8')


class ProbeStepTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.local = self.root / 'artifacts/local'
        self.local.mkdir(parents=True)
        (self.root / 'load').mkdir()
        (self.root / 'src/main').mkdir(parents=True)
        (self.root / 'load/k6.js').write_text('synthetic source\n', encoding='utf-8')
        (self.root / 'pom.xml').write_text('<project/>\n', encoding='utf-8')
        (self.root / 'Dockerfile').write_text('FROM synthetic\n', encoding='utf-8')
        self.base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        self.policy = self.local / 'load-policy.json'
        save(self.policy, {'format_version': 1, 'created_at': self.at(-1),
            'regression_tolerance': .2, 'max_unexpected_failure_rate': .01,
            'max_unsafe_allow': 0, 'required_probe_rps': [10,25,50,100,200],
            'steady_warmup_seconds': 300, 'steady_measure_seconds': 600,
            'max_sustained_wait_growth_ratio': 1.2,
            'client_vus': 512, 'client_max_vus': 2048, 'tenant_count': 4})
        self.policy_hash = self.digest(self.policy)
        self.environment = {'images': {'database_host': 'postgres'},
            'application_pool_max': {'api-a': 16, 'api-b': 16},
            'client': {'os': 'Linux', 'vus': 512, 'max_vus': 2048},
            'git': {'base_commit': 'a' * 40, 'tracked_tree_dirty': True},
            'application_source_sha256': runner.application_source_identity(self.root),
            'scripts_sha256': hashlib.sha256((self.root / 'load/k6.js').read_bytes()).hexdigest(),
            'policy_sha256': self.policy_hash}
        self.initial = self.make_index('initial', [(s,r) for s in ('hot','single','multi')
                                   for r in (10,25,50,100,200)], 0)
        self.extra = self.make_index('higher', [(s,400) for s in ('hot','single','multi')], 2000,
                                     failed='single')
        self.output = self.local / 'merged/index.json'
        self.run_output = self.local / 'new-step'
        self.now = 3000
        self.jobs = []

    def at(self, seconds):
        return (self.base + dt.timedelta(seconds=seconds)).isoformat()

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def make_run(self, directory, scenario, rps, started, failed=False):
        directory.mkdir(parents=True)
        job = {'kind': 'probe', 'scenario': scenario, 'rps': rps,
               'warmup_seconds': 30, 'measure_seconds': 60, 'repeat': 1}
        points = []
        for phase, offsets in (('warmup', (0, 29.9)), ('measure', (30, 89.9))):
            for offset in offsets:
                tags = {'phase': phase, 'operation': 'read'}
                for metric, extra_tags in (
                    ('cf_business_started', {}), ('http_reqs', {'traffic': 'business'}),
                    ('cf_e2e_ms', {'category': 'unexpected_failure' if failed else 'success',
                                   'issued': '1'})):
                    points.append({'type': 'Point', 'metric': metric,
                        'data': {'time': self.at(started + offset), 'value': 1,
                                 'tags': dict(tags, **extra_tags)}})
        raw = directory / 'raw.jsonl'
        raw.write_text(''.join(json.dumps(row) + '\n' for row in points), encoding='utf-8')
        (directory / 'resources.jsonl').write_text(''.join(json.dumps({
            'recorded_at': self.at(started + offset), 'client': {'rss_bytes': 1024},
            'api_process': {}, 'containers': [], 'database': {}, 'metrics': []}) + '\n'
            for offset in range(0, 95, 5)), encoding='utf-8')
        (directory / 'feeder.jsonl').write_text('{}\n', encoding='utf-8')
        execution = {'job': job, 'k6_exit_code': 99 if failed else 0,
            'protocol_completed': not failed, 'started_at': self.at(started),
            'ended_at': self.at(started + 90), 'feeder_failed': False,
            'resource_sampling_errors': 0}
        save(directory / 'execution.json', execution)
        report = runner.summarize([raw], 60, rps)
        report.update(job)
        report.update({k: execution[k] for k in ('k6_exit_code', 'started_at', 'ended_at',
                                               'feeder_failed', 'resource_sampling_errors')})
        report.update({'client_verdict_export_failed': False,
            'client_verdict_export_failure_details': [], 'feeder_reserved_writes': 0,
            'fixture_aux_requests': 0, 'formal_protocol': False,
            'evidence': {'raw': 'raw.jsonl', 'resources': 'resources.jsonl', 'feeder': 'feeder.jsonl'}})
        report['capacity_eligible'] = report['capacity_eligible'] and not failed
        save(directory / 'summary.json', report)
        return report

    def make_index(self, name, cases, started, failed=None):
        directory = self.local / name
        reports = []
        for i, (scenario, rps) in enumerate(cases):
            run_name = str(i + 1).zfill(2) + '-' + scenario + '-' + str(rps)
            report = self.make_run(directory / run_name, scenario, rps, started + i * 100,
                                   failed=scenario == failed)
            reports.append(dict(report, evidence_directory=run_name))
        index = directory / 'index.json'
        save(index, {'format_version': 1, 'kind': 'probe', 'protocol_complete': True,
            'policy_sha256': self.policy_hash, 'environment': copy.deepcopy(self.environment),
            'started_at': self.at(started), 'ended_at': self.at(started + len(cases) * 100),
            'reports': reports})
        return index

    def merge(self, inputs=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return probe_step.main(['merge', '--root', str(self.root), '--policy', str(self.policy),
                '--inputs'] + [str(p) for p in (inputs or (self.initial, self.extra))] +
                ['--output', str(self.output)])

    def run_step(self, mutate=None, failed=False, rps=400):
        def fake_job(ops, job, output, k6, vus, max_vus, tenant_count):
            self.jobs.append(copy.deepcopy(job))
            if (vus,max_vus,tenant_count) != (512,2048,4):
                raise AssertionError('Supplemental step changed pre-agreed client allocation')
            report = self.make_run(output, job['scenario'], job['rps'], self.now, failed)
            self.now += 100
            if mutate:
                mutate()
            return report
        with patch.object(probe_step, 'environment', return_value=copy.deepcopy(self.environment), create=True), \
             patch.object(probe_step, 'run_job', side_effect=fake_job, create=True), \
             patch.object(probe_step, 'utc_now', side_effect=lambda: self.at(self.now), create=True), \
             patch.object(probe_step, 'platform', types.SimpleNamespace(system=lambda: 'Linux'), create=True), \
             patch.object(runner, 'endpoints', return_value=('http://127.0.0.1:58095',
                                                          'http://127.0.0.1:59095')):
            with contextlib.redirect_stdout(io.StringIO()):
                return probe_step.main(['run','--root',str(self.root),'--policy',str(self.policy),
                    '--initial-probes',str(self.initial),'--rps',str(rps),'--k6','synthetic-k6',
                    '--output',str(self.run_output)])

    def change(self, path, mutate):
        document = json.loads(path.read_text(encoding='utf-8'))
        mutate(document)
        save(path, document)

    def replace_raw(self, slot, rows):
        index = json.loads(self.extra.read_text(encoding='utf-8'))
        report = index['reports'][slot]
        directory = self.extra.parent / report['evidence_directory']
        raw = directory / 'raw.jsonl'
        raw.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        report.update(runner.summarize([raw], 60, report['rps']))
        report['capacity_eligible'] = (report['capacity_eligible'] and report['k6_exit_code'] == 0
            and not report['feeder_failed'] and report['resource_sampling_errors'] == 0
            and not report['client_verdict_export_failed'])
        summary = copy.deepcopy(report)
        summary.pop('evidence_directory')
        save(directory / 'summary.json', summary)
        save(self.extra, index)

    def test_merge_preserves_failed_run_and_maps_original_evidence(self):
        self.assertEqual(0, self.merge())
        self.assertTrue(self.output.is_file(), 'Merge must create a reviewable index')
        result = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(18, len(result['reports']))
        failed = result['reports'][16]
        self.assertEqual(99, failed['k6_exit_code'])
        self.assertFalse(failed['capacity_eligible'])
        self.assertEqual({'unexpected_failure': 2}, failed['categories'])
        evidence = (self.output.parent / failed['evidence_directory']).resolve()
        self.assertEqual(self.extra.parent / '02-single-400', evidence)
        self.assertNotIn('resource_review', result)
        self.assertEqual(2, len(result['source_indexes']))
        self.assertEqual(self.digest(self.extra), result['source_indexes'][1]['sha256'])

    def test_run_adds_three_fixed_protocol_jobs_without_rewriting_initial_evidence(self):
        original = self.digest(self.initial)
        self.assertEqual(0, self.run_step())
        index = self.run_output / 'index.json'
        self.assertTrue(index.is_file(), 'Completed jobs must have a persisted index')
        result = json.loads(index.read_text(encoding='utf-8'))
        self.assertEqual(['hot','single','multi'], [row['scenario'] for row in result['reports']])
        self.assertTrue(result['protocol_complete'])
        self.assertEqual([(400,30,60,1)] * 3,
            [(j['rps'],j['warmup_seconds'],j['measure_seconds'],j['repeat']) for j in self.jobs])
        self.assertEqual(original, self.digest(self.initial))

    def test_run_preserves_complete_failed_runs_and_returns_failure(self):
        self.assertEqual(1, self.run_step(failed=True))
        self.assertTrue((self.run_output / 'index.json').is_file())
        result = json.loads((self.run_output / 'index.json').read_text(encoding='utf-8'))
        self.assertTrue(result['protocol_complete'])
        self.assertEqual([99,99,99], [r['k6_exit_code'] for r in result['reports']])

    def test_partial_input_is_rejected_before_any_output(self):
        self.change(self.extra, lambda d: d.update(protocol_complete=False))
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_duplicate_probe_pairs_are_rejected(self):
        self.assertEqual(1, self.merge([self.initial, self.initial]))
        self.assertFalse(self.output.exists())

    def test_missing_required_scenario_rate_is_rejected(self):
        self.change(self.initial, lambda d: d['reports'].pop())
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_short_raw_measurement_is_rejected_even_when_saved_statistics_match(self):
        directory = self.extra.parent / '01-hot-400'
        raw = directory / 'raw.jsonl'
        rows = [json.loads(line) for line in raw.read_text().splitlines()]
        for row in rows:
            if row['data']['tags']['phase'] == 'measure':
                row['data']['time'] = self.at(2031)
        raw.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_changed_summary_is_rejected(self):
        self.change(self.extra.parent / '01-hot-400/summary.json',
                    lambda d: d.update(capacity_eligible=True))
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_current_source_identity_must_match_recorded_environment(self):
        (self.root / 'load/k6.js').write_text('changed source\n', encoding='utf-8')
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_output_escape_and_existing_destination_are_rejected(self):
        self.output = self.root / 'published-index.json'
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())
        self.output = self.local / 'existing.json'
        self.output.write_text('preserve\n', encoding='utf-8')
        self.assertEqual(1, self.merge())
        self.assertEqual('preserve\n', self.output.read_text(encoding='utf-8'))

    def test_supplemental_rate_must_increase_and_stay_bounded(self):
        for rps in (200, 3201):
            with self.subTest(rps=rps):
                self.assertEqual(1, self.run_step(rps=rps))
                self.assertFalse(self.run_output.exists())
        self.assertEqual([], self.jobs)

    def test_initial_evidence_mutation_during_run_leaves_an_incomplete_index(self):
        def mutate():
            self.initial.write_bytes(self.initial.read_bytes() + b' ')
        self.assertEqual(1, self.run_step(mutate=mutate))
        self.assertTrue((self.run_output / 'index.json').is_file())
        result = json.loads((self.run_output / 'index.json').read_text(encoding='utf-8'))
        self.assertFalse(result['protocol_complete'])

    def test_execution_job_boolean_does_not_impersonate_integer_repeat(self):
        self.change(self.extra.parent / '01-hot-400/execution.json',
                    lambda d: d['job'].update(repeat=True))
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_export_failure_details_cannot_disagree_with_failure_flag(self):
        detail = [{'stage': 'raw-scan', 'reason': 'OSError', 'errno': 61}]
        self.change(self.extra, lambda d: d['reports'][0].update(client_verdict_export_failure_details=detail))
        self.change(self.extra.parent / '01-hot-400/summary.json',
                    lambda d: d.update(client_verdict_export_failure_details=detail))
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_existing_provenance_rejects_changed_raw_bytes_even_if_statistics_are_unchanged(self):
        self.assertEqual(0, self.merge())
        previous = self.output
        raw = self.extra.parent / '01-hot-400/raw.jsonl'
        raw.write_bytes(raw.read_bytes() + b'{"type":"Metric","metric":"synthetic"}\n')
        self.output = self.local / 'second-merge/index.json'
        self.assertEqual(1, self.merge([previous]))
        self.assertFalse(self.output.exists())

    def test_raw_offset_and_nanosecond_precision_preserve_the_same_window(self):
        raw = self.extra.parent / '01-hot-400/raw.jsonl'
        rows = [json.loads(line) for line in raw.read_text().splitlines()]
        for row in rows:
            parsed = dt.datetime.fromisoformat(row['data']['time'])
            value = parsed.astimezone(dt.timezone(dt.timedelta(hours=8))).isoformat()
            if '.' in value:
                value = value.replace('00000+08:00', '00000000+08:00')
            row['data']['time'] = value
        raw.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        self.assertEqual(0, self.merge())
        self.assertTrue(self.output.is_file())

    def test_non_utc_execution_metadata_is_rejected(self):
        def change_end(document):
            end = dt.datetime.fromisoformat(document['ended_at'])
            document['ended_at'] = end.astimezone(dt.timezone(dt.timedelta(hours=8))).isoformat()
        self.change(self.extra.parent / '01-hot-400/execution.json', change_end)
        self.change(self.extra.parent / '01-hot-400/summary.json', change_end)
        self.change(self.extra, lambda d: change_end(d['reports'][0]))
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_resource_error_count_must_match_original_flags(self):
        resources = self.extra.parent / '01-hot-400/resources.jsonl'
        rows = [json.loads(line) for line in resources.read_text().splitlines()]
        rows[0]['metric_observation_failed'] = True
        resources.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_raw_mutation_during_analysis_is_rejected(self):
        original = runner.summarize
        def mutating_summarize(paths, *args, **kwargs):
            result = original(paths, *args, **kwargs)
            path = pathlib.Path(paths[0])
            if path.parent.name == '01-hot-400':
                path.write_bytes(path.read_bytes() + b'\n')
            return result
        with patch.object(probe_step, 'summarize', side_effect=mutating_summarize, create=True):
            self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_symlinked_evidence_is_rejected(self):
        raw = self.extra.parent / '01-hot-400/raw.jsonl'
        original = raw.with_name('original-raw.jsonl')
        raw.rename(original)
        try:
            os.symlink(original, raw)
        except OSError:
            original.rename(raw)
            self.skipTest('Creating symlinks requires host permission')
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_change_during_output_write_preserves_an_incomplete_merge(self):
        original = probe_step.exclusive_json
        def write_and_change(path, document, root):
            original(path, document, root)
            self.initial.write_bytes(self.initial.read_bytes() + b' ')
        with patch.object(probe_step, 'exclusive_json', side_effect=write_and_change):
            self.assertEqual(1, self.merge())
        self.assertTrue(self.output.is_file())
        self.assertFalse(json.loads(self.output.read_text(encoding='utf-8'))['protocol_complete'])

    def test_complete_failed_window_retains_unissued_client_verdicts_after_last_business_start(self):
        raw = self.extra.parent / '01-hot-400/raw.jsonl'
        rows = [json.loads(line) for line in raw.read_text().splitlines()]
        tail_time = self.at(2089.9)
        rows = [row for row in rows if row['data']['time'] != tail_time]
        rows.append({'type': 'Point', 'metric': 'cf_e2e_ms', 'data': {
            'time': tail_time, 'value': 0, 'tags': {'phase': 'measure', 'operation': 'read',
            'category': 'client_failure', 'issued': '0'}}})
        self.replace_raw(0, rows)
        self.assertEqual(0, self.merge())
        result = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual({'client_failure': 1, 'success': 1}, result['reports'][15]['categories'])
        self.assertFalse(result['reports'][15]['capacity_eligible'])

    def test_complete_failed_window_retains_dropped_arrivals_after_last_business_start(self):
        raw = self.extra.parent / '01-hot-400/raw.jsonl'
        rows = [json.loads(line) for line in raw.read_text().splitlines()]
        tail_time = self.at(2089.9)
        rows = [row for row in rows if row['data']['time'] != tail_time]
        rows.append({'type': 'Point', 'metric': 'dropped_iterations', 'data': {
            'time': tail_time, 'value': 1, 'tags': {'scenario': 'measure'}}})
        self.replace_raw(0, rows)
        self.assertEqual(0, self.merge())
        result = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(1, result['reports'][15]['dropped_iterations'])
        self.assertFalse(result['reports'][15]['capacity_eligible'])

    def test_late_issued_completion_cannot_extend_a_short_invocation_window(self):
        raw = self.extra.parent / '01-hot-400/raw.jsonl'
        rows = [json.loads(line) for line in raw.read_text().splitlines()]
        for row in rows:
            if row['data']['time'] == self.at(2089.9) and row['metric'] == 'cf_business_started':
                row['data']['time'] = self.at(2031)
        self.replace_raw(0, rows)
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())

    def test_execution_booleans_cannot_impersonate_integer_exit_or_error_count(self):
        execution = self.extra.parent / '01-hot-400/execution.json'
        original = json.loads(execution.read_text(encoding='utf-8'))
        for field in ('k6_exit_code', 'resource_sampling_errors'):
            with self.subTest(field=field):
                self.output = self.local / ('metadata-' + field + '/index.json')
                changed = copy.deepcopy(original); changed[field] = False
                save(execution, changed)
                self.assertEqual(1, self.merge())
                self.assertFalse(self.output.exists())

    def test_extended_invocation_window_cannot_be_reported_as_sixty_seconds(self):
        raw = self.extra.parent / '03-multi-400/raw.jsonl'
        rows = [json.loads(line) for line in raw.read_text().splitlines()]
        for row in rows:
            if row['data']['time'] == self.at(2289.9):
                row['data']['time'] = self.at(2293)
        self.replace_raw(2, rows)
        def extend(document):
            document['ended_at'] = self.at(2295)
        self.change(self.extra.parent / '03-multi-400/execution.json', extend)
        self.change(self.extra.parent / '03-multi-400/summary.json', extend)
        self.change(self.extra, lambda d: extend(d['reports'][2]))
        self.assertEqual(1, self.merge())
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
