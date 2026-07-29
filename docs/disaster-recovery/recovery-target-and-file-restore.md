# 恢复目标与文件恢复

[返回灾难恢复设计总览](../disaster-recovery.md)

## 已确认决策

### 恢复目标分为两种模式

交互流程先选择 Barman server，再选择恢复目标模式：

```text
恢复到最新可达状态
恢复到指定时间
```

两种模式的语义为：

- **恢复到最新可达状态**：默认选择最新可用基础备份，并重放当前可用的后续 WAL；
- **恢复到指定时间**：用户输入包含时区的绝对时间，工具默认选择目标时间之前最接近且适用的基础备份，并将该时间映射为 Barman recovery target。

交互界面不把“手动选择基础备份”作为第三个顶层模式。正常恢复由工具自动选取基础备份，减少无必要的选择，也避免选择目标时间之后的备份或因选择过旧备份而无谓延长恢复时间。

高级用户可以在非交互调用中通过 `--backup <backup-id>` 覆盖自动选择，用于绕过疑似损坏的最新备份或专项验证某份基础备份。工具仍须验证显式备份属于所选 server、状态可恢复，并且在指定时间模式下能够作为目标时间的恢复起点。显式输入无效时立即失败，不回退到自动选择。

开始恢复前的确认页必须展示工具最终选中的基础备份及其时间信息，不能只显示 `latest` 或“自动选择”：

```text
Barman server  postgres-offsite
恢复目标       2026-07-29T08:30:00+08:00
基础备份       20260729T000002
备份开始时间   2026-07-29T00:00:02Z
PGDATA         /srv/native-docker/postgres-restore/data
```

“恢复到最新可达状态”是动态终点。工具不暂停或停止常驻 Barman 的 `receive-wal`，避免干扰正常备份链路，也不要求源 PostgreSQL 在灾难恢复时仍然可连接。

开始恢复前，工具记录并展示：

```text
开始恢复时间
Barman 容器名称
Barman server 名称
选中的基础备份 ID
开始时 Barman catalog 中可见的最后 WAL
```

恢复期间 Barman 可能继续接收新的 WAL，因此最终结果可能晚于开始时记录的最后 WAL。工具必须把结果明确标记为“最新可达，非固定终点”，不能承诺精确停在开始时的 WAL。需要稳定、可复述终点时，用户应选择“恢复到指定时间”。

“指定时间”只接受带时区的绝对时间，不接受 `昨天晚上`、`两小时前` 等相对时间，避免恢复结果随执行时刻或宿主机时区变化。开始恢复前，工具必须同时展示原始输入时间和转换后的标准时间。

支持的时间格式限定为 ISO 8601 / RFC 3339 风格，并且必须显式包含 UTC offset 或 `Z`：

```text
2026-07-29T08:30:00+08:00
2026-07-29 08:30:00+08:00
2026-07-29T00:30:00Z
```

以下输入必须拒绝：

```text
2026-07-29 08:30:00
2026/07/29 08:30
昨天 8 点
2 hours ago
```

Questionary 输入提示中应直接给出一个包含时区的有效示例，校验失败时再次说明缺少或错误的部分。确认页同时展示用户输入与换算后的 UTC 时间：

```text
输入时间  2026-07-29T08:30:00+08:00
UTC 时间  2026-07-29T00:30:00Z
```

工具把标准化后的带时区值传给 Barman，不依赖宿主机、容器或 PostgreSQL 的默认时区。目标时间晚于当前时间时直接拒绝，不把未来时间静默解释为“恢复到最新”；早于所选 server 最早可恢复时间时也必须在开始恢复前报错。

指定时间模式固定使用 recovery target action `promote`：

```text
重放 WAL 至目标时间
    ↓
promote
    ↓
结束 recovery 并进入可写状态
```

第一版不提供 `pause` 或 `shutdown` 选项。`promote` 与临时实例的可写验证目标一致，也避免引入暂停恢复、手工继续和额外状态管理。实例完成 promote 后不能继续向更晚的时间重放 WAL；如果需要验证另一个目标时间，用户必须按单份恢复槽流程清理当前结果并重新恢复。

