# 恢复实例与验证流程

[返回灾难恢复设计总览](../disaster-recovery.md)

## 已确认决策

### 临时 PostgreSQL 使用独立 Compose 模板

临时验证实例不属于 edge 或 offsite Barman 部署，使用仓库中独立的 Compose 模板：

```text
postgres-restore/
├── compose.yaml
├── .env.example
└── README.md
```

该模板同时服务于 edge S3 恢复与 offsite 本地恢复，固定身份为：

```text
Compose project  postgres-restore
容器名称         postgres-restore
Docker 卷        barman-restore-postgres-data
宿主机目录       /srv/native-docker/postgres-restore
Docker 网络      外部 pg-net
发布地址         127.0.0.1:5433
```

容器和 Compose project 都不复用生产 PostgreSQL 的名称，加入 `pg-net` 时也不声明 `postgres` 网络别名，避免影响 Barman 或其他客户端解析生产实例。恢复端口默认只发布到回环地址。

现有 `barman-offsite/compose.yml` 中的 `barman-restore`、`fix-recover-permissions` 与 `pg-recovered` recovery profile 将由通用恢复工具和 `postgres-restore/` 模板替代并删除，避免维护两套恢复入口。

`postgres-restore/` 不提供安装 task，也不复制到仓库外的部署目录。它是恢复工具自身的一部分，Python 始终定位当前仓库中的 Compose 文件，并使用显式路径调用：

```bash
docker compose \
  --project-directory <repo>/postgres-restore \
  -f <repo>/postgres-restore/compose.yaml \
  up -d
```

调用不依赖用户当前工作目录。`POSTGRES_IMAGE`、`POSTGRES_NETWORK_NAME`、恢复端口和 volume 名称等运行时值由 Python 明确传给 Compose。这样不会生成一份可能与恢复工具版本不一致的长期部署副本。

生产 PostgreSQL、edge 和 offsite 仍然通过各自安装 task 部署到仓库外；只有一次性的恢复验证栈由仓库内恢复工具直接管理。

### 文件恢复与启动是两次显式操作

`barman:restore` 只完成 Barman 文件恢复、写入 `restore.json` 并保持 `data/` 为 `barman:barman`。它不询问是否立即启动，不接受 `--start-postgres` 或 `--no-start-postgres`，也不在内部调用权限或启动逻辑。

恢复完成后，工具输出明确的下一步命令。用户可以先单独检查或传输恢复结果，再显式执行：

```bash
mise run barman:restore:start -- \
  --restore-root /srv/native-docker/postgres-restore \
  --postgres-image postgres:17.10
```

mise 在 start 前自动执行 `barman:restore:permissions`。用户也可以单独运行 permissions task，只转换所有权而不启动实例。

这种拆分不需要嵌套调用 mise，不需要在同一次交互中交接宿主机锁，也不会在文件恢复完成后未经第二次显式操作就执行 WAL replay、更新控制文件或让历史副本进入可写状态。

### 恢复产物必须自包含所需 WAL

所有 Barman 文件恢复仍显式使用 `--no-get-wal`，不依赖 server 配置的默认值。offsite 本地 WAL 由 Barman 直接复制到恢复结果；Barman 3.19 对云 `wals_directory` 会忽略该意图并强制启用 get-wal，因此 edge 需要额外的物化阶段：

```text
barman restore --no-get-wal ...
    ↓
读取 barman list-files --target wal 清单并检查文件名与时间线内连续性
    ↓
本地 WAL：核验 Barman 写入的 barman_wal
云 WAL：逐个执行 barman cloud-wal-restore 写入 barman_wal
    ↓
核验实际文件与大小
    ↓
把 restore_command 改写为从 PGDATA/barman_wal 本地 cp
    ↓
restore.json 标记文件恢复 completed
    ↓
恢复根目录可以离线检查或通过 rsync 搬运
    ↓
permissions/start 不再访问 Barman
```

