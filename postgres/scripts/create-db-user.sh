#!/bin/bash
set -euo pipefail

# 在 PostgreSQL 容器内运行，交互式创建数据库和业务用户。
postgres_user="${POSTGRES_USER:-postgres}"
postgres_db="${POSTGRES_DB:-postgres}"
postgres_host="${PGHOST:-127.0.0.1}"
export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"

if [ -z "$PGPASSWORD" ]; then
  echo "Error: POSTGRES_PASSWORD or PGPASSWORD is required" >&2
  exit 1
fi

if [ "$#" -gt 0 ] && [ "$#" -ne 3 ]; then
  echo "Usage: create-db-user [database user password]" >&2
  exit 2
fi

if [ "$#" -eq 3 ]; then
  db="$1"
  app_user="$2"
  app_password="$3"
else
  read -r -p "db: " db
  read -r -p "user: " app_user
  read -r -s -p "pass: " app_password
  echo
fi

for value_name in db app_user app_password; do
  if [ -z "${!value_name}" ]; then
    echo "Error: ${value_name} cannot be empty" >&2
    exit 1
  fi
done

psql -v ON_ERROR_STOP=1 \
  -h "$postgres_host" \
  -U "$postgres_user" \
  -d "$postgres_db" \
  -v app_user="$app_user" \
  -v app_password="$app_password" \
  -v db="$db" <<'SQL'
CREATE USER :"app_user" WITH PASSWORD :'app_password';
CREATE DATABASE :"db" OWNER :"app_user";
GRANT ALL PRIVILEGES ON DATABASE :"db" TO :"app_user";
SQL

psql -v ON_ERROR_STOP=1 \
  -h "$postgres_host" \
  -U "$postgres_user" \
  -d "$db" \
  -v app_user="$app_user" <<'SQL'
GRANT ALL ON SCHEMA public TO :"app_user";
SQL

echo
echo "done: db=${db} user=${app_user}"
