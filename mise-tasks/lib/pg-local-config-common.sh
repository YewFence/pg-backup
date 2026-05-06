#!/usr/bin/env bash
# shellcheck shell=bash

repo_root_from_task() {
    cd "$(dirname "${BASH_SOURCE[1]}")/.." && pwd
}

default_pg_instance_dir() {
    local root_dir="$1"
    printf "%s\n" "$root_dir/../pg"
}

absolute_existing_dir() {
    local dir="$1"

    if [ ! -d "$dir" ]; then
        echo "错误，目录不存在 $dir"
        exit 1
    fi

    cd "$dir" && pwd
}

require_pg_instance() {
    local dir="$1"

    if [ -f "$dir/.pg-prod-template" ] || [ ! -f "$dir/.pg-instance" ]; then
        echo "错误，目标目录不是 pg-install 生成的生产实例目录 $dir"
        echo "请通过 PG_INSTANCE_DIR 或 --dir 指向实例目录，不要修改 pg-prod 模板目录"
        exit 1
    fi

    for file in compose.yml postgresql.conf pg_hba.conf; do
        if [ ! -f "$dir/$file" ]; then
            echo "错误，实例目录缺少 $file"
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

reload_postgres_config() {
    local dir="$1"
    shift

    if ! command -v docker >/dev/null 2>&1; then
        echo "未检测到 docker，配置会在 PostgreSQL 下次重载或重启后生效"
        return 0
    fi

    (
        cd "$dir"

        local container_id
        container_id="$(docker compose ps -q postgres 2>/dev/null || true)"
        if [ -z "$container_id" ]; then
            echo "未检测到运行中的 postgres 容器，启动或重启后生效"
            return 0
        fi

        if ! docker inspect -f '{{.State.Running}}' "$container_id" 2>/dev/null | grep -qx true; then
            echo "postgres 容器当前未运行，启动或重启后生效"
            return 0
        fi

        echo ""
        echo "正在请求 PostgreSQL 重新加载配置"
        docker compose exec -T postgres gosu postgres psql -d postgres -v ON_ERROR_STOP=1 -c "SELECT pg_reload_conf();" "$@"
    )
}
