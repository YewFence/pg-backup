# Barman 备份恢复指南

## 架构概览

```
pg/                         barman/
├── compose.yml             ├── compose.yml
├── (pg-data) ← PG 数据卷   ├── config/          ← barman 服务器配置
├── initdb/seed.sql         ├── (barman-data)     ← 备份命名卷
└── ...                     └── (barman-recover)  ← 本地恢复命名卷
```

Barman 通过 Tailscale 连接到 PG，备份数据保存在 `barman-data` 命名卷，恢复中转数据保存在 `barman-recover` 命名卷，`pg-recovered` 是本地恢复验证用的独立 PostgreSQL 实例，使用 `recovery` profile 启动并监听宿主机 `5433` 端口。

## 官方恢复建议

Barman 官方文档推荐用 `barman restore SERVER_NAME BACKUP_ID DESTINATION_PATH` 恢复备份，旧命令 `barman recover` 仍可用但已标记为废弃。没有 `--remote-ssh-command` 时属于本地恢复，恢复出的所有文件都会属于运行 Barman 的 `barman` 用户，所以如果后续用 `postgres` 用户启动 PostgreSQL，就必须先把恢复目录和表空间全部改成 `postgres` 所有。

当前 compose 采用更明确的做法，常驻 `barman` 服务只负责备份，恢复时用 `barman-restore` 一次性服务把备份恢复到 `barman-recover` 命名卷里的 `/recover`，然后用 `fix-recover-permissions` 修复成 `postgres` 权限，最后 `pg-recovered` 才启动。这样不需要宿主机 `./recover` 目录，也不会因为 bind mount ownership 变化制造奇怪问题。

## 注意事项

### PG host 必须是 Tailscale 可达入口

`barman` 服务使用 `network_mode: "service:tailscale"` 共享 Tailscale sidecar 的网络命名空间，所以不能假设 Docker Compose 里的 `postgres` 服务名一定能解析。`barman/config/streaming-backup-server.conf` 里的 `conninfo` 和 `streaming_conninfo`，都应该写成 PG 宿主机在 Tailscale 里的真实设备名、MagicDNS 名称或 Tailscale IP，例如 `fedora` 或 `100.x.y.z`。

PG 容器需要把 `5432` 暴露到 PG 宿主机，或者让 PostgreSQL 本身直接运行在 Tailscale 可达的网络上。可以先从 Barman 容器里验证连接。

```bash
docker exec barman psql -U barman -h <PG_TAILSCALE_HOST> postgres -c 'SELECT 1'
```

### 备份和恢复目录都使用命名卷

`barman-data` 保存 `/var/lib/barman`，`barman-recover` 保存 `/recover`，两者都由 Docker 管理。不要把 `/var/lib/barman` 或 `/recover` 改成宿主机 bind mount，除非你愿意额外处理宿主机文件 owner 和 SELinux 标签。

### 首次备份前需要有完整 WAL 段

刚创建备份链时，`barman cron` 会启动 streaming archiver，但如果 Barman 还没有收到任何完整 WAL 段，`barman backup streaming-backup-server --wait` 可能会被 `WAL archive` 检查拦住并提示 `Impossible to start the backup`。首次备份前可以先强制切换一次 WAL，再让 Barman 归档。

```bash
docker exec barman barman cron
sleep 30
docker exec barman barman check streaming-backup-server
docker exec barman barman switch-wal --force streaming-backup-server
docker exec barman barman cron
sleep 10
docker exec barman barman cron
docker exec barman barman check streaming-backup-server
```

首次备份前 `minimum redundancy requirements` 仍然会失败，因为还没有任何 base backup。只要 `WAL archive`、`PostgreSQL streaming` 和 `receive-wal running` 都是 `OK`，就可以继续创建第一份备份。

### 恢复前先删除恢复卷

`barman restore` 不应该覆盖已有 PostgreSQL 数据目录。每次恢复前先停止恢复相关容器并删除 `barman_barman-recover` 命名卷，下一次执行恢复时 Docker 会重新创建干净的恢复卷。

### 权限修复是固定步骤

本地恢复后文件属于 `barman`，而 `postgres:17` 容器用 `postgres` 用户启动数据库。每次恢复完成后都显式运行 `fix-recover-permissions`，这样不会复用上一次已经退出的一次性容器。

```bash
docker compose --profile recovery run --rm fix-recover-permissions
```

### PITR 恢复时 restore_command 自动生效

Barman 执行 PITR 恢复时会把 WAL 文件放在 `/recover/barman_wal/` 并写入 `restore_command = 'cp /recover/barman_wal/%f %p'`，`pg-recovered` 把同一个命名卷同时挂载到 `/var/lib/postgresql/data` 和只读 `/recover`，所以路径会自动匹配。

PITR 完成时可能看到 `recovery_end_command "rm -fr /recover/barman_wal"` 报 `Read-only file system`，这是因为 `pg-recovered` 把 `/recover` 按只读方式挂载。只要 `pg_is_in_recovery()` 返回 `f`，并且目标时间前后的业务数据符合预期，这条日志不代表恢复失败。

### 恢复实例会禁用归档命令

Barman restore 会把恢复目录里的危险归档设置改掉，常见日志是 `archive_command = false` 或恢复实例里出现 `archive command failed`。这是为了避免恢复验证实例继续向原归档链写入 WAL，本地验证时可以忽略。如果要把恢复结果作为长期运行的 PostgreSQL 实例，需要在验证通过后重新配置归档命令。

