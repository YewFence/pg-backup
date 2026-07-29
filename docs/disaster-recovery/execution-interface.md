# 执行环境与命令入口

[返回灾难恢复设计总览](../disaster-recovery.md)

## 已确认决策

### 每次恢复绑定一个已部署的 Barman 容器

恢复工具不把 edge 与 offsite 的 server 合并成虚拟列表。每次恢复先确定一个 Barman 容器，后续的 server 查询、备份查询和恢复命令都在该容器内执行，因此自然使用该部署已有的 Barman 配置、catalog、数据卷和 S3 凭据。

容器选择遵循以下输入优先级：

1. 命令行参数显式指定的容器；
2. 环境变量显式指定的容器；
3. 自动探测名称中包含 `barman` 的可用容器。

显式指定的容器不存在或不可用时立即报错，不静默回退到其他容器。自动探测只有一个候选时直接使用；有多个候选时通过交互界面选择；没有候选时报告如何通过参数或环境变量显式指定。

工具选定容器后，必须先通过实际执行 Barman 命令验证它，而不能仅凭容器名称认定其有效。常驻 Barman 容器本身就是所有后续命令的执行环境，不在宿主机重新拼装 edge/offsite 的 Compose 配置。

### 只支持当前主机的 Docker daemon

恢复工具只支持 Python 进程、Docker daemon、Barman 容器和 `/srv/native-docker` 宿主机路径都位于同一台机器的部署方式。启动时必须检查当前 Docker context；endpoint 不是本机 Unix socket 时立即拒绝执行。

第一版不支持通过 SSH/TCP Docker context 操作远端 daemon，也不通过 `DOCKER_HOST` 编排异地主机。否则 Python 在本机看到的路径与远端 volume `device` 所属文件系统不同，目录状态、安全校验和恢复记录都不可信。

需要使用异地主机上的 offsite 备份时，用户登录该主机并在当地运行恢复工具。需要把恢复结果送往另一台主机时，由用户自行使用 rsync 等方式传输 `/srv/native-docker/postgres-restore` 中的数据；跨主机传输、生产替换与目标机启动不属于本工具职责。

### Docker 操作始终使用当前宿主机用户

恢复工具的所有 Docker CLI 调用都使用启动 mise task 的当前宿主机用户。工具不调用 `sudo docker`，也不在权限失败后自动切换到 root、另一个 Docker context 或另一套 daemon。

能够访问 Docker daemon 的当前用户通常可以直接指定容器内执行用户：

```bash
docker exec --user root <barman-container> ...
```

这不要求宿主机进程以 root 身份运行。前置检查必须分别报告以下问题，不能笼统地建议用 sudo 重试：

```text
无法访问当前 Docker daemon
当前 endpoint 不是受支持的本机 Unix socket
无法在候选容器中以 root 执行
容器内不存在 barman 用户
/restore bind mount 不可访问或其 data 目录不可修改
```

中途切换到 `sudo docker` 可能改变 Docker context、环境变量、凭据和实际 daemon，因此不属于故障回退路径。

### Python 负责控制流程，Questionary 负责交互

恢复工具使用 Python 实现，不用 Shell 承担容器发现、参数优先级、结构化输出解析、目录安全检查和多阶段错误处理。mise task 通过 uv 启动 Python 程序。

实现职责划分为：

```text
argparse                 命令行参数与非交互模式
subprocess               调用 Docker CLI
json、pathlib 等标准库   解析结果与检查宿主机路径
Questionary              交互式选择、输入与确认
```

恢复工具继续调用用户日常使用的 Docker CLI，不直接连接 Docker socket，也不引入 Docker Python SDK。这样可以沿用宿主机现有的 Docker context、认证和权限模型。

Questionary 只负责展示层：

- 从多个候选中选择 Barman 容器；
- 选择 Barman server；
- 展示工具自动选择的基础备份；
- 输入目标恢复时间；
- 展示确认提示并允许用户取消。

核心恢复逻辑不得依赖交互提示。用户提供完整参数时，工具必须可以在非交互环境中运行，并且不得加载提示流程。例如：

```bash
mise run barman:restore -- \
  --container barman-offsite \
  --server postgres-offsite \
  --backup 20260729T010203 \
  --target-time '2026-07-29 08:30:00+08:00' \
  --yes
```

输入优先级为命令行参数、环境变量、自动探测或交互提示。显式参数无效时立即报错，不通过交互提示悄悄替换用户输入。

恢复写入需要明确授权：

```text
有 TTY    展示完整计划，通过 Questionary 最终确认后执行
无 TTY    参数齐全且显式传入 --yes 后才执行
```

无 TTY 时如果缺少 `--yes`，工具只输出解析和验证后的恢复计划，不创建记录、不切换目录所有权、不执行 Barman。参数齐全不等于授权写入固定恢复槽；`--yes` 不得由环境变量隐式启用。

