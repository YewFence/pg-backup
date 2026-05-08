#!/bin/sh
set -eu

CONFIG_DIR="${BARMAN_CONFIG_DIR:-/etc/barman.d}"

discover_server_names() {
    for conf_file in "$CONFIG_DIR"/*.conf; do
        [ -f "$conf_file" ] || continue
        sed -n 's/^[[:space:]]*\[\([A-Za-z0-9_.-][A-Za-z0-9_.-]*\)\][[:space:]]*$/\1/p' "$conf_file"
    done | awk '$0 != "barman" && $0 != "global" && !seen[$0]++'
}

if [ "$#" -lt 1 ]; then
    echo "Usage: barman-for-each-server <barman-command> [args...]"
    exit 2
fi

command_name="$1"
shift
server_names="$(discover_server_names)"

if [ -z "$server_names" ]; then
    echo "Error: no Barman server names found"
    exit 1
fi

status=0

for server_name in $server_names; do
    echo "Running: barman $command_name $server_name $*"
    if ! barman "$command_name" "$server_name" "$@"; then
        status=1
    fi
done

exit "$status"
