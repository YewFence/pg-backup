#!/bin/bash
set -e

# 恢复模式下跳过（用于恢复时预下载 WAL）
if [ "${RECOVERY_MODE}" = "true" ]; then
    exit 0
fi

# S3 未配置则跳过
if [ -z "${S3_BUCKET_URL}" ]; then
    exit 0
fi

ARCHIVE_DIR="/archive"
SERVER_NAME="${BARMAN_CLOUD_SERVER_NAME:-pg}"

# 没有 WAL 文件则退出
shopt -s nullglob
wal_files=("${ARCHIVE_DIR}"/*)
shopt -u nullglob

if [ ${#wal_files[@]} -eq 0 ]; then
    exit 0
fi

ARGS=(--cloud-provider aws-s3)

if [ -n "${S3_ENDPOINT_URL}" ]; then
    ARGS+=(--endpoint-url "${S3_ENDPOINT_URL}")
fi

if [ -n "${BARMAN_CLOUD_COMPRESSION}" ]; then
    ARGS+=("--${BARMAN_CLOUD_COMPRESSION}")
fi

for wal_file in "${wal_files[@]}"; do
    [ -f "$wal_file" ] || continue
    echo "[$(date -Iseconds)] Uploading $(basename "$wal_file")..."
    if barman-cloud-wal-archive "${ARGS[@]}" "${S3_BUCKET_URL}" "${SERVER_NAME}" "$wal_file"; then
        rm -f "$wal_file"
        echo "[$(date -Iseconds)] Done: $(basename "$wal_file")"
    else
        echo "[$(date -Iseconds)] Failed: $(basename "$wal_file")" >&2
    fi
done
