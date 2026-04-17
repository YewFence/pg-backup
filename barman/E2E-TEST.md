# Barman E2E 备份恢复测试

验证完整的备份恢复流水线：插入数据 → WAL 归档 → 销毁 PG → 恢复（latest / PITR）。

## 预期结果

| 阶段 | Users | Products | Orders | Batch1 | Batch2 |
|------|-------|----------|--------|--------|--------|
| Seed 基线 | 5 | 5 | 7 | - | - |
| Batch 1 后 | 6 | 6 | 8 | Yes | No |
| Batch 2 后 | 7 | 7 | 9 | Yes | Yes |
| Recovery A（latest → pg/） | 7 | 7 | 9 | Yes | Yes |
| Recovery B（latest → pg-recovered） | 7 | 7 | 9 | Yes | Yes |
| Recovery C（PITR → pg-recovered） | 6 | 6 | 8 | Yes | **No** |

---

## Phase 0: 前置检查

```bash
# PG 运行中
docker exec postgres psql -U postgres -c 'SELECT version()'

# barman 运行中
docker exec barman barman --version

# Tailscale 连通
docker exec barman psql -U barman -h pg-host postgres -c 'SELECT 1'

# WAL 配置正确
docker exec postgres psql -U postgres -c \
  "SELECT name, setting FROM pg_settings WHERE name IN ('wal_level', 'archive_mode')"
# 期望: wal_level=replica, archive_mode=on

# Seed 数据 5/5/7
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"

# 清空 recover 目录
docker exec barman bash -c "rm -rf /recover/* /recover/.*"
```

## Phase 1: 初始 Base Backup

```bash
# 启动 WAL streaming
docker exec barman barman cron

# 等待 receive-wal 就绪（约 30 秒）
sleep 30
docker exec barman barman check streaming-backup-server
# 全部 OK 后继续，receive-wal running: OK 是关键项

# 创建 base backup
docker exec barman barman backup streaming-backup-server --wait

# 验证
docker exec barman barman list-backups streaming-backup-server
docker exec barman barman check streaming-backup-server
```

> 首次 backup 前 `barman check` 部分项可能 FAILED，这是正常的。backup 完成后应全部 OK。

## Phase 2: 插入 Batch 1 + WAL 归档

```bash
docker exec postgres psql -U postgres -c "
  INSERT INTO users (name, email) VALUES ('Batch1User', 'batch1@example.com');
  INSERT INTO products (name, price, stock) VALUES ('Batch1Product', 111.11, 111);
  INSERT INTO orders (user_id, product_id, quantity) VALUES (
    (SELECT id FROM users WHERE email='batch1@example.com'),
    (SELECT id FROM products WHERE name='Batch1Product'),
    11
  );
"

# 验证 6/6/8
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"

# 归档 WAL
docker exec barman barman switch-wal --force streaming-backup-server
docker exec barman barman cron
sleep 10
docker exec barman barman cron
```

## Phase 3: 记录 PITR 时间戳 + 插入 Batch 2 + WAL 归档

```bash
# 确保时间戳与 Batch 2 有间隔
sleep 3

# !! 记录这个时间戳，后面 PITR 要用 !!
docker exec postgres psql -U postgres -t -A -c "SELECT now()"
# 示例输出: 2026-03-10 13:00:04.656887+00
# 将输出保存到变量:
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

# 验证 7/7/9
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"

# 归档 WAL
docker exec barman barman switch-wal --force streaming-backup-server
docker exec barman barman cron
sleep 10
docker exec barman barman cron
```

## Phase 4: 销毁 PG

```bash
cd pg
docker compose down

# 清空 pgdata（UID 999 所有，普通用户无法直接删除）
docker run --rm -v $(pwd)/pgdata:/data postgres:17 bash -c "rm -rf /data/* /data/.*"

# 验证 barman 备份仍在
docker exec barman barman list-backups streaming-backup-server
```

## Phase 5: Recovery A — 恢复 latest 到 pg/

```bash
# 清空 recover
docker exec barman bash -c "rm -rf /recover/* /recover/.*"

# 恢复
docker exec barman barman recover streaming-backup-server latest /recover

# 复制到 pgdata
docker run --rm \
  -v $(pwd)/../barman/recover:/src:ro \
  -v $(pwd)/pgdata:/dst \
  postgres:17 bash -c "cp -a /src/. /dst/"

# 检查 postgresql.auto.conf，latest 恢复通常不含 restore_command
docker run --rm -v $(pwd)/pgdata:/data postgres:17 cat /data/postgresql.auto.conf

# 启动 PG
docker compose up -d
sleep 5

# 验证: 不在 recovery 模式
docker exec postgres psql -U postgres -c "SELECT pg_is_in_recovery()"
# 期望: f

# 验证: 7/7/9，Batch1 存在，Batch2 存在
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch1User') as batch1;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch2User') as batch2;
"
```

## Phase 6: Recovery B — 恢复 latest 到 pg-recovered

```bash
cd ../barman

# 停止旧实例（如有），清空 recover
docker compose --profile recovery stop pg-recovered 2>/dev/null
docker exec barman bash -c "rm -rf /recover/* /recover/.*"

# 恢复
docker exec barman barman recover streaming-backup-server latest /recover

# 启动
docker compose --profile recovery up -d pg-recovered
sleep 8

# 验证: 7/7/9，Batch1 存在，Batch2 存在
docker exec pg-recovered psql -U postgres -c "
  SELECT pg_is_in_recovery();
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch1User') as batch1;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch2User') as batch2;
"

# 清理
docker compose --profile recovery stop pg-recovered
```

## Phase 7: Recovery C — PITR 到 Batch 1 之后、Batch 2 之前

```bash
# 清空 recover
docker exec barman bash -c "rm -rf /recover/* /recover/.*"

# PITR 恢复（使用 Phase 3 记录的时间戳）
docker exec barman barman recover \
  --target-time "$PITR_TS" \
  --target-action=promote \
  streaming-backup-server latest /recover

# 确认 recovery_target_time 正确
docker exec barman cat /recover/postgresql.auto.conf

# 启动 pg-recovered
docker compose --profile recovery up -d pg-recovered
sleep 8

# 验证: 6/6/8，Batch1 存在，Batch2 不存在
docker exec pg-recovered psql -U postgres -c "
  SELECT pg_is_in_recovery();
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch1User') as batch1;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch2User') as batch2;
"
# 期望: 6/6/8, batch1=t, batch2=f

# 清理
docker compose --profile recovery stop pg-recovered
docker exec barman bash -c "rm -rf /recover/* /recover/.*"
```

---

## 故障排查

### barman check 显示 receive-wal FAILED

刚启动后属正常现象，等 30 秒重试。如果持续失败，检查日志：

```bash
docker exec barman tail -20 /var/log/barman/barman.log
```

常见原因：WAL 位置不一致（PG 被重建过），需要彻底清理 barman 数据，见 [RECOVERY-GUIDE.md](./RECOVERY-GUIDE.md) 注意事项第 4 条。

### PG 恢复后启动失败：WAL system identifier mismatch

barman WAL 归档中混入了旧 PG 实例的 WAL 文件。必须彻底清理 barman 的 `streaming-backup-server` 目录后从 Phase 1 重新开始。

### PG 恢复后启动失败：could not locate required checkpoint record

检查是否缺少 `recovery.signal` 或 `restore_command` 路径不正确。PITR 恢复场景下 pg-recovered 依赖 `./recover:/recover:ro` 挂载来读取 barman_wal。

### switch-wal --archive 超时

不影响数据，多跑几次 `barman cron` 即可。WAL 会在 streaming 中被消费。
