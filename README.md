# pg-backup

> 这是一个**测试项目**，用于测试 [Barman](https://pgbarman.org/) 对 PostgreSQL 数据库的备份与恢复功能。
> Barman 客户端通过 Tailscale 组网连接到 PostgreSQL 服务器。

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
