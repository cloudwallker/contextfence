#!/usr/bin/env python3
"""Measure a real database fault, backup loss, current authority rebuild and safe traffic reopening."""
import argparse
import json
import pathlib
from backup import run_backup
from fixture_producer import prepare_fixture, mutate_fixture
from ops_common import Ops, OpsError, utc_now
from restore import run_restore


def run_demo(ops):
    try:
        directory=ops.root/'.local/recovery'; directory.mkdir(parents=True,exist_ok=True)
        ledger=directory/'ledger.json'; fixture=directory/'fixture.json'
        prepare_fixture(ops,ledger,fixture)
        backup=run_backup(ops)
        mutate_fixture(ops,ledger,fixture)
        failure_at=utc_now()
        ops.compose('stop',ops.database_host(),timeout=60)
        return run_restore(ops,ops.root/'.local/backups'/backup['backup_id'],ledger,fixture,failure_at)
    except Exception:
        try: ops.close_maintenance('recovery-demo-failed')
        except Exception: pass
        raise OpsError('Recovery demo failed; both gates remain closed and private diagnostics were omitted') from None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default=str(pathlib.Path(__file__).resolve().parents[1]))
    args=parser.parse_args(); ops=Ops(args.root)
    try:
        result=run_demo(ops)
        print(json.dumps({'status':result['status'],'rto_seconds':result['rto_seconds'],
             'snapshot_age_seconds':result['rpo']['snapshot_age_seconds'],
             'missing_context_count':result['rpo']['missing_context_count'],
             'missing_receipt_count':result['rpo']['receipt_history']['missing_receipt_count'],
             'source_event_history_rebuilt':result['rpo']['source_event_history']['rebuilt_missing_records'],
             'safety_all_passed':result['safety']['all_passed']},sort_keys=True))
        return 0
    except (OpsError,OSError,ValueError):
        print('FAIL: recovery demo; both gates are closed, inspect private structured evidence')
        return 1


if __name__=='__main__': raise SystemExit(main())
