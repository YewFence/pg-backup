# Barman Edge

`barman-edge` 与 PostgreSQL 部署在同一边缘节点，但只通过 PostgreSQL 复制协议读取数据。基础备份和 WAL 由 Barman 直接写入 S3，不挂载 `PGDATA`，也不需要 WAL 共享卷或 `archive_command` sidecar。

## 部署

```bash
cp .env.example .env
cp config/postgres-edge.conf.example config/postgres-edge.conf
cp config/pgpass.example config/pgpass
cp config/barman.crontab.example config/barman.crontab
chmod 600 config/pgpass
```

编辑 `.env` 中的 S3 凭证，并把 `config/postgres-edge.conf` 的 S3 URL 改成实际 bucket。默认通过外部 Docker 网络 `pg-net` 连接名为 `postgres` 的数据库容器。启动前还要按安装 task 的提示创建 `POSTGRES_RESTORE_ROOT` 及其 `data/`，容器会把完整恢复根目录预挂载到 `/restore`。

```bash
docker compose up -d
docker exec barman-edge barman check postgres-edge
docker exec barman-edge barman backup postgres-edge --wait
docker exec barman-edge barman list-backups postgres-edge
```

自定义 S3 兼容端点通过 `.env` 的 `AWS_ENDPOINT_URL` 配置。Barman 本地卷仅保存 catalog、运行状态、流式 WAL 临时文件和有限大小的上传缓冲，不保存完整基础备份。

## 恢复

统一恢复工具会在当前 Barman 容器内执行恢复，沿用既有 catalog、S3 凭据和端点：

```bash
mise run barman:restore -- \
  --container barman-edge \
  --server postgres-edge \
  --yes
```

所有文件恢复都显式使用 `--no-get-wal`，把启动所需 WAL 写入隔离的恢复结果；后续权限转换和临时 PostgreSQL 启动不再依赖 Barman、S3 凭据或网络。恢复工具不会替换生产 `PGDATA`。
