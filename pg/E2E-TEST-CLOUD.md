# barman-cloud + RustFS E2E 备份恢复测试

验证完整的云备份恢复流水线：插入数据 → WAL 归档到 S3 → Base Backup 到 S3 → 销毁 PG → 恢复（latest / PITR）。

## 预期结果

| 阶段 | Users | Products | Orders | Batch1 | Batch2 |
|------|-------|----------|--------|--------|--------|
| Seed 基线 | 5 | 5 | 7 | - | - |
| Batch 1 后 | 6 | 6 | 8 | Yes | No |
| Batch 2 后 | 7 | 7 | 9 | Yes | Yes |
| Recovery A (latest) | 7 | 7 | 9 | Yes | Yes |
| Recovery B (PITR) | 6 | 6 | 8 | Yes | **No** |

---

## Phase 0: 环境启动与验证

### 启动 RustFS

```bash
cd pg

# 启动 RustFS
docker compose -f rustfs-compose.yml up -d

# 验证 RustFS 运行
docker logs rustfs-test --tail 10

# 创建测试 bucket（手动或使用 mc）
# 方法 1: 使用 mc
docker run --rm --network pg_default minio/mc alias set rustfs http://rustfs-test:9000 rustfsadmin ChangeMe123!
docker run --rm --network pg_default minio/mc mb rustfs/pg-backup-test

# 方法 2: 使用 RustFS Web Console
# 访问 http://localhost:9001，使用 rustfsadmin / ChangeMe123! 登录
```

### 启动 PG + barman-cloud sidecar

```bash
# 确保 .env 已配置 RustFS 连接参数
cat .env | grep -E 'S3|AWS'

# 启动 PG 和 barman-cloud sidecar
docker compose --profile cloud up -d

# 验证 PG 正常
docker exec postgres psql -U postgres -c 'SELECT version()'

# 验证 sidecar 正常（supercronic 每分钟运行 wal-push.sh）
docker logs barman-cloud --tail 20

# 验证 Seed 数据 5/5/7
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
```

> **注意**：`pg/Dockerfile` 已预创建 `/archive` 目录并设置正确权限，无需手动修复

---

## Phase 1: WAL 归档测试

```bash
# 触发 WAL 切换
docker exec postgres psql -U postgres -c "SELECT pg_switch_wal()"

# 检查共享卷中是否有 WAL 文件
docker exec postgres ls -lh /archive/

# 手动触发 WAL 推送（测试 wal-push.sh）
docker exec barman-cloud /usr/local/bin/wal-push.sh

# 验证 WAL 已上传到 RustFS（通过 RustFS 数据目录）
docker exec rustfs-test find /data/pg-backup-test/pg/wals -name "*.gz" | sort

# 验证共享卷中的 WAL 文件已被删除
docker exec postgres ls -lh /archive/
# 应该为空或只有新的 WAL
```

---

## Phase 2: 插入 Batch 1 + WAL 归档

```bash
# 插入 Batch 1
docker exec postgres psql -U postgres -c "
  INSERT INTO users (name, email) VALUES ('Batch1User', 'batch1@example.com');
  INSERT INTO products (name, price, stock) VALUES ('Batch1Product', 111.11, 111);
  INSERT INTO orders (user_id, product_id, quantity) VALUES (
    (SELECT id FROM users WHERE email='batch1@example.com'),
    (SELECT id FROM products WHERE name='Batch1Product'),
    11
  );
"

# 验证数据 6/6/8
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"

# 强制 WAL 切换并归档
docker exec postgres psql -U postgres -c "SELECT pg_switch_wal()"
sleep 5
docker exec barman-cloud /usr/local/bin/wal-push.sh

# 验证 WAL 已上传
docker exec rustfs-test find /data/pg-backup-test/pg/wals -name "*.gz" | sort
```

---

## Phase 3: Base Backup 测试

