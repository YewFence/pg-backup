#!/bin/bash
set -euo pipefail

# 在已有 PostgreSQL 实例中重放 Barman 用户权限，修复旧模板创建出的 superuser barman。
postgres_user="${POSTGRES_USER:-postgres}"
postgres_db="${POSTGRES_DB:-postgres}"
postgres_host="${PGHOST:-127.0.0.1}"
export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"

if [ -z "$PGPASSWORD" ]; then
  echo "Error: POSTGRES_PASSWORD or PGPASSWORD is required" >&2
  exit 1
fi

for var in BARMAN_PASSWORD STREAMING_BARMAN_PASSWORD; do
  if [ -z "${!var:-}" ]; then
    echo "Error: ${var} is required" >&2
    exit 1
  fi
done

psql -v ON_ERROR_STOP=1 \
  -h "$postgres_host" \
  -U "$postgres_user" \
  -d "$postgres_db" \
  -v barman_password="$BARMAN_PASSWORD" \
  -v streaming_barman_password="$STREAMING_BARMAN_PASSWORD" <<'SQL'
\i /usr/local/share/postgres/sql/00-barman-users.sql
SQL

echo "done: repaired barman users"
