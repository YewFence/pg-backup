from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

GIB = 1024**3
DONE_STATUSES = {"DONE", "COMPLETED"}
POSTGRES_LOG_LIMIT = 10 * 1024 * 1024
BARMAN_LOG_LIMIT = 50 * 1024 * 1024
TOOL_VERSION = 1
DEFAULT_WAL_SEGMENT_SIZE = 16 * 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parents[1]
RESTORE_COMPOSE_FILE = REPO_ROOT / "postgres-restore" / "compose.yaml"
WAL_SEGMENT_PATTERN = re.compile(r"^[0-9A-F]{24}$")
WAL_HISTORY_PATTERN = re.compile(r"^[0-9A-F]{8}\.history$")
WAL_BACKUP_PATTERN = re.compile(r"^[0-9A-F]{24}\.[0-9A-F]{8}\.backup$")


class RecoveryError(RuntimeError):
    """Raised when a recovery safety or validation requirement is not met."""


@dataclass(frozen=True)
class TargetTime:
    original: str
    value: datetime

    @property
    def utc_text(self) -> str:
        return self.value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Backup:
    backup_id: str
    status: str
    begin_time: datetime
    end_time: datetime
    cluster_size: int | None = None
    wal_size: int | None = None
    postgres_version: str | None = None
    tablespaces: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class SpaceEstimate:
    cluster_size: int
    wal_size: int | None
    safety_margin: int
    required: int
    available: int
    complete: bool


@dataclass(frozen=True)
class WalInventory:
    names: tuple[str, ...]
    wal_segment_size: int
    estimated_size: int

    @property
    def segment_names(self) -> tuple[str, ...]:
        return tuple(name for name in self.names if WAL_SEGMENT_PATTERN.fullmatch(name))

    @property
    def last_segment(self) -> str | None:
        return self.segment_names[-1] if self.segment_names else None


@dataclass(frozen=True)
class RuntimeConfig:
    restore_root: Path
    production_data: Path
    volume_name: str
    postgres_container: str
    compose_project: str
    postgres_image: str | None
    network_name: str
    bind_address: str
    port: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> RuntimeConfig:
        port_text = environment.get("POSTGRES_RESTORE_PORT", "5433")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise RecoveryError(f"POSTGRES_RESTORE_PORT 不是有效端口: {port_text!r}") from exc
        if not 1 <= port <= 65535:
            raise RecoveryError(f"POSTGRES_RESTORE_PORT 超出有效范围: {port}")
        return cls(
            restore_root=Path(
                environment.get("POSTGRES_RESTORE_ROOT", "/srv/native-docker/postgres-restore")
            ),
            production_data=Path(
                environment.get("POSTGRES_DATA_PATH", "/srv/native-docker/postgres")
            ),
            volume_name=environment.get(
                "POSTGRES_RESTORE_VOLUME_NAME", "barman-restore-postgres-data"
            ),
            postgres_container=environment.get(
                "POSTGRES_RESTORE_CONTAINER_NAME", "postgres-restore"
            ),
            compose_project=environment.get("POSTGRES_RESTORE_PROJECT", "postgres-restore"),
            postgres_image=environment.get("POSTGRES_IMAGE") or None,
            network_name=environment.get("POSTGRES_NETWORK_NAME", "pg-net"),
            bind_address=environment.get("POSTGRES_RESTORE_BIND_ADDRESS", "127.0.0.1"),
            port=port,
        )