```bash
# 手动触发 Base Backup
docker exec barman-cloud /usr/local/bin/cloud-backup.sh

# 查看 sidecar 日志
docker logs barman-cloud --tail 50

# 验证备份已上传到 RustFS
docker exec rustfs-test find /data/pg-backup-test/pg/base -type f

# 列出所有备份（使用 barman-cloud-backup-list）
docker exec barman-cloud barman-cloud-backup-list \
  --cloud-provider aws-s3 \
  --endpoint-url http://rustfs-test:9000 \
  s3://pg-backup-test pg
```

---

## Phase 4: 插入 Batch 2 + 记录 PITR 时间戳

```bash
# 等待 3 秒确保时间戳间隔
sleep 3

# 记录 PITR 时间戳
PITR_TS=$(docker exec postgres psql -U postgres -t -A -c "SELECT now()")
echo "PITR timestamp: $PITR_TS"

sleep 3

# 插入 Batch 2
docker exec postgres psql -U postgres -c "
  INSERT INTO users (name, email) VALUES ('Batch2User', 'batch2@example.com');
  INSERT INTO products (name, price, stock) VALUES ('Batch2Product', 222.22, 222);
  INSERT INTO orders (user_id, product_id, quantity) VALUES (
    (SELECT id FROM users WHERE email='batch2@example.com'),
    (SELECT id FROM products WHERE name='Batch2Product'),
    22
  );
"

# 验证数据 7/7/9
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"

# 归档 WAL
docker exec postgres psql -U postgres -c "SELECT pg_switch_wal()"
sleep 5
docker exec barman-cloud /usr/local/bin/wal-push.sh
```

---

## Phase 5: 销毁 PG

```bash
# 停止 PG（保留 barman-cloud sidecar 用于后续恢复）
docker compose down

# 清空 pgdata
docker run --rm -v $(pwd)/pgdata:/data postgres:17 bash -c "rm -rf /data/* /data/.*"

# 验证 RustFS 中的备份仍在
docker exec rustfs-test find /data/pg-backup-test/pg/base -type f
docker exec rustfs-test find /data/pg-backup-test/pg/wals -name "*.gz" | wc -l
```

---

## Phase 6: Recovery A — 恢复 latest

### 关键发现

恢复时需要：
1. **启用 RECOVERY_MODE**，让 barman-cloud sidecar 暂停所有操作
2. 使用 `pg-barman-cloud` 镜像预先下载所有 WAL 到 `/archive` 卷
3. `restore_command` 使用本地 `cp /archive/%f %p`，无需网络工具

### 步骤

```bash
# 1. 启用 RECOVERY_MODE（让 sidecar 暂停操作，防止删除 WAL）
docker compose --profile cloud stop barman-cloud
docker compose --profile cloud run -d --name barman-cloud \
  -e RECOVERY_MODE=true barman-cloud

# 2. 使用 pg-barman-cloud 镜像恢复 base backup 到 pgdata
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

# 3. 预下载所有 WAL 文件到 /archive 卷
# 先查看 RustFS 中有哪些 WAL
docker exec rustfs-test find /data/pg-backup-test/pg/wals -name "*.gz" | sort

# 批量下载（根据实际 WAL 文件名调整）
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

# 验证 WAL 文件已下载
docker run --rm -v pg_wal-archive:/archive postgres:17 ls -lh /archive/

# 4. 配置 restore_command（使用本地 cp）
docker run --rm -v $(pwd)/pgdata:/data postgres:17 bash -c "
cat > /data/postgresql.auto.conf << 'EOF'
restore_command = 'cp /archive/%f %p'
EOF
touch /data/recovery.signal
chown postgres:postgres /data/postgresql.auto.conf /data/recovery.signal"

# 5. 启动 PG（只启动 postgres 服务，不启动 barman-cloud）
docker compose up -d postgres
sleep 10

# 6. 验证恢复成功
docker exec postgres psql -U postgres -c "SELECT pg_is_in_recovery()"
# 期望: f（已完成恢复并 promote）

# 7. 验证数据 7/7/9，Batch1 和 Batch2 都存在
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch1User') as batch1;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch2User') as batch2;
"
# 期望: 7/7/9, batch1=t, batch2=t
```

