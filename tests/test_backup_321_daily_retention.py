from datetime import UTC, datetime

import pytest

from sre import backup_321_daily_retention as postcopy

SNAPSHOT = "a" * 64
OLDER = "b" * 64
PLAN_SHA = "c" * 64
RESTORE_SHA = "d" * 64
MANIFEST = "e" * 64
RUN = "daily-acceptance-20261113-v2"


def workflow(mode, *, remove=(), failure=None):
    result = {"success": False}
    calls = []
    plan = {
        "plan_sha256": PLAN_SHA,
        "keep_ids": [SNAPSHOT],
        "would_remove_ids": list(remove),
        "snapshot_inventory_unchanged": True,
    }
    before = {SNAPSHOT: {"id": SNAPSHOT}}

    def select():
        calls.append("plan")
        if failure == "backup":
            raise ValueError("RECIBO_DIARIO_ESTADO")
        if failure == "drift" and calls.count("plan") == 2:
            return {**plan, "plan_sha256": "0" * 64}, before, SNAPSHOT, MANIFEST
        return plan, before, SNAPSHOT, MANIFEST

    def restore(snapshot, manifest, selected, observed):
        calls.append("restore")
        assert (snapshot, manifest, selected, observed) == (SNAPSHOT, MANIFEST, plan, before)
        if failure == "restore":
            raise ValueError("SHA_RESTORE")
        return RESTORE_SHA

    def apply(approved, receipt_sha, report):
        calls.append("apply")
        assert (approved, receipt_sha, report) == (plan, RESTORE_SHA, result)
        report.update(success=True, forget_attempted=True, removed_ids=list(remove))

    def run():
        postcopy.run_workflow(mode, select, restore, apply, result)

    return run, calls, result


@pytest.mark.parametrize("remove", [(), (OLDER,)])
def test_audit_reads_full_restore_and_never_calls_apply(remove):
    run, calls, result = workflow("audit", remove=remove)
    run()
    assert calls == ["plan", "restore", "plan"]
    assert result["success"] and result["restoration_verified"]
    assert result["snapshot_inventory_unchanged"] and not result["forget_attempted"]
    assert result["would_remove_ids"] == list(remove)


def test_apply_without_candidates_does_not_restore_or_write_repository():
    run, calls, result = workflow("apply")
    run()
    assert calls == ["plan"]
    assert result["success"] and result["no_op"] and not result["forget_attempted"]


def test_apply_requires_verified_restore_before_reusing_guarded_forget():
    run, calls, result = workflow("apply", remove=(OLDER,))
    run()
    assert calls == ["plan", "restore", "apply"]
    assert result["success"] and result["forget_attempted"]
    assert result["restore_receipt_sha256"] == RESTORE_SHA


@pytest.mark.parametrize(
    ("mode", "failure", "code", "steps"),
    [
        ("apply", "backup", "RECIBO_DIARIO_ESTADO", ["plan"]),
        ("apply", "restore", "SHA_RESTORE", ["plan", "restore"]),
        ("audit", "drift", "PLAN_SHA", ["plan", "restore", "plan"]),
    ],
)
def test_workflow_fails_closed(mode, failure, code, steps):
    run, calls, result = workflow(mode, remove=(OLDER,), failure=failure)
    with pytest.raises(ValueError, match=f"^{code}$"):
        run()
    assert calls == steps and not result["success"]
    assert not result.get("forget_attempted", False)


def test_revalidation_under_lock_refuses_drift_before_any_forget(monkeypatch):
    from contextlib import contextmanager

    events = []

    @contextmanager
    def lock():
        events.append("locked")
        yield
        events.append("unlocked")

    def select():
        assert events == ["locked"]
        return {"plan_sha256": "0" * 64}, {}, SNAPSHOT, MANIFEST

    monkeypatch.setattr(postcopy.core, "exclusive", lock)
    with pytest.raises(ValueError, match="^PLAN_SHA$"):
        postcopy.apply_locked({"plan_sha256": PLAN_SHA}, RESTORE_SHA, {}, select, None, None)


def test_revalidation_uses_existing_retention_guard_and_private_restore(monkeypatch, tmp_path):
    from contextlib import nullcontext

    selected = {"plan_sha256": PLAN_SHA, "keep_ids": [SNAPSHOT], "would_remove_ids": [OLDER]}
    before = {SNAPSHOT: {"id": SNAPSHOT}, OLDER: {"id": OLDER}}
    calls = []
    receipt = {"snapshot_id": SNAPSHOT, "manifest_sha256": MANIFEST}
    receipt_path = tmp_path / "restore.json"
    monkeypatch.setattr(postcopy.core, "exclusive", nullcontext)
    monkeypatch.setattr(
        postcopy.retention,
        "load_restore_receipt",
        lambda path, digest: calls.append((path, digest)) or receipt,
    )

    def apply(plan, rows, now, approved, load, forget, list_after, result):
        assert plan == selected and rows == before and approved == PLAN_SHA
        assert isinstance(now, datetime) and now.tzinfo == UTC
        assert load() == receipt
        forget([OLDER])
        assert list_after() == [before[SNAPSHOT]]
        result.update(success=True)

    def execute(*args, forget_ids=()):
        calls.append((args, forget_ids))
        return [before[SNAPSHOT]]

    monkeypatch.setattr(postcopy.retention, "apply_plan", apply)
    result = {"success": False}
    postcopy.apply_locked(
        selected,
        RESTORE_SHA,
        result,
        lambda: (selected, before, SNAPSHOT, MANIFEST),
        receipt_path,
        execute,
    )
    assert result["success"]
    assert calls == [(receipt_path, RESTORE_SHA), ((), [OLDER]), (("snapshots",), ())]


def test_acceptance_only_audit_and_current_run_allowed_to_apply(monkeypatch):
    monkeypatch.setattr(postcopy.daily, "daily_id", lambda _: "daily-2026-11-13")
    now = datetime(2026, 11, 13, tzinfo=UTC)
    postcopy.check_run(RUN, "audit", now)
    postcopy.check_run("daily-2026-11-13", "apply", now)
    for run_id in (RUN, "daily-2026-11-12"):
        with pytest.raises(ValueError, match="^RETENCION_CORRIDA_ACTUAL$"):
            postcopy.check_run(run_id, "apply", now)
    with pytest.raises(ValueError, match="^RETENCION_RUN_ID$"):
        postcopy.check_run("daily-../secret", "audit", now)


def test_records_independent_immutable_failure_and_blocks_implicit_replay(monkeypatch, tmp_path):
    import json

    monkeypatch.setattr(postcopy.core, "sync_directory", lambda _: None)

    def select(*_):
        raise ValueError("RECIBO_DIARIO_ESTADO")

    monkeypatch.setattr(postcopy, "daily_selection", select)
    result = postcopy.process_run(RUN, "audit", tmp_path, tmp_path, tmp_path, PLAN_SHA, {})
    assert not result["success"] and result["error"] == "RECIBO_DIARIO_ESTADO"
    assert json.loads((tmp_path / "retention-audit.intent.json").read_bytes())["run_id"] == RUN
    saved = (tmp_path / "retention-audit.json").read_bytes()
    assert not json.loads(saved)["forget_attempted"]
    assert not (tmp_path / "retention-audit-restore.json").exists()
    with pytest.raises(ValueError, match="^RETENCION_RECIBO_EXISTENTE$"):
        postcopy.process_run(RUN, "audit", tmp_path, tmp_path, tmp_path, PLAN_SHA, {})
    assert (tmp_path / "retention-audit.json").read_bytes() == saved
