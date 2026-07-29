# 清理流程与恢复记录

[返回灾难恢复设计总览](../disaster-recovery.md)

## 已确认决策

### 清理默认保留恢复数据

清理 task 的默认语义是拆除临时运行环境，不删除从备份恢复出的 `PGDATA`。默认清理后，数据仍保留在：

```text
/srv/native-docker/postgres-restore
```

其中 PostgreSQL 物理数据保存在 `data/`，父目录中的 `restore.json` 保存本次恢复记录。恢复记录不进入 `PGDATA`，也不通过恢复 volume 挂入 PostgreSQL。

恢复数据的身份不依赖 Docker volume 对象，但清理 task 始终保留 `barman-restore-postgres-data` volume 对象。该对象是临时 PostgreSQL 挂载固定 `data/` 路径所需的可重建声明，而且本身没有额外的数据空间成本。Barman 容器使用恢复根目录的普通 bind mount，不占用这个 volume 对象。

默认清理严格执行：

```text
刷新并保存最终日志与恢复记录
停止并删除 postgres-restore 容器
保留 barman-restore-postgres-data volume 对象
保留 /srv/native-docker/postgres-restore 中的恢复数据
```

默认清理的执行顺序固定为：

```text
获取宿主机排他锁
    ↓
由宿主机确认 Barman 写盘锁未占用
    ↓
读取 postgres-restore 最终状态
    ↓
保存最后 10 MiB PostgreSQL 日志
    ↓
更新 restore.json 的验证状态与 stopped_at
    ↓
停止并删除 postgres-restore 容器
```

日志快照或恢复记录任一步写入失败时，清理 task 必须保留容器并以非零状态退出。第一版不提供跳过日志保存或强制删除容器的分支；用户需要先处理恢复根目录权限、磁盘空间或 Docker 日志读取问题，再重新运行清理。

当 `postgres-restore` 容器本来就不存在时，默认清理只报告当前保留的数据与记录，不重写现场，也不视为错误。

即使启用永久删除恢复数据的参数，清理 task 也只清空宿主机恢复目录，仍然保留 volume 对象，供 Barman 下次恢复使用。只有卸载整个 Barman 部署时才由相应 Compose 操作处理 volume 对象，这不属于恢复清理 task 的职责。

只有用户显式传入一个含义非常明确的长参数时，清理 task 才一并永久删除恢复数据。参数暂定为：

```text
--delete-restored-data-permanently
```

该参数不得使用短选项，不得由环境变量隐式启用。交互调用仍需额外展示将被永久删除的精确宿主机路径并进行强确认。无此参数时，无论是否删除容器或 volume 对象，都不得清空、移动、递归修改 `/srv/native-docker/postgres-restore` 的内容。

交互模式要求用户完整输入以下绝对路径，只有逐字一致才执行永久删除：

```text
/srv/native-docker/postgres-restore
```

非交互模式必须同时提供永久删除开关和精确确认路径：

```bash
mise run barman:restore:clean -- \
  --delete-restored-data-permanently \
  --confirm-delete-path /srv/native-docker/postgres-restore
```

`--confirm-delete-path` 不接受相对路径、模糊匹配或其他规范化后碰巧等价的写法。示例使用默认路径；路径被覆盖时，交互提示与参数必须使用从实际 volume device 推导出的恢复根目录。任一参数缺失或确认文本不完全匹配时，task 不得删除恢复数据。

永久删除分支必须在操作前验证：

- 确认路径逐字等于从实际 volume device 推导出的恢复根目录；
- 恢复根目录是绝对路径，且不是空值、`/` 或其上级目录；
- 配置中的生产路径与恢复根目录不相等、互不包含；生产 volume 存在时，其实际 device 也必须通过同样的额外交叉检查；
- `postgres-restore` 已停止并删除；
- 除通过已验证 `/restore` bind mount 访问恢复槽的受支持 Barman 容器外，没有其他容器继续使用恢复目录。

恢复完成后，`data/` 归容器内 `postgres:postgres` 所有，宿主机操作者通常无权直接删除其中内容。永久删除按所有权边界分工：

```text
宿主机 Python
├── 删除 restore.json
├── 删除 .restore.json.tmp
├── 删除 barman-restore.log
└── 删除 postgres-restore.log

已验证 Barman 容器内的 root
└── 清空 /restore/data 内部内容，保留 data/ 目录本身
```

