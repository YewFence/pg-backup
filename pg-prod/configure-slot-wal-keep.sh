#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$DIR/postgresql.conf"
PGDATA_CONF="$DIR/pgdata/postgresql.conf"
DEFAULT_SIZE="5GB"

if [ ! -f "$CONF" ]; then
  echo "错误: 找不到 $CONF"
  exit 1
fi

set_slot_wal_keep_size() {
  local file="$1"
  local size="$2"

  if grep -Eq '^[[:space:]]*max_slot_wal_keep_size[[:space:]]*=' "$file"; then
    sed -i -E "s|^[[:space:]]*max_slot_wal_keep_size[[:space:]]*=.*|max_slot_wal_keep_size = $size    # 可用 configure-slot-wal-keep.sh 调整|" "$file"
  else
    cat >> "$file" <<EOF

# -----------------------------------------------------------------------------
# 复制槽 WAL 保留上限
# -----------------------------------------------------------------------------
max_slot_wal_keep_size = $size    # 可用 configure-slot-wal-keep.sh 调整
EOF
  fi
}

current="$(
  sed -nE 's/^[[:space:]]*max_slot_wal_keep_size[[:space:]]*=[[:space:]]*([^[:space:]#]+).*/\1/p' "$CONF" |
    tail -n 1
)"

if [ -z "$current" ]; then
  current="$DEFAULT_SIZE"
fi

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  echo "用法: $0 [大小]"
  echo ""
  echo "示例:"
  echo "  $0"
  echo "  $0 5GB"
  echo "  $0 2048MB"
  echo "  $0 -1"
  exit 0
fi

if [ "$#" -gt 1 ]; then
  echo "错误: 参数过多"
  echo "用法: $0 [大小]"
  exit 1
fi

if [ "$#" -eq 1 ]; then
  input="$1"
else
  echo "=== PostgreSQL replication slot WAL 保留上限配置 ==="
  echo ""
  echo "当前 max_slot_wal_keep_size = $current"
  echo ""
  echo "这个值用于限制复制槽最多滞留多少 WAL，默认建议 $DEFAULT_SIZE。"
  echo "留空使用 $DEFAULT_SIZE，输入 -1 表示不限制，不建议生产环境长期使用。"
  echo ""
  printf "请输入新的保留上限 [默认: %s]: " "$DEFAULT_SIZE"
  read -r input
fi

size="${input:-$DEFAULT_SIZE}"

if [[ ! "$size" =~ ^-1$ && ! "$size" =~ ^[0-9]+([kKmMgGtT][bB])?$ ]]; then
  echo "错误: 无效大小 '$size'，示例: 512MB、5GB、20GB、-1"
  exit 1
fi

set_slot_wal_keep_size "$CONF" "$size"

if [ -f "$PGDATA_CONF" ]; then
  set_slot_wal_keep_size "$PGDATA_CONF" "$size"
fi

echo ""
echo "已更新 max_slot_wal_keep_size = $size"
if [ -f "$PGDATA_CONF" ]; then
  echo "已同步更新 pgdata/postgresql.conf"
fi
echo "重启 PostgreSQL 使配置生效: docker compose restart postgres"
