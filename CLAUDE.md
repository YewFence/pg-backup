# CLAUDE.md

这是一个测试项目，用于测试 Barman 备份 PostgreSQL 数据库。

## 架构

- `pg/` 目录运行 PostgreSQL 17，是需要被备份的数据库
- `barman/` 目录包含 Barman 客户端的所有配置，通过 Tailscale 组网连接到 PG
  - `barman/docker-compose.yml` - Barman 服务 + Tailscale sidecar
  - `barman/config/` - Barman 服务器配置文件，挂载到容器的 `/etc/barman.d`
  - `barman/Dockerfile` - 基于 debian:bookworm-slim，安装 barman + postgresql-client-17

## 常用命令

```bash
# 启动 PG
cd pg && docker compose up -d

# 启动 Barman（在 barman/ 目录下）
cd barman && docker compose up -d

# 进入 PG
docker exec -it postgres psql -U postgres

# 进入 Barman
docker exec -it barman bash

# 测试 Barman 连接
docker exec -it barman psql -c 'SELECT version()' -U barman -h <tailscale-hostname> postgres
```
