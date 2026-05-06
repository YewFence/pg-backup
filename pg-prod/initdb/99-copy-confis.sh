#!/bin/bash
set -euo pipefail

for file in /etc/postgresql/postgresql.conf /etc/postgresql/pg_hba.conf; do
  if [ ! -r "$file" ]; then
    echo "错误，找不到 PostgreSQL 配置文件 $file"
    exit 1
  fi
done

echo "PostgreSQL 配置文件已挂载到 /etc/postgresql，跳过复制到 PGDATA"
