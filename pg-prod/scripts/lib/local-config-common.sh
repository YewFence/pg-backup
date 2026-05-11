#!/usr/bin/env bash
# shellcheck shell=bash

script_dir_from_common() {
    cd "$(dirname "${BASH_SOURCE[1]}")" && pwd
}

default_instance_dir_from_script() {
    local script_dir="$1"
    cd "$script_dir/.." && pwd
}

absolute_existing_dir() {
    local dir="$1"

    if [ ! -d "$dir" ]; then
        echo "错误，目录不存在 $dir"
        exit 1
    fi

    cd "$dir" && pwd
}

require_pg_prod_dir() {
    local dir="$1"

    for file in compose.yml pg_hba.conf; do
        if [ ! -f "$dir/$file" ]; then
            echo "错误，目标目录缺少 $file"
            exit 1
        fi
    done
}

ensure_conf_dir() {
    local dir="$1"

    mkdir -p "$dir/conf.d"
}

setting_value_from_file() {
    local file="$1"
    local name="$2"

    [ -f "$file" ] || return 0
    sed -nE "s/^[[:space:]]*$name[[:space:]]*=[[:space:]]*([^[:space:]#]+).*/\1/p" "$file" | tail -n 1
}

preview_file_change() {
    local target="$1"
    local candidate="$2"

    echo ""
    echo "即将写入的配置差异"
    if command -v diff >/dev/null 2>&1; then
        if [ -f "$target" ]; then
            diff -u "$target" "$candidate" || true
        else
            diff -u /dev/null "$candidate" || true
        fi
    else
        cat "$candidate"
    fi
}

confirm_or_exit() {
    local apply="$1"

    if [ "$apply" = "true" ]; then
        return 0
    fi

    echo ""
    if [ -t 0 ]; then
        printf "输入 apply 写入配置，输入其他内容退出 "
        read -r confirm
        if [ "$confirm" = "apply" ]; then
            return 0
        fi
    else
        echo "尚未写入，非交互环境请追加 --apply"
    fi

    echo "已退出，未修改任何文件"
    exit 0
}

apply_conf_file() {
    local target="$1"
    local candidate="$2"
    local backup_stem="$3"
    local backup_dir
    local timestamp

    backup_dir="$(dirname "$target")/.backup"
    timestamp="$(date +%Y%m%d-%H%M%S)"

    if [ -f "$target" ]; then
        mkdir -p "$backup_dir"
        cp -p "$target" "$backup_dir/$backup_stem.$timestamp.bak"
        echo "旧配置已备份到 $backup_dir/$backup_stem.$timestamp.bak"
    fi

    chmod 0644 "$candidate"
    mv "$candidate" "$target"
    echo "配置已写入 $target"
}

compose_postgres_container_id() {
    local dir="$1"

    (
        cd "$dir"
        docker compose ps -q postgres 2>/dev/null || true
    )
}

sync_running_postgres_conf() {
    local dir="$1"

    if ! command -v docker >/dev/null 2>&1; then
        echo "未检测到 docker，配置会在 PostgreSQL 下次初始化、重载或重启后生效"
        return 1
    fi

    local container_id
    container_id="$(compose_postgres_container_id "$dir")"
    if [ -z "$container_id" ]; then
        echo "未检测到运行中的 postgres 容器，配置会在 PostgreSQL 下次初始化、重载或重启后生效"
        return 1
    fi

    if ! docker inspect -f '{{.State.Running}}' "$container_id" 2>/dev/null | grep -qx true; then
        echo "postgres 容器当前未运行，配置会在 PostgreSQL 下次初始化、重载或重启后生效"
        return 1
    fi

    echo ""
    echo "正在同步 conf.d 配置到运行中的 PostgreSQL 数据目录"
    (
        cd "$dir"
        docker compose exec -T --user root postgres bash -euo pipefail -c '
            install -d -m 700 -o postgres -g postgres "$PGDATA/conf.d"
            shopt -s nullglob
            for file in /config/conf.d/*.conf; do
                install -m 600 -o postgres -g postgres "$file" "$PGDATA/conf.d/$(basename "$file")"
            done
            if ! grep -Eq "^[[:space:]]*include_dir[[:space:]]*=[[:space:]]*'\''conf\\.d'\''[[:space:]]*(#.*)?$" "$PGDATA/postgresql.conf"; then
                {
                    printf "\n# pg-prod local configuration\n"
                    printf "include_dir = '\''conf.d'\''\n"
                } >> "$PGDATA/postgresql.conf"
                chown postgres:postgres "$PGDATA/postgresql.conf"
            fi
        '
    )
}

reload_postgres_config() {
    local dir="$1"
    shift

    if ! sync_running_postgres_conf "$dir"; then
        return 0
    fi

    (
        cd "$dir"
        echo ""
        echo "正在请求 PostgreSQL 重新加载配置"
        docker compose exec -T postgres gosu postgres psql -d postgres -v ON_ERROR_STOP=1 -c "SELECT pg_reload_conf();" "$@"
    )
}

print_common_usage() {
    echo "通用参数"
    echo "  --apply       写入配置文件"
    echo "  --no-reload   写入后不自动重载 PostgreSQL 配置"
    echo "  --dir <dir>   pg-prod 实例目录，默认是脚本所在 pg-prod 目录"
}
