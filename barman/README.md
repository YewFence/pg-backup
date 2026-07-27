# Barman 共享镜像

这个目录只包含 `barman-edge` 和 `barman-offsite` 共用的容器镜像实现，不是独立部署模板。

镜像提供：

- Barman 3.19.1 和 PostgreSQL 17 客户端
- AWS S3 所需的 boto3
- Debian cron 定时调度，任务以 `barman` 用户运行
- 多 server 批量任务
- HTTP 健康检查
- 运行时 UID/GID 调整和 pgpass 初始化

部署时请使用仓库根目录下的 `barman-edge/` 或 `barman-offsite/`。
