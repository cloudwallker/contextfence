#!/bin/sh
set -eu
if [ -n "${GF_SECURITY_ADMIN_PASSWORD__FILE:-}" ]; then
    GF_SECURITY_ADMIN_PASSWORD=$(cat "$GF_SECURITY_ADMIN_PASSWORD__FILE")
    case "$GF_SECURITY_ADMIN_PASSWORD" in *[!0-9a-f]*|'') exit 2 ;; esac
    [ "${#GF_SECURITY_ADMIN_PASSWORD}" -eq 64 ] || exit 2
    export GF_SECURITY_ADMIN_PASSWORD
    unset GF_SECURITY_ADMIN_PASSWORD__FILE
fi
exec /usr/share/grafana/bin/grafana server --homepath "$GF_PATHS_HOME" --config "$GF_PATHS_CONFIG"
