# pg-backup

> 这是一个**测试项目**，用于测试 [Barman](https://pgbarman.org/) 对 PostgreSQL 数据库的备份与恢复功能。
> Barman 客户端通过 Tailscale 组网连接到 PostgreSQL 服务器。
> 此处我们假设宿主机 + pg/ 与 barman/ 目录在不同机器上，且宿主机已安装并登录 Tailscale 作为 Tailscale 节点，barman 使用 Tailscale Docker 作为 Tailscale 节点。

## 项目结构

```
.
├── pg/                         # PostgreSQL 17 数据库（被备份的目标）
│   ├── docker-compose.yml
│   ├── postgresql.conf
│   ├── pg_hba.conf
│   └── initdb/
├── barman/                     # Barman 客户端（备份工具）
│   ├── Dockerfile
│   ├── docker-compose.yml      # Barman + Tailscale sidecar
│   ├── setup-pgpass.sh         # 配置数据库密码
│   ├── health-check.py         # 健康检查 HTTP 服务
│   ├── entrypoint.sh           # 容器启动脚本
│   ├── config/                 # 挂载到 /etc/barman.d
│   │   ├── streaming-backup-server.conf
│   │   └── barman.crontab      # 定时任务配置
│   └── .env                    # Tailscale auth key（不提交）
└── README.md
```

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
docker exec barman psql -c 'SELECT version()' -U barman -h wsl-yew-branch postgres
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

Barman 容器内运行 cron 守护进程，定时任务配置文件位于 `barman/config/barman.crontab`。

默认任务：
- 每分钟执行 `barman cron`：归档 WAL 文件、清理过期备份
- 每天凌晨 2 点执行 `barman backup`：创建完整的基础备份

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
