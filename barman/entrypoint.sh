#!/bin/bash
set -e

# 配置 pgpass
/usr/local/bin/setup-pgpass.sh

# 启动 health check HTTP server（后台运行）
python3 /usr/local/bin/health-check.py &

# 检查 crontab 文件
CRONTAB_FILE="/etc/barman.d/barman.crontab"
if [ ! -f "$CRONTAB_FILE" ]; then
    echo "Warning: $CRONTAB_FILE not found, no cron jobs will run"
    echo "Keeping container alive..."
    wait
    exit 0
fi

echo "Starting supercronic with crontab: $CRONTAB_FILE"
echo "Loaded cron jobs:"
cat "$CRONTAB_FILE"
echo "---"

# 启动 supercronic（前台运行，不使用 exec）
supercronic "$CRONTAB_FILE"
