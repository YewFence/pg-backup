#!/usr/bin/env bash
set -euo pipefail

role="${1:?usage: check-storage-paths.sh postgres|barman}"
if [ "$role" != postgres ] && [ "$role" != barman ]; then
    echo "错误：未知存储检查角色：$role" >&2
    exit 2
fi

production_data_path="${POSTGRES_DATA_PATH:-/srv/native-docker/postgres}"
restore_root="${POSTGRES_RESTORE_ROOT:-/srv/native-docker/postgres-restore}"
restore_data_path="$restore_root/data"

for path in "$production_data_path" "$restore_root"; do
    case "$path" in
        /*) ;;
        *)
            echo "错误：持久数据路径必须是绝对路径：$path" >&2
            exit 1
            ;;
    esac
done

production_canonical="$(realpath -m "$production_data_path")"
restore_canonical="$(realpath -m "$restore_root")"
case "$production_canonical/" in
    "$restore_canonical/"*)
        echo "错误：生产数据路径与恢复根目录必须隔离，不能相等或互相包含。" >&2
        exit 1
        ;;
esac
case "$restore_canonical/" in
    "$production_canonical/"*)
        echo "错误：生产数据路径与恢复根目录必须隔离，不能相等或互相包含。" >&2
        exit 1
        ;;
esac

if [ "$role" = postgres ]; then
    required_paths=("$production_data_path")
else
    required_paths=("$restore_root" "$restore_data_path")
fi

missing=false
for path in "${required_paths[@]}"; do
    if [ -L "$path" ]; then
        echo "错误：持久数据路径不能是符号链接：$path" >&2
        exit 1
    fi
    if [ ! -d "$path" ]; then
        missing=true
    fi
done

if [ "$missing" = true ]; then
    echo "错误：稳定宿主机目录尚未完成 provisioning。请显式执行：" >&2
    echo >&2
    if [ "$role" = postgres ]; then
        printf 'sudo install -d %q\n' "$(dirname "$production_data_path")" >&2
        printf 'sudo install -d %q\n' "$production_data_path" >&2
    else
        printf 'sudo install -d %q\n' "$(dirname "$restore_root")" >&2
        printf 'sudo install -d -m 0711 -o %q -g %q %q\n' \
            "$(id -u)" "$(id -g)" "$restore_root" >&2
        printf 'sudo install -d -m 0700 -o %q -g %q %q\n' \
            "$(id -u)" "$(id -g)" "$restore_data_path" >&2
    fi
    echo >&2
    echo "完成后重新运行当前安装 task。" >&2
    exit 1
fi

if [ "$role" = postgres ]; then
    exit 0
fi

if [ "$(stat -c %u "$restore_root")" != "$(id -u)" ] || [ "$(stat -c %g "$restore_root")" != "$(id -g)" ]; then
    echo "错误：恢复根目录必须归当前宿主机用户所有：$restore_root" >&2
    exit 1
fi
if [ "$(stat -c %a "$restore_root")" != 711 ]; then
    echo "错误：恢复根目录 mode 必须精确为 0711：$restore_root" >&2
    exit 1
fi
if [ "$(stat -c %a "$restore_data_path")" != 700 ]; then
    echo "错误：恢复数据目录 mode 必须精确为 0700：$restore_data_path" >&2
    exit 1
fi
