# PostgreSQL 灾难恢复设计

## 目标

为 Barman edge 与 offsite 备份提供一套尽可能简单、可演练的交互式恢复流程：

- 在宿主机上通过 mise task 启动交互式脚本；
- 使用 Questionary 选择 Barman 容器、server 与恢复目标；
- 将恢复结果写入与生产数据隔离的宿主机目录；
- 通过独立 mise task 快速启动一个容器名称和端口均与生产实例不同的临时 PostgreSQL；
- 避免 Docker named volume 的 tar 导出、导入流程。

## 已确认决策

- [存储布局与生产隔离](disaster-recovery/storage-layout.md)
- [执行环境与命令入口](disaster-recovery/execution-interface.md)
- [恢复目标与文件恢复](disaster-recovery/recovery-target-and-file-restore.md)
- [恢复实例与验证流程](disaster-recovery/restore-and-validation.md)
- [清理流程与恢复记录](disaster-recovery/cleanup-and-records.md)
- [自动化验收与并发安全](disaster-recovery/testing-and-concurrency.md)
- [文档维护与实现状态](disaster-recovery/implementation-status.md)