`barman:restore:start` 本身是明确的启动命令，不额外要求 `--yes`。默认清理只拆除临时容器并保留数据，也不要求 `--yes`；永久删除继续使用 `--delete-restored-data-permanently` 与完整 `--confirm-delete-path` 的双重确认，不复用 `--yes`。

Python 依赖统一由 uv 管理，不手工维护锁文件。Gum 不再作为该恢复流程的依赖。

### mise 提供四个独立的命名空间入口

根据 mise task 的职责边界，文件恢复、权限切换、启动验证实例和清理是四个独立工作流，不建模为一个要求用户额外记忆位置参数的万能 task。对外提供：

```text
mise run barman:restore
mise run barman:restore:permissions
mise run barman:restore:start
mise run barman:restore:clean
```

四个 task 统一注册在 `mise.toml`，不依赖 `mise-tasks/` 的自动发现。它们只提供稳定、可发现的项目命令入口和简短描述，不复制恢复业务逻辑。

实现该设计时，现有 `postgres-install`、`barman-edge-install`、`barman-offsite-install` 与 `barman-smoke` 也迁移为 `mise.toml` 中的显式 `file` task，保留当前对外命令名。实现脚本移出会被 mise 自动扫描的 `mise-tasks/` 目录，避免同一任务同时来自自动发现与显式配置。安装脚本继续作为独立文件，不为统一形式而塞入恢复 Python CLI。

共享实现位于一个 Python CLI 模块中，并使用四个内部命令：

```text
restore
permissions
start
clean
```

mise task 分别调用对应命令。Questionary、Docker 检查、锁、恢复记录、Compose 调用和清理逻辑都留在 Python 模块中，不拆成四个独立实现文件。

普通恢复参数与业务校验仍由 Python 维护。`permissions` 与 `start` 共享的 `--restore-root`、`--postgres-image` 等参数由 mise Usage DSL 声明，因为 mise 需要在执行父 task 前把运行时值显式传给依赖 task。Python 读取 mise 已解析的值，不再为这组共享参数维护第二套公共 argparse 定义。

`start` 使用结构化 `depends`，把共享 Usage 值传给权限 task：

```toml
[tasks."barman:restore:start"]
depends = [
  { task = "barman:restore:permissions", args = [
    "--restore-root", "{{usage.restore_root}}",
    "--postgres-image", "{{usage.postgres_image}}",
  ] },
]
```

`permissions` 可以独立运行；`start` 每次执行前也一定先运行同一个 task。权限 task 只验证恢复记录与镜像主版本，并把 `data/` 切换为镜像内的 `postgres:postgres`，不创建或启动 PostgreSQL 容器。

由于 mise 在 start 本体之前执行 depends，permissions 必须先检查名为 `postgres-restore` 的容器对象不存在。只要该容器处于 running、stopped、exited 或 created 任一状态，permissions 立即拒绝，mise 不再继续执行 start。start 本体仍重复执行同一个便宜检查，防止两个 task 之间出现竞态。

permissions 是有记录的幂等任务。`restore.json` 保存：

```json
{
  "permissions": {
    "status": "completed",
    "image": "postgres:17.10",
    "image_id": "sha256:...",
    "postgres_uid": 999,
    "postgres_gid": 999,
    "completed_at": "2026-07-29T02:00:00Z"
  }
}
```

开始递归转换前，permissions 把状态原子更新为 `running`；失败时更新为 `failed`。再次运行允许从 `running` 或 `failed` 状态重新执行完整转换。

只有同时满足以下条件时才成功 no-op：

- permissions 状态为 `completed`；
- 当前候选镜像 ID 与记录完全一致，不能只比较可能被重新指向的标签；
- `data/` 根目录的数字 UID/GID 与从镜像内 `postgres:postgres` 解析出的值一致。

任一条件不满足时，task 重新验证 PostgreSQL 主版本并递归转换整个 `data/`，成功后更新记录。

用户通过 mise task 的 Usage 接口传入参数，例如：

```bash
mise run barman:restore -- \
  --container barman-offsite \
  --server postgres-offsite \
  --target-time '2026-07-29T08:30:00+08:00' \
  --yes

mise run barman:restore:start -- \
  --restore-root /srv/native-docker/postgres-restore \
  --postgres-image postgres:17.10

mise run barman:restore:clean -- \
  --delete-restored-data-permanently \
  --confirm-delete-path /srv/native-docker/postgres-restore
```

`mise run barman:restore:start --help` 与 `mise run barman:restore:permissions --help` 由 mise 展示 Usage 接口。task 本身保持薄层，不增加转发脚本链，也不把 Python 实现做成 PEP 723 单文件脚本；项目使用 uv 管理统一的 Python 项目依赖与锁文件。
