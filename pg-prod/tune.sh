#!/usr/bin/env bash
set -euo pipefail

CONF="$(dirname "$0")/postgresql.conf"

if [ ! -f "$CONF" ]; then
  echo "错误: 找不到 $CONF"
  exit 1
fi

echo "选择服务器内存大小:"
echo "  1) 2 GB"
echo "  2) 4 GB"
echo "  3) 8 GB"
echo "  4) 16 GB"
printf "请输入 [1-4]: "
read -r choice

case "$choice" in
  1)
    shared_buffers="512MB"
    effective_cache_size="1536MB"
    work_mem="5MB"
    maintenance_work_mem="128MB"
    label="2GB"
    ;;
  2)
    shared_buffers="1GB"
    effective_cache_size="3GB"
    work_mem="10MB"
    maintenance_work_mem="256MB"
    label="4GB"
    ;;
  3)
    shared_buffers="2GB"
    effective_cache_size="6GB"
    work_mem="20MB"
    maintenance_work_mem="512MB"
    label="8GB"
    ;;
  4)
    shared_buffers="4GB"
    effective_cache_size="12GB"
    work_mem="40MB"
    maintenance_work_mem="1GB"
    label="16GB"
    ;;
  *)
    echo "无效选择"
    exit 1
    ;;
esac

sed -i "s/^shared_buffers = .*/shared_buffers = $shared_buffers/" "$CONF"
sed -i "s/^effective_cache_size = .*/effective_cache_size = $effective_cache_size/" "$CONF"
sed -i "s/^work_mem = .*/work_mem = $work_mem/" "$CONF"
sed -i "s/^maintenance_work_mem = .*/maintenance_work_mem = $maintenance_work_mem/" "$CONF"

echo "已切换到 ${label} 档位:"
echo "  shared_buffers      = $shared_buffers"
echo "  effective_cache_size = $effective_cache_size"
echo "  work_mem            = $work_mem"
echo "  maintenance_work_mem = $maintenance_work_mem"
echo ""
echo "重启 PostgreSQL 使配置生效: docker compose restart postgres"
