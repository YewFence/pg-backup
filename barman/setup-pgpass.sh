#!/bin/sh
set -eu

PGPASS_FILE="/var/lib/barman/.pgpass"
PGPASS_SOURCE="/etc/barman.d/pgpass"
PG_HOST="${PG_HOST:-pg-host}"

if [ -f "$PGPASS_SOURCE" ]; then
    cp "$PGPASS_SOURCE" "$PGPASS_FILE"
    echo "pgpass configured from $PGPASS_SOURCE"
else
    if [ -z "${BARMAN_PASSWORD:-}" ] || [ -z "${STREAMING_BARMAN_PASSWORD:-}" ]; then
        echo "Error: BARMAN_PASSWORD and STREAMING_BARMAN_PASSWORD must be set when $PGPASS_SOURCE does not exist"
        exit 1
    fi

    cat > "$PGPASS_FILE" << EOF
${PG_HOST}:5432:postgres:barman:${BARMAN_PASSWORD}
${PG_HOST}:5432:*:streaming_barman:${STREAMING_BARMAN_PASSWORD}
EOF
    echo "pgpass configured for host: ${PG_HOST}"
fi

chmod 600 "$PGPASS_FILE"

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
