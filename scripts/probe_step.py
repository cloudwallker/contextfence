#!/usr/bin/env python3
"""Add a higher 30+60-second probe step, or bind complete probe indexes for freeze review.

Only local ignored evidence is written. Merge preserves failed observations and never
approves capacity, resource stability, wait growth or a frozen target.
"""
import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import platform
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from load import runner
from load.analyze import EvidenceError, summarize
from load.fixtures import LoadError
from load.runner import environment, run_job
from ops_common import Ops, OpsError, atomic_json, strict_object, utc_now

SCENARIOS = ('hot', 'single', 'multi')
REQUIRED_RPS = (10, 25, 50, 100, 200)


def require(condition):
    if not condition:
        raise LoadError('Supplemental probe evidence contract failed')


def regular_path(path, root, must_exist=True):
    """Reject aliases and symlinks, including an existing parent of a new output."""
    path = pathlib.Path(path)
    require('..' not in path.parts)
    path = path.absolute()
    allowed = root / 'artifacts/local'
    require(path != allowed and path.is_relative_to(allowed))
    for candidate in (path,) + tuple(path.parents):
        require(not candidate.is_symlink())
    require(path.resolve() == path)
    if must_exist:
        require(path.is_file())
    return path


def evidence_path(directory, relative, root):
    require(type(relative) is str and relative and not pathlib.Path(relative).is_absolute())
    # A merged index may refer to a sibling directory, but never outside local evidence.
    candidate = directory / relative
    for parent in (candidate,) + tuple(candidate.parents):
        require(not parent.is_symlink())
    resolved = candidate.resolve()
    require(resolved.is_relative_to(root / 'artifacts/local'))
    return regular_path(resolved, root)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def reject_constant(value):
    raise ValueError('Non-finite JSON value')


class Bindings:
    def __init__(self, root):
        self.root = root
        self.files = {}

    def bind(self, path):
        path = regular_path(path, self.root)
        digest = file_digest(path)
        require(path not in self.files or self.files[path] == digest)
        self.files[path] = digest
        return path

    def read(self, path):
        path = self.bind(path)
        require(path.stat().st_size <= 32 * 1024 * 1024)
        result = json.loads(path.read_text(encoding='utf-8-sig'),
                            object_pairs_hook=strict_object, parse_constant=reject_constant)
        require(type(result) is dict)
        return result

    def verify(self):
        for path, digest in self.files.items():
            regular_path(path, self.root)
            require(file_digest(path) == digest)


def instant(value, utc=True):
    require(type(value) is str and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?'
        r'(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)', value) is not None)
    # Python 3.9 accepts only millisecond/microsecond fractions. Normalize for the
    # boundary comparison without rewriting the original nanosecond evidence.
    normalized = re.sub(r'\.(\d{1,9})', lambda match: '.' + match.group(1)[:6].ljust(6, '0'), value)
    parsed = dt.datetime.fromisoformat(normalized[:-1] + '+00:00'
                                      if normalized.endswith('Z') else normalized)
    require(parsed.utcoffset() is not None)
    if utc:
        require(parsed.utcoffset() == dt.timedelta())
    return parsed.timestamp()


def source_identity(root):
    files = sorted(path for path in (root / 'load').glob('*') if path.is_file())
    require(files)
    for path in files:
        require(not path.is_symlink() and path.resolve() == path.absolute())
    application_files = [root / 'pom.xml', root / 'Dockerfile'] + [
        path for path in (root / 'src/main').rglob('*') if path.is_file()]
    for path in application_files:
        require(path.resolve() == path.absolute())
        for parent in (path,) + tuple(path.parents):
            require(not parent.is_symlink())
    return (runner.application_source_identity(root),
            hashlib.sha256(b''.join(path.read_bytes() for path in files)).hexdigest())


def policy_document(bindings, path):
    policy = bindings.read(path)
    require(type(policy.get('format_version')) is int and policy['format_version'] == 1)
    require(policy.get('required_probe_rps') == list(REQUIRED_RPS))
    require(policy.get('steady_warmup_seconds') == 300 and policy.get('steady_measure_seconds') == 600)
    for key in ('client_vus', 'client_max_vus', 'tenant_count'):
        require(type(policy.get(key)) is int)
    require(1 <= policy['client_vus'] <= policy['client_max_vus'] <= 8192)
    require(2 <= policy['tenant_count'] <= 64)
    require(type(policy.get('regression_tolerance')) in (int, float)
            and math.isfinite(policy['regression_tolerance']) and 0 <= policy['regression_tolerance'] <= .5)
    require(policy.get('max_unexpected_failure_rate') == .01 and policy.get('max_unsafe_allow') == 0)
    require(policy.get('max_sustained_wait_growth_ratio') == 1.2)
    instant(policy.get('created_at'))
    return policy, bindings.files[path]


