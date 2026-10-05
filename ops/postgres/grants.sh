#!/bin/sh
set -eu
sh /ops/client.sh admin <<'SQL'
GRANT pg_monitor TO cf_monitor WITH INHERIT TRUE;
GRANT SELECT,INSERT,UPDATE ON tenant_guard,source_state TO cf_runtime;
GRANT SELECT,INSERT ON source_events,context_items,context_sources,admission_receipts TO cf_runtime;
GRANT SELECT ON flyway_schema_history TO cf_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cf_backup;
SQL
