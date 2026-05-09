#!/bin/bash
set -euo pipefail

source_hba=/config/pg_hba.conf
source_conf_dir=/config/conf.d
target_conf_dir="$PGDATA/conf.d"

if [ ! -r "$source_hba" ]; then
  echo "错误，找不到 PostgreSQL 访问控制配置 $source_hba"
  exit 1
fi

if [ ! -d "$source_conf_dir" ]; then
  echo "错误，找不到 PostgreSQL 覆盖配置目录 $source_conf_dir"
  exit 1
fi

shopt -s nullglob
conf_files=("$source_conf_dir"/*.conf)

if [ "${#conf_files[@]}" -eq 0 ]; then
  echo "错误，$source_conf_dir 中没有 .conf 配置文件"
  exit 1
fi

install -d -m 700 "$target_conf_dir"
install -m 600 "$source_hba" "$PGDATA/pg_hba.conf"

for file in "${conf_files[@]}"; do
  install -m 600 "$file" "$target_conf_dir/$(basename "$file")"
done

if ! grep -Eq "^[[:space:]]*include_dir[[:space:]]*=[[:space:]]*'conf\\.d'[[:space:]]*(#.*)?$" "$PGDATA/postgresql.conf"; then
  {
    printf "\n# pg-prod local configuration\n"
    printf "include_dir = 'conf.d'\n"
  } >> "$PGDATA/postgresql.conf"
fi

echo "PostgreSQL 配置已写入 $PGDATA，使用默认配置文件位置和 conf.d 覆盖配置"
