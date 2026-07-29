# 存储布局与生产隔离

[返回灾难恢复设计总览](../disaster-recovery.md)

## 已确认决策

### 恢复流程不替换生产数据

交互式恢复流程只生成并验证隔离的候选数据目录，不自动覆盖、替换或复用生产 PostgreSQL 的 `PGDATA`。

临时 PostgreSQL 验证完成后，是否以及如何替换生产数据完全由用户另行处理，不属于该流程的职责。

### 使用稳定宿主机路径

PostgreSQL 数据使用 bind mount 或 bind-backed local volume，使 Barman 能直接把数据恢复到稳定的宿主机路径。Docker volume 对象只负责挂载，可以由 Compose 重建，不再需要通过辅助容器导出或导入 tar。

这项设计沿用《Docker 使用 Bind-backed Local Volume 管理持久数据》的核心原则，但 PostgreSQL 物理数据不放在通用文件备份根目录 `/srv/docker` 下。物理备份与时间点恢复由 Barman 负责，再对 `PGDATA` 做通用文件备份只会重复占用空间，而且不能代替数据库一致性恢复。

PostgreSQL 专用数据根目录确定为：

```text
/srv/native-docker/
├── postgres/
└── postgres-restore/
    ├── data/
    └── restore.json
```

`native-docker` 表示这里保存 Docker 服务使用、但由服务原生工具负责备份与恢复的数据。以后可以在同一根目录下容纳其他采用原生备份方案的服务。

### 生产实例与恢复实例使用独立目录

该模板不为同一宿主机上的多个生产 PostgreSQL 实例设计目录命名空间；需要部署多个实例时，由用户自行配置不同路径。

生产实例固定使用：

```text
/srv/native-docker/postgres
```

恢复验证被视为一套新的 PostgreSQL 实例，固定使用：

```text
/srv/native-docker/postgres-restore/data
```

恢复脚本不得读取、覆盖、重命名或以其他方式改动 `/srv/native-docker/postgres`。两者不采用 `primary/`、`restores/` 或实例标识等嵌套命名空间，以免为模板不负责的多实例场景增加复杂度。

### 数据路径允许显式覆盖

生产与恢复路径提供安全默认值，但允许部署到其他磁盘或目录：

```text
POSTGRES_DATA_PATH=/srv/native-docker/postgres
POSTGRES_RESTORE_ROOT=/srv/native-docker/postgres-restore
```

恢复数据路径固定为 `${POSTGRES_RESTORE_ROOT}/data`，不再提供第三个可以独立指向任意位置的配置项，避免恢复元数据与 `PGDATA` 分离到无法验证的路径。