### PG 重建后必须清理 Barman 服务目录

如果 PG 被完全重建并产生新的 system identifier，Barman 中旧 WAL 会与新 PG 冲突。测试环境里可以直接删除这个服务器目录后重建备份链。

```bash
docker exec barman rm -rf /var/lib/barman/streaming-backup-server
docker exec barman barman cron
docker exec barman barman receive-wal --create-slot streaming-backup-server
docker exec barman barman cron
sleep 30
docker exec barman barman check streaming-backup-server
```

## 本地恢复流程

### 1. 确认可用备份

```bash
cd barman
docker exec barman barman list-backups streaming-backup-server
docker exec barman barman check streaming-backup-server
```

### 2. 停止验证实例并删除恢复卷

```bash
docker compose --profile recovery stop pg-recovered 2>/dev/null || true
docker compose --profile recovery rm -sf pg-recovered fix-recover-permissions barman-restore
docker volume rm barman_barman-recover 2>/dev/null || true
```

### 3. 恢复最新备份

```bash
docker compose --profile recovery run --rm barman-restore \
  barman restore streaming-backup-server latest /recover
```

### 4. 或者恢复到指定时间点

```bash
docker compose --profile recovery run --rm barman-restore \
  barman restore \
  --target-time "<TIMESTAMP>" \
  --target-action=promote \
  streaming-backup-server latest /recover
```

时间戳可以从当前 PG 获取。

```bash
docker exec postgres psql -U postgres -t -A -c "SELECT now()"
```

### 5. 修复权限并启动本地恢复实例

```bash
docker compose --profile recovery run --rm fix-recover-permissions
docker compose --profile recovery up -d pg-recovered
```

### 6. 验证恢复结果

```bash
sleep 8
docker exec pg-recovered psql -U postgres -c "SELECT pg_is_in_recovery()"
docker exec pg-recovered psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
```

`pg_is_in_recovery()` 期望返回 `f`。PITR 场景下，再验证目标时间前后的业务数据是否符合预期。

### 7. 清理本地恢复实例

```bash
docker compose --profile recovery stop pg-recovered
docker compose --profile recovery rm -sf pg-recovered fix-recover-permissions barman-restore
docker volume rm barman_barman-recover 2>/dev/null || true
```

## 把本地恢复结果交给远端

主流程只负责恢复到本地命名卷并通过 `pg-recovered` 验证。需要迁移到远端时，建议在确认本地验证通过后，再把 `barman-recover` 命名卷内容导出到临时目录或归档文件，并在远端导入目标 PostgreSQL 数据卷，导入后同样确保文件 owner 是远端 PostgreSQL 容器里的 `postgres` 用户。

导出必须在删除 `barman_barman-recover` 之前完成。可以先停止 `pg-recovered`，保留恢复卷，导出成功后再按清理步骤删除恢复容器和恢复卷。

一个简单方式是在 Barman 宿主机导出归档。

```bash
docker run --rm \
  -v barman_barman-recover:/src:ro \
  -v "$PWD":/out \
  postgres:17 bash -c "cd /src && tar -cf /out/pg-recover.tar ."
```

远端导入时先停止 PostgreSQL，再清空目标数据卷、解包、修复权限并启动。远端 compose 项目名不同的话，数据卷名也会不同，需要按实际项目名替换。

```bash
docker compose down
docker run --rm \
  -v pg_pg-data:/dst \
  -v "$PWD":/in:ro \
  postgres:17 bash -c "find /dst -mindepth 1 -delete && tar -xf /in/pg-recover.tar -C /dst && chown -R postgres:postgres /dst && find /dst -type d -exec chmod 700 {} +"
docker compose up -d
```

## 常用命令

```bash
# 查看备份列表
docker exec barman barman list-backups streaming-backup-server

# 查看备份详情
docker exec barman barman show-backup streaming-backup-server latest

# 全面检查
docker exec barman barman check streaming-backup-server

# 手动触发 WAL 切换和归档维护
docker exec barman barman switch-wal --force streaming-backup-server
docker exec barman barman cron

# 查看 streaming 状态
docker exec barman barman replication-status streaming-backup-server

# 手动创建备份
docker exec barman barman backup streaming-backup-server --wait
```

## 故障排查

### receive-wal running 短暂失败

刚启动后 `barman check` 中的 `receive-wal running` 可能短暂显示 FAILED，等待 15 到 30 秒后重试即可。如果持续失败，检查 Barman 日志。

```bash
docker exec barman tail -20 /var/log/barman/barman.log
```

### WAL system identifier mismatch

这通常是 PG 被重建过，但 Barman 还保留旧实例 WAL。测试环境可以删除 `/var/lib/barman/streaming-backup-server` 后重新创建复制槽和 base backup。

### could not locate required checkpoint record

检查是否恢复到了空目录、是否缺少 PITR 需要的 WAL、`postgresql.auto.conf` 中的 `restore_command` 是否指向 `/recover/barman_wal/%f`，以及 `pg-recovered` 是否已经挂载同一个 `barman-recover` 命名卷到 `/recover:ro`。

### Permission denied

先停掉 `pg-recovered`，再重新执行权限修复服务。

```bash
docker compose --profile recovery stop pg-recovered
docker compose --profile recovery run --rm fix-recover-permissions
docker compose --profile recovery up -d pg-recovered
```
