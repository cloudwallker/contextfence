#!/usr/bin/env python3
"""Close both persistent gates before stopping; preserve database volumes and credentials."""
import argparse
import pathlib
import sys
from ops_common import Ops


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default=str(pathlib.Path(__file__).resolve().parents[1]))
    args = parser.parse_args(); ops = Ops(args.root)
    try:
        if not (ops.root / '.env').is_file() or not (ops.root / 'compose.yaml').is_file(): raise ValueError()
        ops.close_maintenance('stopping')
        ops.compose('down',timeout=180)
        print('ContextFence stopped. Database volumes and local credentials were retained; ingress remains closed.')
        return 0
    except Exception:
        print('Stop failed; private Docker output was not printed. Persisted gates must be checked.',file=sys.stderr)
        return 1


if __name__ == '__main__': raise SystemExit(main())
