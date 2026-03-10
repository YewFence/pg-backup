#!/bin/bash
set -e

# 配置 pgpass
/usr/local/bin/setup-pgpass.sh

# 安装 crontab（如果文件存在）
CRONTAB_FILE="/etc/barman.d/barman.crontab"
if [ -f "$CRONTAB_FILE" ]; then
    echo "Installing crontab from $CRONTAB_FILE"
    # 重定向 cron 输出到 stdout/stderr，让 docker logs 可以看到
    sed 's|$| >> /proc/1/fd/1 2>> /proc/1/fd/2|' "$CRONTAB_FILE" | crontab -
    echo "Installed cron jobs:"
    crontab -l
else
    echo "Warning: $CRONTAB_FILE not found, no cron jobs installed"
fi

# 启动 cron 守护进程（前台运行）
echo "Starting cron daemon..."
exec cron -f -L 2