class DockerCLI:
    def __init__(self, *, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment or os.environ)

    def run(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        capture: bool = True,
        extra_environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment.copy()
        if extra_environment:
            environment.update(extra_environment)
        process = subprocess.run(
            ["docker", *arguments],
            check=False,
            text=True,
            input=input_text,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            env=environment,
        )
        if check and process.returncode != 0:
            output = (process.stdout or "").strip()
            summary = output[-4000:] if output else "无输出"
            raise RecoveryError(
                f"Docker 命令失败 ({process.returncode}): docker {' '.join(arguments)}\n{summary}"
            )
        return process

    def json(self, arguments: list[str]) -> Any:
        process = self.run(arguments)
        try:
            return json.loads(process.stdout or "")
        except json.JSONDecodeError as exc:
            raise RecoveryError(
                f"Docker 命令没有返回有效 JSON: docker {' '.join(arguments)}"
            ) from exc

    def container_exists(self, name: str) -> bool:
        process = self.run(["container", "inspect", name], check=False)
        return process.returncode == 0

    def volume_exists(self, name: str) -> bool:
        process = self.run(["volume", "inspect", name], check=False)
        return process.returncode == 0


def validate_local_docker(docker: DockerCLI) -> None:
    docker_host = docker.environment.get("DOCKER_HOST")
    if docker_host and not is_local_docker_endpoint(docker_host):
        raise RecoveryError(f"当前 DOCKER_HOST 不是受支持的本机 Unix socket: {docker_host}")
    context = docker.run(["context", "show"]).stdout.strip()
    payload = docker.json(["context", "inspect", context])
    try:
        endpoint = payload[0]["Endpoints"]["docker"]["Host"]
    except (IndexError, KeyError, TypeError) as exc:
        raise RecoveryError("无法从当前 Docker context 读取 endpoint") from exc
    if not isinstance(endpoint, str) or not is_local_docker_endpoint(endpoint):
        raise RecoveryError(f"当前 Docker endpoint 不是受支持的本机 Unix socket: {endpoint}")
    docker.run(["info", "--format", "{{.ServerVersion}}"])


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RecoveryError(f"恢复记录不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RecoveryError(f"恢复记录不是有效 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RecoveryError(f"恢复记录根节点必须是 JSON object: {path}")
    return payload


def write_record(restore_root: Path, record: Mapping[str, Any]) -> None:
    atomic_write_json(
        restore_root / "restore.json",
        record,
        temporary=restore_root / ".restore-record.update.tmp",
    )


def validate_completed_restore(config: RuntimeConfig) -> tuple[dict[str, Any], Path]:
    validate_isolated_paths(config.production_data, config.restore_root)
    if config.restore_root.is_symlink() or not config.restore_root.is_dir():
        raise RecoveryError(f"恢复根目录必须是已存在的真实目录: {config.restore_root}")
    if (config.restore_root / ".restore.json.tmp").exists():
        raise RecoveryError("存在未完成的 .restore.json.tmp，拒绝启动或转换权限")
    record = read_json_file(config.restore_root / "restore.json")
    if record.get("status") != "completed" or record.get("file_restore_status") != "completed":
        raise RecoveryError("restore.json 没有记录已完成的 Barman 文件恢复")
    if record.get("restore_root") != str(config.restore_root):
        raise RecoveryError("restore.json 中的恢复根目录与当前输入不一致")
    data_path = config.restore_root / "data"
    if data_path.is_symlink() or not data_path.is_dir():
        raise RecoveryError(f"恢复数据路径必须是已存在的真实目录: {data_path}")
    return record, data_path


def ensure_restore_volume(docker: DockerCLI, volume_name: str, data_path: Path) -> None:
    if not docker.volume_exists(volume_name):
        docker.run(
            [
                "volume",
                "create",
                "--driver",
                "local",
                "--opt",
                "type=none",
                "--opt",
                "o=bind",
                "--opt",
                f"device={data_path}",
                volume_name,
            ]
        )
    actual = volume_device_from_inspect(docker.json(["volume", "inspect", volume_name]))
    if actual != data_path:
        raise RecoveryError(f"恢复 volume {volume_name} 指向 {actual}，预期精确指向 {data_path}")


def ensure_barman_lock_is_free(restore_root: Path) -> None:
    lock_path = restore_root / ".barman-restore.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryError("Barman 恢复进程仍持有写盘锁，当前操作已拒绝") from exc
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def docker_exec_json(docker: DockerCLI, container: str, arguments: list[str]) -> Any:
    process = docker.run(["exec", container, *arguments])
    try:
        return json.loads(process.stdout or "")
    except json.JSONDecodeError as exc:
        raise RecoveryError(f"容器 {container} 中的 Barman 命令没有返回有效 JSON") from exc


def discover_barman_containers(docker: DockerCLI) -> list[str]:
    process = docker.run(
        ["ps", "--filter", "name=barman", "--format", "{{.Names}}"],
        check=True,
    )
    return sorted(name for name in process.stdout.splitlines() if name)


def validate_barman_container(docker: DockerCLI, container: str) -> Path:
    if not docker.container_exists(container):
        raise RecoveryError(f"显式指定的 Barman 容器不存在或不可访问: {container}")
    source = barman_restore_source_from_inspect(docker.json(["container", "inspect", container]))
    docker.run(
        [
            "exec",
            "--user",
            "root",
            container,
            "sh",
            "-ec",
            "command -v barman >/dev/null; command -v gosu >/dev/null; "
            "command -v flock >/dev/null; id barman >/dev/null; "
            "test -d /restore/data; test -w /restore/data",
        ]
    )
    return source


def parse_server_names(payload: Any) -> list[str]:
    if isinstance(payload, list):
        names = [
            item if isinstance(item, str) else item.get("name")
            for item in payload
            if isinstance(item, (str, dict))
        ]
    elif isinstance(payload, dict):
        if "servers" in payload:
            return parse_server_names(payload["servers"])
        names = list(payload)
    else:
        raise RecoveryError("无法识别 Barman server 列表 JSON")
    result = [name for name in names if isinstance(name, str) and name not in {"all", "global"}]
    if not result:
        raise RecoveryError("所选 Barman 容器没有可用 server")
    return sorted(set(result))


def format_bytes(value: int | None) -> str:
    if value is None:
        return "未知"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    current = float(value)
    for unit in units:
        if current < 1024 or unit == units[-1]:
            return f"{current:.1f} {unit}"
        current /= 1024
    return f"{value} B"


def postgres_major(version: str) -> int:
    match = re.search(r"(?:PostgreSQL\s+)?(\d+)(?:\.\d+)?", version)
    if match is None:
        raise RecoveryError(f"无法识别 PostgreSQL 主版本: {version!r}")
    return int(match.group(1))


def validate_postgres_image(
    docker: DockerCLI, image: str, expected_major: int
) -> tuple[str, int, int]:
    if docker.run(["image", "inspect", image], check=False).returncode != 0:
        raise RecoveryError(f"验证镜像尚未存在于本机，工具不会自动拉取: {image}")
    image_payload = docker.json(["image", "inspect", image])
    try:
        image_id = image_payload[0]["Id"]
    except (IndexError, KeyError, TypeError) as exc:
        raise RecoveryError(f"无法读取验证镜像 ID: {image}") from exc
    if not isinstance(image_id, str):
        raise RecoveryError(f"验证镜像 ID 无效: {image}")
    version_output = docker.run(
        ["run", "--rm", "--entrypoint", "postgres", image, "--version"]
    ).stdout.strip()
    actual_major = postgres_major(version_output)
    if actual_major != expected_major:
        raise RecoveryError(
            f"验证镜像 PostgreSQL 主版本为 {actual_major}，备份主版本为 {expected_major}"
        )
    ids = docker.run(
        [
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            image,
            "-ec",
            "id -u postgres; id -g postgres",
        ]
    ).stdout.splitlines()
    if len(ids) != 2 or not all(value.isdigit() for value in ids):
        raise RecoveryError(f"无法从验证镜像解析 postgres UID/GID: {image}")
    return image_id, int(ids[0]), int(ids[1])


def save_postgres_logs(docker: DockerCLI, container: str, restore_root: Path) -> None:
    process = docker.run(["logs", container], check=False)
    if process.returncode != 0:
        raise RecoveryError(f"无法读取 {container} 的 PostgreSQL 日志")
    path = restore_root / "postgres-restore.log"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(tail_bytes((process.stdout or "").encode(), POSTGRES_LOG_LIMIT))
        output.flush()
        os.fsync(output.fileno())


def trim_file(path: Path, limit: int) -> None:
    if not path.exists() or path.stat().st_size <= limit:
        return
    with path.open("rb") as source:
        head = source.read(min(1024 * 1024, limit // 5))
        source.seek(-max(0, limit - len(head)), os.SEEK_END)
        tail = source.read()
    descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(head)
        output.write(b"\n--- log truncated ---\n")
        output.write(tail[-(limit - len(head) - 23) :])


def now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _questionary() -> Any:
    try:
        import questionary
    except ImportError as exc:
        raise RecoveryError("交互模式需要通过 uv 安装 Questionary") from exc
    return questionary


def select_value(*, explicit: str | None, values: list[str], label: str, interactive: bool) -> str:
    if explicit is not None:
        if explicit not in values:
            raise RecoveryError(f"显式指定的{label}无效: {explicit}")
        return explicit
    if len(values) == 1:
        return values[0]
    if not values:
        raise RecoveryError(f"没有可用的{label}")
    if not interactive:
        raise RecoveryError(f"发现多个{label}，非交互模式必须显式指定")
    selected = _questionary().select(f"选择{label}", choices=values).ask()
    if not isinstance(selected, str):
        raise RecoveryError("操作已取消")
    return selected


def validated_barman_candidates(docker: DockerCLI) -> tuple[list[str], dict[str, str]]:
    valid: list[str] = []
    rejected: dict[str, str] = {}
    for container in discover_barman_containers(docker):
        try:
            validate_barman_container(docker, container)
        except RecoveryError as exc:
            rejected[container] = str(exc)
        else:
            valid.append(container)
    return valid, rejected


def choose_barman_container(
    docker: DockerCLI, explicit: str | None, *, interactive: bool
) -> tuple[str, Path]:
    if explicit is not None:
        return explicit, validate_barman_container(docker, explicit)
    candidates, rejected = validated_barman_candidates(docker)
    if not candidates:
        details = "; ".join(f"{name}: {reason}" for name, reason in rejected.items())
        suffix = f"。候选拒绝原因: {details}" if details else ""
        raise RecoveryError(
            "没有自动发现可用的 Barman 容器，请通过 --container 或 BARMAN_CONTAINER 指定" + suffix
        )
    selected = select_value(
        explicit=None, values=candidates, label="Barman 容器", interactive=interactive
    )
    return selected, validate_barman_container(docker, selected)


def list_barman_servers(docker: DockerCLI, container: str) -> list[str]:
    payload = docker_exec_json(docker, container, ["barman", "-f", "json", "list-servers"])
    return parse_server_names(payload)


def list_barman_backups(docker: DockerCLI, container: str, server: str) -> list[Backup]:
    payload = docker_exec_json(docker, container, ["barman", "-f", "json", "list-backups", server])
    return parse_backup_payload(payload)


def backup_details(docker: DockerCLI, container: str, server: str, selected: Backup) -> Backup:
    payload = docker_exec_json(
        docker,
        container,
        ["barman", "-f", "json", "show-backup", server, selected.backup_id],
    )
    try:
        details = parse_backup_payload(payload)
    except RecoveryError:
        return selected
    matching = next((backup for backup in details if backup.backup_id == selected.backup_id), None)
    return matching or selected


def _find_json_field(value: Any, names: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in names:
                return item
            nested = _find_json_field(item, names)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _find_json_field(item, names)
            if nested is not None:
                return nested
    return None


def parse_wal_inventory(output: str, *, wal_segment_size: int) -> WalInventory:
    if (
        wal_segment_size < 1024 * 1024
        or wal_segment_size > GIB
        or wal_segment_size & (wal_segment_size - 1)
        or (2**32) % wal_segment_size != 0
    ):
        raise RecoveryError(f"Barman 返回了无效的 WAL segment size: {wal_segment_size}")

    names: list[str] = []
    seen: set[str] = set()
    for line in output.splitlines():
        path = line.strip()
        if not path:
            continue
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if not any(
            pattern.fullmatch(name)
            for pattern in (WAL_SEGMENT_PATTERN, WAL_HISTORY_PATTERN, WAL_BACKUP_PATTERN)
        ):
            raise RecoveryError(f"Barman WAL 清单包含无法识别的文件名: {name!r}")
        if name in seen:
            raise RecoveryError(f"Barman WAL 清单包含重复文件名: {name}")
        seen.add(name)
        names.append(name)
    if not names:
        raise RecoveryError("Barman WAL 清单为空，无法生成自包含恢复结果")

    segments_per_log = (2**32) // wal_segment_size
    by_timeline: dict[int, list[tuple[int, str]]] = {}
    for name in names:
        if not WAL_SEGMENT_PATTERN.fullmatch(name):
            continue
        timeline = int(name[:8], 16)
        log = int(name[8:16], 16)
        segment = int(name[16:24], 16)
        if segment >= segments_per_log:
            raise RecoveryError(f"WAL 文件名与 segment size 不兼容: {name}")
        by_timeline.setdefault(timeline, []).append((log * segments_per_log + segment, name))
    for timeline, entries in by_timeline.items():
        ordered = sorted(entries)
        for previous, current in pairwise(ordered):
            if current[0] != previous[0] + 1:
                raise RecoveryError(
                    f"Barman WAL 清单在 timeline {timeline:08X} 上不连续: "
                    f"{previous[1]} -> {current[1]}"
                )

    return WalInventory(
        names=tuple(names),
        wal_segment_size=wal_segment_size,
        estimated_size=len(names) * wal_segment_size,
    )


def barman_wal_segment_size(server_details: Any) -> int:
    value = _find_json_field(
        server_details,
        {"xlog_segment_size", "wal_segment_size", "xlog-segment-size"},
    )
    if value is None:
        return DEFAULT_WAL_SEGMENT_SIZE
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RecoveryError(f"Barman WAL segment size 不是有效字节数: {value!r}") from exc


def barman_uses_cloud_wal(server_details: Any) -> bool:
    value = _find_json_field(server_details, {"wals_directory", "wals-directory"})
    return isinstance(value, str) and "://" in value


def list_barman_wal_inventory(
    docker: DockerCLI,
    container: str,
    server: str,
    backup_id: str,
    *,
    wal_segment_size: int,
) -> WalInventory:
    process = docker.run(
        [
            "exec",
            container,
            "barman",
            "list-files",
            server,
            backup_id,
            "--target",
            "wal",
        ]
    )
    return parse_wal_inventory(process.stdout or "", wal_segment_size=wal_segment_size)


def barman_wal_archive_times(
    docker: DockerCLI, container: str, server_name: str
) -> dict[str, datetime]:
    script = (
        "import json, sys\n"
        "from barman.config import Config\n"
        "from barman.infofile import WalFileInfo\n"
        "from barman.server import Server\n"
        "config = Config()\n"
        "config.load_configuration_files_directory()\n"
        "server_config = config.get_server(sys.argv[1])\n"
        "if server_config is None:\n"
        "    raise SystemExit('unknown Barman server')\n"
        "server = Server(server_config)\n"
        "try:\n"
        "    with server.xlogdb() as catalog:\n"
        "        entries = [WalFileInfo.from_xlogdb_line(line) for line in catalog]\n"
        "    print(json.dumps({entry.name: entry.time for entry in entries}))\n"
        "finally:\n"
        "    server.close()\n"
    )
    payload = docker_exec_json(docker, container, ["python3", "-c", script, server_name])
    if not isinstance(payload, dict):
        raise RecoveryError("Barman xlogdb 没有返回有效的 WAL 归档时间")
    result: dict[str, datetime] = {}
    for name, timestamp in payload.items():
        if not isinstance(name, str) or not isinstance(timestamp, (int, float)):
            raise RecoveryError("Barman xlogdb 包含无法识别的 WAL 归档时间")
        result[name] = datetime.fromtimestamp(timestamp, UTC)
    return result


def validate_target_wal_coverage(
    target: TargetTime | None,
    inventory: WalInventory,
    archive_times: Mapping[str, datetime],
) -> None:
    if target is None:
        return
    last_segment = inventory.last_segment
    if last_segment is None:
        raise RecoveryError("Barman WAL 清单没有可用于目标时间恢复的 segment")
    archived_at = archive_times.get(last_segment)
    if archived_at is None:
        raise RecoveryError(f"Barman xlogdb 缺少最后 WAL 的归档时间: {last_segment}")
    if archived_at < target.value:
        raise RecoveryError(
            f"Barman WAL 尚未覆盖目标时间: 最后 WAL {last_segment} "
            f"归档于 {archived_at.astimezone(UTC).isoformat()}"
        )


def materialize_cloud_wals(
    docker: DockerCLI,
    *,
    container: str,
    server: str,
    inventory: WalInventory,
) -> None:
    docker.run(
        [
            "exec",
            "--user",
            "root",
            container,
            "sh",
            "-ec",
            "install -d -m 700 -o barman -g barman /restore/data/barman_wal",
        ]
    )
    shell = (
        "flock -n /restore/.barman-restore.lock "
        'gosu barman barman cloud-wal-restore "$1" "$2" '
        '"/restore/data/barman_wal/$2" 2>&1 | tee -a /restore/barman-restore.log'
    )
    for name in inventory.names:
        process = docker.run(
            [
                "exec",
                "--user",
                "root",
                container,
                "bash",
                "-o",
                "pipefail",
                "-c",
                shell,
                "barman-cloud-wal",
                server,
                name,
            ],
            check=False,
            capture=False,
        )
        if process.returncode != 0:
            raise RecoveryError(f"云 WAL 物化失败: {name}，退出码 {process.returncode}")


def inspect_materialized_wals(
    docker: DockerCLI, *, container: str, inventory: WalInventory
) -> tuple[int, dict[str, int]]:
    script = (
        "import json\n"
        "from pathlib import Path\n"
        "root = Path('/restore/data/barman_wal')\n"
        "print(json.dumps({p.name: p.stat().st_size for p in root.iterdir() if p.is_file()}))\n"
    )
    payload = docker_exec_json(docker, container, ["python3", "-c", script])
    if not isinstance(payload, dict) or not all(
        isinstance(name, str) and isinstance(size, int) and size >= 0
        for name, size in payload.items()
    ):
        raise RecoveryError("无法读取物化 WAL 文件清单")
    missing = [name for name in inventory.names if name not in payload]
    if missing:
        raise RecoveryError("恢复结果缺少物化 WAL: " + ", ".join(missing[:10]))
    sizes = {name: payload[name] for name in inventory.names}
    return sum(sizes.values()), sizes


def rewrite_recovery_config(docker: DockerCLI, *, container: str) -> str:
    script = (
        "import os, re\n"
        "from pathlib import Path\n"
        "path = Path('/restore/data/postgresql.auto.conf')\n"
        "lines = path.read_text(encoding='utf-8').splitlines()\n"
        "setting = re.compile(r'^\\s*(restore_command|recovery_end_command)\\s*=')\n"
        "lines = [line for line in lines if not setting.match(line)]\n"
        "lines.extend([\n"
        "    \"restore_command = 'cp /var/lib/postgresql/data/barman_wal/%f %p'\",\n"
        "    \"recovery_end_command = 'rm -rf /var/lib/postgresql/data/barman_wal'\",\n"
        "])\n"
        "temporary = path.with_name('.postgresql.auto.conf.tmp')\n"
        "temporary.write_text('\\n'.join(lines) + '\\n', encoding='utf-8')\n"
        "os.chmod(temporary, 0o600)\n"
        "os.replace(temporary, path)\n"
        "print(path.read_text(encoding='utf-8'), end='')\n"
    )
    return docker.run(["exec", "--user", "barman", container, "python3", "-c", script]).stdout


def validate_self_contained_recovery_config(content: str) -> None:
    if re.search(r"\b(?:cloud-wal-restore|barman-wal-restore|barman\s+get-wal)\b", content):
        raise RecoveryError("恢复配置仍然依赖外部 Barman WAL 命令")
    expected = "restore_command = 'cp /var/lib/postgresql/data/barman_wal/%f %p'"
    if expected not in {line.strip() for line in content.splitlines()}:
        raise RecoveryError("恢复配置没有使用固定 PGDATA 内的本地 WAL 路径")


def validate_production_volume_if_present(
    docker: DockerCLI, config: RuntimeConfig, restore_root: Path
) -> None:
    volume_name = docker.environment.get("POSTGRES_DATA_VOLUME_NAME", "postgres_data")
    if not docker.volume_exists(volume_name):
        return
    try:
        production_device = volume_device_from_inspect(
            docker.json(["volume", "inspect", volume_name])
        )
    except RecoveryError:
        return
    validate_isolated_paths(production_device, restore_root)
    if production_device != config.production_data:
        raise RecoveryError(
            f"生产 volume {volume_name} 实际指向 {production_device}，"
            f"与 POSTGRES_DATA_PATH={config.production_data} 不一致"
        )


def validate_restore_root_layout(config: RuntimeConfig) -> Path:
    validate_isolated_paths(config.production_data, config.restore_root)
    root = config.restore_root
    data = root / "data"
    if root.is_symlink() or not root.is_dir():
        raise RecoveryError(f"恢复根目录必须由管理员预先创建为真实目录: {root}")
    if data.is_symlink() or not data.is_dir():
        raise RecoveryError(f"恢复数据目录必须由管理员预先创建为真实目录: {data}")
    if root.stat().st_uid != os.getuid():
        raise RecoveryError(f"恢复根目录不属于当前宿主机用户: {root}")
    if root.stat().st_mode & 0o777 != 0o711:
        raise RecoveryError(f"恢复根目录 mode 必须精确为 0711: {root}")
    if data.stat().st_mode & 0o777 != 0o700:
        raise RecoveryError(f"恢复数据目录 mode 必须精确为 0700: {data}")
    return data


def available_bytes(path: Path) -> int:
    status = os.statvfs(path)
    return status.f_bavail * status.f_frsize


def display_restore_plan(
    *,
    container: str,
    server: str,
    backup: Backup,
    target: TargetTime | None,
    data_path: Path,
    estimate: SpaceEstimate,
    last_wal: str | None,
) -> None:
    rows = [
        ("Barman 容器", container),
        ("Barman server", server),
        ("恢复目标", target.original if target else "最新可达，非固定终点"),
        ("UTC 时间", target.utc_text if target else "动态"),
        ("基础备份", backup.backup_id),
        ("备份开始时间", backup.begin_time.astimezone(UTC).isoformat()),
        ("备份结束时间", backup.end_time.astimezone(UTC).isoformat()),
        ("开始时最后 WAL", last_wal or "未知"),
        ("PGDATA", str(data_path)),
        ("基础备份展开大小", format_bytes(estimate.cluster_size)),
        ("已知 WAL 大小", format_bytes(estimate.wal_size)),
        ("安全余量", format_bytes(estimate.safety_margin)),
        ("最低需求", format_bytes(estimate.required)),
        ("当前可用", format_bytes(estimate.available)),
        ("估算状态", "完整" if estimate.complete else "不完整"),
    ]
    width = max(len(label) for label, _ in rows)
    print("\n恢复计划")
    for label, value in rows:
        print(f"{label:<{width}}  {value}")


def _restore_record(
    *,
    config: RuntimeConfig,
    container: str,
    server: str,
    backup: Backup,
    target: TargetTime | None,
    estimate: SpaceEstimate,
    last_wal: str | None,
    wal_inventory: WalInventory,
    wal_source: str,
    last_wal_archived_at: datetime | None,
) -> dict[str, Any]:
    version = backup.postgres_version or "unknown"
    major = postgres_major(version) if version != "unknown" else None
    return {
        "tool_version": TOOL_VERSION,
        "status": "running",
        "file_restore_status": "running",
        "container": container,
        "server": server,
        "backup_id": backup.backup_id,
        "backup_begin_time": backup.begin_time.astimezone(UTC).isoformat(),
        "backup_end_time": backup.end_time.astimezone(UTC).isoformat(),
        "postgres_version": version,
        "postgres_major": major,
        "restore_root": str(config.restore_root),
        "target_mode": "time" if target else "latest",
        "target_time_input": target.original if target else None,
        "target_time": target.utc_text if target else None,
        "target_is_fixed": target is not None,
        "target_status": "not_verified",
        "wal_replay_status": "not_started",
        "writable": False,
        "last_wal_at_start": last_wal,
        "last_wal_archived_at_start": (
            last_wal_archived_at.astimezone(UTC).isoformat()
            if last_wal_archived_at is not None
            else None
        ),
        "wal_materialization": {
            "source": wal_source,
            "wal_segment_size": wal_inventory.wal_segment_size,
            "estimated_size": wal_inventory.estimated_size,
            "actual_size": None,
            "files": list(wal_inventory.names),
        },
        "started_at": now_text(),
        "barman_log": "barman-restore.log",
        "space_estimate": {
            "estimated_at": now_text(),
            "cluster_size": estimate.cluster_size,
            "wal_size": estimate.wal_size,
            "safety_margin": estimate.safety_margin,
            "required": estimate.required,
            "available": estimate.available,
            "complete": estimate.complete,
        },
    }


def execute_barman_restore(
    docker: DockerCLI,
    *,
    container: str,
    command: list[str],
) -> subprocess.CompletedProcess[str]:
    shell = (
        'flock -n /restore/.barman-restore.lock gosu barman "$@" '
        "2>&1 | tee -a /restore/barman-restore.log"
    )
    return docker.run(
        [
            "exec",
            "--user",
            "root",
            container,
            "bash",
            "-o",
            "pipefail",
            "-c",
            shell,
            "barman-restore",
            *command,
        ],
        check=False,
        capture=False,
    )


def command_restore(arguments: argparse.Namespace, environment: Mapping[str, str]) -> int:
    docker = DockerCLI(environment=environment)
    validate_local_docker(docker)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    explicit_container = resolve_option(arguments.container, environment, "BARMAN_CONTAINER")
    container, restore_source = choose_barman_container(
        docker, explicit_container, interactive=interactive
    )
    config = replace(RuntimeConfig.from_environment(environment), restore_root=restore_source)
    data_path = validate_restore_root_layout(config)
    validate_production_volume_if_present(docker, config, restore_source)
    if docker.container_exists(config.postgres_container):
        raise RecoveryError(
            f"临时容器 {config.postgres_container} 已存在，请先运行 barman:restore:clean"
        )

    explicit_server = resolve_option(arguments.server, environment, "BARMAN_SERVER")
    server = select_value(
        explicit=explicit_server,
        values=list_barman_servers(docker, container),
        label="Barman server",
        interactive=interactive,
    )
    target_value = resolve_option(arguments.target_time, environment, "BARMAN_TARGET_TIME")
    if target_value is None and interactive:
        mode = (
            _questionary()
            .select(
                "选择恢复目标",
                choices=[
                    ("恢复到最新可达状态", "latest"),
                    ("恢复到指定时间", "time"),
                ],
            )
            .ask()
        )
        if mode is None:
            raise RecoveryError("操作已取消")
        if mode == "time":
            entered = (
                _questionary().text("输入带时区的目标时间，例如 2026-07-29T08:30:00+08:00").ask()
            )
            if not isinstance(entered, str) or not entered:
                raise RecoveryError("操作已取消")
            target_value = entered
    target = parse_target_time(target_value) if target_value else None
    backups = list_barman_backups(docker, container, server)
    explicit_backup = resolve_option(arguments.backup, environment, "BARMAN_BACKUP")
    selected = choose_backup(
        backups,
        target_time=target.value if target else None,
        explicit_backup=explicit_backup,
    )
    selected = backup_details(docker, container, server, selected)
    validate_no_custom_tablespaces(selected.tablespaces)
    docker.run(["exec", container, "barman", "check-backup", server, selected.backup_id])
    server_details = docker_exec_json(
        docker, container, ["barman", "-f", "json", "show-server", server]
    )
    wal_segment_size = barman_wal_segment_size(server_details)
    wal_inventory = list_barman_wal_inventory(
        docker,
        container,
        server,
        selected.backup_id,
        wal_segment_size=wal_segment_size,
    )
    wal_archive_times = barman_wal_archive_times(docker, container, server)
    validate_target_wal_coverage(target, wal_inventory, wal_archive_times)
    cloud_wal = barman_uses_cloud_wal(server_details)
    if selected.cluster_size is None:
        raise RecoveryError("Barman 备份详情缺少 cluster size，无法执行保守磁盘空间检查")
    estimate = estimate_required_space(
        cluster_size=selected.cluster_size,
        wal_size=wal_inventory.estimated_size,
        available=available_bytes(data_path),
        wal_size_complete=target is not None,
    )
    if estimate.available < estimate.required:
        shortfall = estimate.required - estimate.available
        raise RecoveryError(
            f"恢复空间不足: 最低需求 {format_bytes(estimate.required)}，"
            f"当前可用 {format_bytes(estimate.available)}，缺少 {format_bytes(shortfall)}"
        )
    if not estimate.complete and not interactive and not arguments.allow_unknown_space_requirement:
        raise RecoveryError(
            "空间估算不完整；非交互模式必须显式传入 --allow-unknown-space-requirement"
        )
    last_wal = wal_inventory.last_segment
    last_wal_archived_at = wal_archive_times.get(last_wal) if last_wal is not None else None
    display_restore_plan(
        container=container,
        server=server,
        backup=selected,
        target=target,
        data_path=data_path,
        estimate=estimate,
        last_wal=last_wal,
    )
    if interactive:
        if (
            not estimate.complete
            and not _questionary().confirm("空间估算不完整，仍要继续吗？", default=False).ask()
        ):
            raise RecoveryError("操作已取消")
        if not _questionary().confirm("按以上计划写入固定恢复槽？", default=False).ask():
            raise RecoveryError("操作已取消")
    elif not arguments.yes:
        print("\n未传入 --yes；仅展示计划，没有写入恢复槽。")
        return 0

    with exclusive_operation_lock(config.restore_root, "restore"):
        ensure_barman_lock_is_free(config.restore_root)
        remaining_data = docker.run(
            [
                "exec",
                "--user",
                "root",
                container,
                "find",
                "/restore/data",
                "-mindepth",
                "1",
                "-print",
                "-quit",
            ]
        ).stdout.strip()
        validate_restore_slot_empty(config.restore_root, data_directory_empty=not remaining_data)
        if docker.container_exists(config.postgres_container):
            raise RecoveryError(
                f"临时容器 {config.postgres_container} 已存在，请先运行 barman:restore:clean"
            )
        ensure_restore_volume(docker, config.volume_name, data_path)
        lock_path = config.restore_root / ".barman-restore.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        os.chmod(lock_path, 0o600)
        log_path = config.restore_root / "barman-restore.log"
        log_path.touch(mode=0o600, exist_ok=False)
        os.chmod(log_path, 0o600)
        record = _restore_record(
            config=config,
            container=container,
            server=server,
            backup=selected,
            target=target,
            estimate=estimate,
            last_wal=last_wal,
            wal_inventory=wal_inventory,
            wal_source="cloud" if cloud_wal else "local",
            last_wal_archived_at=last_wal_archived_at,
        )
        temporary_record = config.restore_root / ".restore.json.tmp"
        atomic_write_json(
            temporary_record,
            record,
            temporary=config.restore_root / ".restore.json.write.tmp",
        )
        docker.run(
            [
                "exec",
                "--user",
                "root",
                container,
                "sh",
                "-ec",
                "chown barman:barman /restore/data && chmod 700 /restore/data",
            ]
        )
        command = build_restore_command(
            server=server,
            backup_id=selected.backup_id,
            target_time=target.utc_text if target else None,
        )
        failure_stage = "barman_restore"
        try:
            process = execute_barman_restore(docker, container=container, command=command)
            trim_file(log_path, BARMAN_LOG_LIMIT)
            if process.returncode != 0:
                raise RecoveryError(f"Barman 文件恢复失败，退出码 {process.returncode}")
            failure_stage = "wal_materialization"
            if cloud_wal:
                materialize_cloud_wals(
                    docker,
                    container=container,
                    server=server,
                    inventory=wal_inventory,
                )
            actual_wal_size, wal_sizes = inspect_materialized_wals(
                docker, container=container, inventory=wal_inventory
            )
            failure_stage = "recovery_config"
            recovery_config = rewrite_recovery_config(docker, container=container)
            validate_self_contained_recovery_config(recovery_config)
        except BaseException as exc:
            record["status"] = "failed"
            record["file_restore_status"] = "failed"
            record["failed_at"] = now_text()
            record["failure_stage"] = failure_stage
            record["error"] = str(exc)[:2000]
            atomic_write_json(
                temporary_record,
                record,
                temporary=config.restore_root / ".restore.json.write.tmp",
            )
            raise
        finally:
            trim_file(log_path, BARMAN_LOG_LIMIT)
        wal_record = record["wal_materialization"]
        wal_record["actual_size"] = actual_wal_size
        wal_record["file_sizes"] = wal_sizes
        record["status"] = "completed"
        record["file_restore_status"] = "completed"
        record["completed_at"] = now_text()
        atomic_write_json(
            temporary_record,
            record,
            temporary=config.restore_root / ".restore.json.write.tmp",
        )
        os.replace(temporary_record, config.restore_root / "restore.json")
        os.chmod(config.restore_root / "restore.json", 0o600)
    print("\nBarman 文件恢复已完成，PGDATA 仍归 barman 用户所有。")
    print("下一步：mise run barman:restore:start")
    return 0


def _record_postgres_major(
    record: Mapping[str, Any], docker: DockerCLI, image: str, volume_name: str
) -> int:
    process = docker.run(
        [
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "--volume",
            f"{volume_name}:/restore-data:ro",
            image,
            "-ec",
            "test -f /restore-data/PG_VERSION; cat /restore-data/PG_VERSION",
        ]
    )
    pg_version = process.stdout.strip()
    data_major = postgres_major(pg_version)
    recorded_major = record.get("postgres_major")
    if isinstance(recorded_major, int) and recorded_major != data_major:
        raise RecoveryError(
            f"restore.json 记录的 PostgreSQL 主版本为 {recorded_major}，"
            f"PG_VERSION 实际为 {data_major}"
        )
    return recorded_major if isinstance(recorded_major, int) else data_major


def command_permissions(environment: Mapping[str, str]) -> int:
    config = RuntimeConfig.from_environment(environment)
    docker = DockerCLI(environment=environment)
    validate_local_docker(docker)
    with exclusive_operation_lock(config.restore_root, "permissions"):
        ensure_barman_lock_is_free(config.restore_root)
        if docker.container_exists(config.postgres_container):
            raise RecoveryError(
                f"临时容器 {config.postgres_container} 已存在，不能转换 PGDATA 权限"
            )
        record, data_path = validate_completed_restore(config)
        ensure_restore_volume(docker, config.volume_name, data_path)
        image = config.postgres_image
        if image is None:
            raise RecoveryError("必须通过 --postgres-image/POSTGRES_IMAGE 指定完整验证镜像")
        expected_major = _record_postgres_major(record, docker, image, config.volume_name)
        image_id, uid, gid = validate_postgres_image(docker, image, expected_major)
        permissions = record.get("permissions")
        if (
            isinstance(permissions, dict)
            and permissions.get("status") == "completed"
            and permissions.get("image_id") == image_id
            and data_path.stat().st_uid == uid
            and data_path.stat().st_gid == gid
        ):
            print("恢复数据权限已经与当前镜像完全匹配，无需重复转换。")
            return 0
        record["permissions"] = {
            "status": "running",
            "image": image,
            "image_id": image_id,
            "postgres_uid": uid,
            "postgres_gid": gid,
            "started_at": now_text(),
        }
        write_record(config.restore_root, record)
        try:
            docker.run(
                [
                    "run",
                    "--rm",
                    "--user",
                    "root",
                    "--entrypoint",
                    "bash",
                    "--volume",
                    f"{config.volume_name}:/restore-data",
                    image,
                    "-ec",
                    "chown -R postgres:postgres /restore-data; "
                    "chmod 700 /restore-data; "
                    "find /restore-data -xdev -type d -exec chmod 700 {} +",
                ]
            )
        except BaseException as exc:
            record["permissions"] = {
                **record["permissions"],
                "status": "failed",
                "failed_at": now_text(),
                "error": str(exc)[:2000],
            }
            write_record(config.restore_root, record)
            raise
        record["permissions"] = {
            **record["permissions"],
            "status": "completed",
            "completed_at": now_text(),
        }
        write_record(config.restore_root, record)
    print(f"恢复数据已切换为镜像 {image} 中的 postgres:postgres。")
    return 0


def _compose_arguments(config: RuntimeConfig, *arguments: str) -> list[str]:
    return [
        "compose",
        "--project-directory",
        str(RESTORE_COMPOSE_FILE.parent),
        "--project-name",
        config.compose_project,
        "--file",
        str(RESTORE_COMPOSE_FILE),
        *arguments,
    ]


def _compose_environment(config: RuntimeConfig) -> dict[str, str]:
    if config.postgres_image is None:
        raise RecoveryError("必须通过 --postgres-image/POSTGRES_IMAGE 指定完整验证镜像")
    return {
        "POSTGRES_IMAGE": config.postgres_image,
        "POSTGRES_NETWORK_NAME": config.network_name,
        "POSTGRES_RESTORE_VOLUME_NAME": config.volume_name,
        "POSTGRES_RESTORE_CONTAINER_NAME": config.postgres_container,
        "POSTGRES_RESTORE_BIND_ADDRESS": config.bind_address,
        "POSTGRES_RESTORE_PORT": str(config.port),
    }


def _container_running(docker: DockerCLI, container: str) -> bool:
    payload = docker.json(["container", "inspect", container])
    try:
        return payload[0]["State"]["Running"] is True
    except (IndexError, KeyError, TypeError):
        return False


def wait_for_postgres(docker: DockerCLI, container: str, timeout: int = 120) -> str | None:
    deadline = time.monotonic() + timeout
    last_output: str | None = None
    while time.monotonic() < deadline:
        if not _container_running(docker, container):
            raise RecoveryError(f"临时 PostgreSQL 容器 {container} 已退出")
        readiness = docker.run(["exec", "--user", "postgres", container, "pg_isready"], check=False)
        if readiness.returncode == 0:
            sql = docker.run(
                [
                    "exec",
                    "--user",
                    "postgres",
                    container,
                    "psql",
                    "-d",
                    "postgres",
                    "-Atqc",
                    "SELECT pg_is_in_recovery();",
                ],
                check=False,
            )
            last_output = (sql.stdout or "").strip()
            if sql.returncode != 0 or last_output not in {"t", "f"}:
                return None
            if last_output == "f":
                return last_output
        time.sleep(2)
    detail = f"，最后 SQL 状态为 {last_output!r}" if last_output is not None else ""
    raise RecoveryError(f"等待临时 PostgreSQL recovery 超时（{timeout} 秒）{detail}")


def command_start(environment: Mapping[str, str]) -> int:
    config = RuntimeConfig.from_environment(environment)
    docker = DockerCLI(environment=environment)
    validate_local_docker(docker)
    if not RESTORE_COMPOSE_FILE.is_file():
        raise RecoveryError(f"恢复 Compose 模板不存在: {RESTORE_COMPOSE_FILE}")
    with exclusive_operation_lock(config.restore_root, "start"):
        ensure_barman_lock_is_free(config.restore_root)
        if docker.container_exists(config.postgres_container):
            raise RecoveryError(
                f"临时容器 {config.postgres_container} 已存在，请先检查或运行 clean"
            )
        record, data_path = validate_completed_restore(config)
        ensure_restore_volume(docker, config.volume_name, data_path)
        image = config.postgres_image
        if image is None:
            raise RecoveryError("必须通过 --postgres-image/POSTGRES_IMAGE 指定完整验证镜像")
        expected_major = _record_postgres_major(record, docker, image, config.volume_name)
        image_id, uid, gid = validate_postgres_image(docker, image, expected_major)
        permissions = record.get("permissions")
        if not isinstance(permissions, dict) or permissions.get("status") != "completed":
            raise RecoveryError("权限转换尚未完成；start 必须通过 mise 的 permissions 依赖执行")
        if permissions.get("image_id") != image_id:
            raise RecoveryError("permissions 使用的镜像 ID 与当前 start 镜像不一致")
        if data_path.stat().st_uid != uid or data_path.stat().st_gid != gid:
            raise RecoveryError("PGDATA 根目录 UID/GID 与已验证镜像中的 postgres 用户不一致")
        if docker.run(["network", "inspect", config.network_name], check=False).returncode != 0:
            raise RecoveryError(f"外部 Docker 网络不存在: {config.network_name}")
        if docker.container_exists(config.postgres_container):
            raise RecoveryError(f"临时容器 {config.postgres_container} 已在检查期间出现")
        compose_environment = _compose_environment(config)
        record["postgres"] = {
            "status": "starting",
            "image": image,
            "image_id": image_id,
            "network": config.network_name,
            "bind_address": config.bind_address,
            "port": config.port,
            "started_at": now_text(),
        }
        record["wal_replay_status"] = "started"
        write_record(config.restore_root, record)
        try:
            docker.run(
                _compose_arguments(config, "up", "--detach", "--no-build"),
                extra_environment=compose_environment,
            )
            sql_result = wait_for_postgres(docker, config.postgres_container)
            save_postgres_logs(docker, config.postgres_container, config.restore_root)
        except BaseException as exc:
            if docker.container_exists(config.postgres_container):
                with suppress(RecoveryError):
                    save_postgres_logs(docker, config.postgres_container, config.restore_root)
            record["postgres"] = {
                **record["postgres"],
                "status": "failed",
                "failed_at": now_text(),
                "error": str(exc)[:2000],
            }
            record["wal_replay_status"] = "failed"
            record["target_status"] = "failed"
            record["writable"] = False
            write_record(config.restore_root, record)
            raise
        logs = (config.restore_root / "postgres-restore.log").read_text(
            encoding="utf-8", errors="replace"
        )
        sql_verified = sql_result == "f"
        target_verified = sql_verified
        if record.get("target_mode") == "time":
            target_verified = sql_verified and "recovery stopping" in logs.lower()
        record["postgres"] = {
            **record["postgres"],
            "status": "running",
            "sql_validation": "verified" if sql_verified else "not_verified",
            "checked_at": now_text(),
        }
        record["wal_replay_status"] = "completed" if sql_verified else "started"
        record["target_status"] = "verified" if target_verified else "not_verified"
        record["writable"] = sql_verified
        write_record(config.restore_root, record)
    print(f"临时 PostgreSQL 已启动: {config.bind_address}:{config.port}")
    if sql_verified:
        print("自动 SQL 检查确认实例已结束 recovery 并进入可写状态。")
    else:
        print("自动 SQL 验证未完成，请使用恢复点已有的数据库与凭据继续检查。")
    return 0


def _restore_root_for_clean(docker: DockerCLI, config: RuntimeConfig) -> RuntimeConfig:
    if docker.volume_exists(config.volume_name):
        device = volume_device_from_inspect(docker.json(["volume", "inspect", config.volume_name]))
        actual_root = device.parent
        if config.restore_root != actual_root:
            raise RecoveryError(
                f"恢复 volume 推导出的根目录为 {actual_root}，"
                f"与 POSTGRES_RESTORE_ROOT={config.restore_root} 不一致"
            )
        return replace(config, restore_root=actual_root)
    return config


def _containers_using_restore_path(
    docker: DockerCLI, config: RuntimeConfig, allowed_barman: set[str]
) -> list[str]:
    process = docker.run(["container", "ls", "--all", "--format", "{{.Names}}"])
    users: list[str] = []
    for name in process.stdout.splitlines():
        if not name or name in allowed_barman:
            continue
        payload = docker.json(["container", "inspect", name])
        mounts = payload[0].get("Mounts", []) if isinstance(payload, list) and payload else []
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            source = mount.get("Source")
            if source in {str(config.restore_root), str(config.restore_root / "data")}:
                users.append(name)
                break
    return users


def choose_cleanup_barman(
    docker: DockerCLI,
    *,
    explicit: str | None,
    restore_root: Path,
    interactive: bool,
) -> str:
    candidates: list[str] = []
    names = [explicit] if explicit else discover_barman_containers(docker)
    for name in names:
        if name is None:
            continue
        try:
            source = validate_barman_container(docker, name)
        except RecoveryError:
            if explicit:
                raise
            continue
        if source == restore_root:
            candidates.append(name)
        elif explicit:
            raise RecoveryError(
                f"Barman 容器 {name} 的 /restore 指向 {source}，不是 {restore_root}"
            )
    return select_value(
        explicit=explicit,
        values=candidates,
        label="用于永久清理的 Barman 容器",
        interactive=interactive,
    )


def validated_barman_containers_for_restore_root(docker: DockerCLI, restore_root: Path) -> set[str]:
    containers: set[str] = set()
    for name in discover_barman_containers(docker):
        try:
            source = validate_barman_container(docker, name)
        except RecoveryError:
            continue
        if source == restore_root:
            containers.add(name)
    return containers


def command_clean(arguments: argparse.Namespace, environment: Mapping[str, str]) -> int:
    docker = DockerCLI(environment=environment)
    validate_local_docker(docker)
    config = _restore_root_for_clean(docker, RuntimeConfig.from_environment(environment))
    validate_isolated_paths(config.production_data, config.restore_root)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    with exclusive_operation_lock(config.restore_root, "clean"):
        ensure_barman_lock_is_free(config.restore_root)
        if docker.container_exists(config.postgres_container):
            save_postgres_logs(docker, config.postgres_container, config.restore_root)
            record_path = config.restore_root / "restore.json"
            if record_path.exists():
                record = read_json_file(record_path)
                record["stopped_at"] = now_text()
                postgres_state = record.get("postgres")
                record["postgres"] = {
                    **(postgres_state if isinstance(postgres_state, dict) else {}),
                    "status": "stopped",
                }
                write_record(config.restore_root, record)
            docker.run(["container", "rm", "--force", config.postgres_container])
            print(f"已停止并删除临时容器 {config.postgres_container}，恢复数据仍保留。")
        else:
            print(f"临时容器 {config.postgres_container} 不存在，保留当前恢复现场。")

        if not arguments.delete_restored_data_permanently:
            return 0
        confirmation = arguments.confirm_delete_path
        if interactive and confirmation is None:
            entered = (
                _questionary().text(f"永久删除恢复数据，请完整输入路径 {config.restore_root}").ask()
            )
            confirmation = entered if isinstance(entered, str) else None
        if confirmation is None:
            raise RecoveryError("永久删除需要 --confirm-delete-path 提供精确恢复根目录")
        validate_delete_confirmation(confirmation, config.restore_root)
        validate_production_volume_if_present(docker, config, config.restore_root)
        explicit = resolve_option(arguments.container, environment, "BARMAN_CONTAINER")
        barman_container = choose_cleanup_barman(
            docker,
            explicit=explicit,
            restore_root=config.restore_root,
            interactive=interactive,
        )
        allowed_barman = validated_barman_containers_for_restore_root(docker, config.restore_root)
        allowed_barman.add(barman_container)
        users = _containers_using_restore_path(docker, config, allowed_barman)
        if users:
            raise RecoveryError("仍有其他容器使用恢复目录: " + ", ".join(users))
        docker.run(
            [
                "exec",
                "--user",
                "root",
                barman_container,
                "find",
                "/restore/data",
                "-xdev",
                "-mindepth",
                "1",
                "-delete",
            ]
        )
        remaining = docker.run(
            [
                "exec",
                "--user",
                "root",
                barman_container,
                "find",
                "/restore/data",
                "-mindepth",
                "1",
                "-print",
                "-quit",
            ]
        ).stdout.strip()
        if remaining:
            raise RecoveryError("Barman 容器清理后 /restore/data 仍然非空")
        for name in (
            "restore.json",
            ".restore.json.tmp",
            ".restore-record.update.tmp",
            ".restore.json.write.tmp",
            "barman-restore.log",
            "postgres-restore.log",
        ):
            (config.restore_root / name).unlink(missing_ok=True)
    print(f"已永久删除 {config.restore_root} 中的恢复产物，固定 data/ 与 volume 对象保留。")
    return 0


def parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RecoveryError(f"无法解析时间 {value!r}，请使用 ISO 8601 格式") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecoveryError(f"时间 {value!r} 必须显式包含时区 offset 或 Z")
    return parsed


def parse_target_time(value: str, *, now: datetime | None = None) -> TargetTime:
    parsed = parse_datetime(value)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if parsed > current:
        raise RecoveryError(f"目标时间 {value!r} 位于未来")
    return TargetTime(original=value, value=parsed)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_isolated_paths(production_data: Path, restore_root: Path) -> None:
    if not production_data.is_absolute() or not restore_root.is_absolute():
        raise RecoveryError("生产数据路径和恢复根目录都必须是绝对路径")
    if restore_root == Path("/"):
        raise RecoveryError("恢复根目录不能是 /，无法保证生产数据隔离")
    if (
        production_data == restore_root
        or _is_relative_to(production_data, restore_root)
        or _is_relative_to(restore_root, production_data)
    ):
        raise RecoveryError("生产数据路径与恢复根目录必须完全隔离，不能相等或互相包含")


def _unwrap_backup_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise RecoveryError("Barman 备份列表不是有效的 JSON 对象或数组")

    if "backups" in payload:
        return _unwrap_backup_payload(payload["backups"])

    values = list(payload.values())
    if len(values) == 1:
        return _unwrap_backup_payload(values[0])

    candidates = [value for value in values if isinstance(value, dict) and "backup_id" in value]
    if candidates:
        return candidates
    raise RecoveryError("无法从 Barman JSON 中识别备份列表")


def _read_size(item: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = item.get(name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise RecoveryError(f"Barman 字段 {name} 不是有效字节数: {value!r}") from exc
    return None


def _backup_time(item: dict[str, Any], name: str) -> datetime | None:
    timestamp = item.get(f"{name}_timestamp")
    if isinstance(timestamp, (str, int, float)):
        try:
            return datetime.fromtimestamp(int(timestamp), UTC)
        except (ValueError, OSError, OverflowError):
            pass
    value = item.get(name) or item.get(name.replace("_", "-"))
    if not isinstance(value, str):
        return None
    try:
        return parse_datetime(value)
    except RecoveryError:
        try:
            return datetime.strptime(value, "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC)
        except ValueError as exc:
            raise RecoveryError(f"无法解析 Barman 备份时间: {value!r}") from exc


def parse_backup_payload(payload: Any) -> list[Backup]:
    backups: list[Backup] = []
    for item in _unwrap_backup_payload(payload):
        backup_id = item.get("backup_id") or item.get("backup_name")
        status = item.get("status")
        end_time = _backup_time(item, "end_time")
        begin_time = _backup_time(item, "begin_time") or end_time
        if (
            not isinstance(backup_id, str)
            or not isinstance(status, str)
            or begin_time is None
            or end_time is None
        ):
            raise RecoveryError("Barman 备份记录缺少 backup_id、status、begin_time 或 end_time")
        tablespaces = item.get("tablespaces") or ()
        if isinstance(tablespaces, dict):
            tablespaces = tuple(
                {"name": name, **details} if isinstance(details, dict) else {"name": name}
                for name, details in tablespaces.items()
            )
        elif isinstance(tablespaces, list):
            tablespaces = tuple(entry for entry in tablespaces if isinstance(entry, dict))
        else:
            tablespaces = ()
        backups.append(
            Backup(
                backup_id=backup_id,
                status=status.upper(),
                begin_time=begin_time,
                end_time=end_time,
                cluster_size=_read_size(item, "cluster_size", "size_bytes", "deduplicated_size"),
                wal_size=_read_size(item, "wal_size_bytes", "wal_bytes"),
                postgres_version=item.get("version") or item.get("postgres_version"),
                tablespaces=tablespaces,
            )
        )
    return backups


def choose_backup(
    backups: list[Backup],
    *,
    target_time: datetime | None,
    explicit_backup: str | None,
) -> Backup:
    by_id = {backup.backup_id: backup for backup in backups}
    if explicit_backup is not None:
        selected = by_id.get(explicit_backup)
        if selected is None:
            raise RecoveryError(f"Barman server 中不存在备份 {explicit_backup!r}")
        if selected.status not in DONE_STATUSES:
            raise RecoveryError(f"备份 {explicit_backup!r} 状态为 {selected.status}，不可恢复")
        if target_time is not None and selected.end_time > target_time:
            raise RecoveryError(f"备份 {explicit_backup!r} 的结束时间晚于目标时间")
        return selected

    candidates = [backup for backup in backups if backup.status in DONE_STATUSES]
    if target_time is not None:
        candidates = [backup for backup in candidates if backup.end_time <= target_time]
    if not candidates:
        if target_time is None:
            raise RecoveryError("所选 Barman server 没有状态为 DONE 的可恢复备份")
        raise RecoveryError("目标时间早于所选 server 的最早可恢复时间")
    return max(candidates, key=lambda backup: backup.end_time)


def volume_device_from_inspect(payload: Any) -> Path:
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RecoveryError("Docker volume inspect 返回了无法识别的 JSON")
    volume = payload[0]
    options = volume.get("Options")
    if volume.get("Driver") != "local" or not isinstance(options, dict):
        raise RecoveryError("恢复 volume 必须使用 Docker local driver 的 bind 模式")
    mount_options = {option.strip() for option in str(options.get("o", "")).split(",")}
    device = options.get("device")
    if options.get("type") != "none" or "bind" not in mount_options or not isinstance(device, str):
        raise RecoveryError("恢复 volume 不是有效的 bind-backed local volume")
    path = Path(device)
    if not path.is_absolute():
        raise RecoveryError("恢复 volume 的 bind device 必须是绝对路径")
    return path


def estimate_required_space(
    *,
    cluster_size: int,
    wal_size: int | None,
    available: int,
    wal_size_complete: bool = True,
) -> SpaceEstimate:
    known_size = cluster_size + (wal_size or 0)
    margin = max(GIB, math.ceil(known_size * 0.1))
    return SpaceEstimate(
        cluster_size=cluster_size,
        wal_size=wal_size,
        safety_margin=margin,
        required=known_size + margin,
        available=available,
        complete=wal_size is not None and wal_size_complete,
    )


def validate_delete_confirmation(value: str, restore_root: Path) -> None:
    if not restore_root.is_absolute() or restore_root == Path("/"):
        raise RecoveryError("永久删除的恢复根目录必须是安全的绝对路径")
    if value != str(restore_root):
        raise RecoveryError("永久删除确认路径必须与实际恢复根目录逐字一致")


def resolve_option(
    cli_value: str | None, environment: Mapping[str, str], environment_name: str
) -> str | None:
    if cli_value is not None:
        return cli_value or None
    return environment.get(environment_name) or None


def is_local_docker_endpoint(endpoint: str) -> bool:
    return endpoint.startswith("unix://")


def barman_restore_source_from_inspect(payload: Any) -> Path:
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RecoveryError("Docker container inspect 返回了无法识别的 JSON")
    container = payload[0]
    state = container.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        raise RecoveryError("Barman 容器没有处于 running 状态")
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        raise RecoveryError("Barman 容器 inspect 结果缺少 Mounts")
    matching = [
        mount
        for mount in mounts
        if isinstance(mount, dict) and mount.get("Destination") == "/restore"
    ]
    if len(matching) != 1 or matching[0].get("Type") != "bind":
        raise RecoveryError("Barman 容器的 /restore 必须是唯一的普通 bind mount")
    source = matching[0].get("Source")
    if not isinstance(source, str) or not Path(source).is_absolute():
        raise RecoveryError("Barman /restore bind source 必须是宿主机绝对路径")
    return Path(source)


@contextmanager
def exclusive_operation_lock(restore_root: Path, command: str) -> Iterator[None]:
    restore_root.mkdir(mode=0o711, parents=False, exist_ok=True)
    lock_path = restore_root / ".lock"
    lock_file = lock_path.open("a+", encoding="utf-8")
    os.chmod(lock_path, 0o600)
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.seek(0)
            holder = lock_file.read().strip() or "未知操作"
            raise RecoveryError(f"恢复槽正被其他操作占用: {holder}") from exc
        diagnosis = {
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "command": command,
        }
        lock_file.seek(0)
        lock_file.truncate()
        json.dump(diagnosis, lock_file, ensure_ascii=False)
        lock_file.flush()
        os.fsync(lock_file.fileno())
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def atomic_write_json(destination: Path, value: Mapping[str, Any], *, temporary: Path) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def tail_bytes(value: bytes, limit: int) -> bytes:
    if limit < 0:
        raise ValueError("limit must not be negative")
    return value[-limit:] if len(value) > limit else value


def build_restore_command(*, server: str, backup_id: str, target_time: str | None) -> list[str]:
    command = ["barman", "restore", "--no-get-wal"]
    if target_time is not None:
        command.extend(["--target-time", target_time, "--target-action", "promote"])
    command.extend([server, backup_id, "/restore/data"])
    return command


def validate_no_custom_tablespaces(tablespaces: tuple[dict[str, Any], ...]) -> None:
    custom = [item for item in tablespaces if item.get("name") not in {"pg_default", "pg_global"}]
    if not custom:
        return
    details = "; ".join(
        f"{item.get('name', '?')} (OID {item.get('oid', '?')}, path {item.get('location', '?')})"
        for item in custom
    )
    raise RecoveryError(f"第一版不支持用户自定义 tablespace: {details}")


def validate_restore_slot_empty(
    restore_root: Path, *, data_directory_empty: bool | None = None
) -> None:
    data_path = restore_root / "data"
    if data_path.is_symlink() or not data_path.is_dir():
        raise RecoveryError(f"恢复数据路径必须是已存在的真实目录: {data_path}")
    artifacts = [
        restore_root / "restore.json",
        restore_root / ".restore.json.tmp",
        restore_root / "barman-restore.log",
        restore_root / "postgres-restore.log",
    ]
    existing = [path.name for path in artifacts if path.exists()]
    if data_directory_empty is None:
        data_directory_empty = next(data_path.iterdir(), None) is None
    if not data_directory_empty:
        existing.append("data/")
    if existing:
        raise RecoveryError(
            "固定恢复槽已有产物，先检查现场并运行 barman:restore:clean: " + ", ".join(existing)
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pg-backup-restore")
    subparsers = parser.add_subparsers(dest="command", required=True)

    restore = subparsers.add_parser("restore", help="从 Barman 恢复文件到固定恢复槽")
    restore.add_argument("--container")
    restore.add_argument("--server")
    restore.add_argument("--backup")
    restore.add_argument("--target-time")
    restore.add_argument("--yes", action="store_true")
    restore.add_argument("--allow-unknown-space-requirement", action="store_true")

    subparsers.add_parser("permissions", help="把恢复文件切换为 postgres 用户所有")
    subparsers.add_parser("start", help="启动临时 PostgreSQL 验证实例")

    clean = subparsers.add_parser("clean", help="拆除验证实例并按需永久清理恢复数据")
    clean.add_argument("--container")
    clean.add_argument("--delete-restored-data-permanently", action="store_true")
    clean.add_argument("--confirm-delete-path")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "restore":
            return command_restore(arguments, os.environ)
        if arguments.command == "permissions":
            return command_permissions(os.environ)
        if arguments.command == "start":
            return command_start(os.environ)
        if arguments.command == "clean":
            return command_clean(arguments, os.environ)
        raise RecoveryError(f"未知命令: {arguments.command}")
    except RecoveryError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
