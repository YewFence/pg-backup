# barman-cloud 备份恢复指南

## 架构概览

```
pg/                              RustFS (S3-compatible)
├── compose.yml                  ┌──────────────────┐
├── pgdata/  ← PG 数据           │ rustfs-test:9000 │
├── initdb/seed.sql              │ /pg-backup-test  │
└── barman-cloud (sidecar)       │   ├── base/      │
    ├── wal-push.sh              │   └── wals/      │
    ├── cloud-backup.sh          └──────────────────┘
    └── cloud-backup-delete.sh
```

- **barman-cloud sidecar**：与 PG 共享 `/archive` 卷，定期推送 WAL 和 Base Backup 到 S3
- **RustFS**：本地 S3 兼容存储，用于测试（生产环境可替换为 AWS S3 / Cloudflare R2 / MinIO）
- **恢复流程**：使用 `pg-barman-cloud` 镜像从 S3 拉取备份和 WAL，恢复到 `pgdata`

---

## 注意事项

### 1. /archive 卷权限（已解决）

`pg/Dockerfile` 已预创建 `/archive` 目录并设置正确权限（postgres:postgres），无需手动干预。

历史版本中，PG 的 `archive_command` 需要写入 `/archive` 卷，但该卷默认是 root 所有，导致 WAL 归档失败。当前版本已在镜像构建时解决此问题。

### 2. 恢复时必须启用 RECOVERY_MODE

`barman-cloud` 的 cron job 每分钟运行 `wal-push.sh`，会把 `/archive` 中的 WAL 文件推送到 S3 后删除。

恢复时如果 sidecar 还在正常运行，预下载的 WAL 文件会被删掉，导致 PG 恢复失败。

**解决方案**：恢复前启用 RECOVERY_MODE，让 sidecar 暂停所有操作：

```bash
docker compose --profile cloud stop barman-cloud
docker compose --profile cloud run -d --name barman-cloud \
  -e RECOVERY_MODE=true barman-cloud
```

恢复完成后，重启 sidecar 恢复正常备份：

```bash
docker compose --profile cloud restart barman-cloud
```

### 3. 恢复策略：预下载 WAL 到本地

由于 `postgres:17` 镜像没有 `barman-cloud-wal-restore` 工具，无法在 `restore_command` 中直接从 S3 拉取 WAL。

**推荐方案**：
1. 使用 `pg-barman-cloud` 镜像预先下载所有 WAL 到 `/archive` 卷
2. `restore_command` 配置为 `cp /archive/%f %p`（本地复制，无需网络工具）

这样 PG 容器无需安装任何额外工具，恢复流程简单可靠。

### 4. RustFS 不会自动创建 bucket

首次使用前需要手动创建 bucket：

```bash
# 使用 mc
docker run --rm --network pg_default minio/mc alias set rustfs http://rustfs-test:9000 rustfsadmin ChangeMe123!
docker run --rm --network pg_default minio/mc mb rustfs/pg-backup-test

# 或使用 RustFS Web Console
# 访问 http://localhost:9001，使用 rustfsadmin / ChangeMe123! 登录
```

### 5. barman-cloud-restore 成功时无输出

`barman-cloud-restore` 命令成功时不输出任何内容，只有失败时才会报错。检查目标目录是否有文件即可。

---

## 恢复场景 A：恢复到最新状态（latest）

适用于：灾难恢复、迁移到新服务器。

### 前置条件

- RustFS 容器正在运行
- 确认有可用备份：
  ```bash
  docker exec barman-cloud barman-cloud-backup-list \
    --cloud-provider aws-s3 \
    --endpoint-url http://rustfs-test:9000 \
    s3://pg-backup-test pg
  ```

### 步骤

#### 1. 停止 PG 并启用 RECOVERY_MODE

```bash
cd pg
docker compose down

# 启用 RECOVERY_MODE（让 sidecar 暂停操作）
docker compose --profile cloud stop barman-cloud
docker compose --profile cloud run -d --name barman-cloud \
  -e RECOVERY_MODE=true barman-cloud
```

