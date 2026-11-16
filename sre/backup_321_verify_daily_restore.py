"""Coteja el manifiesto y los cuatro archivos de una copia diaria en NAS y R2."""

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

if __package__:
    from . import backup_321_audit_daily_coverage as coverage
    from . import backup_321_verify_r2_restore as restore
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_audit_daily_coverage as coverage
    import backup_321_verify_r2_restore as restore


retention = restore.retention
daily = restore.daily
core = restore.core


def nas_environment(profile):
    target = profile["targets"]["nas"]
    repository = Path(target["repository"])
    core.require(repository.resolve(strict=True) == repository, "REPOSITORIO_NAS_RUTA")
    mount = (
        subprocess.check_output(
            [
                "/usr/bin/findmnt",
                "-rn",
                "-T",
                str(repository),
                "-o",
                "SOURCE,TARGET,FSTYPE,OPTIONS",
            ],
            env=core.COMMAND_ENV,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        .decode("ascii")
        .split()
    )
    core.require(
        len(mount) == 4
        and mount[:3] == ["192.168.0.8:/volume2/producciones", "/mnt/producciones", "nfs4"]
        and {"vers=4.1", "proto=tcp"} <= set(mount[3].split(",")),
        "MONTAJE_NAS",
    )
    roots = [Path(path) for path in profile["roots"].values()]
    return {
        **core.COMMAND_ENV,
        "TZ": "UTC",
        "RESTIC_REPOSITORY": str(repository),
        "RESTIC_PASSWORD_FILE": str(daily.secret_file(target["password_file"], roots)),
    }


def verify_destination(env, repository_id, snapshot_id, run, staging, now):
    start = time.monotonic()
    config = retention.run_restic(env, "cat", "config")
    core.require(
        isinstance(config, dict)
        and config.get("id") == repository_id
        and type(config.get("version")) is int
        and config["version"] in (1, 2),
        "REPO_ID",
    )
    before = retention.inventory(retention.run_restic(env, "snapshots"))
    snapshot = before.get(snapshot_id)
    core.require(
        snapshot is not None and "run:" + run["run_id"] in (snapshot.get("tags") or ()),
        "SNAPSHOT_CORRIDA",
    )
    instant = retention.cohort_time(snapshot, staging, now)
    core.require(
        instant is not None
        and coverage.utc_time(run["started_at_utc"])
        <= instant[0]
        <= coverage.utc_time(run["completed_at_utc"])
        <= now,
        "RECIBO_DIARIO_FECHA",
    )
    deadline = time.monotonic() + restore.RESTORE_TIMEOUT

    def stream(path):
        return restore.stream_dump(env, snapshot_id, path, deadline)

    # Los cinco nombres se guardan en la raíz del árbol de restic.
    manifest = run["stages"]["assemble"]["manifest_sha256"]
    verified = restore.verify_payloads(stream, PurePosixPath("/"), manifest)
    core.require(
        before == retention.inventory(retention.run_restic(env, "snapshots")), "INVENTARIO_CAMBIO"
    )
    return {
        "snapshot_id": snapshot_id,
        "manifest_sha256": manifest,
        "verified_payloads": verified,
        "elapsed_s": round(time.monotonic() - start, 3),
    }


def verify_run(profile, run_id):
    if re.fullmatch(r"daily-\d{4}-\d\d-\d\d", run_id):
        date.fromisoformat(run_id.removeprefix("daily-"))
    else:
        core.require(coverage.ACCEPTANCE_ID.fullmatch(run_id), "CORRIDA_ID")
    day = Path(profile["roots"]["daily"]) / run_id
    journal = Path(profile["roots"]["journal"]) / run_id
    run, _ = coverage.read_private(day / "result.json")
    paired, journal_sha = coverage.read_private(journal / "result.json")
    coverage.verify_pair(run_id, run, paired, journal_sha)
    manifest = run["stages"]["assemble"]["manifest_sha256"]
    staging, r2 = retention.r2_context(profile)
    core.request_settings(
        daily.backup_request(profile, run_id, staging / run_id / "staging", manifest), "run"
    )
    environments = {"nas": nas_environment(profile), "r2": r2}
    snapshots = run["stages"]["run"]["snapshots"]
    now = datetime.now(UTC)
    verified = {
        name: verify_destination(
            environments[name], profile["targets"][name]["id"], snapshots[name], run, staging, now
        )
        for name in core.TARGETS
    }
    return {"success": True, "run_id": run_id, "destinations": verified}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
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
        profile, _ = daily.load_profile(args.profile, args.profile_pin)
        result = verify_run(profile, args.run_id)
    except (*retention.COMMAND_ERRORS, AttributeError) as error:
        result = {"success": False, "error": core.output_code(error)}
    print(core.encode(result).decode(), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
