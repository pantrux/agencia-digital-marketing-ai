from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from subprocess import CompletedProcess

import pytest

from sre import backup_321_retention as retention

NOW = datetime(2026, 11, 13, 18, tzinfo=UTC)
STAGING = PurePosixPath("/var/lib/media-platform/backup-321-daily-staging-v1")
CONFIG = {"id": "2221e96438027e76a521267c42b57ec382cd53670b84ac3febbfa00645bcd3a8", "version": 2}
PROFILE_SHA = "d" * 64
FORGET = (
    "forget",
    "--dry-run",
    "--host",
    "vm-media-01",
    "--tag",
    "agency-backup-321",
    "--group-by",
    "host",
    "--keep-within",
    "30d",
    "--keep-last",
    "1",
)


def snapshot(character, run_id, age, *, staging=STAGING):
    return {
        "id": character * 64,
        "hostname": "vm-media-01",
        "time": (NOW - timedelta(days=age)).isoformat(),
        "tags": ["agency-backup-321", f"run:{run_id}"],
        "paths": [
            str(staging / run_id / "staging" / name)
            for name in (
                "postgres-apps.sql.zst",
                "postgres-immich.sql.zst",
                "d1.sql.zst",
                "openclaw.tar.zst",
                "SHA256SUMS",
            )
        ],
        "summary": {"total_files_processed": 5},
    }


def restic_stub(before, keep, remove, *, after=None, config=CONFIG):
    calls = []
    rows = iter((before, list(reversed(before)) if after is None else after))

    def execute(*args):
        calls.append(args)
        if args == ("cat", "config"):
            return config
        if args == ("snapshots",):
            return next(rows)
        assert args == FORGET
        return [
            {"host": "vm-media-01", "paths": None, "tags": None, "keep": keep, "remove": remove}
        ]

    return execute, calls


def test_retains_recent_full_snapshots_and_excludes_partial_snapshot():
    latest = snapshot("a", "daily-2026-11-12", 1)
    recent = snapshot("b", "daily-2026-10-15", 29)
    boundary = snapshot("c", "daily-2026-10-13", 31)
    partial = {"id": "d" * 64, "hostname": "vm-media-01", "tags": ["kitsu-postgres-acceptance-v1"]}
    execute, calls = restic_stub([latest, recent, boundary, partial], [latest, recent], [boundary])

    result, _ = retention.retention_plan(execute, STAGING, NOW, PROFILE_SHA)

    assert result["snapshot_inventory_unchanged"]
    assert result["keep_ids"] == [latest["id"], recent["id"]]
    assert result["would_remove_ids"] == [boundary["id"]]
    assert result["outside_cohort"] == 1
    assert calls == [("cat", "config"), ("snapshots",), FORGET, ("snapshots",)]


def test_preserves_only_snapshot_in_outage_and_accepts_prior_production_staging():
    latest = snapshot(
        "a",
        "fase4-produccion-20261113-v1",
        40,
        staging=PurePosixPath("/var/lib/media-platform/backup-321-production-staging-v1"),
    )
    execute, _ = restic_stub([latest], [latest], None)

    result, _ = retention.retention_plan(execute, STAGING, NOW, PROFILE_SHA)

    assert result["keep_ids"] == [latest["id"]]
    assert result["would_remove_ids"] == []
    assert result["newest_age_seconds"] == 40 * 86400


def test_retention_boundary_uses_restic_nanoseconds():
    latest = snapshot("a", "latest", 1)
    latest["time"] = "2026-11-12T18:00:00.000000001Z"
    at_cutoff = snapshot("b", "at-cutoff", 31)
    at_cutoff["time"] = "2026-10-13T18:00:00.000000001Z"
    after_cutoff = snapshot("c", "after-cutoff", 31)
    after_cutoff["time"] = "2026-10-13T18:00:00.000000002Z"
    execute, _ = restic_stub([latest, at_cutoff, after_cutoff], [latest, after_cutoff], [at_cutoff])

    result, _ = retention.retention_plan(execute, STAGING, NOW, PROFILE_SHA)

    assert result["would_remove_ids"] == [at_cutoff["id"]]


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("repository", "REPO_R2_ID"),
        ("source_path", "SNAPSHOT_RUTA"),
        ("future", "SNAPSHOT_FUTURO"),
        ("wrong_host", "SNAPSHOT_HOST"),
        ("extra_tag", "SNAPSHOT_TAGS"),
        ("short_summary", "SNAPSHOT_ARCHIVOS"),
        ("outside_removal", "POLITICA_RESULTADO"),
        ("latest_removed", "POLITICA_RESULTADO"),
        ("inventory_drift", "INVENTARIO_CAMBIO"),
        ("empty_cohort", "COHORTE_VACIA"),
    ],
)
def test_rejects_unsafe_inventory_or_simulation(case, code):
    latest = snapshot("a", "daily-2026-11-12", 1)
    old = snapshot("b", "daily-2026-10-01", 43)
    partial = {
        "id": "c" * 64,
        "hostname": "vm-media-01",
        "tags": ["immich-postgres-initialized-v1"],
    }
    before, keep, remove, after, config = [latest, old, partial], [latest], [old], None, CONFIG
    if case == "repository":
        config = {**CONFIG, "id": "0" * 64}
    elif case == "source_path":
        latest["paths"][0] = latest["paths"][0].replace("/staging/", "/other/")
    elif case == "future":
        latest["time"] = (NOW + timedelta(seconds=1)).isoformat()
    elif case == "wrong_host":
        latest["hostname"] = "wrong-host"
    elif case == "extra_tag":
        latest["tags"].append("pinned")
    elif case == "short_summary":
        latest["summary"]["total_files_processed"] = 4
    elif case == "outside_removal":
        remove.append(partial)
    elif case == "latest_removed":
        keep, remove = [old], [latest]
    elif case == "inventory_drift":
        after = [latest, old]
    elif case == "empty_cohort":
        before = [partial]
    execute, _ = restic_stub(before, keep, remove, after=after, config=config)

    with pytest.raises(ValueError, match=f"^{code}$"):
        retention.retention_plan(execute, STAGING, NOW, PROFILE_SHA)