#### 2. 清空 pgdata

```bash
docker run --rm -v $(pwd)/pgdata:/data postgres:17 bash -c "rm -rf /data/* /data/.*"
```

#### 3. 恢复 base backup

```bash
docker run --rm \
  --network pg_default \
  -v $(pwd)/pgdata:/recover \
  -e AWS_ACCESS_KEY_ID=rustfsadmin \
  -e AWS_SECRET_ACCESS_KEY=ChangeMe123! \
  -e AWS_DEFAULT_REGION=us-east-1 \
  pg-barman-cloud:latest \
  barman-cloud-restore \
    --cloud-provider aws-s3 \
    --endpoint-url http://rustfs-test:9000 \
    s3://pg-backup-test pg latest /recover
```

> **生产环境**：将 `--endpoint-url` 和凭证替换为实际的 S3 配置。

#### 4. 预下载所有 WAL 文件

先查看 S3 中有哪些 WAL：

```bash
# 通过 RustFS 数据目录查看（仅限本地测试）
docker exec rustfs-test find /data/pg-backup-test/pg/wals -name "*.gz" | sort

# 或使用 barman-cloud-wal-archive --list（需要 barman 3.0+）
```

批量下载 WAL：

```bash
# 根据实际 WAL 文件名调整列表
for wal in 000000010000000000000003 000000010000000000000004 000000010000000000000005 \
           000000010000000000000006 000000010000000000000007 000000010000000000000008; do
  docker run --rm \
    --network pg_default \
    -v pg_wal-archive:/archive \
    -e AWS_ACCESS_KEY_ID=rustfsadmin \
    -e AWS_SECRET_ACCESS_KEY=ChangeMe123! \
    -e AWS_DEFAULT_REGION=us-east-1 \
    pg-barman-cloud:latest \
    barman-cloud-wal-restore \
      --cloud-provider aws-s3 \
      --endpoint-url http://rustfs-test:9000 \
      s3://pg-backup-test pg $wal /archive/$wal
  echo "Downloaded: $wal"
done
```

验证 WAL 文件已下载：

```bash
docker run --rm -v pg_wal-archive:/archive postgres:17 ls -lh /archive/
```

#### 5. 配置 restore_command

```bash
docker run --rm -v $(pwd)/pgdata:/data postgres:17 bash -c "
cat > /data/postgresql.auto.conf << 'EOF'
restore_command = 'cp /archive/%f %p'
EOF
touch /data/recovery.signal
chown postgres:postgres /data/postgresql.auto.conf /data/recovery.signal"
```

#### 6. 启动 PG

```bash
# 只启动 postgres 服务，不启动 barman-cloud sidecar
docker compose up -d postgres
sleep 10
```

#### 7. 验证恢复结果

```bash
# 确认已完成恢复
docker exec postgres psql -U postgres -c "SELECT pg_is_in_recovery()"
# 期望: f

# 验证数据
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
```

#### 8. 恢复后重建备份链

恢复后 PG 的 system identifier 不变，可以继续使用原有的 S3 备份。建议立即创建新的 base backup：

```bash
# 重启 sidecar 恢复正常备份（移除 RECOVERY_MODE）
docker compose --profile cloud restart barman-cloud

# 等待 WAL 归档稳定
sleep 30

# 手动触发 base backup
docker exec barman-cloud /usr/local/bin/cloud-backup.sh
```

---

## 恢复场景 B：PITR 恢复到指定时间点

适用于：误删数据恢复、回滚到特定时间点。

### 前置条件

- 同恢复场景 A
- 已知目标时间戳（格式：`2026-03-17 10:39:50.236053+00`）

### 步骤

#### 1-4. 同恢复场景 A（停止 PG、启用 RECOVERY_MODE、清空 pgdata、恢复 base backup、下载 WAL）

#### 5. 配置 PITR 恢复参数

