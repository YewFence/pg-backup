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

每个 PostgreSQL 使用独立的 `.conf` 文件和复制槽。模板默认槽名为 `barman_offsite`，不要与边缘节点的 `barman_edge` 共用。

```bash
docker exec barman-offsite barman check postgres-offsite
docker exec barman-offsite barman backup postgres-offsite --wait
docker exec barman-offsite barman list-backups postgres-offsite
```

## 本地恢复验证

```bash
docker compose --profile recovery run --rm barman-restore \
  barman restore postgres-offsite latest /recover
docker compose --profile recovery run --rm fix-recover-permissions
docker compose --profile recovery up -d pg-recovered
```

恢复验证实例默认只发布到 `127.0.0.1:5433`。
