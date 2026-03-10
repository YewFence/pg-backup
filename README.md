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
│   ├── config/                 # 挂载到 /etc/barman.d
│   │   └── streaming-backup-server.conf
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
启动 Barman：

```bash
cd barman
docker compose up -d
```

Barman 容器会加入你的 Tailscale 网络，通过宿主机的 Tailscale hostname 连接 PostgreSQL。

## 常用命令

```bash
# 进入 psql
docker exec -it postgres psql -U postgres

# 进入 Barman 容器
docker exec -it barman bash

# 手动测试 Barman 连接
docker exec -it barman psql -c 'SELECT version()' -U barman -h <tailscale-hostname> postgres

# 使用 Barman 测试连接
docker exec -it barman barman check streaming-backup-server

# 导出 PG 默认配置
docker exec postgres cat /var/lib/postgresql/data/postgresql.conf > pg/postgresql.conf
docker exec postgres cat /var/lib/postgresql/data/pg_hba.conf > pg/pg_hba.conf
```
