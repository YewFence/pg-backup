# 文档维护与实现状态

[返回灾难恢复设计总览](../disaster-recovery.md)

## 已确认决策

### 决策文档随项目版本化

本项目专属的恢复约定记录在本组文档中。Obsidian 中的通用指南继续只说明 bind-backed local volume 的通用设计，不混入本项目的 Barman server、容器和恢复目录约定。

## 当前实现基线

- 生产 PostgreSQL 当前使用普通 named volume `postgres_data`。
- edge 的 Barman 数据卷只保存 catalog、运行状态和上传缓冲，完整基础备份与 WAL 位于 S3。
- offsite 将完整基础备份与 WAL 保存到本地 Barman 数据卷。
- offsite 已有 `barman-restore`、`fix-recover-permissions` 和 `pg-recovered` recovery profile，但仍把恢复结果写入普通 named volume。
- 临时恢复实例当前使用独立容器名 `postgres-recovered` 和宿主机端口 `5433`，但还没有接入生产侧的外部 `pg-net`。

## 待决策

当前没有尚未讨论的顶层决策。实现前仍需进行一次完整的一致性审查，并由用户确认本组文档已经形成共同理解。
