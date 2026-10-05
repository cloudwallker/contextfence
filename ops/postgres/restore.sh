#!/bin/sh
set -eu
archive=${1:?custom-format archive required}
case "$archive" in /tmp/contextfence-recovery.dump) ;; *) exit 2 ;; esac
PGPASSWORD=$(cat /run/secrets/db-admin)
export PGPASSWORD
# Fresh service/volume only. Never clean a database or restore cluster identities/ACLs.
pg_restore --list "$archive" >/dev/null
pg_restore --exit-on-error --single-transaction --no-owner --no-privileges --role=cf_migration --username cf_admin --dbname "$POSTGRES_DB" "$archive"
rm -f "$archive"
