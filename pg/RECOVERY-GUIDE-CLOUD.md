# 使用 pg-recovery 服务恢复备份

## 架构说明

`pg-recovery` 是一个专用的恢复服务，基于 `postgres:17` 镜像并集成了 `barman-cli-cloud` 工具。

**优势**：
- 自动配置 `restore_command`，无需手动编辑配置文件
- 支持 PITR（Point-in-Time Recovery），只需设置环境变量
- WAL 文件按需从 S3 拉取，无需预下载
- 恢复环境与生产环境隔离

---

## 恢复场景 A：恢复到最新状态

### 步骤

#### 1. 清空恢复目录

```bash
cd pg
rm -rf pgdata-recovery/*
```

#### 2. 恢复 base backup

```bash
docker compose --profile recovery run --rm \
  -e AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY \
  -e AWS_DEFAULT_REGION \
  pg-recovery \
  barman-cloud-restore \
    --cloud-provider aws-s3 \
    --endpoint-url http://rustfs-test:9000 \
    s3://pg-backup-test pg latest /var/lib/postgresql/data
```

#### 3. 创建 recovery.signal

```bash
docker compose --profile recovery run --rm pg-recovery \
  bash -c "touch /var/lib/postgresql/data/recovery.signal"
```

#### 4. 启动恢复

```bash
docker compose --profile recovery up -d pg-recovery
```

entrypoint 会自动检测 `recovery.signal` 并配置 `restore_command`，PG 启动后会自动从 S3 拉取 WAL 并完成恢复。

**可选：启用自动关闭**

如果希望恢复完成后容器自动退出（无需手动验证和停止）：

```bash
docker compose --profile recovery run --rm \
  -e AUTO_SHUTDOWN_AFTER_RECOVERY=true \
  pg-recovery
```

容器会在恢复完成后自动执行验证查询并优雅退出。

#### 5. 验证恢复结果

```bash
sleep 10

# 确认已完成恢复
docker exec pg-recovery psql -U postgres -c "SELECT pg_is_in_recovery();"
# 期望: f

# 验证数据
docker exec pg-recovery psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
```

#### 6. 清理

```bash
docker compose --profile recovery down
```

---

## 恢复场景 B：PITR 恢复到指定时间点

### 步骤

#### 1-2. 同场景 A（清空目录、恢复 base backup）

#### 3. 创建 recovery.signal 并设置 PITR 目标时间

```bash
# 获取当前 PG 时间作为参考
docker exec postgres psql -U postgres -t -A -c "SELECT now();"

# 创建 recovery.signal
docker compose --profile recovery run --rm pg-recovery \
  bash -c "touch /var/lib/postgresql/data/recovery.signal"
```

#### 4. 启动恢复（设置 RECOVERY_TARGET_TIME）

编辑 `compose.yml`，取消注释并设置目标时间：

```yaml
pg-recovery:
  environment:
    RECOVERY_TARGET_TIME: "2026-03-17 10:39:50+00"  # 替换为实际时间戳
    AUTO_SHUTDOWN_AFTER_RECOVERY: "true"  # 可选：恢复完成后自动退出
```

或者直接在命令行指定：

```bash
docker compose --profile recovery run --rm \
  -e RECOVERY_TARGET_TIME="2026-03-17 10:39:50+00" \
  -e AUTO_SHUTDOWN_AFTER_RECOVERY=true \
  pg-recovery
```

entrypoint 会自动配置：
- `restore_command`：从 S3 按需拉取 WAL
- `recovery_target_time`：指定的时间点
- `recovery_target_action = 'promote'`：恢复完成后自动提升为主库
- 如果启用了 `AUTO_SHUTDOWN_AFTER_RECOVERY`，恢复完成后自动验证并退出

#### 5. 验证恢复结果

```bash
sleep 10

# 确认已完成恢复
docker exec pg-recovery psql -U postgres -c "SELECT pg_is_in_recovery();"

# 验证数据是否符合目标时间点
docker exec pg-recovery psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"

# 查看 PG 日志确认恢复停止点
docker logs pg-recovery | grep "recovery stopping"
```

---

## 高级用法：一键恢复脚本

创建 `scripts/quick-restore.sh`：

```bash
#!/bin/bash
set -e

BACKUP_ID="${1:-latest}"
TARGET_TIME="${2:-}"

echo "==> 清空恢复目录"
rm -rf pgdata-recovery/*

echo "==> 恢复 base backup: $BACKUP_ID"
docker compose --profile recovery run --rm pg-recovery \
  barman-cloud-restore \
    --cloud-provider aws-s3 \
    --endpoint-url http://rustfs-test:9000 \
    s3://pg-backup-test pg "$BACKUP_ID" /var/lib/postgresql/data

echo "==> 创建 recovery.signal"
docker compose --profile recovery run --rm pg-recovery \
  bash -c "touch /var/lib/postgresql/data/recovery.signal"

if [ -n "$TARGET_TIME" ]; then
    echo "==> 启动 PITR 恢复到: $TARGET_TIME"
    docker compose --profile recovery run -d \
      -e RECOVERY_TARGET_TIME="$TARGET_TIME" \
      pg-recovery
else
    echo "==> 启动恢复到最新状态"
    docker compose --profile recovery up -d pg-recovery
fi

echo "==> 等待恢复完成..."
sleep 10

echo "==> 验证恢复状态"
docker exec pg-recovery psql -U postgres -c "SELECT pg_is_in_recovery();"
```

使用方法：

```bash
# 恢复到最新
./scripts/quick-restore.sh

# 恢复到指定备份
./scripts/quick-restore.sh 20260317T120000

# PITR 恢复
./scripts/quick-restore.sh latest "2026-03-17 10:39:50+00"
```

---

## 工作原理

`recovery-entrypoint.sh` 在 PostgreSQL 启动前执行以下逻辑：

1. 检测 `$PGDATA/recovery.signal` 是否存在
2. 如果存在，自动生成 `restore_command`：
   ```
   restore_command = 'barman-cloud-wal-restore --cloud-provider aws-s3 --endpoint-url http://rustfs-test:9000 s3://pg-backup-test pg %f %p'
   ```
3. 如果设置了 `RECOVERY_TARGET_TIME` 环境变量，自动添加：
   ```
   recovery_target_time = '2026-03-17 10:39:50+00'
   recovery_target_action = 'promote'
   ```
4. 如果启用了 `AUTO_SHUTDOWN_AFTER_RECOVERY=true`，启动后台监控进程：
   - 每 2 秒检查一次 `pg_is_in_recovery()` 状态
   - 当恢复完成（返回 `f`）时，执行验证查询
   - 可选执行自定义验证查询（`RECOVERY_VERIFY_QUERY`）
   - 等待 5 秒后优雅关闭 PostgreSQL（`pg_ctl stop -m fast`）
   - 容器自动退出
5. 调用原始的 PostgreSQL entrypoint 启动数据库

这样就无需手动编辑配置文件，所有恢复参数通过环境变量和文件信号控制。

---

## 注意事项

1. **网络连通性**：pg-recovery 容器需要能访问 S3 endpoint（RustFS 或云端 S3）
2. **凭证配置**：确保 `.env` 中的 S3 凭证正确
3. **端口冲突**：pg-recovery 使用 5433 端口，避免与生产 PG（5432）冲突
4. **数据隔离**：恢复数据存储在 `./pgdata-recovery`，与生产数据（`./pgdata`）完全隔离
