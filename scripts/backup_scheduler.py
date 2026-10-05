#!/usr/bin/env python3
"""Run verified backups hourly; failures update metrics and do not stop subsequent attempts."""
import argparse
import pathlib
import time
from backup import run_backup
from ops_common import Ops


def schedule(job, interval=3600, iterations=None, sleep=time.sleep, monotonic=time.monotonic):
    attempts = failures = 0
    while iterations is None or attempts < iterations:
        started = monotonic()
        try: job()
        except Exception: failures += 1
        attempts += 1
        if iterations is None or attempts < iterations: sleep(max(0, interval - (monotonic() - started)))
    return {'attempts': attempts, 'failures': failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(pathlib.Path(__file__).resolve().parents[1]))
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(); ops = Ops(args.root)
    result = schedule(lambda: run_backup(ops), iterations=1 if args.once else None)
    print('PASS: backup scheduler completed' if result['failures'] == 0 else 'FAIL: backup attempts failed; inspect backup metrics')
    return int(result['failures'] > 0)


if __name__ == '__main__': raise SystemExit(main())
