# Barman E2E 备份恢复测试

验证完整的本地恢复流水线，插入数据、归档 WAL、恢复 latest、恢复 PITR，并通过 `pg-recovered` 验证结果。

## 预期结果

| 阶段 | Users | Products | Orders | Batch1 | Batch2 |
|------|-------|----------|--------|--------|--------|
| Seed 基线 | 5 | 5 | 7 | - | - |
| Batch 1 后 | 6 | 6 | 8 | Yes | No |
| Batch 2 后 | 7 | 7 | 9 | Yes | Yes |
| Recovery latest | 7 | 7 | 9 | Yes | Yes |
| Recovery PITR | 6 | 6 | 8 | Yes | No |

## Phase 0 前置检查

```bash
cd barman
docker exec postgres psql -U postgres -c 'SELECT version()'
docker exec barman barman --version
docker exec barman psql -U barman -h pg-host postgres -c 'SELECT 1'
docker exec postgres psql -U postgres -c \
  "SELECT name, setting FROM pg_settings WHERE name IN ('wal_level', 'archive_mode')"
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
docker compose --profile recovery rm -sf pg-recovered fix-recover-permissions barman-restore
docker volume rm barman_barman-recover 2>/dev/null || true
```

`wal_level` 期望是 `replica`，`archive_mode` 期望是 `on`，初始 seed 数据期望是 `5/5/7`。

## Phase 1 初始 Base Backup

```bash
docker exec barman barman cron
sleep 30
docker exec barman barman check streaming-backup-server
docker exec barman barman backup streaming-backup-server --wait
docker exec barman barman list-backups streaming-backup-server
docker exec barman barman check streaming-backup-server
```

首次 backup 前 `barman check` 部分项可能 FAILED，backup 完成后应全部 OK，其中 `receive-wal running` 是关键项。

## Phase 2 插入 Batch 1 并归档 WAL

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
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
docker exec barman barman switch-wal --force streaming-backup-server
docker exec barman barman cron
sleep 10
docker exec barman barman cron
```

## Phase 3 记录 PITR 时间戳并插入 Batch 2

```bash
sleep 3
PITR_TS=$(docker exec postgres psql -U postgres -t -A -c "SELECT now()")
echo "PITR timestamp: $PITR_TS"
sleep 3
docker exec postgres psql -U postgres -c "
  INSERT INTO users (name, email) VALUES ('Batch2User', 'batch2@example.com');
  INSERT INTO products (name, price, stock) VALUES ('Batch2Product', 222.22, 222);
  INSERT INTO orders (user_id, product_id, quantity) VALUES (
    (SELECT id FROM users WHERE email='batch2@example.com'),
    (SELECT id FROM products WHERE name='Batch2Product'),
    22
  );
"
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
docker exec barman barman switch-wal --force streaming-backup-server
docker exec barman barman cron
sleep 10
docker exec barman barman cron
```

## Phase 4 恢复 latest 到 pg-recovered

```bash
docker compose --profile recovery stop pg-recovered 2>/dev/null || true
docker compose --profile recovery rm -sf pg-recovered fix-recover-permissions barman-restore
docker volume rm barman_barman-recover 2>/dev/null || true
docker compose --profile recovery run --rm barman-restore \
  barman restore streaming-backup-server latest /recover
docker compose --profile recovery run --rm fix-recover-permissions
docker compose --profile recovery up -d pg-recovered
sleep 8
docker exec pg-recovered psql -U postgres -c "
  SELECT pg_is_in_recovery();
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch1User') as batch1;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch2User') as batch2;
"
docker compose --profile recovery stop pg-recovered
```

期望 `pg_is_in_recovery()` 返回 `f`，数据是 `7/7/9`，`batch1=t`，`batch2=t`。

## Phase 5 PITR 到 Batch 1 之后、Batch 2 之前

```bash
docker compose --profile recovery rm -sf pg-recovered fix-recover-permissions barman-restore
docker volume rm barman_barman-recover 2>/dev/null || true
docker compose --profile recovery run --rm barman-restore \
  barman restore \
  --target-time "$PITR_TS" \
  --target-action=promote \
  streaming-backup-server latest /recover
docker compose --profile recovery run --rm barman-restore \
  cat /recover/postgresql.auto.conf
docker compose --profile recovery run --rm fix-recover-permissions
docker compose --profile recovery up -d pg-recovered
sleep 8
docker exec pg-recovered psql -U postgres -c "
  SELECT pg_is_in_recovery();
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch1User') as batch1;
  SELECT EXISTS(SELECT 1 FROM users WHERE name='Batch2User') as batch2;
"
docker compose --profile recovery stop pg-recovered
docker compose --profile recovery rm -sf pg-recovered fix-recover-permissions barman-restore
docker volume rm barman_barman-recover 2>/dev/null || true
```

期望 `pg_is_in_recovery()` 返回 `f`，数据是 `6/6/8`，`batch1=t`，`batch2=f`。

## 故障排查

### receive-wal running 显示 FAILED

刚启动后属正常现象，等 30 秒重试。如果持续失败，检查日志。

```bash
docker exec barman tail -20 /var/log/barman/barman.log
```

### WAL system identifier mismatch

PG 被重建过时，Barman 旧 WAL 会与新 PG 冲突。测试环境可以删除 `/var/lib/barman/streaming-backup-server` 后从 Phase 1 重新开始。

### could not locate required checkpoint record

检查是否恢复到了空目录，是否缺少 PITR 需要的 WAL，以及 `postgresql.auto.conf` 的 `restore_command` 是否能在 `pg-recovered` 容器内访问 `/recover/barman_wal/`。

### Permission denied

重新执行权限修复服务后再启动 `pg-recovered`。

```bash
docker compose --profile recovery stop pg-recovered
docker compose --profile recovery run --rm fix-recover-permissions
docker compose --profile recovery up -d pg-recovered
```
