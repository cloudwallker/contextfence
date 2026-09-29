#!/usr/bin/env bash
set -eu

if [ "$#" -gt 1 ]; then
  printf '%s\n' 'Usage: bash scripts/start.sh [project-directory]' >&2
  exit 2
fi
project_root="$(cd "${1:-$(dirname "${BASH_SOURCE[0]}")/..}" && pwd)"
if [ ! -f "$project_root/compose.yaml" ]; then
  printf '%s\n' 'Start failed: compose.yaml is missing from the project directory.' >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'Start failed: Docker with the Compose plugin is required.' >&2
  exit 1
fi
if command -v python3 >/dev/null 2>&1; then
  python_command=python3
elif command -v python >/dev/null 2>&1; then
  python_command=python
else
  printf '%s\n' 'Start failed: Python 3 is required to initialize local credentials.' >&2
  exit 1
fi
docker compose version --short
"$python_command" "$(dirname "${BASH_SOURCE[0]}")/bootstrap.py" --root "$project_root"
if docker compose --project-name contextfence --project-directory "$project_root" \
  --file "$project_root/compose.yaml" --env-file "$project_root/.env" \
  up --detach --build --wait --wait-timeout 180; then
  printf '%s\n' 'ContextFence is healthy. Use scripts/stop.sh to stop this project while retaining its database volume.'
else
  command_exit=$?
  printf '%s\n' 'ContextFence did not start successfully. Docker errors are preserved above; check the engine, build, or configured ports.' >&2
  exit "$command_exit"
fi