永久删除前必须由宿主机确认 Barman 写盘锁未被占用，再重新选择或探测一个正确挂载同一 `/restore` 根目录的受支持 Barman 容器执行清空。没有可验证的 Barman 容器时，永久删除立即拒绝，不通过宿主机 `sudo`、数字 UID/GID 或临时 helper 容器绕过权限边界。

清空操作只能针对容器内已经验证的固定路径 `/restore/data`，不得删除 `/restore/data` 目录本身，也不得触碰 `/restore/.lock` 或 `/restore/.barman-restore.lock`。删除逻辑不得跟随 `data/` 内的符号链接跳出恢复目录。完成后必须验证 `data/` 为空；下次恢复开始前再把空目录切换为 `barman:barman`。

### 恢复记录与 PGDATA 分离

一次恢复占用固定目录：

```text
/srv/native-docker/postgres-restore/
├── data/          # barman-restore-postgres-data 的 bind device，也是 PGDATA
└── restore.json   # 本次恢复的机器可读记录
```

恢复记录分阶段写入。开始执行 Barman 恢复前，工具先在父目录创建 `.restore.json.tmp`，记录已经确认的恢复计划并设置 `status: "running"`。Barman 文件恢复成功后，工具补齐文件恢复结果，再以原子替换方式将临时文件重命名为 `restore.json`。后续 `start` 流程再更新权限转换、容器启动、WAL replay 与目标验证状态。

最终记录至少包含：

```json
{
  "container": "barman-offsite",
  "server": "postgres-offsite",
  "backup_id": "20260729T000002",
  "target_mode": "time",
  "target_time": "2026-07-29T00:30:00Z",
  "started_at": "2026-07-29T01:00:00Z",
  "completed_at": "2026-07-29T01:12:34Z",
  "status": "completed",
  "tool_version": 1
}
```

最新可达模式的记录还应包含开始时可见的最后 WAL，并标记终点不是固定值。清理 task 使用该记录向用户展示磁盘数据的来源；记录仅用于审计和操作提示，不能替代对 volume、容器挂载和实际目录的安全校验。

如果 Barman 文件恢复或工具自身在文件恢复阶段失败，工具尽力把临时记录更新为 `status: "failed"`，并保存失败阶段、退出码和简短错误摘要。`start` 阶段的镜像验证、权限转换或容器启动失败则更新已经存在的 `restore.json`，但不得把成功的文件恢复状态改写为失败。记录不得包含 AWS 凭据、数据库密码、完整环境变量或未经筛选的 `docker inspect` 内容。

如果进程被强制终止，`.restore.json.tmp` 可以保持 `running` 状态。只要 `data/` 非空，或 `restore.json`、`.restore.json.tmp`、`barman-restore.log`、`postgres-restore.log` 任一存在，下一次恢复就必须拒绝覆盖，并提示用户检查现场后运行显式清理 task。工具不自动把残留临时记录解释为可以重试。

默认清理保留 `data/`、`restore.json`、`.restore.json.tmp`、`barman-restore.log` 与 `postgres-restore.log`。带永久删除参数时，它们作为同一次恢复的产物一起删除，随后保留空的 `data/` 末级目录，供固定 volume 下次使用。

### 恢复 task 不自动清理临时实例

开始恢复前必须同时满足：

```text
postgres-restore 容器不存在
/srv/native-docker/postgres-restore/data 目录为空
/srv/native-docker/postgres-restore/restore.json 不存在
/srv/native-docker/postgres-restore/.restore.json.tmp 不存在
/srv/native-docker/postgres-restore/barman-restore.log 不存在
/srv/native-docker/postgres-restore/postgres-restore.log 不存在
barman-restore-postgres-data 挂载正确
选定的 Barman 容器有效
```

只要名为 `postgres-restore` 的容器仍然存在，恢复 task 就立即拒绝执行，即使容器已经停止或恢复目录看起来为空。恢复 task 不自动停止、删除或替换临时实例，也不尝试复用其容器对象。

错误信息必须明确提示用户先运行独立的清理 task。这样停止实例、保留或永久删除恢复数据的决策始终留在清理入口中，恢复入口只处理一个已经为空且没有 PostgreSQL 使用者的恢复槽。