def test_restic_rejects_partial_inventory_even_with_exit_zero(monkeypatch):
    monkeypatch.setattr(retention.daily, "verified_pin", lambda *_: None)

    def run(args, **_kwargs):
        assert "--no-lock" in args and "--no-cache" in args and "--quiet" in args
        return CompletedProcess(args, 0, b"[]", b"Ignoring snapshot: sensitive error detail")

    monkeypatch.setattr(retention.subprocess, "run", run)
    with pytest.raises(ValueError, match="^RESTIC_LECTURA$"):
        retention.run_restic({}, "snapshots")


@pytest.mark.parametrize("args", [("forget",), ("prune",), (*FORGET, "--prune"), ("unlock",)])
def test_restic_rejects_mutating_commands(args):
    with pytest.raises(ValueError, match="^RESTIC_OPERACION$"):
        retention.run_restic({}, *args)


def test_explicit_forget_locks_and_passes_only_complete_ids(monkeypatch):
    monkeypatch.setattr(retention.daily, "verified_pin", lambda *_: None)
    removed = ["a" * 64, "b" * 64]

    def run(args, **_kwargs):
        assert "--no-lock" not in args and "--prune" not in args
        assert args[-4:] == ["forget", "--", *removed]
        return CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(retention.subprocess, "run", run)
    retention.run_restic({}, forget_ids=removed)
    with pytest.raises(ValueError, match="^RESTIC_OPERACION$"):
        retention.run_restic({}, forget_ids=["latest"])


def restore_receipt(snapshot_id):
    return {
        "schema": 1,
        "destination": "r2",
        "repository_id": CONFIG["id"],
        "snapshot_id": snapshot_id,
        "manifest_sha256": "0" * 64,
        "verified_payloads": [
            "postgres-apps.sql.zst",
            "postgres-immich.sql.zst",
            "d1.sql.zst",
            "openclaw.tar.zst",
        ],
        "completed_at_utc": (NOW - timedelta(hours=1)).isoformat(),
        "success": True,
    }


def apply_fixture(*, nonempty=True):
    latest = snapshot("a", "latest", 1)
    older = snapshot("b", "older", 35)
    partial = {"id": "c" * 64, "hostname": "vm-media-01", "tags": ["kitsu-postgres-acceptance-v1"]}
    before = [latest, older, partial] if nonempty else [latest, partial]
    execute, _ = restic_stub(before, [latest], [older] if nonempty else None)
    plan, observed = retention.retention_plan(execute, STAGING, NOW, PROFILE_SHA)
    return latest, older, partial, plan, observed


def test_noop_apply_needs_no_receipt_or_forget():
    _, _, _, plan, observed = apply_fixture(nonempty=False)
    result = {"success": False}
    retention.apply_plan(plan, observed, NOW, None, None, None, None, result)
    assert result["success"] and result["no_op"]
    assert not result["forget_attempted"]


