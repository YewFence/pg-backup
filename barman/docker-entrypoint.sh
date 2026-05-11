#!/bin/bash
set -euo pipefail

is_uint() {
    [[ "$1" =~ ^[0-9]+$ ]]
}

is_true() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

configure_barman_user() {
    local desired_uid="${BARMAN_UID:-999}"
    local desired_gid="${BARMAN_GID:-999}"
    local current_uid
    local current_gid
    local existing_user
    local existing_group

    if ! is_uint "$desired_uid"; then
        echo "BARMAN_UID must be a numeric uid, got: $desired_uid" >&2
        exit 1
    fi

    if ! is_uint "$desired_gid"; then
        echo "BARMAN_GID must be a numeric gid, got: $desired_gid" >&2
        exit 1
    fi

    existing_user="$(getent passwd "$desired_uid" | cut -d: -f1 || true)"
    if [ -n "$existing_user" ] && [ "$existing_user" != "barman" ]; then
        echo "BARMAN_UID $desired_uid is already used by user $existing_user" >&2
        exit 1
    fi

    existing_group="$(getent group "$desired_gid" | cut -d: -f1 || true)"
    if [ -n "$existing_group" ] && [ "$existing_group" != "barman" ]; then
        echo "BARMAN_GID $desired_gid is already used by group $existing_group" >&2
        exit 1
    fi

    current_uid="$(id -u barman)"
    current_gid="$(id -g barman)"

    if [ "$current_gid" != "$desired_gid" ]; then
        groupmod --gid "$desired_gid" barman
    fi

    current_gid="$(id -g barman)"
    if [ "$current_uid" != "$desired_uid" ] || [ "$current_gid" != "$desired_gid" ]; then
        usermod --uid "$desired_uid" --gid "$desired_gid" --home /var/lib/barman barman
    fi
}

prepare_writable_paths() {
    mkdir -p /var/lib/barman /recover /var/log/barman
    touch /var/log/barman/barman.log

    if is_true "${BARMAN_FIX_OWNERSHIP:-true}"; then
        chown -R barman:barman /var/lib/barman /recover /var/log/barman
    fi
}

if [ "$(id -u)" = "0" ]; then
    configure_barman_user
    prepare_writable_paths
    exec gosu barman "$@"
fi

exec "$@"
