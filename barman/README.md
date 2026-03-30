# Barman 备份服务

基于 [Barman](https://pgbarman.org/) 的 PostgreSQL 流复制备份服务，通过 Tailscale 组网连接 PG 服务器。

## 配置文件

部署前需要配置两个文件（`just barman-install` 会自动从 example 复制）：

| 文件 | 用途 | 必须修改 |
|------|------|----------|
| `config/streaming-backup-server.conf` | Barman 连接 PG 的配置（主机地址、复制参数） | host 地址 |
| `config/barman.crontab` | 定时任务：WAL 归档、基础备份、备份验证 | 按需调整时间 |

### barman.crontab 说明

这个文件控制 Barman 的自动备份行为，**如果不配置，备份服务会静默运行但不做任何备份**。

默认启用的任务（来自 example）：

```crontab
# 每分钟：WAL 归档 + 清理过期备份（必须开启）
* * * * * barman -q cron

# 每天凌晨 2 点：创建完整基础备份
0 2 * * * barman backup streaming-backup-server

# 每周日凌晨 3 点：验证最新备份完整性
0 3 * * 0 barman verify-backup streaming-backup-server latest
```

修改后需要重启容器生效：

```bash
docker compose restart barman
```

## 基本用法

### 启动服务

```bash
# 启动，会构建镜像并运行容器
docker compose up -d
```

> 特殊构建：
> 如果需要在构建时修改参数，可以注释掉 `docker-compose.yml` 中的 `build` 部分，直接使用 `docker build` 构建镜像：
> 例如使用代理构建：
> ```bash
> docker build \--add-host=host.docker.internal:host-gateway \
    --build-arg HTTP_PROXY="http://host.docker.internal:7890" \
    --build-arg HTTPS_PROXY="http://host.docker.internal:7890" -t barman:latest .
> ```

### 验证连接

```bash
docker exec barman barman check streaming-backup-server
```

### 手动操作

```bash
# 创建备份
docker exec barman barman backup streaming-backup-server

# 查看备份列表
docker exec barman barman list-backups streaming-backup-server

# 恢复最新备份
docker exec barman barman recover streaming-backup-server latest /recover

# PITR 恢复到指定时间点
docker exec barman barman recover \
  --target-time "2026-03-10 12:00:00" \
  --target-action=promote \
  streaming-backup-server latest /recover
```

### 查看状态

```bash
# 服务器状态
docker exec barman barman status streaming-backup-server

# 复制状态
docker exec barman barman replication-status streaming-backup-server

# cron 日志
docker logs barman
```

## 完整测试流程

详细的端到端备份恢复测试步骤见 [E2E-TEST.md](./E2E-TEST.md)，恢复指南见 [RECOVERY-GUIDE.md](./RECOVERY-GUIDE.md)。
