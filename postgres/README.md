# PostgreSQL

这个模板只负责运行 PostgreSQL，并向两个独立的 Barman 消费者提供协调连接和流复制连接：

- `barman-edge` 使用复制槽 `barman_edge`，把备份直接写入 S3。
- `barman-offsite` 使用复制槽 `barman_offsite`，把备份写入异地主机本地磁盘。

PostgreSQL 不运行备份 cron，不安装 Barman 工具，也不挂载 WAL 归档共享卷。`archive_mode` 默认关闭，两套 Barman 都通过 `pg_receivewal` 主动接收 WAL。

## 启动

```bash
cp .env.example .env
docker compose up -d
```

默认仅将 PostgreSQL 发布到宿主机 `127.0.0.1:5432`。异地 Barman 通过 Tailscale 访问时，将 `POSTGRES_BIND_ADDRESS` 改为当前宿主机的 Tailscale IP。

## 常用操作

```bash
docker exec -it -u postgres postgres psql -U postgres
docker exec -it postgres create-db-user
docker exec postgres repair-barman-users
```

`repair-barman-users` 用于已有数据卷升级模板时重放最小权限授权，不需要重建数据库。
