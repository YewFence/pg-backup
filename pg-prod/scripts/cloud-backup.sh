#!/bin/bash
set -e

# 恢复模式下跳过（用于恢复时预下载 WAL）
if [ "${RECOVERY_MODE}" = "true" ]; then
    exit 0
fi

if [ -z "${S3_BUCKET_URL}" ]; then
    echo "Error: S3_BUCKET_URL is not set" >&2
    exit 1
fi

export PGPASSWORD="${BARMAN_PASSWORD}"

ARGS=(
    --cloud-provider aws-s3
    -h "${PG_HOST:-postgres}"
    -U "${PG_USER:-barman}"
    --immediate-checkpoint
)

if [ -n "${S3_ENDPOINT_URL}" ]; then
    ARGS+=(--endpoint-url "${S3_ENDPOINT_URL}")
fi

if [ -n "${BARMAN_CLOUD_COMPRESSION}" ]; then
    ARGS+=("--${BARMAN_CLOUD_COMPRESSION}")
fi

echo "[$(date -Iseconds)] Starting cloud backup..."

barman-cloud-backup "${ARGS[@]}" \
    "${S3_BUCKET_URL}" \
    "${BARMAN_CLOUD_SERVER_NAME:-pg}"

echo "[$(date -Iseconds)] Cloud backup completed."