def raw_windows(path, execution):
    windows = {'warmup': [], 'measure': []}
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            point = json.loads(line, object_pairs_hook=strict_object, parse_constant=reject_constant)
            require(type(point) is dict)
            if point.get('type') != 'Point':
                continue
            metric = point.get('metric')
            if metric not in ('cf_business_started', 'http_reqs', 'cf_e2e_ms', 'dropped_iterations'):
                continue
            data = point['data']; tags = data.get('tags') or {}
            phase = tags.get('scenario', tags.get('phase')) if metric == 'dropped_iterations' else tags.get('phase')
            if phase not in windows:
                continue
            if metric == 'http_reqs' and tags.get('traffic') != 'business':
                continue
            require(type(data.get('value')) in (int, float) and math.isfinite(data['value']))
            at = instant(data['time'], utc=False)  # Original k6 samples may carry a local offset.
            require(instant(execution['started_at']) - 2 <= at <= instant(execution['ended_at']) + 2)
            if metric in ('cf_business_started', 'dropped_iterations') and data['value'] > 0:
                windows[phase].append(at)
            elif metric == 'cf_e2e_ms' and tags.get('issued') == '0':
                require(tags.get('category') == 'client_failure')
                windows[phase].append(at)
            # Issued request completions may use grace time; they cannot extend
            # invocation coverage after the arrival-rate executor stopped.
    started = instant(execution['started_at'])
    for phase, duration, offset in (('warmup', 30, 0), ('measure', 60, 30)):
        samples = windows[phase]
        require(len(samples) >= 2)
        first, last = min(samples), max(samples)
        require(last - first >= duration - 2 and first >= started + offset - 2)
        if phase == 'measure':
            require(last - first <= duration + 2)
    return min(windows['measure']), max(windows['measure'])


def resource_records(path, execution, errors, window):
    """Bind complete timed records; resource sufficiency remains a separate human review."""
    times = []; observed_errors = 0
    flags = ('client_observation_failed', 'api_process_observation_failed',
             'service_observation_failed', 'metric_observation_failed')
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line, object_pairs_hook=strict_object, parse_constant=reject_constant)
            require(type(row) is dict)
            at = instant(row.get('recorded_at'))
            require(instant(execution['started_at']) <= at <= instant(execution['ended_at']))
            require(not times or at >= times[-1])
            times.append(at)
            for flag in flags:
                require(type(row.get(flag, False)) is bool)
                observed_errors += int(row.get(flag, False))
    require(observed_errors == errors and len(times) >= 2)
    require(times[0] <= window[0] + 15 and times[-1] >= window[1] - 15)


def validate_report(bindings, index_path, report, policy):
    require(type(report) is dict and report.get('kind') == 'probe')
    require(type(report.get('format_version')) is int and report['format_version'] == 1)
    require(report.get('scenario') in SCENARIOS and type(report.get('rps')) is int
            and 1 <= report['rps'] <= 3200)
    require(all(type(report.get(key)) is int and report[key] == value
                for key, value in (('warmup_seconds', 30), ('measure_seconds', 60), ('repeat', 1))))
    require(report.get('formal_protocol') is False and report.get('phase') == 'measure')
    directory_name = report.get('evidence_directory')
    require(type(directory_name) is str and directory_name)
    # Validate a mapped directory via a known regular evidence file.
    summary_path = evidence_path(index_path.parent, directory_name + '/summary.json', bindings.root)
    directory = summary_path.parent
    summary = bindings.read(summary_path)
    original = copy.deepcopy(report); original.pop('evidence_directory')
    require(summary == original)
    execution = bindings.read(directory / 'execution.json')
    expected_job = {key: report[key] for key in ('kind', 'scenario', 'rps',
                    'warmup_seconds', 'measure_seconds', 'repeat')}
    require(execution.get('job') == expected_job)
    require(all(type(execution['job'].get(key)) is type(value)
                for key, value in expected_job.items()))
    for key in ('started_at', 'ended_at', 'k6_exit_code', 'feeder_failed', 'resource_sampling_errors'):
        require(type(execution.get(key)) is type(report.get(key))
                and execution.get(key) == report.get(key))
    require(type(report.get('k6_exit_code')) is int and -32768 <= report['k6_exit_code'] <= 32767)
    require(type(execution.get('protocol_completed')) is bool
            and execution['protocol_completed'] == (report['k6_exit_code'] == 0))
    require(type(report.get('feeder_failed')) is bool and type(report.get('resource_sampling_errors')) is int
            and report['resource_sampling_errors'] >= 0)
    require(type(report.get('client_verdict_export_failed')) is bool
            and type(report.get('client_verdict_export_failure_details')) is list)
    details = report['client_verdict_export_failure_details']
    require(len(details) <= 3 and bool(details) == report['client_verdict_export_failed'])
    for detail in details:
        require(type(detail) is dict and set(detail) == {'stage','reason','errno'})
        require(detail['stage'] in ('init-publish','raw-scan','periodic-publish',
                'thread-not-stopped','final-drain','final-publish'))
        require(detail['reason'] in ('ThreadNotStopped','EvidenceError','OSError','TypeError','ValueError'))
        require(detail['errno'] is None or (type(detail['errno']) is int and 0 <= detail['errno'] <= 4095))
    require(type(report.get('capacity_eligible')) is bool)
    started, ended = instant(report.get('started_at')), instant(report.get('ended_at'))
    require(started >= instant(policy['created_at']) and ended - started >= 90)
    require(report.get('evidence') == {'raw': 'raw.jsonl', 'feeder': 'feeder.jsonl', 'resources': 'resources.jsonl'})
    paths = {name: bindings.bind(directory / filename) for name, filename in report['evidence'].items()}
    window = raw_windows(paths['raw'], execution)
    actual = summarize([paths['raw']], 60, report['rps'])
    summarize([paths['raw']], 30, report['rps'], phase='warmup')
    eligible = (actual['capacity_eligible'] and report['k6_exit_code'] == 0
                and not report['feeder_failed'] and report['resource_sampling_errors'] == 0
                and not report['client_verdict_export_failed'])
    for key, value in actual.items():
        require(report.get(key) == (eligible if key == 'capacity_eligible' else value))
    resource_records(paths['resources'], execution, report['resource_sampling_errors'], window)
    return started, ended, directory


