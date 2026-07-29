# 自动化验收与并发安全

[返回灾难恢复设计总览](../disaster-recovery.md)

## 已确认决策

### 自动化验收覆盖真实的两条恢复路径

恢复工具的自动化验证分为两层。

快速测试覆盖不需要真实备份介质的控制逻辑：

```text
参数与环境变量优先级
容器候选发现和显式输入失败语义
带时区时间解析、UTC 转换与范围校验
Docker inspect 结构化解析和路径隔离
基础备份自动选择与显式覆盖校验
宿主机锁与容器内进程锁
恢复记录状态机与原子替换
PostgreSQL 日志截断和敏感信息边界
默认清理保留数据
永久删除参数与完整路径确认
```

`barman-smoke` 必须端到端验证两条实际恢复路径：

```text
edge S3 备份
    ↓ 非交互恢复
    ↓ PostgreSQL WAL replay 与 promote
    ↓ 查询目标数据

offsite 本地备份
    ↓ 非交互恢复
    ↓ PostgreSQL WAL replay 与 promote
    ↓ 查询目标数据
```

edge 与 offsite 各至少覆盖一次指定时间 PITR，测试数据需要能区分目标时间之前和之后的状态。验收必须证明恢复实例到达指定目标、完成 promote，并能查到目标时间应存在且不应存在的数据；不能只检查 Barman 命令退出码或 PostgreSQL 端口开放。

smoke 还必须验证：

- 默认清理删除临时容器，但恢复数据、记录、日志和 volume 对象仍然存在；
- 永久清理删除恢复产物并重新建立空 `data/`，但保留固定 volume 对象；
- edge 与 offsite 使用同一固定恢复槽时不能并发恢复；
- 不匹配的 volume device、Barman bind source、PostgreSQL 主版本和外部网络都会在写入前失败。

smoke 只使用 `/tmp` 下的隔离恢复根目录，以及测试专用的容器、volume、网络和 Compose project 名称。测试不得读取、创建、修改或删除 `/srv/native-docker` 下的生产与人工恢复路径。生产默认名称是运行时协议；测试通过明确注入的测试配置覆盖名称，不能依靠随机发现宿主机现有容器。

### 恢复、启动与清理共享本机排他锁

固定恢复槽是单主机排他资源。恢复、启动临时 PostgreSQL 和清理 task 都必须先使用 Python 标准库 `fcntl.flock` 获取同一把排他锁，并一直持有到本次操作结束：

```text
/srv/native-docker/postgres-restore/.lock
```

如果恢复根目录被显式覆盖，锁文件随实际恢复根目录移动。工具拿不到锁时立即退出，不等待，也不提供强抢、删除锁文件或忽略锁的选项。错误信息读取锁文件中的非敏感诊断信息并展示当前操作的 PID、开始时间和命令名称。

锁是否有效只由内核 `flock` 状态决定，不能根据普通文件是否存在判断。进程异常退出时内核自动释放锁，残留 `.lock` 文件不需要删除。

`.lock` 属于恢复基础设施，不属于恢复数据：

- 不计入恢复槽非空判断；
- 默认清理与永久删除都保留；
- 不挂入 `PGDATA`；
- 不写入凭据或完整命令环境。

宿主机 `.lock` 只能覆盖 Python 工具自身的生命周期。为防止终端中断后由 `docker exec` 启动的 Barman 进程继续写入，实际恢复命令还必须在容器内持有第二把排他锁：

```text
/restore/.barman-restore.lock
```

执行模型为：

```bash
docker exec --user root <barman-container> \
  flock -n /restore/.barman-restore.lock \
  gosu barman barman restore ... /restore/data
```

宿主机 Python 必须在执行 `docker exec` 前创建 `.barman-restore.lock` 并设置 mode `0600`。容器内 root 打开并持有现有文件，再通过 `gosu barman` 执行实际恢复命令；文件描述符跨 exec 保持，覆盖 Barman 实际写盘生命周期。这样根目录与锁文件继续由宿主机操作者管理，Barman 只需要对已经切换所有权的 `/restore/data` 写入。候选容器必须具备 `flock` 与 `gosu`，缺少任一命令都不可用于该恢复流程。

permissions、start 和清理在操作 `data/` 前都由宿主机 Python 直接检查 Barman 写盘锁当前没有被占用，不要求 Barman 容器仍然运行。判断依据仍是内核锁状态，而不是锁文件是否存在；未占用的残留锁文件本身可以忽略。

恢复根目录通过 rsync 复制到另一台主机后，用户必须确保根目录及两个锁文件归目标主机上运行工具的用户所有。没有进程持有时，复制来的普通锁文件不会阻止操作。

用户按 `Ctrl+C`、SSH 断开或 Python 异常退出时，工具不得宣称 Barman 已停止。前端退出后，容器内恢复进程可能继续运行并持有锁，后续恢复、启动或清理必须因此拒绝操作。

第一版不自动终止容器内 Barman 恢复进程，也不提供强制杀死恢复的选项。进程自然结束后内核释放容器侧锁；半恢复数据和 `.restore.json.tmp` 仍然阻止覆盖，用户检查现场后通过显式清理 task 处理。
