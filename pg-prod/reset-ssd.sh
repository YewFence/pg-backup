#!/usr/bin/env bash
# 将 SSD 优化参数恢复为 PostgreSQL 默认值（适用于 HDD 场景）
set -euo pipefail

CONF="$(dirname "$0")/postgresql.conf"

if [ ! -f "$CONF" ]; then
  echo "错误: 找不到 $CONF"
  exit 1
fi

sed -i "s/^random_page_cost = .*/random_page_cost = 4/" "$CONF"
sed -i "s/^effective_io_concurrency = .*/effective_io_concurrency = 1/" "$CONF"

echo "已恢复 HDD 默认值:"
echo "  random_page_cost         = 4"
echo "  effective_io_concurrency = 1"
echo ""
echo "重启 PostgreSQL 使配置生效: docker compose restart postgres"
