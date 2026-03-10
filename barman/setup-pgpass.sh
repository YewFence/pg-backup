#!/bin/sh
set -eu

PGPASS_FILE="/var/lib/barman/.pgpass"
PG_HOST="${PG_HOST:-wsl-yew-branch}"

# Check required variables
if [ -z "${BARMAN_PASSWORD:-}" ] || [ -z "${STREAMING_BARMAN_PASSWORD:-}" ]; then
    echo "Error: BARMAN_PASSWORD and STREAMING_BARMAN_PASSWORD must be set"
    exit 1
fi

cat > "$PGPASS_FILE" << EOF
${PG_HOST}:5432:postgres:barman:${BARMAN_PASSWORD}
${PG_HOST}:5432:postgres:streaming_barman:${STREAMING_BARMAN_PASSWORD}
EOF

chmod 600 "$PGPASS_FILE"

echo "pgpass configured for host: ${PG_HOST}"
