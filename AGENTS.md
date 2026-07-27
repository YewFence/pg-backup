这是一个测试项目和部署模板，用于验证 PostgreSQL 17 的双路径 Barman 物理备份。

## 架构

- `postgres/`：纯 PostgreSQL 模板，只负责数据库、备份协调用户和复制连接。
- `barman/`：`barman-edge` 与 `barman-offsite` 共用的容器镜像实现。
- `barman-edge/`：与 PostgreSQL 同节点部署，通过 Docker 网络连接 PG，将基础备份和 WAL 直接写入 S3。
- `barman-offsite/`：部署在异地主机，通过宿主机 Tailscale 连接 PG，将基础备份和 WAL 写入本地卷。
- `smoke/`：同时验证 edge S3 备份/恢复和 offsite 本地备份。

## 关键约束

- PostgreSQL 不运行备份调度，不使用 WAL 共享卷，也不安装 Barman 工具。
- edge 和 offsite 必须使用不同的复制槽，默认分别为 `barman_edge` 和 `barman_offsite`。
- 两套基础备份任务必须错峰。
- 自定义 S3 端点通过 Barman 容器的 `AWS_ENDPOINT_URL` 提供。

## 常用命令

```bash
mise run postgres-install
mise run barman-edge-install
mise run barman-offsite-install
mise run barman-smoke
```

这是测试环境，不需要考虑测试数据安全；仍然不要覆盖用户已有的未提交文件或真实 `.env`。
