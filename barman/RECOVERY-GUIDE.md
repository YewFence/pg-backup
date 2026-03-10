# Barman 备份恢复指南

## 已知问题与注意事项

### 1. barman-data 必须使用命名卷

barman 容器以 `barman` 用户（UID 102）运行。如果使用 bind mount（`./barman-data:/var/lib/barman`），宿主机目录的 ownership 可能在容器重建时变为 root，导致 barman 无法写入 `.pgpass`，容器进入 crash loop。

**已修复**：`compose.yml` 中 `barman-data` 已改为 Docker 命名卷，由 Docker 管理权限。

### 2. /recover 目录 ownership 需要反复切换

`/recover` 目录在两个容器间共享：
- **barman 写入**时需要 UID 102:103（barman:barman）
- **pg-recovered 读取**时需要 UID 999:999（postgres:postgres）

每次恢复流程中，需要在 barman 写完后、pg-recovered 启动前执行 chown。由于宿主机用户无法直接操作这些 UID 拥有的文件，需要借助容器：

```bash
# barman 恢复完成后，修复为 postgres 所有
docker run --rm \
  -v $(pwd)/recover:/data \
  postgres:17 chown -R postgres:postgres /data/

# pg-recovered 使用完成后，恢复为 barman 所有（为下次恢复做准备）
docker run --rm \
  -v $(pwd)/recover:/data \
  postgres:17 chown -R 102:103 /data/
```

### 3. restore_command 路径不匹配

barman 恢复时会在 `postgresql.auto.conf` 中写入：

```
restore_command = 'cp /recover/barman_wal/%f %p'
```

这个 `/recover` 路径是 barman 容器内的挂载点。但在 pg-recovered 容器中，数据目录挂载在 `/var/lib/postgresql/data/`，所以 `barman_wal` 目录实际位于 `/var/lib/postgresql/data/barman_wal/`。

**处理方式取决于恢复场景**：

| 场景 | 处理 |
|------|------|
| 恢复到 pg/（原 PG） | `postgresql.auto.conf` 中通常没有 `restore_command`（latest 恢复），无需处理 |
| 恢复到 pg-recovered（latest） | 同上，通常没有 `restore_command` |
| PITR 恢复到 pg-recovered | **必须修改** `restore_command` 路径（见下方恢复步骤） |

### 4. receive-wal 启动需要时间

执行 `barman cron` 启动 WAL streaming 后，`barman check` 中的 `receive-wal running` 可能在 15-30 秒内显示 FAILED。这是正常的，等待后重试即可。

### 5. switch-wal --archive 经常超时

`barman switch-wal --force --archive` 会等待 30 秒让新 WAL 被归档。由于 streaming 延迟，经常超时报错。但这**不影响数据安全**——WAL 会在下一次 `barman cron` 时被处理。

建议的做法：
```bash
docker exec barman barman switch-wal --force streaming-backup-server
docker exec barman barman cron
sleep 10
docker exec barman barman cron
```

### 6. PG 重建后必须彻底清理 barman 服务器数据

如果 PG 被完全重建（新的 system identifier），barman 中旧的 WAL 归档文件名会与新 PG 的 WAL 冲突（都从 `000000010000000000000002` 开始）。仅删除备份和 `identity.json` 不够，旧 WAL 会被错误地用于恢复。

**必须删除整个服务器目录**：
```bash
docker exec barman rm -rf /var/lib/barman/streaming-backup-server
docker exec barman barman cron  # 让 barman 重建目录结构
docker exec barman barman receive-wal --create-slot streaming-backup-server
docker exec barman barman cron
# 等待 30 秒后验证
docker exec barman barman check streaming-backup-server
```

### 7. pgdata 目录无法直接删除

`pg/pgdata` 由容器内的 postgres 用户（UID 999）所有，宿主机普通用户无法直接 `rm -rf`。需要借助容器：

```bash
docker run --rm \
  -v $(pwd)/pg/pgdata:/data \
  postgres:17 bash -c "rm -rf /data/* /data/.*"
```

---

## 恢复场景 A：恢复到原 PG（pg/pgdata）

适用于：原 PG 数据丢失或损坏，需要在原位恢复并继续使用。

### 前置条件

- barman 容器正在运行且有可用备份
- 确认备份：`docker exec barman barman list-backups streaming-backup-server`

### 步骤

#### 1. 停止原 PG

```bash
cd pg
docker compose down
```

#### 2. 清空 pgdata

```bash
docker run --rm \
  -v $(pwd)/pgdata:/data \
  postgres:17 bash -c "rm -rf /data/* /data/.*"
```

#### 3. 确保 /recover 目录可被 barman 写入

```bash
cd ../barman

# 清空并修复权限
docker run --rm \
  -v $(pwd)/recover:/data \
  postgres:17 bash -c "rm -rf /data/* /data/.* 2>/dev/null; chown 102:103 /data"
```

#### 4. 执行 barman 恢复

```bash
docker exec barman barman recover streaming-backup-server latest /recover
```

验证输出中包含 `Your PostgreSQL server has been successfully prepared for recovery!`。

#### 5. 将恢复数据复制到 pgdata

