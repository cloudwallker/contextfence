#!/usr/bin/env bash
set -eu

if [ "$#" -gt 1 ]; then
  printf '%s\n' 'Usage: bash scripts/stop.sh [project-directory]' >&2
  exit 2
fi
project_root="$(cd "${1:-$(dirname "${BASH_SOURCE[0]}")/..}" && pwd)"
if [ ! -f "$project_root/compose.yaml" ] || [ ! -f "$project_root/.env" ]; then
  printf '%s\n' 'Stop failed: the project compose.yaml or .env is missing. No configuration was generated or replaced.' >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'Stop failed: Docker with the Compose plugin is required.' >&2
  exit 1
fi
if docker compose --project-name contextfence --project-directory "$project_root" \
  --file "$project_root/compose.yaml" --env-file "$project_root/.env" down; then
  printf '%s\n' 'ContextFence stopped. Database volumes and local credentials were retained.'
else
  command_exit=$?
  printf '%s\n' 'Stopping ContextFence failed. Docker errors are preserved above.' >&2
  exit "$command_exit"
fi
