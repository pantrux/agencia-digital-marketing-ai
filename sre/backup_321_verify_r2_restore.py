"""Verifica una restauración R2 diaria y publica un recibo privado de retención."""

import argparse
import hashlib
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__:
    from . import backup_321_retention as retention
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_retention as retention


core = retention.core
daily = retention.daily
MANIFEST_LIMIT = 1024
CHUNK_SIZE = 128 * 1024
RESTORE_TIMEOUT = 900


def select_daily_snapshot(requested, plan, before, staging, now):
    for snapshot_id in plan["keep_ids"]:
        snapshot = before[snapshot_id]
        run_tag = next(tag for tag in snapshot["tags"] if retention.RUN_TAG.fullmatch(tag))
        run_id = run_tag.removeprefix("run:")
        if run_id.startswith("daily-"):
            core.require(snapshot_id == requested, "RESTORE_SNAPSHOT_DIARIO")
            instant, _ = retention.cohort_time(snapshot, staging, now)
            core.require(now - instant < timedelta(days=30), "RESTORE_ANTIGUO")
            return run_id, snapshot
    raise ValueError("RESTORE_SNAPSHOT_DIARIO")


def paired_manifest(run_id, snapshot, daily_row, journal, journal_sha, now):
    core.require(
        daily_row.get("run_id") == journal.get("run_id") == run_id
        and daily_row.get("success") is True
        and journal.get("success") is True
        and daily_row.get("backup_set_complete") is True
        and journal.get("backup_set_complete") is True,
        "RECIBO_DIARIO_ESTADO",
    )
    stages = daily_row["stages"]
    manifest = stages["assemble"]["manifest_sha256"]
    core.require(
        daily.pin(manifest)
        and manifest == stages["plan"]["manifest_sha256"] == journal["manifest_sha256"],
        "RECIBO_DIARIO_MANIFIESTO",
    )
    core.require(
        daily.pin(journal_sha) and stages["run"]["sha256"] == journal_sha, "RECIBO_DIARIO_JOURNAL"
    )
    snapshots = stages["run"]["snapshots"]
    destinations = journal["destinations"]
    daily.fields(snapshots, core.TARGETS, "RECIBO_DIARIO_DESTINOS")
    daily.fields(destinations, core.TARGETS, "RECIBO_DIARIO_DESTINOS")
    core.require(
        snapshots["r2"] == snapshot["id"] and snapshots["nas"] != snapshots["r2"],
        "RECIBO_DIARIO_DESTINOS",
    )
    for name, target in destinations.items():
        core.require(
            target.get("success") is True
            and target.get("reconciliation_required") is False
            and daily.pin(target.get("receipt_sha256"))
            and daily.pin(snapshots[name])
            and target.get("snapshot_id") == snapshots[name],
            "RECIBO_DIARIO_DESTINOS",
        )
    start = datetime.fromisoformat(daily_row["started_at_utc"])
    end = datetime.fromisoformat(daily_row["completed_at_utc"])
    snapshot_time = datetime.fromisoformat(snapshot["time"])
    core.require(
        start.utcoffset() == end.utcoffset() == timedelta(0)
        and start <= snapshot_time <= end <= now,
        "RECIBO_DIARIO_FECHA",
    )
    return manifest


def read_manifest(stream, path, expected_sha):
    manifest = bytearray()
    with closing(stream(path)) as chunks:
        for chunk in chunks:
            manifest.extend(chunk)
            core.require(len(manifest) <= MANIFEST_LIMIT, "RESTORE_MANIFIESTO_COTA")
    core.require(hashlib.sha256(manifest).hexdigest() == expected_sha, "SHA_MANIFIESTO")
    hashes = {}
    for row in manifest.splitlines(keepends=True):
        match = re.fullmatch(rb"([a-f0-9]{64})  ([a-z0-9.-]+)\n", row)
        core.require(match is not None, "MANIFIESTO_INVALIDO")
        digest, name = (part.decode("ascii") for part in match.groups())
        core.require(name in core.FILES and name not in hashes, "MANIFIESTO_NO_EXACTO")
        hashes[name] = digest
    core.require(hashes.keys() == set(core.FILES), "MANIFIESTO_INCOMPLETO")
    return hashes


