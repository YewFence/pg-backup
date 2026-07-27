#!/bin/bash
set -euo pipefail

# 创建 postgres_exporter 监控用户，供 Alloy 的 PostgreSQL exporter 采集实例指标。
psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  -v postgres_exporter_password="$POSTGRES_EXPORTER_PASSWORD" \
  -v postgres_db="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE postgres_exporter WITH LOGIN PASSWORD %L', :'postgres_exporter_password')
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_catalog.pg_roles
  WHERE rolname = 'postgres_exporter'
)
\gexec

ALTER ROLE postgres_exporter WITH LOGIN PASSWORD :'postgres_exporter_password';
GRANT pg_monitor TO postgres_exporter;
GRANT CONNECT ON DATABASE :"postgres_db" TO postgres_exporter;
SQL