恢复结果生成的 PostgreSQL recovery 配置不得调用 `barman get-wal`、`barman-wal-restore`、`barman cloud-wal-restore` 或其他目标 PostgreSQL 镜像中不存在的 Barman 工具。最终配置固定从 `/var/lib/postgresql/data/barman_wal/%f` 复制 WAL；临时 PostgreSQL 镜像不安装 Barman CLI，也不需要连接 Barman 容器所在网络。

指定时间所需 WAL 不完整时，Barman 文件恢复阶段必须失败并保留现场，不能把一个已知不完整的恢复结果标记为 completed，再把问题推迟到脱离 Barman 的 start 阶段。

这个选择会增加恢复目录中的临时 WAL 空间占用，换取恢复产物可搬运、可离线启动，以及 permissions/start 不依赖 Barman 容器、catalog、凭据或网络的明确边界。

Barman 3.19 强制 cloud get-wal 是已确认的上游版本行为，不能仅靠 `--no-get-wal` 关闭。工具只在文件恢复阶段借用常驻 Barman 容器的云配置与凭据完成物化；物化成功后的恢复结果不再依赖云端。

### 第一版拒绝用户自定义 tablespace

固定单一 `data/` 目录和单一 `barman-restore-postgres-data` volume 只支持 PostgreSQL 默认 tablespace：

```text
pg_default
pg_global
```

恢复计划阶段必须从 Barman `show-backup` 的结构化详情读取 tablespace 信息。发现任何用户自定义 tablespace 时，在展示最终确认页之前直接拒绝，并列出 tablespace 名称、OID 和原始路径。

拒绝发生在所有写操作之前：

```text
不创建 .restore.json.tmp
不切换 data/ 所有权
不执行 Barman restore
不尝试按原绝对路径创建目录
```

第一版不实现 `--tablespace name:location` 重映射、多 tablespace bind mount、动态 Compose volume、`pg_tblspc` 链接管理或多路径永久删除。未来确有需求时单独设计 tablespace 映射，不能静默按备份中的原始绝对路径恢复。

### 文件恢复前执行保守的磁盘空间检查

恢复工具在创建临时记录、切换 `data/` 所有权或执行 Barman restore 之前，估算自包含恢复结果的最低空间需求：

```text
基础备份展开后的 cluster size
+ 目标所需的已知 WAL 大小
+ 10% 安全余量
+ 最低 1 GiB 余量
```

当前可用空间必须通过宿主机 `statvfs` 从实际 `/restore/data` 所在文件系统取得，不能使用 Docker volume 的表面容量或其他路径的磁盘空间。

可用空间低于可计算的最低需求时硬失败，不提供 `--ignore-insufficient-space` 或其他绕过参数。失败发生在任何恢复写操作之前，并展示各项估算、可用空间与差额，避免把文件系统写满并影响同盘服务。

空间满足最低需求时，最终确认页展示：

```text
基础备份展开大小   184 GiB
已知 WAL 大小       12 GiB
安全余量             20 GiB
最低需求            216 GiB
当前可用            480 GiB
估算状态            完整
```

Barman 无法提供完整 WAL 大小时，尤其“最新可达”仍可能增长，工具把估算标记为不完整。交互模式增加一次明确确认；非交互模式只有显式传入 `--allow-unknown-space-requirement` 才能继续。该参数不能绕过已经确认的空间不足。

`restore.json` 记录估算时间、基础备份展开大小、已知 WAL 大小、最低需求、当时可用空间、估算是否完整，以及最新模式下当时可见的最后 WAL。估算通过不承诺动态 WAL 不再增长。

### 恢复健康门槛只依赖已保存的备份链

恢复计划阶段只列出 Barman 标记为可恢复完成状态的基础备份。用户通过 `--backup` 显式指定 failed、waiting、obsolete、未知或其他不可恢复状态的备份时立即拒绝，不回退到自动选择。

最终选中备份后，写入前必须完成：

```text
barman check-backup <server> <backup-id>
目标范围内 WAL 连续性检查
用户自定义 tablespace 检查
磁盘空间检查
```

指定时间模式验证从所选基础备份结束位置到目标时间所需 WAL 的连续性。最新模式只验证开始恢复时 catalog 已知范围内的连续性，并记录当时可见的最后 WAL；不承诺恢复期间新接收 WAL 的后续连续性。

