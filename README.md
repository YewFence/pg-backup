# pg-backup

> 这是一个**测试项目**，用于测试 [Barman](https://pgbarman.org/) 对 PostgreSQL 数据库的备份与恢复功能。
> Barman 客户端通过 Tailscale 组网连接到 PostgreSQL 服务器。
> 此处我们假设宿主机 + pg/ 与 barman/ 目录在不同机器上，且宿主机已安装并登录 Tailscale 作为 Tailscale 节点，barman 使用 Tailscale Docker 作为 Tailscale 节点。

## 项目结构

```
.
├── pg/                         # PostgreSQL 17 数据库（被备份的目标）
│   ├── compose.yml
│   ├── postgresql.conf
│   ├── pg_hba.conf
│   ├── initdb/
│   ├── Dockerfile.cloud        # barman-cloud sidecar 镜像
│   ├── cloud-crontab.example   # sidecar 定时任务模板
│   └── scripts/                # sidecar 脚本
│       ├── wal-push.sh         # WAL 推送到 S3
│       ├── cloud-backup.sh     # 基础备份到 S3
│       ├── cloud-backup-delete.sh  # 清理旧备份
│       └── sidecar-entrypoint.sh   # sidecar 入口
├── barman/                     # Barman 客户端（备份工具）
│   ├── Dockerfile              # barman 用户 UID/GID 统一为 999
│   ├── docker-compose.yml      # Barman + Tailscale sidecar
│   ├── setup-pgpass.sh         # 配置数据库密码
│   ├── health-check.py         # 健康检查 HTTP 服务
│   ├── entrypoint.sh           # 容器启动脚本
│   ├── config/                 # 挂载到 /etc/barman.d
│   │   ├── streaming-backup-server.conf
│   │   └── barman.crontab      # 定时任务配置
│   ├── recover/                # 本地恢复中转目录
│   ├── RECOVERY-GUIDE.md       # 详细恢复指南
│   ├── E2E-TEST.md             # 端到端测试流程
│   └── .env                    # Tailscale auth key（不提交）
└── README.md
```

**重要说明**：
- barman 容器内的 barman 用户 UID/GID 为 999，与 postgres:17 镜像中的 postgres 用户一致
- 这样无论是本地恢复（pg-recovered）还是 rsync 远程恢复，文件权限都自动正确，无需 chown

## 快速开始

### 1. 启动 PostgreSQL

```bash
cd pg
docker compose up -d
```

### 2. 启动 Barman

在 `barman/` 目录下创建 `.env` 文件：

```bash
cp .env.example .env
vim .env
```

> 获取 Tailscale auth key: https://login.tailscale.com/admin/settings/keys

编辑连接配置文件
```bash
cp config/streaming-backup-server.conf.example config/streaming-backup-server.conf
vim config/streaming-backup-server.conf
```

配置定时任务：
```bash
cp config/barman.crontab.example config/barman.crontab
vim config/barman.crontab
```

启动 Barman：

```bash
cd barman
docker compose up -d
```

Barman 容器会加入你的 Tailscale 网络，通过宿主机的 Tailscale hostname 连接 PostgreSQL。

## 常用命令

### 容器管理

```bash
# 进入 psql
docker exec -it postgres psql -U postgres

# 进入 Barman 容器
docker exec -it barman bash

# 查看 Barman 日志
docker logs -f barman

# 查看 cron 任务状态
docker exec barman crontab -l

# 手动测试 Posrgres 连接
docker exec barman psql -c 'SELECT version()' -U barman -h pg-host postgres
```

### Barman 操作

```bash
# 测试 Barman 连接
docker exec barman barman check streaming-backup-server

# 手动创建备份
docker exec barman barman backup streaming-backup-server

# 查看备份列表
docker exec barman barman list-backups streaming-backup-server

# 查看服务器状态
docker exec barman barman status streaming-backup-server

# 查看 replication 状态
docker exec barman barman replication-status streaming-backup-server

# 恢复到指定时间点（示例）
docker exec barman barman recover streaming-backup-server latest /var/lib/barman/recover --target-time "2026-03-10 12:00:00"
```

### 异地恢复验证

在 Barman 本机恢复备份并启动一个临时 PG 来验证数据完整性。

```bash
# 1. 恢复最新备份到共享卷
docker exec barman barman recover streaming-backup-server latest /recover

# 2. 拉起验证用 PG（端口 5433，profile 控制，平时不启动）
docker compose --profile recovery up -d pg-recovered

# 3. 验证数据
docker exec pg-recovered psql -U postgres -c '\dt'

# 从 barman 容器直连（都在 barman-net 上）
docker exec barman psql -h pg-recovered -U postgres -c "SELECT count(*) FROM your_table;"

# 或从宿主机
psql -h localhost -p 5433 -U postgres

# 4. 验证完毕，关掉验证 PG
docker compose --profile recovery down

# 如需重新恢复，先清理卷再重来
docker volume rm barman_pg-recover
```

> PITR（恢复到指定时间点）：在 recover 命令后加 `--target-time "2026-03-10 14:30:00"`

### PostgreSQL 配置导出

```bash
# 导出 PG 默认配置
docker exec postgres cat /var/lib/postgresql/data/postgresql.conf > pg/postgresql.conf
docker exec postgres cat /var/lib/postgresql/data/pg_hba.conf > pg/pg_hba.conf
```

## 定时任务说明

> **重要：** 如果不配置 `barman.crontab`，Barman 容器会正常运行但**不会自动备份**，也不会报错。请在部署后确认定时任务已正确配置。

Barman 容器内运行 cron 守护进程，定时任务配置文件位于 `barman/config/barman.crontab`。

默认任务（`barman.crontab.example`）：
- 每分钟执行 `barman cron`：归档 WAL 文件、清理过期备份
- 每天凌晨 2 点执行 `barman backup`：创建完整的基础备份
- 每周日凌晨 3 点执行 `barman verify-backup`：验证备份完整性

修改定时任务后需要重启容器：
```bash
cd barman
docker compose restart barman
```

## 健康检查

Barman 容器内置 HTTP 健康检查服务，定时运行 `barman check` 并缓存结果，适合外部监控系统（如 UptimeFlare）pull 使用。

> **注意：** `barman check` 本身执行很慢（约 30 秒），且容易超时。这里的设计是异步缓存结果，HTTP 端点只返回缓存，响应是即时的。
> 但**容器首次启动时**，第一次 check 尚未完成，端点会返回 503 直到首次检查结束（~30s）。
> 监控系统建议配置：超时 ≥ 60s，失败重试 ≥ 3 次后再告警，避免误报。

端点：`http://<barman-tailscale-ip>:8000/`
- `200` — 备份状态正常
- `503` — 检查失败 / 结果过期 / 首次检查未完成

环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HEALTH_CHECK_PORT` | `8000` | HTTP 监听端口 |
| `CHECK_INTERVAL` | `300` | 检查间隔（秒） |
| `FAIL_THRESHOLD` | `3` | 连续失败多少次后才标记为异常 |
| `BARMAN_SERVER_NAME` | `streaming-backup-server` | barman 服务器名 |

手动测试：
```bash
curl http://<barman-tailscale-ip>:8000/
```

## S3 云备份（barman-cloud sidecar）

除了 Barman 流复制备份，PG_HOST 端还可以通过 barman-cloud sidecar 将 WAL 和基础备份直接推送到 S3 兼容存储，作为不依赖 Barman 机器的异地容灾方案。

**架构**：PG 容器保持原版 `postgres:17` 不修改，通过 `archive_command` 将 WAL 文件复制到共享卷，sidecar 容器定时扫描共享卷并上传到 S3。

```
PG 容器                                Sidecar 容器 (profile: cloud)
┌──────────────────────┐               ┌──────────────────────────┐
│ archive_command:     │  共享卷        │ barman-cli-cloud         │
│   cp %p /archive/%f ─┼──→ /archive ──┼→ wal-push.sh ────────────┼──→ S3
│                      │               │  cloud-backup.sh ────────┼──→ S3
│                      │  Docker 内网   │  cloud-backup-delete.sh ─┼──→ S3
│                      │◄──────────────┤   (复制协议连接 PG)       │
└──────────────────────┘               └──────────────────────────┘
```

> `S3_BUCKET_URL` 未配置时，`archive_command` 不会往共享卷写入任何文件，行为与不启用云备份完全一致。

### 启用云备份

1. 配置 S3 凭证（编辑 `pg/.env`）：

```bash
S3_BUCKET_URL=s3://your-bucket/your-prefix
S3_ENDPOINT_URL=https://your-endpoint    # AWS S3 留空
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
```

2. 创建定时任务配置：

```bash
cd pg
cp cloud-crontab.example cloud-crontab
vim cloud-crontab  # 按需调整调度时间
```

3. 构建并启动 sidecar：

```bash
cd pg
docker compose --profile cloud up -d --build
```

### 云备份常用命令

```bash
# 手动推送 WAL 文件
docker exec barman-cloud /usr/local/bin/wal-push.sh

# 手动创建基础备份
docker exec barman-cloud /usr/local/bin/cloud-backup.sh

# 查看 S3 中的备份列表
docker exec barman-cloud barman-cloud-backup-list \
    --cloud-provider aws-s3 \
    --endpoint-url "$S3_ENDPOINT_URL" \
    "$S3_BUCKET_URL" pg

# 查看 sidecar 日志
docker logs -f barman-cloud

# 手动清理旧备份
docker exec barman-cloud /usr/local/bin/cloud-backup-delete.sh
```

### 云备份环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `S3_BUCKET_URL` | （空） | S3 目标地址，如 `s3://my-bucket/pg-backup` |
| `S3_ENDPOINT_URL` | （空） | S3 兼容服务端点（AWS S3 留空） |
| `AWS_ACCESS_KEY_ID` | （空） | AWS 访问密钥 |
| `AWS_SECRET_ACCESS_KEY` | （空） | AWS 密钥 |
| `BARMAN_CLOUD_SERVER_NAME` | `pg` | S3 路径中的服务器名 |
| `BARMAN_CLOUD_COMPRESSION` | `gzip` | 压缩算法：gzip / bzip2 / snappy |
| `BARMAN_CLOUD_RETENTION` | `RECOVERY WINDOW OF 7 DAYS` | 备份保留策略 |
| `BARMAN_CLOUD_MIN_REDUNDANCY` | `1` | 最少保留备份数 |
