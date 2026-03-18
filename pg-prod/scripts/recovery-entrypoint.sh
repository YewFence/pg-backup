#!/bin/bash
set -e

# 这个 entrypoint 在 PostgreSQL 启动前自动配置 restore_command
# 如果 PGDATA 中已有 recovery.signal，说明需要恢复

PGDATA="${PGDATA:-/var/lib/postgresql/data}"

# 如果存在 recovery.signal，自动配置 restore_command
if [ -f "$PGDATA/recovery.signal" ]; then
    echo "[recovery-entrypoint] 检测到 recovery.signal，配置 restore_command..."

    # 构建 barman-cloud-wal-restore 命令
    RESTORE_CMD="barman-cloud-wal-restore"

    # 添加 cloud provider
    RESTORE_CMD="$RESTORE_CMD --cloud-provider aws-s3"

    # 添加 endpoint URL（如果配置了）
    if [ -n "$S3_ENDPOINT_URL" ]; then
        RESTORE_CMD="$RESTORE_CMD --endpoint-url $S3_ENDPOINT_URL"
    fi

    # 添加 S3 bucket 和 server name
    RESTORE_CMD="$RESTORE_CMD $S3_BUCKET_URL $BARMAN_CLOUD_SERVER_NAME %f %p"

    # 检查是否已经配置了 restore_command
    if ! grep -q "^restore_command" "$PGDATA/postgresql.auto.conf" 2>/dev/null; then
        echo "[recovery-entrypoint] 写入 restore_command 到 postgresql.auto.conf"
        echo "restore_command = '$RESTORE_CMD'" >> "$PGDATA/postgresql.auto.conf"
    else
        echo "[recovery-entrypoint] restore_command 已存在，跳过"
    fi

    # 如果设置了 RECOVERY_TARGET_TIME，自动添加 PITR 配置
    if [ -n "$RECOVERY_TARGET_TIME" ]; then
        echo "[recovery-entrypoint] 配置 PITR 目标时间: $RECOVERY_TARGET_TIME"
        if ! grep -q "^recovery_target_time" "$PGDATA/postgresql.auto.conf" 2>/dev/null; then
            echo "recovery_target_time = '$RECOVERY_TARGET_TIME'" >> "$PGDATA/postgresql.auto.conf"
        fi
        if ! grep -q "^recovery_target_action" "$PGDATA/postgresql.auto.conf" 2>/dev/null; then
            echo "recovery_target_action = 'promote'" >> "$PGDATA/postgresql.auto.conf"
        fi
    fi

    echo "[recovery-entrypoint] 恢复配置完成"

    # 如果启用了自动关闭，启动后台监控进程
    if [ "$AUTO_SHUTDOWN_AFTER_RECOVERY" = "true" ]; then
        echo "[recovery-entrypoint] 启用自动关闭模式，恢复完成后将自动退出"
        (
            # 等待 PostgreSQL 启动
            sleep 5

            # 轮询检查恢复状态
            while true; do
                if pg_isready -U "${POSTGRES_USER:-postgres}" >/dev/null 2>&1; then
                    # 检查是否还在恢复模式
                    IN_RECOVERY=$(psql -U "${POSTGRES_USER:-postgres}" -t -A -c "SELECT pg_is_in_recovery();" 2>/dev/null || echo "t")

                    if [ "$IN_RECOVERY" = "f" ]; then
                        echo "[recovery-monitor] 恢复完成，执行验证查询..."

                        # 执行验证查询
                        psql -U "${POSTGRES_USER:-postgres}" -c "SELECT version();" || true
                        psql -U "${POSTGRES_USER:-postgres}" -c "SELECT pg_is_in_recovery();" || true

                        # 如果设置了自定义验证查询
                        if [ -n "$RECOVERY_VERIFY_QUERY" ]; then
                            echo "[recovery-monitor] 执行自定义验证查询..."
                            psql -U "${POSTGRES_USER:-postgres}" -c "$RECOVERY_VERIFY_QUERY" || true
                        fi

                        echo "[recovery-monitor] 验证完成，5 秒后关闭 PostgreSQL..."
                        sleep 5

                        # 优雅关闭 PostgreSQL
                        pg_ctl stop -D "$PGDATA" -m fast
                        exit 0
                    fi
                fi
                sleep 2
            done
        ) &
    fi
fi

# 调用原始的 PostgreSQL entrypoint
exec docker-entrypoint.sh "$@"