指定时间模式还从 Barman xlogdb 读取 WAL 归档时间，要求清单中最后一个 WAL 的归档时间不早于目标时间。这个检查是 catalog 级的最低覆盖边界，用于在写入前拒绝显然尚未归档到目标时刻的恢复计划；它不等同于证明某个事务提交时间已经越过目标，也不替代 PostgreSQL 启动后的 recovery target、promote、日志和 SQL 验证。

恢复工具不得把以下项目作为允许恢复的前置条件：

```text
barman check <server>
conninfo 当前可连接
streaming_conninfo 当前可连接
复制槽仍然存在
receive-wal 仍在运行
```

源 PostgreSQL 可能正是灾难对象。`barman check` 的结果最多作为信息展示，不能改变恢复是否允许执行；恢复能力只依赖已经保存的基础备份、WAL、catalog 和存储访问。

`barman:restore:start` 在 mise 完成权限依赖后，只负责首次创建并启动临时验证容器。执行前只要名为 `postgres-restore` 的容器对象已经存在，无论其状态是 running、stopped、exited 还是 created，task 都立即拒绝，不重建、不重启、不复用，也不根据容器状态推断用户意图。

已经存在的容器由用户直接检查其状态与日志；需要重新走工具管理的启动流程时，先运行 `barman:restore:clean` 删除临时容器。这个规则避免为停止原因、失败重试和配置漂移维护额外分支。

### 验证镜像必须匹配备份的 PostgreSQL 主版本

PostgreSQL 物理备份只能由兼容的 PostgreSQL 主版本启动。恢复工具从 Barman 的备份详情中读取源 PostgreSQL 版本，并在确认页展示备份主版本与候选验证镜像：

```text
备份 PostgreSQL 版本  17.x
验证镜像             postgres:17.10
```

独立 `postgres-restore` 模板通过 `POSTGRES_IMAGE` 接收完整镜像引用。镜像来源按以下优先级确定：

1. 命令行参数 `--postgres-image`；
2. 部署环境中已经配置的 `POSTGRES_IMAGE`；
3. Barman 部署明确记录的 PostgreSQL 镜像元数据。

工具不得只根据主版本自动拼接 `postgres:<major>`，不得自动拉取镜像，也不得假定官方镜像一定是生产实例使用的镜像。候选镜像必须已经存在于本机；工具在镜像内执行 `postgres --version`，验证其主版本与备份一致后才允许启动临时实例。

镜像不存在、来源不明、无法执行版本检查或主版本不匹配时，Barman 数据恢复仍可完成，但工具拒绝启动临时 PostgreSQL，并提示用户通过 `--postgres-image <完整镜像引用>` 显式指定。已经恢复的数据与恢复记录必须保留，不能因启动验证失败而清理或回滚。

### 临时 PostgreSQL 可写，但限制连接入口

临时验证实例完成 recovery 后允许进入可写状态，以支持应用级查询、事务、索引、迁移和临时校验数据等完整验证。只读模式不能阻止 PostgreSQL 启动过程修改 `PGDATA`，反而会限制恢复演练的验证价值。

隔离边界由独立身份与连接入口保证：

```text
容器名称    postgres-restore
发布地址    127.0.0.1:5433
网络        pg-net
网络别名    不声明 postgres
重启策略    "no"
```

恢复实例不继承生产 PostgreSQL 的 `.env`，也不设置新的 `POSTGRES_PASSWORD`。官方 PostgreSQL 镜像面对非空 `PGDATA` 不会重新初始化数据库或密码；恢复实例继续使用目标恢复点已有的数据库角色与凭据。恢复工具只输出连接方式，不自动创建用户或修改恢复出的凭据。

恢复实例设置 `restart: "no"`。宿主机或 Docker daemon 重启后不得自动重新启动这个验证实例，避免一个临时、可写的历史数据副本长期意外在线。

### 启动后只执行有限的通用健康检查

启动临时实例后，恢复工具自动执行不依赖业务知识的最小检查：

