from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pg_backup_restore.cli import (
    Backup,
    RecoveryError,
    SpaceEstimate,
    atomic_write_json,
    barman_restore_source_from_inspect,
    build_parser,
    build_restore_command,
    choose_backup,
    estimate_required_space,
    exclusive_operation_lock,
    is_local_docker_endpoint,
    parse_backup_payload,
    parse_target_time,
    resolve_option,
    tail_bytes,
    validate_delete_confirmation,
    validate_isolated_paths,
    validate_no_custom_tablespaces,
    validate_restore_slot_empty,
    volume_device_from_inspect,
)


def test_target_time_requires_an_explicit_timezone_and_normalizes_to_utc() -> None:
    parsed = parse_target_time("2026-07-29 08:30:00+08:00", now=datetime(2026, 7, 30, tzinfo=UTC))

    assert parsed.original == "2026-07-29 08:30:00+08:00"
    assert parsed.utc_text == "2026-07-29T00:30:00Z"

    with pytest.raises(RecoveryError, match="时区"):
        parse_target_time("2026-07-29 08:30:00", now=datetime(2026, 7, 30, tzinfo=UTC))


def test_target_time_rejects_future_values() -> None:
    with pytest.raises(RecoveryError, match="未来"):
        parse_target_time("2026-07-31T00:00:00Z", now=datetime(2026, 7, 30, tzinfo=UTC))


def test_restore_root_must_be_disjoint_from_production_data() -> None:
    validate_isolated_paths(
        Path("/srv/native-docker/postgres"),
        Path("/srv/native-docker/postgres-restore"),
    )

    with pytest.raises(RecoveryError, match="隔离"):
        validate_isolated_paths(
            Path("/srv/native-docker/postgres"),
            Path("/srv/native-docker/postgres/restore"),
        )


def test_barman_json_is_parsed_and_latest_applicable_backup_is_selected() -> None:
    backups = parse_backup_payload(
        {
            "postgres-offsite": [
                {
                    "backup_id": "20260729T000002",
                    "status": "DONE",
                    "begin_time": "2026-07-29T00:00:02+00:00",
                    "end_time": "2026-07-29T00:10:00+00:00",
                },
                {
                    "backup_id": "20260729T020002",
                    "status": "DONE",
                    "begin_time": "2026-07-29T02:00:02+00:00",
                    "end_time": "2026-07-29T02:10:00+00:00",
                },
                {
                    "backup_id": "20260729T030002",
                    "status": "FAILED",
                    "begin_time": "2026-07-29T03:00:02+00:00",
                    "end_time": "2026-07-29T03:10:00+00:00",
                },
            ]
        }
    )

    selected = choose_backup(
        backups,
        target_time=datetime(2026, 7, 29, 2, 30, tzinfo=UTC),
        explicit_backup=None,
    )

    assert selected.backup_id == "20260729T020002"


def test_explicit_backup_must_be_done_and_precede_target() -> None:
    backup = Backup(
        backup_id="future",
        status="DONE",
        begin_time=datetime(2026, 7, 29, 3, tzinfo=UTC),
        end_time=datetime(2026, 7, 29, 3, 10, tzinfo=UTC),
    )

    with pytest.raises(RecoveryError, match="晚于目标时间"):
        choose_backup(
            [backup],
            target_time=datetime(2026, 7, 29, 2, 30, tzinfo=UTC),
            explicit_backup="future",
        )


def test_volume_inspect_requires_local_bind_device() -> None:
    payload = [
        {
            "Driver": "local",
            "Options": {
                "type": "none",
                "o": "bind",
                "device": "/srv/native-docker/postgres-restore/data",
            },
        }
    ]

    assert volume_device_from_inspect(payload) == Path("/srv/native-docker/postgres-restore/data")

    with pytest.raises(RecoveryError, match="bind"):
        volume_device_from_inspect([{"Driver": "local", "Options": {}}])


def test_space_estimate_applies_ten_percent_and_one_gib_minimum_margin() -> None:
    gib = 1024**3
    estimate = estimate_required_space(cluster_size=5 * gib, wal_size=2 * gib, available=10 * gib)

    assert estimate == SpaceEstimate(
        cluster_size=5 * gib,
        wal_size=2 * gib,
        safety_margin=1 * gib,
        required=8 * gib,
        available=10 * gib,
        complete=True,
    )