def verify_payloads(stream, stage, manifest_sha):
    hashes = read_manifest(stream, stage / "SHA256SUMS", manifest_sha)
    total = 0
    for name in core.FILES:
        digest = hashlib.sha256()
        size = 0
        with closing(stream(stage / name)) as chunks:
            for chunk in chunks:
                size += len(chunk)
                total += len(chunk)
                core.require(total <= core.MAX_STAGING_BYTES, "RESTORE_COTA")
                digest.update(chunk)
        core.require(size > 0 and digest.hexdigest() == hashes[name], "SHA_RESTORE")
    return list(core.FILES)


def remaining(deadline):
    seconds = deadline - time.monotonic()
    core.require(seconds > 0, "RESTORE_TIMEOUT")
    return seconds


def stream_dump(env, snapshot_id, path, deadline):
    daily.verified_pin(retention.RESTIC, retention.RESTIC_SHA, retention.RESTIC_MODES)
    argv = [
        str(retention.RESTIC),
        "--no-lock",
        *retention.RESTIC_OPTIONS,
        "dump",
        "--",
        snapshot_id,
        str(path),
    ]
    with subprocess.Popen(
        argv,
        env=env,
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                selector.register(child.stderr, selectors.EVENT_READ)
                while selector.get_map():
                    events = selector.select(remaining(deadline))
                    core.require(events, "RESTORE_TIMEOUT")
                    for key, _ in events:
                        chunk = os.read(key.fd, CHUNK_SIZE)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            core.require(key.fileobj is child.stdout, "RESTORE_DUMP")
                            yield chunk
            core.require(child.wait(timeout=remaining(deadline)) == 0, "RESTORE_DUMP")
        finally:
            if child.poll() is None:
                child.kill()


def restore_receipt(snapshot_id, manifest_sha, verified, completed_at):
    return {
        "schema": 1,
        "success": True,
        "destination": "r2",
        "repository_id": retention.REPOSITORY_ID,
        "snapshot_id": snapshot_id,
        "manifest_sha256": manifest_sha,
        "verified_payloads": verified,
        "completed_at_utc": completed_at.isoformat(),
    }


def publish_receipt(path, receipt):
    content = core.encode(receipt) + b"\n"
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".restore-", delete=False) as output:
        pending = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            os.link(pending, path)
        finally:
            pending.unlink()
    core.sync_directory(path.parent)
    return hashlib.sha256(content).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
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
        core.require(daily.pin(args.snapshot_id), "RESTORE_SNAPSHOT_ID")
        core.private_directory(args.receipt.parent)
        core.require(not os.path.lexists(args.receipt), "RESTORE_RECIBO_EXISTENTE")
        profile, profile_sha = daily.load_profile(args.profile, args.profile_pin)
        staging, env = retention.r2_context(profile)

        def execute(*command):
            return retention.run_restic(env, *command)

        now = datetime.now(UTC)
        plan, before = retention.retention_plan(execute, staging, now, profile_sha)
        run_id, snapshot = select_daily_snapshot(args.snapshot_id, plan, before, staging, now)
        day = Path(profile["roots"]["daily"]) / run_id
        journal = Path(profile["roots"]["journal"]) / run_id
        core.private_directory(day)
        core.private_directory(journal)
        daily_row, _ = daily.read_json(day / "result.json")
        journal_row, journal_sha = daily.read_json(journal / "result.json")
        manifest = paired_manifest(run_id, snapshot, daily_row, journal_row, journal_sha, now)
        deadline = time.monotonic() + RESTORE_TIMEOUT

        def stream(path):
            return stream_dump(env, args.snapshot_id, path, deadline)

        # El productor entrega nombres relativos a restic. El árbol guarda esos
        # nombres en la raíz aunque el metadato paths registre orígenes absolutos.
        verified = verify_payloads(stream, Path("/"), manifest)
        core.require(before == retention.inventory(execute("snapshots")), "INVENTARIO_CAMBIO")
        completed = datetime.now(UTC)
        receipt = restore_receipt(args.snapshot_id, manifest, verified, completed)
        retention.verified_restore(receipt, plan["keep_ids"], before, completed)
        receipt_sha = publish_receipt(args.receipt, receipt)
        result = {"success": True, "snapshot_id": args.snapshot_id, "receipt_sha256": receipt_sha}
    except (*retention.COMMAND_ERRORS, AttributeError) as error:
        result = {"success": False, "error": core.output_code(error)}
    print(core.encode(result).decode(), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