def verify_provenance(bindings, index):
    if 'source_indexes' not in index:
        return
    sources = index['source_indexes']
    require(type(sources) is list and 1 <= len(sources) <= 512)
    for source in sources:
        require(type(source) is dict and set(source) == {'path','sha256','evidence_files'})
        references = source['evidence_files']
        require(type(references) is list and references)
        for reference in [{'path':source['path'], 'sha256':source['sha256']}] + references:
            require(type(reference) is dict and set(reference) == {'path','sha256'})
            require(type(reference['path']) is str and not pathlib.Path(reference['path']).is_absolute())
            require(type(reference['sha256']) is str
                    and re.fullmatch(r'[0-9a-f]{64}', reference['sha256']) is not None)
            path = bindings.bind(bindings.root / reference['path'])
            require(bindings.files[path] == reference['sha256'])


def load_inputs(bindings, inputs, policy, policy_hash, require_initial=True):
    reports = []; sources = []; seen = set(); env = None; previous_end = None
    application_hash, scripts_hash = source_identity(bindings.root)
    for input_path in inputs:
        index_path = regular_path(input_path, bindings.root)
        before = set(bindings.files)
        index = bindings.read(index_path)
        verify_provenance(bindings, index)
        require(type(index.get('format_version')) is int and index['format_version'] == 1)
        require(index.get('kind') == 'probe' and index.get('protocol_complete') is True)
        require(index.get('policy_sha256') == policy_hash)
        current = index.get('environment')
        require(type(current) is dict and current.get('policy_sha256') == policy_hash
                and current.get('application_source_sha256') == application_hash
                and current.get('scripts_sha256') == scripts_hash)
        require(current.get('client', {}).get('os') == 'Linux')
        require(current.get('client', {}).get('vus') == policy['client_vus']
                and current.get('client', {}).get('max_vus') == policy['client_max_vus'])
        require(env is None or env == current)
        env = current
        index_start, index_end = instant(index.get('started_at')), instant(index.get('ended_at'))
        require(index_end >= index_start and type(index.get('reports')) is list and index['reports'])
        for report in index['reports']:
            started, ended, directory = validate_report(bindings, index_path, report, policy)
            pair = (report['scenario'], report['rps'])
            require(pair not in seen and index_start <= started <= ended <= index_end)
            require(previous_end is None or started >= previous_end)
            previous_end = ended; seen.add(pair)
            reports.append((copy.deepcopy(report), directory))
        sources.append({'path': index_path.relative_to(bindings.root).as_posix(),
            'sha256': bindings.files[index_path], 'evidence_files': [
                {'path': path.relative_to(bindings.root).as_posix(), 'sha256': bindings.files[path]}
                for path in sorted(set(bindings.files) - before, key=str) if path != index_path]})
    if require_initial:
        require(all((scenario,rps) in seen for scenario in SCENARIOS for rps in REQUIRED_RPS))
        rates = {rps for scenario,rps in seen}
        require(all((scenario,rps) in seen for scenario in SCENARIOS for rps in rates))
    bindings.verify()
    require(source_identity(bindings.root) == (application_hash, scripts_hash))
    return reports, copy.deepcopy(env), sources


