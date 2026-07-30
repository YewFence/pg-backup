#!/usr/bin/env python3
"""Run isolated end-to-end disaster recovery tests for both Barman paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT_DIR / "smoke" / "compose.yaml"
EDGE_CONTAINER = "pg-backup-smoke-barman-edge"
EDGE_SERVER = "postgres-edge"
OFFSITE_CONTAINER = "pg-backup-smoke-barman-offsite"
OFFSITE_SERVER = "postgres-offsite"
RESTORE_CONTAINER = "pg-backup-smoke-postgres-restore"
RESTORE_VOLUME = "pg-backup-smoke-restore-data"
RESTORE_PROJECT = "pg-backup-smoke-restore"
NETWORK_NAME = "pg-backup-smoke-net"
POSTGRES_IMAGE = "postgres:17.10"
MISMATCH_IMAGE = "pg-backup-smoke-postgres-mismatch:latest"


class SmokeError(RuntimeError):
    """Raised when the smoke test cannot complete."""


RESTORE_ROOT = Path(tempfile.mkdtemp(prefix="pg-backup-restore-", dir="/tmp"))
SMOKE_ENV = {
    "SMOKE_RESTORE_ROOT": str(RESTORE_ROOT),
    "SMOKE_NETWORK_NAME": NETWORK_NAME,
    "POSTGRES_RESTORE_ROOT": str(RESTORE_ROOT),
    "POSTGRES_DATA_PATH": str(RESTORE_ROOT.parent / "pg-backup-production-never-touch"),
    "POSTGRES_RESTORE_VOLUME_NAME": RESTORE_VOLUME,
    "POSTGRES_RESTORE_CONTAINER_NAME": RESTORE_CONTAINER,
    "POSTGRES_RESTORE_PROJECT": RESTORE_PROJECT,
    "POSTGRES_RESTORE_BIND_ADDRESS": "127.0.0.1",
    "POSTGRES_RESTORE_PORT": "55433",
    "POSTGRES_NETWORK_NAME": NETWORK_NAME,
    "POSTGRES_IMAGE": POSTGRES_IMAGE,
}


def run(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    environment: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command_environment = os.environ.copy()
    command_environment.update(SMOKE_ENV)
    if environment:
        command_environment.update(environment)
    process = subprocess.run(
        args,
        cwd=ROOT_DIR,
        check=False,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        env=command_environment,
    )
    if capture and process.stdout:
        print(process.stdout, end="")
    if check and process.returncode != 0:
        raise SmokeError(f"命令失败，退出码 {process.returncode}: {' '.join(args)}")
    return process


def compose(
    *args: str,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        check=check,
        capture=capture,
    )


def compose_exec(
    service: str,
    *args: str,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return compose("exec", "-T", service, *args, check=check, capture=capture)


def retry(
    label: str,
    attempts: int,
    delay_seconds: int,
    action: Callable[[], subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    for attempt in range(1, attempts + 1):
        process = action()
        if process.returncode == 0:
            return process
        if attempt < attempts:
            print(f"{label} 未就绪，重试 {attempt}/{attempts}")
            time.sleep(delay_seconds)
    raise SmokeError(f"{label} 没有在预期时间内就绪")


def docker_object_exists(kind: str, name: str) -> bool:
    return run(["docker", kind, "inspect", name], check=False, capture=True).returncode == 0


def prepare_restore_root() -> None:
    RESTORE_ROOT.chmod(0o711)
    data_path = RESTORE_ROOT / "data"
    data_path.mkdir(mode=0o700)
    data_path.chmod(0o700)


def make_restore_data_host_owned() -> None:
    if not (RESTORE_ROOT / "data").exists():
        return
    run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "root",
            "--volume",
            f"{RESTORE_ROOT}:/restore",
            "--entrypoint",
            "bash",
            POSTGRES_IMAGE,
            "-ec",
            f"chown -R {os.getuid()}:{os.getgid()} /restore/data",
        ],
        check=False,
        capture=True,
    )


def cleanup() -> None:
    try:
        run(["docker", "container", "rm", "--force", RESTORE_CONTAINER], check=False)
        make_restore_data_host_owned()
        compose("down", "--volumes", "--remove-orphans", check=False)
        run(["docker", "volume", "rm", RESTORE_VOLUME], check=False)
        run(["docker", "image", "rm", MISMATCH_IMAGE], check=False)
    except OSError as exc:
        print(f"清理 smoke Docker 资源失败: {exc}", file=sys.stderr)
    finally:
        shutil.rmtree(RESTORE_ROOT, ignore_errors=True)


def wait_for_postgres_seed() -> None:
    retry(
        "PostgreSQL seed 数据",
        attempts=30,
        delay_seconds=2,
        action=lambda: compose_exec(
            "postgres",
            "gosu",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atqc",
            "SELECT count(*) FROM users; SELECT count(*) FROM products;",
            check=False,
            capture=True,
        ),
    )
    postgres_sql(
        "CREATE TABLE IF NOT EXISTS recovery_markers ("
        "label text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT clock_timestamp())"
    )


def postgres_sql(sql: str, *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return compose_exec(
        "postgres",
        "gosu",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-Atqc",
        sql,
        capture=capture,
    )


def create_s3_bucket() -> None:
    script = """
