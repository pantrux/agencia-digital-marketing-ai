"""Verifica y, en modo explícito, aplica la retención tras la copia diaria."""

import argparse
import os
import re
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from . import backup_321_verify_r2_restore as restore
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_verify_r2_restore as restore


retention = restore.retention
daily = restore.daily
core = restore.core
ERRORS = (*retention.COMMAND_ERRORS, AttributeError)
RUN_ID = re.compile(r"daily-[A-Za-z0-9][A-Za-z0-9._-]{0,57}")


def run_workflow(mode, select, verify, apply, result):
    core.require(mode in ("audit", "apply"), "RETENCION_MODO")
    plan, before, snapshot_id, manifest = select()
    result.update(
        plan,
        snapshot_id=snapshot_id,
        forget_attempted=False,
        reconciliation_required=False,
        restoration_verified=False,
        removed_ids=[],
        no_op=False,
    )
    candidates = plan["would_remove_ids"]
    if mode == "apply" and not candidates:
        result.update(success=True, no_op=True)
        return
    receipt_sha = verify(snapshot_id, manifest, plan, before)
    result.update(restoration_verified=True, restore_receipt_sha256=receipt_sha)
    if mode == "audit":
        observed, _, _, _ = select()
        core.require(observed["plan_sha256"] == plan["plan_sha256"], "PLAN_SHA")
        result.update(success=True, no_op=not candidates)
    else:
        apply(plan, receipt_sha, result)


def apply_locked(approved, receipt_sha, result, select, receipt_path, execute):
    with core.exclusive():
        plan, before, snapshot_id, manifest = select()
        core.require(plan["plan_sha256"] == approved["plan_sha256"], "PLAN_SHA")

        def load_receipt():
            receipt = retention.load_restore_receipt(receipt_path, receipt_sha)
            core.require(
                receipt["snapshot_id"] == snapshot_id and receipt["manifest_sha256"] == manifest,
                "RECIBO_RESTORE_DIARIO",
            )
            return receipt

        retention.apply_plan(
            plan,
            before,
            datetime.now(UTC),
            approved["plan_sha256"],
            load_receipt,
            lambda ids: execute(forget_ids=ids),
            lambda: execute("snapshots"),
            result,
        )


def check_run(run_id, mode, now):
    core.require(RUN_ID.fullmatch(run_id), "RETENCION_RUN_ID")
    if mode == "apply":
        core.require(run_id == daily.daily_id(now), "RETENCION_CORRIDA_ACTUAL")


def daily_selection(run_id, mode, day, journal, staging, profile_sha, execute):
    now = datetime.now(UTC)
    check_run(run_id, mode, now)
    daily_row, _ = daily.read_json(day / "result.json")
    core.require(
        daily_row.get("success") is True and daily_row.get("backup_set_complete") is True,
        "RECIBO_DIARIO_ESTADO",
    )
    journal_row, journal_sha = daily.read_json(journal / "result.json")
    snapshot_id = daily_row["stages"]["run"]["snapshots"]["r2"]
    core.require(daily.pin(snapshot_id), "RESTORE_SNAPSHOT_ID")
    plan, before = retention.retention_plan(execute, staging, now, profile_sha)
    selected, snapshot = restore.select_daily_snapshot(snapshot_id, plan, before, staging, now)
    core.require(selected == run_id, "RETENCION_CORRIDA_SNAPSHOT")
    manifest = restore.paired_manifest(run_id, snapshot, daily_row, journal_row, journal_sha, now)
    return plan, before, snapshot_id, manifest


def verify_restore(snapshot_id, manifest, plan, before, path, env):
    deadline = time.monotonic() + restore.RESTORE_TIMEOUT

    def stream(filename):
        return restore.stream_dump(env, snapshot_id, filename, deadline)

    verified = restore.verify_payloads(stream, Path("/"), manifest)
    completed = datetime.now(UTC)
    receipt = restore.restore_receipt(snapshot_id, manifest, verified, completed)
    retention.verified_restore(receipt, plan["keep_ids"], before, completed)
    return restore.publish_receipt(path, receipt)


def process_run(run_id, mode, day, journal, staging, profile_sha, env):
    stem = f"retention-{mode}"
    intent_path = day / f"{stem}.intent.json"
    restore_path = day / f"{stem}-restore.json"
    result_path = day / f"{stem}.json"
    result = {
        "schema": 1,
        "success": False,
        "mode": mode,
        "run_id": run_id,
        "forget_attempted": False,
        "reconciliation_required": False,
        "started_at_utc": datetime.now(UTC).isoformat(),
    }
    core.require(
        not any(os.path.lexists(path) for path in (intent_path, restore_path, result_path)),
        "RETENCION_RECIBO_EXISTENTE",
    )
    # El intento impide repetir una aplicación interrumpida antes de reconciliarla.
    restore.publish_receipt(intent_path, result)

    def execute(*command, forget_ids=()):
        return retention.run_restic(env, *command, forget_ids=forget_ids)

    def select():
        return daily_selection(run_id, mode, day, journal, staging, profile_sha, execute)

    def verify(snapshot_id, manifest, plan, before):
        return verify_restore(snapshot_id, manifest, plan, before, restore_path, env)

    def apply(plan, receipt_sha, report):
        apply_locked(plan, receipt_sha, report, select, restore_path, execute)

    try:
        run_workflow(mode, select, verify, apply, result)
    except ERRORS as error:
        result.update(success=False, error=core.output_code(error))
    result["completed_at_utc"] = datetime.now(UTC).isoformat()
    digest = restore.publish_receipt(result_path, result)
    return {**result, "receipt_sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=("audit", "apply"), default="audit")
    parser.add_argument("--profile", type=Path, default=daily.DEFAULT_CONFIG / "profile.json")
    parser.add_argument("--profile-pin", type=Path, default=daily.DEFAULT_CONFIG / "profile.sha256")
    args = parser.parse_args()
    try:
        host = os.uname() if hasattr(os, "uname") else None
        core.require(
            host is not None
            and host.sysname == "Linux"
            and host.nodename == retention.HOST
            and os.geteuid() == os.getegid() == 0,
            "HOST_O_IDENTIDAD",
        )
        os.umask(0o077)
        signal.signal(signal.SIGTERM, core.interrupted)
        signal.signal(signal.SIGHUP, core.interrupted)
        check_run(args.run_id, args.mode, datetime.now(UTC))
        profile, profile_sha = daily.load_profile(args.profile, args.profile_pin)
        staging, env = retention.r2_context(profile)
        day = Path(profile["roots"]["daily"]) / args.run_id
        journal = Path(profile["roots"]["journal"]) / args.run_id
        core.private_directory(day)
        core.private_directory(journal)
        result = process_run(args.run_id, args.mode, day, journal, staging, profile_sha, env)
    except ERRORS as error:
        result = {"success": False, "mode": args.mode, "error": core.output_code(error)}
    print(core.encode(result).decode(), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