def test_permanent_delete_confirmation_is_literal_not_normalized() -> None:
    restore_root = Path("/srv/native-docker/postgres-restore")

    validate_delete_confirmation(str(restore_root), restore_root)

    with pytest.raises(RecoveryError, match="逐字"):
        validate_delete_confirmation("/srv/native-docker/./postgres-restore", restore_root)


def test_cli_option_precedes_environment_without_treating_empty_as_a_value() -> None:
    environment = {"BARMAN_CONTAINER": "barman-offsite"}

    assert resolve_option("barman-edge", environment, "BARMAN_CONTAINER") == "barman-edge"
    assert resolve_option(None, environment, "BARMAN_CONTAINER") == "barman-offsite"
    assert resolve_option("", environment, "BARMAN_CONTAINER") is None


def test_only_local_unix_docker_endpoints_are_supported() -> None:
    assert is_local_docker_endpoint("unix:///var/run/docker.sock")
    assert is_local_docker_endpoint("unix:///run/user/1000/docker.sock")
    assert not is_local_docker_endpoint("tcp://docker.example:2376")
    assert not is_local_docker_endpoint("ssh://backup-host")


def test_barman_container_requires_an_exact_restore_bind_mount() -> None:
    inspect_payload = [
        {
            "State": {"Running": True},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/srv/native-docker/postgres-restore",
                    "Destination": "/restore",
                }
            ],
        }
    ]

    assert barman_restore_source_from_inspect(inspect_payload) == Path(
        "/srv/native-docker/postgres-restore"
    )

    inspect_payload[0]["Mounts"][0]["Type"] = "volume"
    with pytest.raises(RecoveryError, match="普通 bind mount"):
        barman_restore_source_from_inspect(inspect_payload)


def test_operation_lock_fails_immediately_and_reports_holder(tmp_path: Path) -> None:
    with (
        exclusive_operation_lock(tmp_path, "restore"),
        pytest.raises(RecoveryError, match="restore"),
        exclusive_operation_lock(tmp_path, "clean"),
    ):
        pass


def test_atomic_record_replaces_temp_file_with_mode_0600(tmp_path: Path) -> None:
    destination = tmp_path / "restore.json"
    temporary = tmp_path / ".restore.json.tmp"

    atomic_write_json(destination, {"status": "completed"}, temporary=temporary)

    assert json.loads(destination.read_text()) == {"status": "completed"}
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not temporary.exists()


def test_postgres_log_snapshot_keeps_only_the_last_limit() -> None:
    assert tail_bytes(b"0123456789", 4) == b"6789"
    assert tail_bytes(b"short", 10) == b"short"


def test_time_recovery_command_is_self_contained_and_promotes() -> None:
    command = build_restore_command(
        server="postgres-offsite",
        backup_id="20260729T000002",
        target_time="2026-07-29T00:30:00Z",
    )

    assert command == [
        "barman",
        "restore",
        "--no-get-wal",
        "--target-time",
        "2026-07-29T00:30:00Z",
        "--target-action",
        "promote",
        "postgres-offsite",
        "20260729T000002",
        "/restore/data",
    ]


def test_custom_tablespaces_are_rejected_before_restore() -> None:
    with pytest.raises(RecoveryError, match=r"analytics.*16384.*mnt/tablespaces"):
        validate_no_custom_tablespaces(
            (
                {"name": "pg_default", "oid": 1663, "location": ""},
                {"name": "analytics", "oid": 16384, "location": "/mnt/tablespaces/analytics"},
            )
        )


def test_restore_slot_rejects_any_existing_recovery_artifact(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    validate_restore_slot_empty(tmp_path)

    (tmp_path / "restore.json").write_text("{}")
    with pytest.raises(RecoveryError, match=r"restore\.json"):
        validate_restore_slot_empty(tmp_path)


def test_cli_exposes_four_internal_commands() -> None:
    parser = build_parser()

    assert parser.parse_args(["permissions"]).command == "permissions"
    assert parser.parse_args(["start"]).command == "start"
    assert parser.parse_args(["clean"]).command == "clean"
    restore = parser.parse_args(
        [
            "restore",
            "--container",
            "barman-offsite",
            "--server",
            "postgres-offsite",
            "--target-time",
            "2026-07-29T08:30:00+08:00",
            "--yes",
        ]
    )
    assert restore.container == "barman-offsite"
    assert restore.server == "postgres-offsite"
    assert restore.yes is True