```text
等待 postgres-restore 容器进入 running
等待 PostgreSQL 开始接受本地连接
尝试确认 pg_is_in_recovery() 已变为 false
确认容器在限定等待时间内没有退出
输出容器状态、回环端口和后续连接命令
```

工具优先尝试通过容器内的 `postgres` 操作系统用户和 Unix socket 执行 SQL，不要求用户把恢复点的数据库密码提供给工具：

```bash
docker exec --user postgres postgres-restore \
  psql -d postgres -Atqc 'SELECT pg_is_in_recovery();'
```

恢复出的集群可能没有名为 `postgres` 的数据库，或其 `pg_hba.conf` 不允许这种本地连接。SQL 检查无法执行时，工具把结果标记为“自动 SQL 验证未完成”，保留运行中的实例并提示用户使用恢复点已有的数据库与凭据继续验证。

结果必须分层报告：

```text
Barman 数据恢复       成功或失败
PostgreSQL 容器启动   成功、失败或未请求
自动 SQL 验证         成功、未验证或未执行
业务数据验证          由用户完成
```

自动 SQL 验证失败不得反向把成功的 Barman 数据恢复判定为失败，不得自动停止实例，也不得删除或修改恢复数据。

### 保存有限的 PostgreSQL 验证日志快照

启动临时 PostgreSQL 时，工具保存本次 `postgres-restore` 容器从创建后产生的日志快照：

```text
/srv/native-docker/postgres-restore/postgres-restore.log
```

工具在健康检查完成或启动失败后通过 `docker logs postgres-restore` 获取日志，只保留最后 10 MiB，避免异常日志无限占用磁盘。日志文件由宿主机工具以 mode `0600` 写入，并归运行恢复工具的宿主机用户所有。

日志快照用于在容器被默认清理后继续保留 WAL replay、recovery target、promote 和启动失败的诊断证据。日志可能包含数据库名、路径、SQL 错误和业务对象名称，应按敏感恢复数据对待。

工具不得把容器环境变量、Docker inspect 全量输出或凭据写入日志快照。默认清理保留该文件；启用 `--delete-restored-data-permanently` 时，它与 `data/`、`restore.json` 和 `.restore.json.tmp` 一起删除。

### 保存可跨前端中断的 Barman 恢复日志

执行文件恢复前，宿主机 Python 创建：

```text
<restore-root>/barman-restore.log
```

文件使用 mode `0600` 并归宿主机操作者所有。容器 root 打开现有日志文件与 `.barman-restore.lock`，再通过 `gosu barman` 执行 restore。Barman stdout/stderr 同时实时显示在前端并写入日志；Python、SSH 或终端断开后，容器内进程仍继续向该日志写入。

日志记录恢复计划摘要、server、备份 ID、阶段和 Barman 输出，不记录 AWS 环境变量、`.pgpass` 内容或其他凭据。日志上限为 50 MiB；超过上限时保留开头的恢复计划与末尾的结果或错误信息，不能因日志无限增长挤占恢复空间。

edge 云 WAL 物化和 recovery 配置改写也会继续追加这份日志，因此工具在这些阶段退出时无论成功或失败都会再次执行 50 MiB 截断，确保最终保留的现场日志仍满足同一上限。

`restore.json` 只保存结构化状态与 `barman-restore.log` 的相对路径，不复制大段命令输出。日志存在即属于恢复现场，会阻止新的 restore 覆盖；默认清理保留，永久删除时与其他恢复产物一起删除。

### 恢复实例只加入已存在的外部网络

恢复实例默认加入已经存在的外部 Docker 网络 `pg-net`。网络名称允许通过环境变量 `POSTGRES_NETWORK_NAME` 或对应命令行参数显式覆盖，但指定的网络必须已经存在。

启动前，恢复工具检查目标网络；不存在时立即报错并停止。工具不自动创建同名网络，也不静默回退到 Compose 私有网络，避免创建出 driver、IPv6、subnet 或 labels 与生产配置不一致的网络，或者让用户误以为恢复实例已经接入预期网络。

恢复数据本身不依赖 `pg-net`。因此网络缺失只阻止启动临时 PostgreSQL，不把已经成功完成的数据恢复判定为失败。
