# Barman 备份服务

基于 [Barman](https://pgbarman.org/) 的 PostgreSQL 流复制备份服务，通过 Tailscale 组网连接 PG 服务器。

## 配置文件

部署前需要配置这些文件（`mise r barman-install` 会自动为第一个 PostgreSQL 生成）：

| 文件 | 用途 | 必须修改 |
|------|------|----------|
| `config/*.conf` | Barman server 配置，一个 PostgreSQL 对应一个 server 段 | server 名、host 地址、slot 名 |
| `config/pgpass` | Barman 连接各个 PostgreSQL 的密码文件 | host、用户、密码 |
| `config/barman.crontab` | 定时任务：WAL 归档、基础备份、备份验证 | 按需调整时间 |

同一个 Barman host 备份多个 PostgreSQL 时，直接在 `config/` 里放多个 `.conf` 文件即可，例如 `pg-main.conf` 和 `pg-report.conf`。每个文件里写一个 Barman 原生的 `[server-name]` 段，`conninfo` 和 `streaming_conninfo` 指向对应 PostgreSQL，然后在 `config/pgpass` 里追加对应连接密码。

`config/pgpass` 格式与 PostgreSQL passfile 一致：

```text
pg-main:5432:postgres:barman:main-password
pg-main:5432:*:streaming_barman:main-streaming-password
pg-report:5432:postgres:barman:report-password
pg-report:5432:*:streaming_barman:report-streaming-password
```

### barman.crontab 说明

这个文件控制 Barman 的自动备份行为，**如果不配置，备份服务会静默运行但不做任何备份**。

默认启用的任务（来自 example）：

```crontab
# 每分钟：WAL 归档 + 清理过期备份（必须开启）
* * * * * barman -q cron

# 每天凌晨 2 点：为所有 config/*.conf 里的 server 创建完整基础备份
0 2 * * * barman-for-each-server backup

# 每周日凌晨 3 点：验证所有 server 的最新备份完整性
0 3 * * 0 barman-for-each-server verify-backup latest
```

修改后需要重启容器生效：

```bash
docker compose restart barman
```

## 基本用法

### 启动服务

```bash
# 启动，会拉取预构建镜像并运行容器
docker compose up -d
```

> 如需使用本地构建镜像，可以先构建镜像，再通过 `.env` 覆盖 `BARMAN_IMAGE`：
> ```bash
> docker build --add-host=host.docker.internal:host-gateway \
    --build-arg HTTP_PROXY="http://host.docker.internal:7890" \
    --build-arg HTTPS_PROXY="http://host.docker.internal:7890" -t barman:latest .
> echo "BARMAN_IMAGE=barman:latest" >> .env
> ```

### 验证连接

```bash
docker exec barman barman check streaming-backup-server
docker exec barman barman check all
```

### 手动操作

```bash
# 创建备份
docker exec barman barman backup streaming-backup-server
docker exec barman barman-for-each-server backup

# 查看备份列表
docker exec barman barman list-backups streaming-backup-server

# 恢复最新备份
docker compose --profile recovery rm -sf pg-recovered fix-recover-permissions barman-restore
docker volume rm barman_barman-recover 2>/dev/null || true
docker compose --profile recovery run --rm barman-restore \
  barman restore streaming-backup-server latest /recover
docker compose --profile recovery run --rm fix-recover-permissions

# PITR 恢复到指定时间点
docker compose --profile recovery rm -sf pg-recovered fix-recover-permissions barman-restore
docker volume rm barman_barman-recover 2>/dev/null || true
docker compose --profile recovery run --rm barman-restore \
  barman restore \
  --target-time "2026-03-10 12:00:00" \
  --target-action=promote \
  streaming-backup-server latest /recover
docker compose --profile recovery run --rm fix-recover-permissions
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

### 健康检查

```bash
# 聚合状态，只有所有 server 健康才返回 200
curl http://<barman-tailscale-ip>:8000/

# 单个 server 状态，路径名就是 Barman server 名
curl http://<barman-tailscale-ip>:8000/streaming-backup-server
```

## 完整测试流程

详细的端到端备份恢复测试步骤见 [E2E-TEST.md](./E2E-TEST.md)，恢复指南见 [RECOVERY-GUIDE.md](./RECOVERY-GUIDE.md)。