import boto3
client = boto3.client('s3', endpoint_url='http://rustfs:9000', region_name='us-east-1')
client.create_bucket(Bucket='pg-backup-smoke')
"""
    retry(
        "RustFS",
        attempts=30,
        delay_seconds=2,
        action=lambda: compose_exec(
            "barman-edge", "python3", "-c", script, check=False, capture=True
        ),
    )


def wait_for_barman_connection(service: str, label: str) -> None:
    retry(
        f"{label} 到 PostgreSQL 的连接",
        attempts=30,
        delay_seconds=2,
        action=lambda: compose_exec(
            service,
            "psql",
            "-U",
            "barman",
            "-h",
            "postgres",
            "postgres",
            "-c",
            "SELECT 1",
            check=False,
            capture=True,
        ),
    )


def start_receive_wal(service: str, server: str) -> None:
    def check_receive_wal() -> subprocess.CompletedProcess[str]:
        compose_exec(service, "barman", "cron", check=False)
        process = compose_exec(service, "barman", "check", server, check=False, capture=True)
        if "receive-wal running: OK" not in (process.stdout or ""):
            return subprocess.CompletedProcess(process.args, 1, process.stdout, process.stderr)
        return process

    retry(f"{server} receive-wal", attempts=12, delay_seconds=5, action=check_receive_wal)


def wait_for_barman_check(service: str, server: str) -> None:
    retry(
        f"{server} check",
        attempts=12,
        delay_seconds=5,
        action=lambda: compose_exec(service, "barman", "check", server, check=False, capture=True),
    )


def run_backup(service: str, server: str) -> str:
    compose_exec(service, "barman", "switch-wal", "--force", server)
    compose_exec(service, "barman", "cron")
    compose_exec(service, "barman", "backup", server, "--wait")
    compose_exec(service, "barman", "cron")
    wait_for_barman_check(service, server)
    process = compose_exec(
        service,
        "barman",
        "-f",
        "json",
        "list-backups",
        server,
        capture=True,
    )
    payload = json.loads(process.stdout or "{}")
    backups = payload.get(server, []) if isinstance(payload, dict) else []
    if not isinstance(backups, list) or not backups:
        raise SmokeError(f"{server} JSON 备份列表为空: {payload!r}")
    backup_id = backups[0].get("backup_id")
    if not isinstance(backup_id, str):
        raise SmokeError(f"{server} JSON 备份记录缺少 backup_id")
    compose_exec(service, "barman", "check-backup", server, backup_id)
    return backup_id


def verify_edge_cloud_objects() -> None:
    script = """
