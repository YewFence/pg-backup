# CLAUDE.md

这是一个**测试项目**和**模板项目**，用于测试 Barman 备份 PostgreSQL 数据库。

**重要说明：**
- 这是测试环境，不需要考虑数据安全
- 所有配置和脚本仅供学习和测试使用

## 架构

### 网络拓扑假设

- `pg/` 和宿主机视为**同一台主机**（本地环境）
- `barman/` 视为**异地主机**（远程备份服务器）
- 两者仅通过 **Tailscale** 进行通信

### 目录说明

- `pg/` - PostgreSQL 17 测试实例，是需要被备份的数据库
- `barman/` - Barman 备份服务器配置，通过 Tailscale 组网连接到 PG
  - `barman/docker-compose.yml` - Barman 服务 + Tailscale sidecar
  - `barman/config/` - Barman 服务器配置文件，挂载到容器的 `/etc/barman.d`
  - `barman/Dockerfile` - 基于 debian:bookworm-slim，安装 barman + postgresql-client-17
- `pg-prod/` - **生产环境 PostgreSQL 模板**，基于 `pg/` 扩展而来，包含性能优化和健康检查等生产级特性

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
