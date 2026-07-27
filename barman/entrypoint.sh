#!/bin/bash
set -e

gosu barman /usr/local/bin/setup-pgpass.sh

gosu barman python3 /usr/local/bin/health-check.py &

# 检查 crontab 文件
CRONTAB_FILE="/etc/barman.d/barman.crontab"
if [ ! -f "$CRONTAB_FILE" ]; then
    echo "Warning: $CRONTAB_FILE not found, no cron jobs will run"
    echo "Keeping container alive..."
    wait
    exit 0
fi

CRON_ENV_FILE="/var/lib/barman/cron.env"
CRONTAB_RUNTIME="/var/lib/barman/barman.crontab"

umask 077
while IFS='=' read -r name value; do
    case "$name" in
        AWS_*) printf 'export %s=%q\n' "$name" "$value" ;;
    esac
done < <(env) > "$CRON_ENV_FILE"
chown barman:barman "$CRON_ENV_FILE"

{
    printf 'BASH_ENV=%s\n' "$CRON_ENV_FILE"
    cat "$CRONTAB_FILE"
} > "$CRONTAB_RUNTIME"
chown barman:barman "$CRONTAB_RUNTIME"
crontab -u barman "$CRONTAB_RUNTIME"

echo "Starting cron with crontab: $CRONTAB_FILE"
echo "Loaded cron jobs:"
cat "$CRONTAB_FILE"
echo "---"

exec cron -f
