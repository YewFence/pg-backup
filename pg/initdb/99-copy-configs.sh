#!/bin/bash
set -e

# 这个脚本在 initdb 完成后、postgres 启动前执行
# 复制自定义配置文件到 pgdata
echo "Copying custom config files..."
cp /etc/postgresql/postgresql.conf "$PGDATA/postgresql.conf"
cp /etc/postgresql/pg_hba.conf "$PGDATA/pg_hba.conf"

echo "Config files copied successfully"