---

## Phase 7: Recovery B — PITR 恢复到 Batch 1 之后

```bash
# 1. 停止 PG 并清空 pgdata
docker compose down
docker run --rm -v $(pwd)/pgdata:/data postgres:17 bash -c "rm -rf /data/* /data/.*"

# 2. 恢复 base backup
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

# 3. 配置 PITR 恢复参数（使用 Phase 4 记录的时间戳）
docker run --rm -v $(pwd)/pgdata:/data postgres:17 bash -c "
cat > /data/postgresql.auto.conf << 'EOF'
restore_command = 'cp /archive/%f %p'
recovery_target_time = '$PITR_TS'
recovery_target_action = 'promote'
EOF
touch /data/recovery.signal
chown postgres:postgres /data/postgresql.auto.conf /data/recovery.signal"

# 4. 启动 PG
docker compose up -d postgres
sleep 10

# 5. 验证数据 6/6/8，Batch1 存在，Batch2 不存在
docker exec postgres psql -U postgres -c "
  SELECT pg_is_in_recovery();
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch1User') as batch1;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch2User') as batch2;
"
# 期望: 6/6/8, batch1=t, batch2=f
```

---

## 故障排查

### WAL 文件没有上传到 RustFS

**症状**：`docker exec barman-cloud /usr/local/bin/wal-push.sh` 执行后，RustFS 中没有 WAL 文件。

**原因**：历史版本中 `/archive` 卷权限不对，postgres 用户无法写入。

**解决**：当前版本的 `pg/Dockerfile` 已预创建 `/archive` 目录并设置正确权限，无需手动修复。如果仍有问题，检查 PG 日志：
```bash
docker logs postgres | grep -i permission
```

### 恢复时 PG 找不到 WAL 文件

**症状**：PG 日志显示 `cp: cannot stat '/archive/000000010000000000000006': No such file or directory`

**原因**：`barman-cloud` sidecar 还在运行，它的 cron job 把下载的 WAL 文件推送后删掉了。

**解决**：启用 RECOVERY_MODE 让 sidecar 暂停操作
```bash
# 重启 sidecar 并启用 RECOVERY_MODE
docker compose --profile cloud stop barman-cloud
docker compose --profile cloud run -d --name barman-cloud \
  -e RECOVERY_MODE=true barman-cloud

# 重新下载 WAL 文件
# （见 Phase 6 步骤 3）
```

### barman-cloud-restore 没有输出

**症状**：`barman-cloud-restore` 命令执行后没有任何输出，但容器正常退出。

**说明**：这是正常的，`barman-cloud-restore` 成功时不输出任何内容。检查目标目录是否有文件即可。

### RustFS bucket 不存在

**症状**：`barman-cloud-wal-archive` 报错 `NoSuchBucket`。

**解决**：手动创建 bucket（RustFS 不会自动创建）：
```bash
# 使用 mc
docker run --rm --network pg_default minio/mc alias set rustfs http://rustfs-test:9000 rustfsadmin ChangeMe123!
docker run --rm --network pg_default minio/mc mb rustfs/pg-backup-test

# 或使用 RustFS Web Console (http://localhost:9001)
```

---

## 清理测试环境

```bash
# 停止所有容器
docker compose --profile cloud down
docker compose -f rustfs-compose.yml down

# 清空 pgdata 和 archive 卷
docker run --rm -v $(pwd)/pgdata:/data postgres:17 bash -c "rm -rf /data/* /data/.*"
docker volume rm pg_wal-archive

# 删除 RustFS 数据卷（可选）
docker volume rm pg_rustfs-data
```
