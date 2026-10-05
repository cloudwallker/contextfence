#!/usr/bin/env python3
"""Test, build and release digest-pinned API instances; failed releases never downgrade schema."""
import argparse
import json
import os
import pathlib
import re
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from ops_common import Ops, OpsError, atomic_json, atomic_bytes, initialize_ops, utc_now


def clear_maven_test_reports(root, archive=None):
    root=pathlib.Path(root).resolve();target=root/'target'
    archive=pathlib.Path(archive) if archive else root/'.local/deployment-logs'/('prior-reports-'+uuid.uuid4().hex)
    for name in ('surefire-reports','failsafe-reports'):
        directory=target/name
        for report in directory.glob('TEST-*.xml'):
            if report.is_symlink() or not report.resolve().is_relative_to(target):
                raise OpsError('Generated Maven report has invalid workspace scope')
            preserved=archive/name/report.name;preserved.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(report,preserved);preserved.chmod(0o600)
            report.unlink()


def maven_test_summary(root, started):
    summary = {}
    for kind, directory in (('unit','surefire-reports'),('integration','failsafe-reports')):
        reports = sorted((pathlib.Path(root)/'target'/directory).glob('TEST-*.xml'))
        totals = {'tests':0,'failures':0,'errors':0,'skipped':0,'suites':len(reports)}
        if not reports: raise OpsError('Mandatory Maven test reports are unavailable')
        for report in reports:
            if report.stat().st_mtime < started - 2: raise OpsError('Maven test report is stale')
            try:
                suite = ET.parse(report).getroot()
                for field in ('tests','failures','errors','skipped'):
                    count = int(suite.attrib[field])
                    if count < 0: raise ValueError()
                    totals[field] += count
            except (ET.ParseError,KeyError,ValueError): raise OpsError('Invalid Maven test report') from None
        if totals['tests']<=0 or any(totals[key] for key in ('failures','errors','skipped')):
            raise OpsError('Mandatory Maven checks were not fully exercised successfully')
        summary[kind] = totals
    return summary


def python_test_summary(result):
    output = result.stdout + '\n' + result.stderr
    ran = re.findall(r'^Ran (\d+) tests? in [0-9.]+s\s*$',output,re.MULTILINE)
    ok = re.findall(r'^OK(?: \(skipped=(\d+)\))?\s*$',output,re.MULTILINE)
    if result.returncode!=0 or len(ran)!=1 or len(ok)!=1 or int(ran[0])<=int(ok[0] or 0):
        raise OpsError('Mandatory Python checks did not produce a completed success report')
    return {'exit_code':0,'tests':int(ran[0]),'skipped':int(ok[0] or 0),'failures':0,'errors':0}