def exclusive_json(path, document, root):
    regular_path(path, root, must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    regular_path(path, root, must_exist=False)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(str(path), flags, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(document, stream, sort_keys=True, indent=2, ensure_ascii=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'merge'))
    parser.add_argument('--root', default=str(ROOT)); parser.add_argument('--policy', required=True)
    parser.add_argument('--initial-probes'); parser.add_argument('--inputs', nargs='+')
    parser.add_argument('--rps', type=int); parser.add_argument('--k6', default='k6')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        root = pathlib.Path(args.root).absolute()
        require('..' not in root.parts and root.resolve() == root and root.is_dir())
        for parent in (root,) + tuple(root.parents):
            require(not parent.is_symlink())
        bindings = Bindings(root)
        policy_path = regular_path(args.policy, root)
        policy, policy_hash = policy_document(bindings, policy_path)
        output = regular_path(args.output, root, must_exist=False)
        require(not output.exists())
        source_before = source_identity(root)
        if args.action == 'merge':
            require(args.inputs and args.initial_probes is None and args.rps is None)
            reports, env, sources = load_inputs(bindings, args.inputs, policy, policy_hash)
            mapped = []
            for report, directory in reports:
                report['evidence_directory'] = pathlib.Path(os.path.relpath(directory, output.parent)).as_posix()
                mapped.append(report)
            document = {'format_version': 1, 'kind': 'probe', 'protocol_complete': True,
                'started_at': mapped[0]['started_at'], 'ended_at': mapped[-1]['ended_at'],
                'created_at': utc_now(), 'policy_sha256': policy_hash, 'environment': env,
                'reports': mapped, 'source_indexes': sources}
            bindings.verify(); require(source_identity(root) == source_before)
            exclusive_json(output, document, root)
            try:
                bindings.verify(); require(source_identity(root) == source_before)
            except (LoadError, OSError):
                document['protocol_complete'] = False
                atomic_json(output, document)
                raise
            print('PASS: complete probe indexes bound; failed observations retained; no capacity approval')
            return 0
        require(args.initial_probes and args.inputs is None and type(args.rps) is int)
        require(platform.system() == 'Linux')
        reports, previous_env, sources = load_inputs(bindings, [args.initial_probes], policy, policy_hash)
        require(max(report['rps'] for report, directory in reports) < args.rps <= 3200)
        ops = Ops(root)
        runner.endpoints(ops)  # Enforce the public local HAProxy/Prometheus port and project guard.
        env = environment(ops, args.k6, policy['client_vus'], policy['client_max_vus'])
        env['policy_sha256'] = policy_hash
        require(env == previous_env)
        bindings.verify(); require(source_identity(root) == source_before)
        output.parent.mkdir(parents=True, exist_ok=True)
        regular_path(output, root, must_exist=False)
        output.mkdir(mode=0o700, exist_ok=False)
        document = {'format_version': 1, 'kind': 'probe', 'protocol_complete': False,
            'started_at': utc_now(), 'policy_sha256': policy_hash, 'environment': env,
            'reports': [], 'source_indexes': sources}
        atomic_json(output / 'index.json', document)
        for number, scenario in enumerate(SCENARIOS, 1):
            bindings.verify(); require(source_identity(root) == source_before)
            job = {'kind': 'probe', 'scenario': scenario, 'rps': args.rps,
                   'warmup_seconds': 30, 'measure_seconds': 60, 'repeat': 1}
            directory = output / (str(number).zfill(2) + '-' + scenario + '-' + str(args.rps))
            report = run_job(ops, job, directory, args.k6, policy['client_vus'],
                             policy['client_max_vus'], policy['tenant_count'])
            report['evidence_directory'] = directory.name
            document['reports'].append(report)
            atomic_json(output / 'index.json', document)
        after_env = environment(ops, args.k6, policy['client_vus'], policy['client_max_vus'])
        after_env['policy_sha256'] = policy_hash
        require(after_env == env)
        bindings.verify(); require(source_identity(root) == source_before)
        document['ended_at'] = utc_now(); document['protocol_complete'] = True
        atomic_json(output / 'index.json', document)
        succeeded = all(report['k6_exit_code'] == 0 and not report['feeder_failed']
            and report['resource_sampling_errors'] == 0 and not report['client_verdict_export_failed']
            for report in document['reports'])
        print(('PASS' if succeeded else 'FAIL') +
              ': supplemental protocol completed; original failure evidence retained; no capacity approval')
        return 0 if succeeded else 1
    except (LoadError, EvidenceError, OpsError, OSError, ValueError, KeyError, TypeError, AttributeError):
        print('FAIL: supplemental probe execution or evidence validation; private outputs were not printed')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