```bash
docker run --rm \
  -v $(pwd)/recover:/src:ro \
  -v $(pwd)/../pg/pgdata:/dst \
  postgres:17 bash -c "cp -a /src/. /dst/ && chown -R postgres:postgres /dst/"
```

#### 6. 检查并修改 postgresql.auto.conf（如有必要）

```bash
docker run --rm \
  -v $(pwd)/../pg/pgdata:/data \
  postgres:17 cat /data/postgresql.auto.conf
```

如果存在 `restore_command`（通常 latest 恢复不会有），需要删除：

```bash
docker run --rm \
  -v $(pwd)/../pg/pgdata:/data \
  postgres:17 bash -c "
    sed -i '/^restore_command/d' /data/postgresql.auto.conf
    sed -i '/^recovery_end_command/d' /data/postgresql.auto.conf
  "
```

#### 7. 启动 PG

```bash
cd ../pg
docker compose up -d
```

#### 8. 验证恢复结果

```bash
# 等待启动完成
sleep 5

# 确认不在恢复模式
docker exec postgres psql -U postgres -c "SELECT pg_is_in_recovery()"
# 期望: f

# 验证数据
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"

# 验证可写
docker exec postgres psql -U postgres -c "SELECT 1"
```

#### 9. 恢复后重建 barman 备份链

原 PG 恢复后 system identifier 不变，barman 可以继续使用。但建议立即创建一个新的 base backup：

```bash
docker exec barman barman cron
# 等待 receive-wal 就绪（约 30 秒）
docker exec barman barman backup streaming-backup-server --wait
```

---

## 恢复场景 B：恢复到独立 PG 实例（pg-recovered）

适用于：验证备份、数据审计、PITR 到指定时间点。原 PG 可以继续运行不受影响。

### 前置条件

- barman 容器正在运行且有可用备份
- 确认备份：`docker exec barman barman list-backups streaming-backup-server`

### 步骤

#### 1. 清空 /recover 目录

```bash
cd barman

# 如果 pg-recovered 正在运行，先停止
docker compose --profile recovery stop pg-recovered 2>/dev/null

# 清空并修复权限为 barman 用户
docker run --rm \
  -v $(pwd)/recover:/data \
  postgres:17 bash -c "rm -rf /data/* /data/.* 2>/dev/null; chown 102:103 /data"
```

#### 2. 执行 barman 恢复

**恢复到最新状态**：

```bash
docker exec barman barman recover streaming-backup-server latest /recover
```

**或者 PITR 到指定时间点**：

```bash
# 将 <TIMESTAMP> 替换为目标时间，格式：2026-03-10 13:00:04.656887+00
docker exec barman barman recover \
  --target-time "<TIMESTAMP>" \
  --target-action=promote \
  streaming-backup-server latest /recover
```

> 提示：获取当前 PG 时间 `docker exec postgres psql -U postgres -t -A -c "SELECT now()"`

#### 3. 修复文件权限

```bash
docker run --rm \
  -v $(pwd)/recover:/data \
  postgres:17 chown -R postgres:postgres /data/
```

#### 4. 修改 restore_command 路径（PITR 时必须）

PITR 恢复会在 `postgresql.auto.conf` 中写入 barman 容器内的路径，需要修改为 pg-recovered 容器内的路径：

```bash
docker run --rm \
  -v $(pwd)/recover:/data \
  postgres:17 bash -c "
    sed -i 's|/recover/barman_wal|/var/lib/postgresql/data/barman_wal|g' /data/postgresql.auto.conf
    echo '--- postgresql.auto.conf ---'
    cat /data/postgresql.auto.conf
  "
```

确认输出中：
- `restore_command` 路径为 `/var/lib/postgresql/data/barman_wal/%f`
- `recovery_target_time` 为你指定的时间
- `recovery_target_action` 为 `promote`

> 注意：latest 恢复通常不会生成 `restore_command`，此步骤可跳过。

#### 5. 启动 pg-recovered

```bash
docker compose --profile recovery up -d pg-recovered
```

#### 6. 验证恢复结果

```bash
# 等待启动和恢复完成
sleep 8

# 确认已完成恢复并 promote
docker exec pg-recovered psql -U postgres -c "SELECT pg_is_in_recovery()"
# 期望: f

# 验证数据
docker exec pg-recovered psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
```

对于 PITR，还需验证时间点边界上的数据是否符合预期。

#### 7. 使用完毕后清理

```bash
docker compose --profile recovery stop pg-recovered

# 恢复 /recover 目录权限为 barman，为下次恢复做准备
docker run --rm \
  -v $(pwd)/recover:/data \
  postgres:17 bash -c "rm -rf /data/* /data/.* 2>/dev/null; chown 102:103 /data"
```

---

## 附录：常用 barman 命令速查

```bash
# 查看备份列表
docker exec barman barman list-backups streaming-backup-server

# 查看备份详情
docker exec barman barman show-backup streaming-backup-server latest

# 全面检查
docker exec barman barman check streaming-backup-server

# 手动触发 WAL 归档
docker exec barman barman switch-wal --force streaming-backup-server
docker exec barman barman cron

# 查看 streaming 状态
docker exec barman barman replication-status streaming-backup-server

# 手动创建备份
docker exec barman barman backup streaming-backup-server --wait
```
