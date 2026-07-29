# Barman Offsite

`barman-offsite` 部署在异地主机，通过宿主机的 Tailscale 网络连接一个或多个 PostgreSQL，并把基础备份和 WAL 保存到 Barman 本地磁盘。

## 部署

```bash
cp .env.example .env
cp config/postgres-offsite.conf.example config/postgres-offsite.conf
cp config/pgpass.example config/pgpass
cp config/barman.crontab.example config/barman.crontab
chmod 600 config/pgpass
docker compose up -d
```

每个 PostgreSQL 使用独立的 `.conf` 文件和复制槽。模板默认槽名为 `barman_offsite`，不要与边缘节点的 `barman_edge` 共用。启动前还要按安装 task 的提示创建 `POSTGRES_RESTORE_ROOT` 及其 `data/`，容器会把完整恢复根目录预挂载到 `/restore`。

```bash
docker exec barman-offsite barman check postgres-offsite
docker exec barman-offsite barman backup postgres-offsite --wait
docker exec barman-offsite barman list-backups postgres-offsite
```

## 恢复验证

```bash
mise run barman:restore -- \
  --container barman-offsite \
  --server postgres-offsite \
  --yes

mise run barman:restore:start -- \
  --restore-root /srv/native-docker/postgres-restore \
  --postgres-image postgres:17.10
```

文件恢复与启动是两个显式操作。恢复结果位于固定的隔离目录，验证实例默认只发布到 `127.0.0.1:5433`，不会替换或复用生产 PostgreSQL。
