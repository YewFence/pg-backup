# 文档维护与实现状态

[返回灾难恢复设计总览](../disaster-recovery.md)

## 已确认决策

### 决策文档随项目版本化

本项目专属的恢复约定记录在本组文档中。Obsidian 中的通用指南继续只说明 bind-backed local volume 的通用设计，不混入本项目的 Barman server、容器和恢复目录约定。

## 当前实现状态

- 生产 PostgreSQL 使用指向稳定宿主机目录的 bind-backed local volume。
- edge 与 offsite Barman 都预挂载固定恢复根目录到 `/restore`，不再维护旧的 offsite recovery profile。
- `pg_backup_restore` 提供 `restore`、`permissions`、`start` 和 `clean` 四个内部命令，由 `mise.toml` 中的四个恢复 task 暴露。
- 恢复工具实现本机 Docker 校验、容器与 server 选择、备份选择、目标时间标准化、tablespace 拒绝、空间检查、双层文件锁、原子恢复记录和日志快照。
- 恢复 PostgreSQL 使用仓库内 `postgres-restore/compose.yaml`，独立容器、project、端口和外部网络均可显式配置。
- edge 云 WAL 与 offsite 本地 WAL 都会形成 PGDATA 内的自包含 WAL staging；`permissions` 和 `start` 不依赖 Barman 容器、catalog、凭据或网络。
- 指定时间恢复会读取 Barman xlogdb，以清单中最后一个 WAL 的归档时间作为 catalog 级覆盖边界；最后 WAL 早于目标时间时，文件恢复在写入前直接拒绝。
- 默认清理只删除临时 PostgreSQL 容器并保留现场；永久清理要求精确路径确认，通过受支持 Barman 容器清空 `data/`，同时保留固定 volume 对象。

## 已完成验收

`smoke/run.py` 使用 `/tmp` 和测试专用 Docker 对象，真实验证：

- edge 基础备份与 WAL 写入 S3 兼容存储，并完成指定时间 PITR；
- offsite 本地基础备份与 WAL 完成指定时间 PITR；
- 两条路径都到达目标时间、promote、退出 recovery、进入可写状态；
- 目标时间之前的 marker 存在，之后的 marker 不存在；
- 错误 Barman bind、错误 volume device、PostgreSQL 主版本不匹配和缺失外部网络被拒绝；
- 同一恢复槽已由另一个完整 restore 流程持锁时，第二个 restore 被拒绝且不创建恢复记录或日志；
- 默认清理与永久清理符合恢复记录、日志、数据目录和 volume 的保留约定。

## 版本行为说明

Barman 3.19 在 `wals_directory` 使用云存储时会强制启用 get-wal，即使恢复命令显式传入 `--no-get-wal`。实现因此在 Barman 基础恢复后读取经过连续性校验的 WAL 清单，使用 `barman cloud-wal-restore` 将每个 WAL 物化到 `PGDATA/barman_wal`，核验文件与大小，再把 PostgreSQL `restore_command` 原子改写为本地 `cp`。任何物化或配置校验失败都会把文件恢复记录为 failed，不会把问题推迟到 `start`。

## 待决策

当前没有尚未讨论的顶层决策。第一版仍明确不支持自定义 tablespace、远程 Docker context、自动替换生产实例或跨主机传输。
