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


#### 4. 检查 postgresql.auto.conf

```bash
docker run --rm -v $(pwd)/pgdata:/data postgres:17 cat /data/postgresql.auto.conf
```

latest 恢复通常不包含 `restore_command`。如果存在，需要删除（postgres:17 镜像内没有 `barman-wal-restore`）：

```bash
docker run --rm -v $(pwd)/pgdata:/data postgres:17 bash -c "
  sed -i '/^restore_command/d' /data/postgresql.auto.conf
  sed -i '/^recovery_end_command/d' /data/postgresql.auto.conf
"
```

#### 5. 启动 PG 并验证

```bash
docker compose up -d
sleep 5

# 确认不在恢复模式
docker exec postgres psql -U postgres -c "SELECT pg_is_in_recovery()"
# 期望: f

# 验证数据完整
docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
```

#### 6. 恢复后重建备份链

PG 恢复后 system identifier 不变，barman 可以继续工作。建议立即创建新的 base backup：

```bash
docker exec barman barman cron
sleep 30  # 等待 receive-wal 就绪
docker exec barman barman backup streaming-backup-server --wait
```

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

## 恢复场景 B：通过 SSH 远程恢复到异地 PG 服务器

适用于：barman 和 PG 在不同机器上（通过 Tailscale 连接），需要直接恢复到远程 PG 服务器。

### 前置条件

- barman 容器正在运行
- 确认有可用备份：`docker exec barman barman list-backups streaming-backup-server`
- 远程机器上已配置 SSH 访问（建议使用 UID 999 的用户，与 postgres 容器一致）
- 远程机器上 PG 容器已停止

### SSH 用户配置

在远程机器（PG 所在机器）上创建 UID 999 的恢复用户：

```bash
# 在远程机器上执行
sudo useradd -u 999 -m -s /bin/bash pgrecovery
sudo mkdir -p /home/pgrecovery/.ssh
sudo cp ~/.ssh/authorized_keys /home/pgrecovery/.ssh/
sudo chown -R pgrecovery:pgrecovery /home/pgrecovery/.ssh
sudo chmod 700 /home/pgrecovery/.ssh
sudo chmod 600 /home/pgrecovery/.ssh/authorized_keys
```

在 barman 容器中配置 SSH 密钥：

```bash
# 在 barman 容器内生成密钥（如果还没有）
docker exec -u barman barman ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""

# 查看公钥
docker exec -u barman barman cat ~/.ssh/id_ed25519.pub
# 将公钥添加到远程机器的 /home/pgrecovery/.ssh/authorized_keys
```

### 步骤

#### 1. 停止远程 PG 并清空 pgdata

```bash
# 在远程机器上
cd /path/to/pg
docker compose down
sudo rm -rf pgdata/*
```

#### 2. 执行远程恢复

**恢复到最新状态**：

```bash
docker exec barman barman recover \
  --remote-ssh-command "ssh pgrecovery@<remote-host>" \
  streaming-backup-server latest /path/to/pgdata
```

**或者 PITR 到指定时间点**：

```bash
docker exec barman barman recover \
  --remote-ssh-command "ssh pgrecovery@<remote-host>" \
  --target-time "<TIMESTAMP>" \
  --target-action=promote \
  streaming-backup-server latest /path/to/pgdata
```

**使用 delta-restore 增量恢复**（如果 pgdata 已有部分数据）：

```bash
docker exec barman barman recover \
  --remote-ssh-command "ssh pgrecovery@<remote-host>" \
  --delta-restore \
  streaming-backup-server latest /path/to/pgdata
```

> 注意：
> - `<remote-host>` 替换为远程机器的 Tailscale hostname 或 IP
> - `/path/to/pgdata` 是远程机器上的绝对路径
> - barman 会通过 SSH 在远程机器上以 pgrecovery 用户（UID 999）执行恢复
> - 恢复的文件自动拥有正确的权限（UID 999），postgres 容器可以直接使用

#### 3. 检查 postgresql.auto.conf（在远程机器上）

```bash
# 在远程机器上
cat /path/to/pgdata/postgresql.auto.conf
```

latest 恢复通常不包含 `restore_command`。如果是 PITR 恢复，会包含 `restore_command`，但路径可能不正确（指向 barman 机器的路径）。需要删除或修改：

```bash
# 在远程机器上
sed -i '/^restore_command/d' /path/to/pgdata/postgresql.auto.conf
sed -i '/^recovery_end_command/d' /path/to/pgdata/postgresql.auto.conf
```

> 对于 PITR 恢复，如果需要 `restore_command`，可以配置 barman-wal-restore 通过网络获取 WAL，或者在恢复前确保所有需要的 WAL 都已包含在备份中。

#### 4. 启动远程 PG 并验证

```bash
# 在远程机器上
cd /path/to/pg
docker compose up -d
sleep 5

# 验证
docker exec postgres psql -U postgres -c "SELECT pg_is_in_recovery()"
# 期望: f

docker exec postgres psql -U postgres -c "
  SELECT 'users' as tbl, count(*) FROM users
  UNION ALL SELECT 'products', count(*) FROM products
  UNION ALL SELECT 'orders', count(*) FROM orders;
"
```

#### 5. 恢复后重建备份链

```bash
# 在 barman 机器上
docker exec barman barman cron
sleep 30
docker exec barman barman backup streaming-backup-server --wait
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
