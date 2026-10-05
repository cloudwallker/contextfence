#!/bin/sh
set -eu
# Password files are generated as hex, never interpolated from arbitrary user text.
for role in migration runtime backup monitor; do
  password=$(cat "/run/secrets/db-$role")
  psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-psqlrc --set ON_ERROR_STOP=1 <<SQL
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L', 'cf_$role', '$password') \gexec
SQL
done
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-psqlrc --set ON_ERROR_STOP=1 <<SQL
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE "$POSTGRES_DB" FROM PUBLIC;
GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO cf_migration,cf_runtime,cf_backup,cf_monitor;
ALTER SCHEMA public OWNER TO cf_migration;
GRANT USAGE ON SCHEMA public TO cf_runtime,cf_backup,cf_monitor;
ALTER DEFAULT PRIVILEGES FOR ROLE cf_migration IN SCHEMA public GRANT SELECT ON TABLES TO cf_backup;
GRANT pg_monitor TO cf_monitor WITH INHERIT TRUE;
SQL