```bash
# 将 $PITR_TS 替换为实际时间戳
docker run --rm -v $(pwd)/pgdata:/data postgres:17 bash -c "
cat > /data/postgresql.auto.conf << 'EOF'
restore_command = 'cp /archive/%f %p'
recovery_target_time = '2026-03-17 10:39:50.236053+00'
recovery_target_action = 'promote'
EOF
touch /data/recovery.signal
chown postgres:postgres /data/postgresql.auto.conf /data/recovery.signal"
```

> **关键参数**：
> - `recovery_target_time`：目标时间戳
> - `recovery_target_action = 'promote'`：恢复到目标时间后自动 promote 为主库

#### 6. 启动 PG

```bash
docker compose up -d postgres
sleep 10
```

#### 7. 验证恢复结果

```bash
# 确认已完成恢复
docker exec postgres psql -U postgres -c "SELECT pg_is_in_recovery()"
# 期望: f

# 验证数据是否符合目标时间点
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
```

查看 PG 日志确认恢复停止点：

```bash
docker logs postgres | grep "recovery stopping"
# 示例输出: recovery stopping before commit of transaction 759, time 2026-03-17 10:41:20.697953+00
```

#### 8. 恢复后重建备份链

同恢复场景 A 步骤 8。

---

## 生产环境配置

### 替换 RustFS 为 AWS S3

修改 `pg/.env`：

```bash
S3_BUCKET_URL=s3://my-production-bucket/pg-backup
S3_ENDPOINT_URL=  # 留空使用 AWS S3
AWS_ACCESS_KEY_ID=<your-access-key>
AWS_SECRET_ACCESS_KEY=<your-secret-key>
AWS_DEFAULT_REGION=us-east-1
BARMAN_CLOUD_SERVER_NAME=pg-prod
BARMAN_CLOUD_COMPRESSION=gzip
BARMAN_CLOUD_RETENTION=RECOVERY WINDOW OF 30 DAYS
BARMAN_CLOUD_MIN_REDUNDANCY=3
```

### 替换为 Cloudflare R2

```bash
S3_BUCKET_URL=s3://my-r2-bucket/pg-backup
S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID=<r2-access-key>
AWS_SECRET_ACCESS_KEY=<r2-secret-key>
AWS_DEFAULT_REGION=auto
```

### 自动化备份策略

`barman-cloud` sidecar 默认配置（`cloud-crontab`）：

```
# WAL 推送：每分钟
* * * * * /usr/local/bin/wal-push.sh

# Base Backup：每天凌晨 2 点
0 2 * * * /usr/local/bin/cloud-backup.sh

# 清理过期备份：每天凌晨 3 点
0 3 * * * /usr/local/bin/cloud-backup-delete.sh
```

根据需求调整 `pg/cloud-crontab` 后重启 sidecar：

```bash
docker compose --profile cloud restart barman-cloud
```

---

## 附录：常用命令

### 查看 S3 中的备份

```bash
docker exec barman-cloud barman-cloud-backup-list \
  --cloud-provider aws-s3 \
  --endpoint-url http://rustfs-test:9000 \
  s3://pg-backup-test pg
```

### 手动触发 WAL 推送

```bash
docker exec postgres psql -U postgres -c "SELECT pg_switch_wal()"
docker exec barman-cloud /usr/local/bin/wal-push.sh
```

### 手动触发 Base Backup

```bash
docker exec barman-cloud /usr/local/bin/cloud-backup.sh
```

### 手动清理过期备份

```bash
docker exec barman-cloud /usr/local/bin/cloud-backup-delete.sh
```

### 查看 sidecar 日志

```bash
docker logs barman-cloud --tail 50 -f
```

### 验证 S3 连接

```bash
docker exec barman-cloud barman-cloud-wal-archive --test \
  --cloud-provider aws-s3 \
  --endpoint-url http://rustfs-test:9000 \
  s3://pg-backup-test pg
```
