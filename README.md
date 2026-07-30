# PostgreSQL + Barman 备份模板

这个仓库提供一套 PostgreSQL 17 双路径物理备份模板：边缘节点直接备份到 S3，异地节点通过 Tailscale 备份到本地磁盘。

## 架构

```text
边缘节点
┌─────────────────────────────────────────────┐
│ postgres                                    │
│   ├── replication slot: barman_edge         │
│   └── replication slot: barman_offsite      │
│                                             │
│ barman-edge                                 │
│   pg_basebackup + pg_receivewal ───────> S3 │
└─────────────────────────────────────────────┘
                        ▲
                        │ Tailscale
                        │
异地节点                │
┌───────────────────────┴─────────────────────┐
│ barman-offsite                              │
│   pg_basebackup + pg_receivewal ──> 本地磁盘│
└─────────────────────────────────────────────┘
```

PostgreSQL 不负责上传、调度或清理备份。两套 Barman 都主动通过复制协议读取 PostgreSQL，并使用独立复制槽，避免共享消费进度。

## 目录

```text
postgres/         PostgreSQL 生产模板，只提供数据库和复制接口
barman/           edge/offsite 共用的 Barman 容器镜像实现
barman-edge/      与 PostgreSQL 同节点部署，备份直接写入 S3
barman-offsite/   部署在异地主机，备份写入本地卷
pg_backup_restore/ 灾难恢复 CLI，负责文件恢复、权限、启动和清理
postgres-restore/ 隔离的临时 PostgreSQL 验证实例模板
smoke/            同时验证 edge 和 offsite 的端到端测试
scripts/          安装脚本和 smoke 入口
```

## 为什么不再使用 WAL sidecar

旧模板的云备份路径需要 PostgreSQL `archive_command`、WAL 共享卷、定时扫描脚本和独立的 barman-cloud sidecar：

```text
PostgreSQL -> 共享卷 -> 定时扫描 -> S3
```

Barman 3.19 支持 `backup_method = postgres` 配合云存储目录。新路径由 Barman 直接接收基础备份和 WAL：

```text
PostgreSQL -> Barman streaming -> S3
```

完整基础备份不会落到 edge 本地磁盘，只使用有限大小的 staging。Barman catalog、WAL 索引和流式接收临时文件仍保存在 `barman-edge` 本地卷。

## 部署 PostgreSQL

手动复制模板：

```bash
cp -a postgres ../postgres-instance
cd ../postgres-instance
cp .env.example .env
docker compose up -d
```

也可以使用安装任务生成密码：

```bash
mise run postgres-install
```

异地 Barman 通过 Tailscale 访问时，把 `.env` 中的 `POSTGRES_BIND_ADDRESS` 设置为 PostgreSQL 宿主机的 Tailscale IP。不要直接绑定所有公网接口。

## 部署 Barman Edge

```bash
mise run barman-edge-install
```

或者按 [`barman-edge/README.md`](./barman-edge/README.md) 手动配置。核心 server 配置是：

```ini
backup_method = postgres
streaming_archiver = on
slot_name = barman_edge
basebackups_directory = s3://bucket/postgres-backups
wals_directory = s3://bucket/postgres-backups
```

对于 RustFS、MinIO 等 S3 兼容服务，在 `.env` 中设置：

```bash
AWS_ENDPOINT_URL=http://object-storage:9000
```

AWS S3 使用默认端点时留空。

## 部署 Barman Offsite

```bash
mise run barman-offsite-install
```

或者按 [`barman-offsite/README.md`](./barman-offsite/README.md) 手动配置。`conninfo` 和 `streaming_conninfo` 应指向 PostgreSQL 宿主机的 Tailscale IP 或 MagicDNS 名称。

同一个 offsite Barman 可以管理多个 PostgreSQL。每个 PostgreSQL 使用独立 `.conf` 文件、server 名和复制槽。

## 调度

默认模板将两条基础备份错峰：

| 时间 | 任务 |
|---|---|
| 每分钟 | 两套 Barman 运行 `barman cron`，接收 WAL 并执行维护 |
| 02:07 | `barman-edge` 创建 S3 基础备份 |
| 04:07 | `barman-offsite` 创建本地基础备份 |
| 每周 | 分别验证最新备份 |

不要让两套完整基础备份同时运行，否则会重复占用 PostgreSQL 磁盘、CPU 和网络。

## 常用命令

```bash
# PostgreSQL
docker exec -it -u postgres postgres psql -U postgres

# Edge
docker exec barman-edge barman check postgres-edge
docker exec barman-edge barman backup postgres-edge --wait
docker exec barman-edge barman list-backups postgres-edge

# Offsite
docker exec barman-offsite barman check postgres-offsite
docker exec barman-offsite barman backup postgres-offsite --wait
docker exec barman-offsite barman list-backups postgres-offsite
```

## 灾难恢复

edge 和 offsite 共用同一套恢复入口，恢复结果固定写入与生产数据隔离的宿主机目录。文件恢复、权限转换、启动验证实例和清理是四次独立操作：

```bash
mise run barman:restore
mise run barman:restore:permissions -- --postgres-image postgres:17.10
mise run barman:restore:start -- --postgres-image postgres:17.10
mise run barman:restore:clean
```

恢复工具会验证本机 Docker context、Barman `/restore` bind、固定 bind-backed volume、备份状态、WAL 清单连续性、磁盘空间、PostgreSQL 主版本和外部网络。edge 云 WAL 会先物化到恢复目录，再把 `restore_command` 固定为读取 PGDATA 内的本地 WAL，因此启动和搬运恢复结果不依赖 Barman、S3 凭据或网络。

完整设计与运行约束见 [`docs/disaster-recovery.md`](./docs/disaster-recovery.md)。

## 测试

本地 smoke 会启动 PostgreSQL、RustFS、Barman Edge 和 Barman Offsite，验证：

- 两个独立复制槽都能持续接收 WAL
- edge 基础备份和 WAL 写入 S3
- edge 从 S3 完成指定时间 PITR、promote 和数据边界查询
- offsite 从本地备份完成指定时间 PITR、promote 和数据边界查询
- 错误 bind、错误 volume、主版本不匹配和缺失外部网络会被拒绝
- 默认清理保留恢复现场，永久清理删除数据和记录但保留固定 volume

```bash
mise run barman-smoke
```

## 当前限制

Barman 3.19 的 `backup_method = postgres` 流式直写云存储仍被上游标记为 experimental。用于生产前应在目标 S3 实现上验证上传中断、staging 达到上限、保留策略和 PITR 恢复。
