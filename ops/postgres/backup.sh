#!/bin/sh
set -eu
snapshot=${1:?exported snapshot required}
destination=${2:?archive path required}
case "$snapshot" in *[!0-9a-fA-F-]*|'') exit 2 ;; esac
case "$destination" in /tmp/contextfence-*.dump) ;; *) exit 2 ;; esac
PGPASSWORD=$(cat /run/secrets/db-backup)
export PGPASSWORD
exec pg_dump --host "${PGHOST:-127.0.0.1}" --username cf_backup --dbname "$POSTGRES_DB" \
  --format custom --no-owner --no-acl --snapshot "$snapshot" --file "$destination"