import boto3
client = boto3.client('s3', endpoint_url='http://rustfs:9000', region_name='us-east-1')
response = client.list_objects_v2(Bucket='pg-backup-smoke')
keys = [item['Key'] for item in response.get('Contents', [])]
assert any(key.startswith('postgres-edge/base/') for key in keys), keys
assert any(key.startswith('postgres-edge/wals/') for key in keys), keys
print(chr(10).join(keys))
"""
    compose_exec("barman-edge", "python3", "-c", script)


def create_pitr_boundary(prefix: str, service: str, server: str) -> str:
    before = f"{prefix}-before"
    after = f"{prefix}-after"
    postgres_sql(f"INSERT INTO recovery_markers(label) VALUES ('{before}')")
    time.sleep(1)
    target = postgres_sql(
        "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')",
        capture=True,
    ).stdout.strip()
    time.sleep(1)
    postgres_sql(f"INSERT INTO recovery_markers(label) VALUES ('{after}')")
    compose_exec(service, "barman", "switch-wal", "--force", "--archive", server)
    return target


def recovery_cli(
    command: str,
    *arguments: str,
    check: bool = True,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run(
        ["uv", "run", "pg-backup-restore", command, *arguments],
        check=check,
        capture=True,
        environment=environment,
    )


def assert_failure(process: subprocess.CompletedProcess[str], expected: str) -> None:
    if process.returncode == 0 or expected not in (process.stdout or ""):
        raise SmokeError(
            f"预期失败信息 {expected!r}，实际退出码 {process.returncode}: {process.stdout}"
        )


def verify_bad_barman_bind_rejected() -> None:
    name = "pg-backup-smoke-bad-barman"
    run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--entrypoint",
            "sleep",
            "pg-backup-smoke-barman-edge:latest",
            "infinity",
        ]
    )
    try:
        process = recovery_cli("restore", "--container", name, "--yes", check=False)
        assert_failure(process, "普通 bind mount")
    finally:
        run(["docker", "container", "rm", "--force", name], check=False)


def verify_mismatched_volume_rejected(container: str, server: str, target_time: str) -> None:
    wrong_root = Path(tempfile.mkdtemp(prefix="pg-backup-wrong-volume-", dir="/tmp"))
    wrong_data = wrong_root / "data"
    wrong_data.mkdir()
    run(
        [
            "docker",
            "volume",
            "create",
            "--driver",
            "local",
            "--opt",
            "type=none",
            "--opt",
            "o=bind",
            "--opt",
            f"device={wrong_data}",
            RESTORE_VOLUME,
        ]
    )
    try:
        process = recovery_cli(
            "restore",
            "--container",
            container,
            "--server",
            server,
            "--target-time",
            target_time,
            "--yes",
            "--allow-unknown-space-requirement",
            check=False,
        )
        assert_failure(process, "预期精确指向")
        if any((RESTORE_ROOT / "data").iterdir()):
            raise SmokeError("错误 volume 校验失败后恢复 data/ 被写入")
    finally:
        run(["docker", "volume", "rm", RESTORE_VOLUME], check=False)
        shutil.rmtree(wrong_root)


def build_mismatch_image() -> None:
    dockerfile = """FROM postgres:17.10
RUN printf '#!/bin/sh\\necho postgres \"(PostgreSQL) 16.9\"\\n' > /usr/local/bin/postgres \\
    && chmod +x /usr/local/bin/postgres
