#!/bin/sh
set -eu
role=${1:?database role required}
case "$role" in admin|migration|runtime|backup|monitor) ;; *) exit 2 ;; esac
PGPASSWORD=$(cat "/run/secrets/db-$role")
export PGPASSWORD
exec psql --no-psqlrc --set ON_ERROR_STOP=1 --quiet --tuples-only --no-align --host "${PGHOST:-127.0.0.1}" --username "cf_$role" --dbname "${2:-$POSTGRES_DB}"