执行恢复、启动临时实例或永久删除数据时，Python 工具不能只相信当前 shell 中的环境变量。受支持 Barman 容器的 `/restore` 普通 bind mount source 是恢复根目录的事实来源。`barman-restore-postgres-data` 已存在时，工具通过 `docker volume inspect` 读取 local volume 的 `Options.device`，并验证它精确等于 `<restore-source>/data`；volume 尚不存在时，按[恢复根目录挂载规则](recovery-target-and-file-restore.md#barman-容器预挂载固定恢复根目录)从已经验证的 `/restore` source 推导并创建，再重新 inspect。

实际 device 必须满足以下关系：

```text
<restore-root>/data
├── volume device 指向这里
└── ../restore.json 与 ../.restore.json.tmp 位于恢复根目录
```

永久删除确认文本使用实际 `/restore` bind source，不硬编码默认路径。生产路径隔离首先在安装配置层验证：`POSTGRES_DATA_PATH` 与 `POSTGRES_RESTORE_ROOT` 不相等、互不包含。

生产 volume 只用于可选的额外交叉检查，不是恢复前置条件。本机存在生产 volume 时，工具 inspect 其实际 device 并再次确认它与恢复根目录隔离；生产 volume 不存在时不阻止恢复、启动或清理。这样纯 offsite 主机和生产 Docker 对象已经丢失的灾难场景仍能执行恢复。

### 安装 task 只检查宿主机目录，不主动提权

生产与 Barman 安装 task 负责检查各自需要的稳定宿主机目录是否已经精确存在，但不在 task 或 Python 恢复工具中调用 `sudo`、弹出提权密码或自行修改宿主机权限。

默认目录缺失时，安装 task 打印需要用户显式执行的 provisioning 命令并退出。例如：

```bash
sudo install -d /srv/native-docker
sudo install -d /srv/native-docker/postgres
sudo install -d -m 0711 \
  -o "$(id -u)" \
  -g "$(id -g)" \
  /srv/native-docker/postgres-restore
sudo install -d -m 0700 \
  -o "$(id -u)" \
  -g "$(id -g)" \
  /srv/native-docker/postgres-restore/data
```

用户完成 provisioning 后重新运行安装 task。路径被环境变量覆盖时，提示命令必须使用实际配置值，不能仍然输出默认路径。

目录已存在时，task 只验证类型和必要结构，不清空目录、不递归 `chown`、不修正已有数据的 mode。生产目录非空表示可能已有数据库，恢复目录存在恢复产物表示可能有待验证或失败现场，都必须保留原状。

恢复根目录归运行恢复工具的宿主机用户所有，使 Python 可以直接管理锁与恢复记录：

```text
postgres-restore/          宿主机操作者所有
├── .lock                  Python 按需创建并管理
├── .restore.json.tmp      Python 管理
├── restore.json           Python 管理
└── data/                  恢复前切给 barman，恢复后切给 postgres
```

`.lock` 不需要 provisioning 提前创建。`data/` 初始由宿主机操作者所有，开始恢复前通过选定 Barman 容器切换给 `barman:barman`。文件恢复完成后继续保持该所有权；只有执行 `start` 流程时，才通过已经验证版本的 PostgreSQL 镜像切换给 `postgres:postgres`。父目录与元数据文件不得被容器递归修改。

恢复根目录固定使用 mode `0711`。宿主机操作者可以管理其中的文件；其他 UID（包括可配置的 Barman UID）只能穿过该目录访问已经归自己所有的 `data/`，不能列出目录内容。`data/` 固定使用 mode `0700`，所有权按恢复阶段切换。

`.lock`、`.barman-restore.lock`、`.restore.json.tmp`、`restore.json`、`barman-restore.log` 与 `postgres-restore.log` 都使用 mode `0600`。因此即使其他用户知道文件名，也不能读取恢复来源、日志或锁诊断内容。

两把锁文件都由宿主机 Python 按需创建，并归运行恢复工具的宿主机用户所有。`.lock` 由宿主机工具持有；`.barman-restore.lock` 通过 bind mount 暴露给 Barman 容器，容器内 root 只负责打开并持有它，不负责创建、删除或改变权限。

如果改由另一个宿主机用户运行恢复工具，用户必须显式调整恢复根目录的操作者所有权；工具不通过 root helper 容器绕过父目录权限。

### 恢复目录是单份固定恢复槽

`/srv/native-docker/postgres-restore/data` 只容纳一份当前恢复结果，不按 server、备份 ID 或时间戳创建多份恢复目录。

恢复 task 启动前必须检查 `data/`、`restore.json`、`.restore.json.tmp`、`barman-restore.log` 与 `postgres-restore.log`；任一恢复产物已经存在就立即拒绝恢复，不提供覆盖选项，也不自动删除或移动已有结果。这样可以防止误操作破坏仍待验证的数据。

重新恢复前，用户通过独立、显式的清理 task 停止临时 PostgreSQL 并清空恢复目录。恢复 task 自身不承担清理职责。固定的恢复实例身份暂定为：

```text
宿主机目录  /srv/native-docker/postgres-restore/data
Docker 卷   barman-restore-postgres-data
容器名称    postgres-restore
宿主机端口  5433
```

### 生产与恢复实例统一使用 bind-backed local volume

生产 PostgreSQL 与恢复验证 PostgreSQL 都使用 Docker local driver 的 bind 模式。真实数据身份是稳定宿主机路径，Docker volume 对象只负责可重建的挂载声明：

```yaml
volumes:
  postgres-data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /srv/native-docker/postgres

  barman-restore-postgres-data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /srv/native-docker/postgres-restore/data
```

安装 task 负责创建精确的末级空目录。首次部署时，生产目录可以利用 Docker volume populate 初始化 PostgreSQL 数据目录的 UID、GID、mode 和镜像预置内容。恢复父目录与 `data/` 由 provisioning 创建，`restore.json` 不得放入 `PGDATA`。

如果目录已经包含生产或恢复数据，安装和恢复流程不得递归 `chown`、清空或覆盖目录内容。恢复实例也不需要先启动 PostgreSQL 来初始化普通 named volume，Barman 可以直接向稳定宿主机目录恢复数据。

生产和恢复使用相同的存储模型，使日常部署、恢复演练和后续人工切换面对相同的挂载语义，不需要 tar 导出、导入，也不依赖 Docker 内部的 `_data` 路径。
