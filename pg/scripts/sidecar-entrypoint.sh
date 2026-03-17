#!/bin/bash
set -e

for var in S3_BUCKET_URL BARMAN_PASSWORD; do
    if [ -z "${!var}" ]; then
        echo "Error: ${var} is not set" >&2
        exit 1
    fi
done

export PGPASSWORD="${BARMAN_PASSWORD}"

echo "=== barman-cloud sidecar ==="
echo "S3 bucket:   ${S3_BUCKET_URL}"
echo "S3 endpoint: ${S3_ENDPOINT_URL:-(default AWS)}"
echo "PG host:     ${PG_HOST:-postgres}"
echo "Server name: ${BARMAN_CLOUD_SERVER_NAME:-pg}"
echo "Compression: ${BARMAN_CLOUD_COMPRESSION:-(none)}"

CRONTAB_FILE="/etc/barman-cloud/crontab"
if [ ! -f "$CRONTAB_FILE" ]; then
    echo "Error: $CRONTAB_FILE not found" >&2
    exit 1
fi

echo ""
echo "Cron jobs:"
cat "$CRONTAB_FILE"
echo "---"

exec supercronic "$CRONTAB_FILE"
