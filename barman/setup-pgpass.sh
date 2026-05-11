#!/bin/sh
set -eu

PGPASS_FILE="/var/lib/barman/.pgpass"
PGPASS_SOURCE="/etc/barman.d/pgpass"

if [ ! -f "$PGPASS_SOURCE" ]; then
    echo "Error: $PGPASS_SOURCE not found. Create config/pgpass from config/pgpass.example."
    exit 1
fi

cp "$PGPASS_SOURCE" "$PGPASS_FILE"
chmod 600 "$PGPASS_FILE"
echo "pgpass configured from $PGPASS_SOURCE"

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
