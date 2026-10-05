#!/bin/sh
set -eu
if [ -n "${CONTEXTFENCE_DATABASE_PASSWORD_FILE:-}" ]; then
  if [ ! -r "$CONTEXTFENCE_DATABASE_PASSWORD_FILE" ]; then
    printf '%s\n' 'Database credential file is unavailable.' >&2
    exit 1
  fi
  CONTEXTFENCE_DATABASE_PASSWORD=$(cat "$CONTEXTFENCE_DATABASE_PASSWORD_FILE")
  export CONTEXTFENCE_DATABASE_PASSWORD
fi
exec java -jar /app/context-fence.jar "$@"
