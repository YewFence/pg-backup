#!/usr/bin/env python3
"""Run local smoke tests for edge and offsite Barman deployments."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT_DIR / "smoke" / "compose.yaml"
EDGE_SERVICE = "barman-edge"
EDGE_SERVER = "postgres-edge"
OFFSITE_SERVICE = "barman-offsite"
OFFSITE_SERVER = "postgres-offsite"


class SmokeError(RuntimeError):
    """Raised when the smoke test cannot complete."""


def run(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args,
        cwd=ROOT_DIR,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )

    if capture and proc.stdout:
        print(proc.stdout, end="")

    if check and proc.returncode != 0:
        raise SmokeError(f"命令失败，退出码 {proc.returncode}: {' '.join(args)}")

    return proc


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
        proc = action()
        if proc.returncode == 0:
            return proc
        if attempt < attempts:
            print(f"{label} 未就绪，重试 {attempt}/{attempts}")
            time.sleep(delay_seconds)

    raise SmokeError(f"{label} 没有在预期时间内就绪")


def cleanup() -> None:
    try:
        compose("down", "-v", "--remove-orphans", check=False)
    except OSError as exc:
        print(f"清理 smoke 环境失败: {exc}", file=sys.stderr)


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
            "-c",
            "SELECT count(*) FROM users; SELECT count(*) FROM products;",
            check=False,
            capture=True,
        ),
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
            EDGE_SERVICE,
            "python3",
            "-c",
            script,
            check=False,
            capture=True,
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
        proc = compose_exec(
            service,
            "barman",
            "check",
            server,
            check=False,
            capture=True,
        )
        if "receive-wal running: OK" not in (proc.stdout or ""):
            return subprocess.CompletedProcess(proc.args, 1, proc.stdout, proc.stderr)
        return proc

    retry(f"{server} receive-wal", attempts=12, delay_seconds=5, action=check_receive_wal)


def wait_for_barman_check(service: str, server: str) -> None:
    retry(
        f"{server} check",
        attempts=12,
        delay_seconds=5,
        action=lambda: compose_exec(
            service,
            "barman",
            "check",
            server,
            check=False,
            capture=True,
        ),
    )


def run_backup(service: str, server: str) -> None:
    compose_exec(service, "barman", "switch-wal", "--force", server)
    compose_exec(service, "barman", "cron")
    compose_exec(service, "barman", "backup", server, "--wait")
    compose_exec(service, "barman", "cron")
    compose_exec(service, "barman", "list-backups", server)
    wait_for_barman_check(service, server)
    check_latest_backup(service, server)


def check_latest_backup(service: str, server: str) -> None:
    compose_exec(service, "barman", "check-backup", server, "latest")
    proc = compose_exec(
        service,
        "barman",
        "show-backup",
        server,
        "latest",
        capture=True,
    )
    if not re.search(r"^\s*Status\s*:\s*DONE\s*$", proc.stdout or "", re.MULTILINE):
        raise SmokeError(f"{server} 最新备份没有进入 DONE 状态")


def verify_edge_cloud_objects() -> None:
    script = """
import boto3
client = boto3.client('s3', endpoint_url='http://rustfs:9000', region_name='us-east-1')
keys = [item['Key'] for item in client.list_objects_v2(Bucket='pg-backup-smoke').get('Contents', [])]
assert any(key.startswith('postgres-edge/base/') for key in keys), keys
assert any(key.startswith('postgres-edge/wals/') for key in keys), keys
print('\\n'.join(keys))
"""
    compose_exec(EDGE_SERVICE, "python3", "-c", script)


def verify_edge_restore() -> None:
    compose_exec(
        EDGE_SERVICE,
        "barman",
        "restore",
        EDGE_SERVER,
        "latest",
        "/var/lib/barman/cloud-restore",
    )
    compose_exec(
        EDGE_SERVICE,
        "test",
        "-f",
        "/var/lib/barman/cloud-restore/PG_VERSION",
    )


def show_failure_context() -> None:
    try:
        for service in ("postgres", "rustfs", EDGE_SERVICE, OFFSITE_SERVICE):
            print(f"=== {service} logs ===")
            compose("logs", service, check=False)
    except OSError as exc:
        print(f"无法收集 smoke 日志: {exc}", file=sys.stderr)


def main() -> int:
    try:
        cleanup()
        compose("build")
        compose("up", "-d", "postgres", "rustfs", EDGE_SERVICE, OFFSITE_SERVICE)

        wait_for_postgres_seed()
        create_s3_bucket()
        wait_for_barman_connection(EDGE_SERVICE, "Barman Edge")
        wait_for_barman_connection(OFFSITE_SERVICE, "Barman Offsite")
        start_receive_wal(EDGE_SERVICE, EDGE_SERVER)
        start_receive_wal(OFFSITE_SERVICE, OFFSITE_SERVER)

        run_backup(EDGE_SERVICE, EDGE_SERVER)
        verify_edge_cloud_objects()
        verify_edge_restore()

        run_backup(OFFSITE_SERVICE, OFFSITE_SERVER)
        return 0
    except (SmokeError, subprocess.SubprocessError, OSError) as exc:
        print(f"smoke 测试失败: {exc}", file=sys.stderr)
        show_failure_context()
        return 1
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