### 文件恢复成功不等于 PITR 已达到目标

Barman 把基础备份与 recovery 配置写入 `data/` 成功，只表示文件恢复阶段完成。指定时间的实际 WAL replay 在 PostgreSQL 启动后发生，仍可能因为缺失或损坏的 WAL、归档范围不足、时间线不连续或 `restore_command` 失败而无法到达目标。

结果必须继续细分：

```text
Barman 文件恢复       completed 或 failed
PostgreSQL WAL replay started、not_started 或 failed
指定目标时间          verified、not_verified 或 failed
实例可写              yes 或 no
```

指定时间只有在 PostgreSQL 启动、日志表明达到 recovery target、完成 promote，并确认不再处于 recovery 后，才能标记为 `verified`。判定应结合 PostgreSQL 标准日志和 `SELECT pg_is_in_recovery()`；不能仅以容器处于 running、端口开放或 Barman 命令退出码为依据。

用户选择不启动 PostgreSQL 时，`restore.json` 记录文件恢复已完成，但 `target_status` 必须是 `not_verified`，不能声称已经恢复到指定时间。

PostgreSQL 启动后若 WAL replay 失败或无法到达目标，工具保留数据、恢复记录与可用日志，把 `target_status` 记为 `failed`，输出经过筛选的相关 PostgreSQL 日志摘要并返回非零退出码。工具不得自动清理或把失败现场还原成空恢复槽。

### Barman 容器预挂载固定恢复根目录

所有受支持的 edge 与 offsite Barman 容器都在部署时通过普通 bind mount 预先挂载完整恢复根目录：

```yaml
services:
  barman-edge:
    volumes:
      - type: bind
        source: ${POSTGRES_RESTORE_ROOT:-/srv/native-docker/postgres-restore}
        target: /restore
        bind:
          create_host_path: false
```

offsite 使用相同的 bind mount。edge 与 offsite 位于同一宿主机时，两个容器可以看到同一个固定恢复槽与容器侧进程锁；一次恢复仍然只选择其中一个容器执行。

Barman 的恢复目标固定为 `/restore/data`。`/restore` 根目录保存锁和恢复元数据，只有 `/restore/data` 是纯 `PGDATA`。临时 PostgreSQL 不挂载整个根目录，只通过 `barman-restore-postgres-data` bind-backed local volume 挂载 `data/`。

`barman-restore-postgres-data` 不要求在首次恢复前由用户单独创建。创建路径取决于当前工作流。

`restore` 从已经验证的 Barman `/restore` bind mount 推导：

```text
检查选定 Barman 容器的 /restore
    ↓
取得并验证宿主机绝对 source
    ↓
验证 <source>/data 是预期的恢复数据目录
    ↓
创建 barman-restore-postgres-data
    ↓
重新 inspect 并核对 Options.device
```

等价的 Docker 操作为：

```bash
docker volume create \
  --driver local \
  --opt type=none \
  --opt o=bind \
  --opt device=<validated-restore-source>/data \
  barman-restore-postgres-data
```

自动创建只建立可重建的挂载声明，不启动 PostgreSQL，也不写入或初始化 `PGDATA`。volume 已存在时，工具只能 inspect 和验证，绝不删除、重建或修改；其 device 与 Barman `/restore/data` 不一致时立即拒绝执行。

`permissions` 与 `start` 不依赖 Barman。volume 不存在时，它们从用户显式传入的绝对 `--restore-root` 推导 `<restore-root>/data`，但必须先验证：

- `restore.json` 存在、结构有效且文件恢复状态为 completed；
- `.restore.json.tmp` 不存在；
- `data/` 是真实目录而不是符号链接，并且包含可识别的 PostgreSQL `PG_VERSION`；
- `restore.json` 中记录的恢复根目录与当前显式输入一致；
- 目标路径通过绝对路径、父子关系和生产路径隔离检查。

通过后可以创建同一个固定 volume，再重新 inspect 并确认 device 精确等于 `<restore-root>/data`。volume 已存在但指向其他路径时立即拒绝，不自动修复。

