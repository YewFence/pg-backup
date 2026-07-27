#!/bin/bash
set -euo pipefail

# 创建 barman 协调用户和 streaming_barman 复制用户，供 Barman 基础备份、监控和外部流复制归档使用。
psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  -v barman_password="$BARMAN_PASSWORD" \
  -v streaming_barman_password="$STREAMING_BARMAN_PASSWORD" \
  -f /usr/local/share/postgres/sql/00-barman-users.sql
