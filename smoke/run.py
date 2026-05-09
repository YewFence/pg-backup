#!/usr/bin/env python3
"""Run the local Barman smoke test."""

from __future__ import annotations

import subprocess
import sys
import time
import re
from collections.abc import Callable
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT_DIR / "smoke" / "compose.yaml"
SERVER_NAME = "streaming-backup-server"


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


def compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
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
    query = (
        "SELECT count(*) FROM users "
        "UNION ALL SELECT count(*) FROM products "
        "UNION ALL SELECT count(*) FROM orders"
    )
    retry(
        "PostgreSQL seed 数据",
        attempts=30,
        delay_seconds=2,
        action=lambda: compose_exec(
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            query,
            check=False,
            capture=True,
        ),
    )


def wait_for_barman_connection() -> None:
    retry(
        "Barman 到 PostgreSQL 的连接",
        attempts=30,
        delay_seconds=2,
        action=lambda: compose_exec(
            "barman",
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


def start_receive_wal() -> None:
    def check_receive_wal() -> subprocess.CompletedProcess[str]:
        compose_exec("barman", "barman", "cron", check=False)
        proc = compose_exec(
            "barman",
            "barman",
            "check",
            SERVER_NAME,
            check=False,
            capture=True,
        )
        if "receive-wal running: OK" not in (proc.stdout or ""):
            return subprocess.CompletedProcess(proc.args, 1, proc.stdout, proc.stderr)
        return proc

    retry("Barman receive-wal", attempts=10, delay_seconds=5, action=check_receive_wal)


def wait_for_barman_check() -> None:
    def check_barman() -> subprocess.CompletedProcess[str]:
        compose_exec("barman", "barman", "cron", check=False)
        return compose_exec(
            "barman",
            "barman",
            "check",
            SERVER_NAME,
            check=False,
            capture=True,
        )

    retry("Barman check", attempts=10, delay_seconds=5, action=check_barman)


def check_latest_backup() -> None:
    compose_exec("barman", "barman", "check-backup", SERVER_NAME, "latest")
    proc = compose_exec(
        "barman",
        "barman",
        "show-backup",
        SERVER_NAME,
        "latest",
        capture=True,
    )
    if not re.search(r"^\s*Status\s*:\s*DONE\s*$", proc.stdout or "", re.MULTILINE):
        raise SmokeError("最新备份没有进入 DONE 状态")


def show_failure_context() -> None:
    try:
        print("=== postgres logs ===")
        compose("logs", "postgres", check=False)
        print("=== barman logs ===")
        compose("logs", "barman", check=False)
    except OSError as exc:
        print(f"无法收集 smoke 日志: {exc}", file=sys.stderr)


def main() -> int:
    try:
        cleanup()
        compose("build")
        compose("up", "-d", "postgres", "barman")

        wait_for_postgres_seed()
        compose_exec(
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-c",
            "SELECT name, setting FROM pg_settings WHERE name IN ('wal_level', 'archive_mode')",
        )
        compose_exec(
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-c",
            (
                "SELECT 'users' AS tbl, count(*) FROM users "
                "UNION ALL SELECT 'products', count(*) FROM products "
                "UNION ALL SELECT 'orders', count(*) FROM orders"
            ),
        )

        wait_for_barman_connection()
        start_receive_wal()
        compose_exec("barman", "barman", "backup", SERVER_NAME, "--wait")
        compose_exec("barman", "barman", "switch-wal", "--force", SERVER_NAME)
        compose_exec("barman", "barman", "cron")
        compose_exec("barman", "barman", "list-backups", SERVER_NAME)
        wait_for_barman_check()
        check_latest_backup()
        return 0
    except (SmokeError, subprocess.SubprocessError, OSError) as exc:
        print(f"smoke 测试失败: {exc}", file=sys.stderr)
        show_failure_context()
        return 1
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
