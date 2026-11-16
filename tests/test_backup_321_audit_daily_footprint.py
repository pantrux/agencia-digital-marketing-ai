from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sre import backup_321_audit_daily_footprint as audit

TODAY = date(2026, 11, 13)
NOW = datetime(2026, 11, 13, 20, tzinfo=UTC)
ZONE = timezone(timedelta(hours=-3))


def inode(number, blocks, *, device=1):
    return SimpleNamespace(st_dev=device, st_ino=number, st_blocks=blocks)


def test_footprint_attributes_hardlinks_once_and_separates_acceptance(tmp_path, monkeypatch):
    roots = {role: tmp_path / role for role in audit.ROOTS}
    daily_id = f"daily-{TODAY}"
    acceptance_id = "daily-acceptance-test"
    content = {
        roots["postgres"] / daily_id: [inode(1, 8), inode(2, 16)],
        roots["staging"] / daily_id: [inode(3, 8), inode(2, 16)],
        roots["staging"] / acceptance_id: [inode(4, 8), inode(5, 32)],
    }
    monkeypatch.setattr(audit, "tree_stats", lambda path, mounts: content.get(path))
    monkeypatch.setattr(
        audit.coverage,
        "observe_day",
        lambda *args: {"date": str(TODAY), "run_id": daily_id, "status": "failed"},
    )
    monkeypatch.setattr(
        audit.coverage,
        "observe_acceptance",
        lambda *args: {
            "run_id": acceptance_id,
            "status": "complete",
            "started": NOW - timedelta(seconds=25),
            "confirmed_by": NOW,
        },
    )

    result = audit.measure(roots, [TODAY], ZONE, NOW, acceptance_id, 1, ())

    day = result["days"][0]
    assert day["status"] == "failed"
    assert day["roots"]["postgres"] == {"present": True, "allocated_bytes": 12288}
    assert day["roots"]["staging"] == {"present": True, "allocated_bytes": 4096}
    assert day["roots"]["d1"] == {"present": False, "allocated_bytes": 0}
    assert result["calendar_allocated_bytes"] == 16384
    assert result["acceptance"]["allocated_bytes"] == 20480
    assert result["acceptance"]["duration_seconds"] == 25


def test_nested_mount_is_rejected_even_on_same_device(tmp_path):
    trial = tmp_path / "trial"
    with pytest.raises(ValueError, match="^MONTAJE_SUBARBOL$"):
        audit.tree_stats(trial, (trial / "nested",))


def test_unexpected_device_is_rejected_before_returning_allocated_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "tree_stats", lambda path, mounts: [inode(1, 8, device=2)])
    with pytest.raises(ValueError, match="^DISPOSITIVO_CAMBIO$"):
        audit.root_usage(tmp_path, 1, (), set())