def test_apply_drops_exact_ids_and_reconciles_preserved_inventory():
    latest, older, partial, plan, observed = apply_fixture()
    result = {"success": False}
    deleted = []

    retention.apply_plan(
        plan,
        observed,
        NOW,
        plan["plan_sha256"],
        lambda: restore_receipt(latest["id"]),
        lambda ids: deleted.extend(ids),
        lambda: [partial, latest],
        result,
    )

    assert deleted == [older["id"]]
    assert result["success"] and result["forget_attempted"] and not result["no_op"]
    assert result["removed_ids"] == deleted
    assert not result["reconciliation_required"]


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("no_pin", "PLAN_SHA"),
        ("bad_pin", "PLAN_SHA"),
        ("receipt_missing", "RECIBO_RESTORE_REQUERIDO"),
        ("unsafe_keep", "RESTORE_SNAPSHOT"),
        ("wrong_repo", "RESTORE_R2"),
        ("stale_keep", "RESTORE_ANTIGUO"),
        ("before_restore", "RESTORE_FECHA"),
    ],
)
def test_apply_rejects_unreviewed_plan_or_unverified_keep(case, code):
    latest, older, _, plan, observed = apply_fixture()
    pin = plan["plan_sha256"]
    now = NOW
    receipt = restore_receipt(latest["id"])

    def load_receipt():
        return receipt

    if case == "no_pin":
        pin = None
    elif case == "bad_pin":
        pin = "e" * 64
    elif case == "receipt_missing":
        load_receipt = None
    elif case == "unsafe_keep":
        receipt["snapshot_id"] = older["id"]
    elif case == "wrong_repo":
        receipt["repository_id"] = "e" * 64
    elif case == "stale_keep":
        now += timedelta(days=30)
    elif case == "before_restore":
        receipt["completed_at_utc"] = (NOW - timedelta(days=2)).isoformat()

    def fail(*_):
        pytest.fail("forget/listing unexpectedly attempted")

    result = {"success": False}
    with pytest.raises(ValueError, match=f"^{code}$"):
        retention.apply_plan(plan, observed, now, pin, load_receipt, fail, fail, result)
    assert not result["forget_attempted"]


@pytest.mark.parametrize("case", ["command_failed", "timeout", "unexpected_delta"])
def test_uncertain_write_reports_observed_ids_without_retry(case):
    latest, older, partial, plan, observed = apply_fixture()
    attempted = []
    result = {"success": False}

    def forget(ids):
        attempted.extend(ids)
        if case == "command_failed":
            raise ValueError("RESTIC_FORGET")
        if case == "timeout":
            raise TimeoutError()

    after = [latest] if case == "unexpected_delta" else [partial, latest]
    with pytest.raises((TimeoutError, ValueError)):
        retention.apply_plan(
            plan,
            observed,
            NOW,
            plan["plan_sha256"],
            lambda: restore_receipt(latest["id"]),
            forget,
            lambda: after,
            result,
        )
    assert attempted == [older["id"]]
    assert result["reconciliation_required"] and not result["success"]
    assert result["removed_ids"] == attempted


def test_plan_sha_stable_over_age_but_changes_when_an_unselected_row_changes():
    latest, _, partial, plan, _ = apply_fixture(nonempty=False)
    execute, _ = restic_stub([partial, latest], [latest], None)
    later, _ = retention.retention_plan(execute, STAGING, NOW + timedelta(hours=1), PROFILE_SHA)
    changed = {**partial, "tags": ["new-outside-tag"]}
    execute, _ = restic_stub([latest, changed], [latest], None)
    drifted, _ = retention.retention_plan(execute, STAGING, NOW, PROFILE_SHA)
    assert later["plan_sha256"] == plan["plan_sha256"]
    assert drifted["plan_sha256"] != plan["plan_sha256"]


def test_receipt_file_needs_matching_sha(monkeypatch, tmp_path):
    path = tmp_path / "r2-restore.json"
    receipt = restore_receipt("a" * 64)
    monkeypatch.setattr(retention.core, "private_directory", lambda parent: parent == tmp_path)
    monkeypatch.setattr(
        retention.daily, "read_json", lambda *_args, **_kwargs: (receipt, PROFILE_SHA)
    )
    with pytest.raises(ValueError, match="^RESTORE_RECIBO_SHA$"):
        retention.load_restore_receipt(path, "e" * 64)
    assert retention.load_restore_receipt(path, PROFILE_SHA) == receipt


def test_cli_noop_apply_does_not_acquire_lock(monkeypatch, capsys):
    import json
    import sys
    from types import SimpleNamespace

    latest = snapshot("a", "latest", 1)
    execute, calls = restic_stub([latest], [latest], None)
    monkeypatch.setattr(sys, "argv", ["backup_321_retention.py", "apply"])
    monkeypatch.setattr(
        retention.os,
        "uname",
        lambda: SimpleNamespace(sysname="Linux", nodename="vm-media-01"),
        raising=False,
    )
    monkeypatch.setattr(retention.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(retention.os, "getegid", lambda: 0, raising=False)
    monkeypatch.setattr(retention.daily, "load_profile", lambda *_: ({}, PROFILE_SHA))
    monkeypatch.setattr(retention, "r2_context", lambda _: (STAGING, {}))
    monkeypatch.setattr(retention, "run_restic", lambda _env, *args: execute(*args))
    monkeypatch.setattr(
        retention.core, "exclusive", lambda: pytest.fail("lock unexpectedly acquired")
    )
    assert retention.main() == 0
    assert json.loads(capsys.readouterr().out)["no_op"] is True
    assert calls == [("cat", "config"), ("snapshots",), FORGET, ("snapshots",)]
