# Barman 备份恢复指南

## 架构概览

```
pg/                         barman/
├── compose.yml             ├── compose.yml
├── pgdata/  ← PG 数据      ├── config/          ← barman 服务器配置
├── initdb/seed.sql         ├── recover/         ← 恢复中转目录
└── ...                     └── (barman-data)     ← 命名卷，barman 自管
```

- barman 通过 Tailscale 连接到 PG（host: `wsl-yew-branch`）
- barman 和 postgres 容器内均使用 **UID 999** 运行，共享文件无需 chown
- `pg-recovered` 是恢复验证用的独立 PG 实例（profile: `recovery`，端口 5433）

---

## 注意事项

### 1. barman-data 使用命名卷

barman 的备份数据（`/var/lib/barman`）使用 Docker 命名卷 `barman-data`，由 Docker 管理权限。避免使用 bind mount，否则容器重建时目录 ownership 可能变为 root，导致 crash loop。

### 2. receive-wal 启动需要 15-30 秒

执行 `barman cron` 启动 WAL streaming 后，`barman check` 中的 `receive-wal running` 可能短暂显示 FAILED。等待后重试即可。

### 3. switch-wal --archive 经常超时

`barman switch-wal --force --archive` 默认等待 30 秒。由于 streaming 延迟，经常超时报错，但 WAL 会在下一次 `barman cron` 时被处理，**不影响数据安全**。

建议的做法：

```bash
docker exec barman barman switch-wal --force streaming-backup-server
docker exec barman barman cron
sleep 10
docker exec barman barman cron
```

### 4. PG 重建后必须彻底清理 barman 数据

如果 PG 被完全重建（新的 system identifier），barman 中旧的 WAL 文件名会与新 PG 冲突。**必须删除整个服务器目录**后重建：

```bash
docker exec barman rm -rf /var/lib/barman/streaming-backup-server
docker exec barman barman cron                                        # 重建目录结构
docker exec barman barman receive-wal --create-slot streaming-backup-server
docker exec barman barman cron
sleep 30
docker exec barman barman check streaming-backup-server               # 验证全部 OK
```

### 5. PITR 恢复时 restore_command 自动生效

barman 执行 PITR 恢复时，会将 WAL 文件放在 `/recover/barman_wal/` 并写入：

```
restore_command = 'cp /recover/barman_wal/%f %p'
```

pg-recovered 的 compose 配置中已额外挂载 `./recover:/recover:ro`，使该路径在容器内直接可用，无需手动修改。

远程恢复场景中，远端 PG 的 compose.yml 也建议额外挂载 `./pgdata:/recover:ro`，同样无需改 conf。

---

## 恢复场景 A：恢复到独立 PG 实例（pg-recovered）

适用于：验证备份完整性、数据审计、PITR 到指定时间点。原 PG 不受影响。

### 前置条件

- barman 容器正在运行
- 确认有可用备份：`docker exec barman barman list-backups streaming-backup-server`

### 步骤

#### 1. 停止旧实例（如有）并清空 /recover

```bash
cd barman
docker compose --profile recovery stop pg-recovered 2>/dev/null
docker exec barman bash -c "rm -rf /recover/* /recover/.*"
```

#### 2. 执行恢复

**恢复到最新状态**：

```bash
docker exec barman barman recover streaming-backup-server latest /recover
```

**或者 PITR 到指定时间点**：

```bash
docker exec barman barman recover \
  --target-time "<TIMESTAMP>" \
  --target-action=promote \
  streaming-backup-server latest /recover
```

> 时间戳格式示例：`2026-03-10 13:00:04.656887+00`
>
> 获取当前 PG 时间：`docker exec postgres psql -U postgres -t -A -c "SELECT now()"`

#### 3. 启动 pg-recovered

```bash
docker compose --profile recovery up -d pg-recovered
```

> PITR 恢复的 `restore_command` 指向 `/recover/barman_wal/`，pg-recovered 已挂载 `./recover:/recover:ro`，路径自动匹配。

#### 4. 验证恢复结果

```bash
sleep 8

# 确认已完成恢复
docker exec pg-recovered psql -U postgres -c "SELECT pg_is_in_recovery()"
# 期望: f

# 验证数据
docker exec pg-recovered psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
```

PITR 场景下，验证时间边界前后的数据是否符合预期。

#### 5. 使用完毕后清理

```bash
docker compose --profile recovery stop pg-recovered
docker exec barman bash -c "rm -rf /recover/* /recover/.*"
```
---

## 恢复场景 B：本地恢复 + rsync 到远端 PG 服务器

适用于：barman 和 PG 在不同机器上（通过 Tailscale 连接），需要恢复到远端 PG 服务器。

策略：先在 barman 本地恢复到 `/recover`，再通过宿主机 rsync（走 Tailscale SSH）传到远端。无需在 barman 容器内安装 SSH 或管理密钥。

### 前置条件

- barman 容器正在运行
- 确认有可用备份：`docker exec barman barman list-backups streaming-backup-server`
- barman 宿主机已加入 Tailscale，能 SSH 到远端
- 远端 PG 已停止

### 远端 PG compose.yml 建议配置

为了让 PITR 恢复时 `restore_command` 路径自动生效，远端 PG 的 compose.yml 建议额外挂载一个 `/recover:ro`：

```yaml
services:
  postgres:
    image: postgres:17
    volumes:
      - ./pgdata:/var/lib/postgresql/data
      - ./pgdata:/recover:ro          # PITR 时 restore_command 指向 /recover/barman_wal/
```

这样无论 latest 还是 PITR 恢复，rsync 过去后直接启动即可，无需修改任何配置文件。

### 步骤

#### 1. 本地恢复到 /recover

```bash
cd barman
docker exec barman bash -c "rm -rf /recover/* /recover/.*"
```

**恢复到最新状态**：

```bash
docker exec barman barman recover streaming-backup-server latest /recover
```

**或者 PITR 到指定时间点**：

```bash
docker exec barman barman recover \
  --target-time "<TIMESTAMP>" \
  --target-action=promote \
  streaming-backup-server latest /recover
```

> 时间戳格式示例：`2026-03-10 13:00:04.656887+00`

#### 2.（可选）本地验证

恢复完成后可以先用 pg-recovered 在本地验证数据完整性，确认无误后再传到远端：

```bash
docker compose --profile recovery up -d pg-recovered
sleep 8
docker exec pg-recovered psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
docker compose --profile recovery stop pg-recovered
```

#### 3. rsync 到远端

从 barman **宿主机**执行（`./recover` 是 bind mount，宿主机可直接访问）：

```bash
rsync -avz --delete ./recover/ <user>@<ts-hostname>:/path/to/pg/pgdata/
```

> - `<ts-hostname>` 是远端机器的 Tailscale hostname
> - Tailscale SSH 无需管理密钥，宿主机已加入 Tailscale 即可直接连接
> - `--delete` 确保远端 pgdata 与本地恢复结果完全一致

#### 4. 启动远端 PG 并验证

```bash
# 在远端机器上
cd /path/to/pg
docker compose up -d
sleep 5

docker exec postgres psql -U postgres -c "SELECT pg_is_in_recovery()"
# 期望: f

docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
```

#### 5. 恢复后重建备份链

PG 恢复后 system identifier 不变，barman 可以继续工作。建议立即创建新的 base backup：

```bash
# 在 barman 机器上
docker exec barman barman cron
sleep 30
docker exec barman barman backup streaming-backup-server --wait
```

#### 6. 清理 barman 本地 /recover

```bash
docker exec barman bash -c "rm -rf /recover/* /recover/.*"
```

---

## 附录：常用 barman 命令

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
