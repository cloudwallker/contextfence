#!/usr/bin/env bash
set -eu
if [ "$#" -gt 1 ]; then printf '%s\n' 'Usage: bash scripts/start.sh [project-directory]' >&2; exit 2; fi
project_root="$(cd "${1:-$(dirname "${BASH_SOURCE[0]}")/..}" && pwd)"
if command -v python3 >/dev/null 2>&1; then python_command=python3
elif command -v python >/dev/null 2>&1; then python_command=python
else printf '%s\n' 'Start failed: Python 3 is required.' >&2; exit 1; fi
exec "$python_command" "$(dirname "${BASH_SOURCE[0]}")/start.py" --root "$project_root"