Python 工具在选定容器后通过 `docker exec` 执行 Barman 查询和恢复命令，不动态创建一次性 Barman 容器。这样无需重建或复制 catalog 卷、`/etc/barman.d` 配置、AWS 凭据、S3 端点、UID/GID、网络和镜像 entrypoint。

edge 恢复直接沿用选定常驻 Barman 容器已经配置的 AWS 凭据、区域、S3 端点与校验和选项。恢复工具不读取凭据值、不把凭据复制到宿主机进程环境，也不在命令行中重新传递或输出凭据。

名称包含 `barman` 只用于发现候选。候选必须通过以下检查才能用于恢复：

- 容器处于运行状态；
- 容器内可以执行 `barman`；
- `/restore` 是普通 bind mount；
- `/restore` 的宿主机 source 是满足安全约束的绝对路径；
- `barman-restore-postgres-data` 已存在时，其 device 与 `/restore/data` 一致；不存在时可以按已确认规则创建后再核对；
- `/restore/data` 为空，且 `/restore` 中不存在 `restore.json`、`.restore.json.tmp`、`barman-restore.log` 或 `postgres-restore.log`。

未正确挂载预期 `/restore` 的 Barman 容器直接判定为不可用于恢复，并给出原因。工具不得尝试在运行中的容器上动态增加挂载，也不得退回到容器可写层保存恢复数据。

当前 Barman 镜像会创建并递归修改 `/recover`；实现时必须移除这项旧恢复路径行为。Barman entrypoint 不得递归修改 `/restore` 或 `/restore/data`，恢复结果交给 PostgreSQL 后，Barman 容器重启不得再次改写其所有权。

### 权限转换通过容器内用户名执行

宿主机工具不硬编码 Barman 与 PostgreSQL 的数字 UID/GID。恢复目录的所有权通过对应镜像内的用户名按实际阶段转换：

```text
已确认为空的恢复目录
    ↓ 以 root 身份在选定 Barman 容器内执行
barman:barman
    ↓ 执行 Barman 恢复
刚恢复出的 PGDATA 保持 barman:barman
    ↓ 用户请求 start，且 PostgreSQL 镜像版本验证成功
    ↓ 通过该 PostgreSQL 镜像的一次性权限服务执行
postgres:postgres
    ↓
启动临时 PostgreSQL
```

恢复前只修改已经确认为空的 `/restore/data` 目录本身：

```bash
docker exec --user root <barman-container> chown barman:barman /restore/data
```

执行独立的 `barman:restore:permissions` task 时，由与备份主版本一致且已经通过验证的 PostgreSQL 镜像递归接管恢复文件，并设置 PostgreSQL 所需的目录权限：

```bash
chown -R postgres:postgres /restore/data
chmod 700 /restore/data
find /restore/data -type d -exec chmod 700 {} +
```

使用容器内用户名可以跟随镜像和 Barman 的 UID/GID 配置，不要求宿主机存在同名用户。候选 Barman 容器如果不能以 root 执行、没有 `barman` 用户或无法修改 `/restore/data`，必须在恢复前验证失败，不猜测或回退到数字 UID。

纯文件恢复不要求提供 PostgreSQL 镜像，成功后 `data/` 保持 `barman:barman`。权限 task 可以由用户单独执行；`barman:restore:start` 通过 mise `depends` 始终先执行同一个权限 task，不在 start 实现中复制权限逻辑。

`permissions` 与 `start` 都不依赖 Barman 容器。它们使用用户显式传入的恢复根目录，验证 `restore.json`、`data/`、宿主机锁和本机 Docker 对象。权限 task 通过已经验证版本的 PostgreSQL 镜像挂载 `data/` 并切换所有权；Barman 容器、catalog、配置和凭据都不是启动验证实例的前置条件。

### SELinux 不在当前范围内

目标服务主机使用 Debian，本项目的恢复流程当前只处理 Unix 所有权与 mode，不配置、不检查 SELinux label，也不依赖 `semanage`、`restorecon` 或 Compose relabel 选项。

如果以后把模板部署到启用 SELinux 强制模式的系统，应当单独设计稳定宿主机路径的持久标签策略，而不是在当前 Debian 流程中加入未验证的兼容分支。