"""
    run(
        ["docker", "build", "--tag", MISMATCH_IMAGE, "-"],
        input_text=dockerfile,
    )


def restore_files(container: str, server: str, target_time: str) -> None:
    recovery_cli(
        "restore",
        "--container",
        container,
        "--server",
        server,
        "--target-time",
        target_time,
        "--yes",
        "--allow-unknown-space-requirement",
    )


def verify_cross_path_restore_lock_rejected(container: str, server: str, target_time: str) -> None:
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from pg_backup_restore.cli import exclusive_operation_lock\n"
        "with exclusive_operation_lock(Path(sys.argv[1]), 'restore-offsite'):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(60)\n"
    )
    environment = os.environ.copy()
    environment.update(SMOKE_ENV)
    holder = subprocess.Popen(
        ["uv", "run", "python", "-c", script, str(RESTORE_ROOT)],
        cwd=ROOT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    try:
        ready = holder.stdout.readline().strip() if holder.stdout is not None else ""
        if ready != "locked":
            raise SmokeError(f"并发锁测试进程未就绪: {ready!r}")
        process = recovery_cli(
            "restore",
            "--container",
            container,
            "--server",
            server,
            "--target-time",
            target_time,
            "--yes",
            "--allow-unknown-space-requirement",
            check=False,
        )
        assert_failure(process, "恢复槽正被其他操作占用")
        for name in ("restore.json", ".restore.json.tmp", "barman-restore.log"):
            if (RESTORE_ROOT / name).exists():
                raise SmokeError(f"并发 restore 被拒绝后仍创建了恢复产物: {name}")
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def verify_major_mismatch_rejected() -> None:
    build_mismatch_image()
    process = recovery_cli(
        "permissions",
        check=False,
        environment={"POSTGRES_IMAGE": MISMATCH_IMAGE},
    )
    assert_failure(process, "主版本为 16")


def verify_missing_network_rejected() -> None:
    recovery_cli("permissions")
    process = recovery_cli(
        "start",
        check=False,
        environment={"POSTGRES_NETWORK_NAME": "pg-backup-smoke-missing-net"},
    )
    assert_failure(process, "外部 Docker 网络不存在")
    if docker_object_exists("container", RESTORE_CONTAINER):
        raise SmokeError("缺失网络校验失败后错误创建了恢复容器")


def start_and_verify(prefix: str) -> None:
    recovery_cli("start")
    process = run(
        [
            "docker",
            "exec",
            "--user",
            "postgres",
            RESTORE_CONTAINER,
            "psql",
            "-d",
            "postgres",
            "-Atqc",
            "SELECT label FROM recovery_markers "
            f"WHERE label IN ('{prefix}-before', '{prefix}-after') ORDER BY label",
        ],
        capture=True,
    )
    labels = process.stdout.splitlines()
    if labels != [f"{prefix}-before"]:
        raise SmokeError(f"{prefix} PITR 数据边界错误: {labels!r}")
    record = json.loads((RESTORE_ROOT / "restore.json").read_text())
    if record.get("target_status") != "verified" or record.get("writable") is not True:
        raise SmokeError(f"{prefix} 恢复记录未确认 target/promote: {record!r}")


def verify_cleanup(container: str) -> None:
    recovery_cli("clean")
    if docker_object_exists("container", RESTORE_CONTAINER):
        raise SmokeError("默认清理后恢复容器仍存在")
    for name in ("restore.json", "barman-restore.log", "postgres-restore.log"):
        if not (RESTORE_ROOT / name).is_file():
            raise SmokeError(f"默认清理错误删除了恢复产物: {name}")
    if not docker_object_exists("volume", RESTORE_VOLUME):
        raise SmokeError("默认清理错误删除了固定恢复 volume")

    recovery_cli(
        "clean",
        "--container",
        container,
        "--delete-restored-data-permanently",
        "--confirm-delete-path",
        str(RESTORE_ROOT),
    )
    remaining = run(
        [
            "docker",
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
        ],
        capture=True,
    ).stdout.strip()
    if remaining:
        raise SmokeError("永久清理后 data/ 仍然非空")
    for name in ("restore.json", ".restore.json.tmp", "barman-restore.log", "postgres-restore.log"):
        if (RESTORE_ROOT / name).exists():
            raise SmokeError(f"永久清理后恢复产物仍存在: {name}")
    if not docker_object_exists("volume", RESTORE_VOLUME):
        raise SmokeError("永久清理错误删除了固定恢复 volume")


def run_recovery_path(container: str, service: str, server: str, prefix: str) -> None:
    run_backup(service, server)
    if service == "barman-edge":
        verify_edge_cloud_objects()
    target_time = create_pitr_boundary(prefix, service, server)
    if prefix == "edge":
        verify_cross_path_restore_lock_rejected(container, server, target_time)
        verify_mismatched_volume_rejected(container, server, target_time)
    restore_files(container, server, target_time)
    if prefix == "edge":
        verify_major_mismatch_rejected()
        verify_missing_network_rejected()
    else:
        recovery_cli("permissions")
    start_and_verify(prefix)
    verify_cleanup(container)


def show_failure_context() -> None:
    for service in ("postgres", "rustfs", "barman-edge", "barman-offsite"):
        print(f"=== {service} logs ===")
        compose("logs", service, check=False)
    if docker_object_exists("container", RESTORE_CONTAINER):
        run(["docker", "logs", RESTORE_CONTAINER], check=False)


def main() -> int:
    try:
        prepare_restore_root()
        cleanup_previous = compose("down", "--volumes", "--remove-orphans", check=False)
        if cleanup_previous.returncode != 0:
            raise SmokeError("无法清理上一次 smoke Compose 环境")
        compose("build")
        compose("up", "--detach", "postgres", "rustfs", "barman-edge", "barman-offsite")
        wait_for_postgres_seed()
        create_s3_bucket()
        wait_for_barman_connection("barman-edge", "Barman Edge")
        wait_for_barman_connection("barman-offsite", "Barman Offsite")
        start_receive_wal("barman-edge", EDGE_SERVER)
        start_receive_wal("barman-offsite", OFFSITE_SERVER)
        verify_bad_barman_bind_rejected()
        run_recovery_path(EDGE_CONTAINER, "barman-edge", EDGE_SERVER, "edge")
        run_recovery_path(OFFSITE_CONTAINER, "barman-offsite", OFFSITE_SERVER, "offsite")
        print("edge S3 与 offsite 本地 PITR、promote、查询及清理验收全部通过。")
        return 0
    except (SmokeError, subprocess.SubprocessError, OSError, ValueError) as exc:
        print(f"smoke 测试失败: {exc}", file=sys.stderr)
        show_failure_context()
        return 1
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
