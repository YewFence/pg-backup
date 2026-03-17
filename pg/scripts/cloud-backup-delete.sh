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

RETENTION="${BARMAN_CLOUD_RETENTION:-RECOVERY WINDOW OF 7 DAYS}"
MIN_REDUNDANCY="${BARMAN_CLOUD_MIN_REDUNDANCY:-1}"

ARGS=(
    --cloud-provider aws-s3
    --retention-policy "${RETENTION}"
    --minimum-redundancy "${MIN_REDUNDANCY}"
)

if [ -n "${S3_ENDPOINT_URL}" ]; then
    ARGS+=(--endpoint-url "${S3_ENDPOINT_URL}")
fi

echo "[$(date -Iseconds)] Cleaning up old backups (retention: ${RETENTION}, min: ${MIN_REDUNDANCY})..."

barman-cloud-backup-delete "${ARGS[@]}" \
    "${S3_BUCKET_URL}" \
    "${BARMAN_CLOUD_SERVER_NAME:-pg}"

echo "[$(date -Iseconds)] Cleanup completed."