def deploy_release(ops, candidate, previous):
    started = time.monotonic()
    # Reserve three minutes for rollback within the five-minute total budget.
    ops.deadline = started + 120
    rows = []
    def event(phase, instance=None):
        rows.append({'phase':phase,'instance':instance,'recorded_at':utc_now(),'elapsed_seconds':round(time.monotonic()-started,3)})
        if time.monotonic() - started > 300: raise OpsError('Deployment acceptance deadline exceeded')
    def report(outcome):
        return {'outcome':outcome,'candidate_digest':candidate,'previous_digests':previous,
            'schema_rollback':False,'duration_seconds':round(time.monotonic()-started,3),'observations':rows}
    try:
        ops.migrate(candidate); event('migration-complete')
        ops.open_app_gate('deployment-internal-smoke')
        # The previous B image must still serve the expanded schema before A is replaced.
        ops.wait_ready('api-b',timeout=30)
        ops.smoke('api-b'); event('previous-schema-safety-passed','api-b')
        for instance in ('api-a','api-b'):
            ops.drain(instance, timeout=30); event('drained',instance)
            ops.switch_image(instance,candidate); event('candidate-started',instance)
            # Leave time to record the readiness failure before the 120-second forward limit.
            readiness_budget = min(90,max(0,ops.deadline-time.monotonic()-2))
            try: ops.wait_ready(instance,timeout=readiness_budget)
            except Exception:
                event('candidate-readiness-failed',instance)
                raise
            ops.smoke(instance); event('candidate-safety-passed',instance)
            ops.resume(instance); event('candidate-in-flow',instance)
        # Initial deployments remain proxy CLOSED until both internal checks passed.
        ops.open_maintenance('deployment-safety-passed',verified=True)
        event('accepted')
        return report('DEPLOYED')
    except Exception:
        # Never expose an exception string: child programs may put tokens/URLs in it.
        try:
            ops.deadline = started + 300
            ops.close_maintenance('deployment-failed')
            event('rollback-ingress-closed')
            ops.open_app_gate('rollback-internal-smoke')
            for instance in ('api-a','api-b'):
                ops.switch_image(instance,previous[instance]); event('previous-started',instance)
                ops.wait_ready(instance,timeout=90)
                ops.smoke(instance); event('rollback-safety-passed',instance)
                ops.resume(instance)
            ops.open_maintenance('rollback-safety-passed',verified=True)
            event('rollback-accepted')
            return report('ROLLED_BACK')
        except Exception:
            try: ops.close_maintenance('rollback-failed')
            except Exception: pass
            return report('FAILED_CLOSED')
    finally:
        ops.deadline = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument('--candidate',help='Existing image reference, still verified and resolved to an immutable local digest')
    parser.add_argument('--output',default='artifacts/local/deployment.json')
    args = parser.parse_args(); ops = Ops(args.root)
    try:
        initialize_ops(ops.root)
        run_id = uuid.uuid4().hex
        log_root = ops.root/'.local/deployment-logs'; log_root.mkdir(parents=True,exist_ok=True)
        clear_maven_test_reports(ops.root,log_root/(run_id+'-prior-reports'))
        verification_started = time.time()
        # No skip-tests switch: core real PostgreSQL integration checks are mandatory.
        if os.name == 'nt': maven = ops.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ops.root / 'scripts/maven.ps1'),'verify'],check=False,timeout=900)
        else: maven = ops.run(['mvn','--batch-mode','--no-transfer-progress','verify'],check=False,timeout=900)
        maven_log = log_root/(run_id+'-maven.log')
        atomic_bytes(maven_log,(maven.stdout+'\n'+maven.stderr).encode(),mode=0o600)
        if maven.returncode!=0: raise OpsError('Mandatory Maven verification failed')
        maven_summary = {'exit_code':maven.returncode,**maven_test_summary(ops.root,verification_started),
            'private_log':maven_log.relative_to(ops.root).as_posix()}
        python = ops.run([os.sys.executable,'-m','unittest','discover','-s','scripts/tests','-v'],check=False,timeout=180)
        python_log = log_root/(run_id+'-python.log')
        atomic_bytes(python_log,(python.stdout+'\n'+python.stderr).encode(),mode=0o600)
        python_summary = {**python_test_summary(python),'private_log':python_log.relative_to(ops.root).as_posix()}
        verification = {'completed_at':utc_now(),'maven':maven_summary,'python':python_summary}
        atomic_json(ops.root/'artifacts/local'/('deployment-verification-'+run_id+'.json'),verification)
        candidate = args.candidate
        if candidate is None:
            candidate = 'contextfence:candidate'
            ops.run(['docker','build','--tag',candidate,'.'],timeout=900)
        image = json.loads(ops.run(['docker','image','inspect',candidate]).stdout)[0]
        digest = image['Id']
        before = ops.image_manifest()
        previous = {name:data['image_digest'] for name,data in before['services'].items()}
        atomic_json(ops.root / 'artifacts/local/pre-deployment-images.json',before)
        report = deploy_release(ops,digest,previous)
        report.update({'format_version':1,'recorded_at':utc_now(),'verification':verification})
        atomic_json(ops.root / args.output,report)
        try: report['actual_images'] = ops.image_manifest()
        except OpsError: report['actual_images_available'] = False
        atomic_json(ops.root / args.output,report)
        print('Deployment outcome: ' + report['outcome'] + '; database schema was not rolled back')
        return 0 if report['outcome'] == 'DEPLOYED' else 1
    except (OpsError,OSError,ValueError):
        print('FAIL: deployment preflight; no private command output was printed')
        return 1


if __name__ == '__main__': raise SystemExit(main())
