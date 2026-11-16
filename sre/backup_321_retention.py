"""Simula la retención de Fase 4 en R2 o aplica un plan revisado."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

if __package__:
    from . import backup_321_daily as daily
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_daily as daily


core = daily.core
HOST = "vm-media-01"
TAG = "agency-backup-321"
PREFIX = "studio-backups-bucket/media-platform-v1/8d9af3d5-7911-4145-8676-064454780857/repository"
REPOSITORY_ID = "2221e96438027e76a521267c42b57ec382cd53670b84ac3febbfa00645bcd3a8"
RESTIC = Path("/opt/media-platform/backup-set-v1/restic")
RESTIC_SHA = "31260ff13e5ad3d63cd0c623843f3a88ae50392b1b98c5a24c325283c51194ca"
RESTIC_MODES = (0o500, 0o555, 0o700, 0o755)
RESTIC_OPTIONS = (
    "--no-cache",
    "--quiet",
    "--json",
    "-o",
    "s3.region=auto",
    "-o",
    "s3.bucket-lookup=path",
)
FORGET = (
    "forget",
    "--dry-run",
    "--host",
    HOST,
    "--tag",
    TAG,
    "--group-by",
    "host",
    "--keep-within",
    "30d",
    "--keep-last",
    "1",
)
PRODUCTION_RUN = "fase4-produccion-20261113-v1"
PRODUCTION_STAGING = PurePosixPath("/var/lib/media-platform/backup-321-production-staging-v1")
RUN_TAG = re.compile(r"run:([A-Za-z0-9][A-Za-z0-9._-]{0,63})")
RESTIC_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.(\d{1,9}))?(?:Z|[+-]\d\d:\d\d)")
RESTORE_FIELDS = {
    "schema",
    "destination",
    "repository_id",
    "snapshot_id",
    "manifest_sha256",
    "verified_payloads",
    "completed_at_utc",
    "success",
}
COMMAND_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    subprocess.SubprocessError,
    KeyboardInterrupt,
)


def inventory(rows):
    core.require(isinstance(rows, list), "INVENTARIO_JSON")
    result = {}
    for row in rows:
        core.require(isinstance(row, dict) and daily.pin(row.get("id")), "SNAPSHOT_ID")
        core.require(row["id"] not in result, "SNAPSHOT_DUPLICADO")
        result[row["id"]] = row
    return result


def cohort_time(row, daily_staging, now):
    tags = row.get("tags") or []
    core.require(
        isinstance(tags, list) and all(isinstance(tag, str) for tag in tags), "SNAPSHOT_TAGS"
    )
    if TAG not in tags:
        return None
    core.require(row.get("hostname") == HOST, "SNAPSHOT_HOST")
    runs = [match.group(1) for tag in tags if (match := RUN_TAG.fullmatch(tag))]
    core.require(len(tags) == 2 and len(runs) == 1, "SNAPSHOT_TAGS")
    run_id = runs[0]
    root = PRODUCTION_STAGING if run_id == PRODUCTION_RUN else daily_staging
    staging = root / run_id / "staging"
    expected_paths = sorted(str(staging / name) for name in core.STAGING_FILES)
    paths = row.get("paths")
    core.require(
        isinstance(paths, list)
        and all(isinstance(path, str) for path in paths)
        and sorted(paths) == expected_paths,
        "SNAPSHOT_RUTA",
    )
    summary = row.get("summary")
    core.require(
        isinstance(summary, dict) and summary.get("total_files_processed") == len(expected_paths),
        "SNAPSHOT_ARCHIVOS",
    )
    raw_time = row.get("time")
    timestamp = RESTIC_TIME.fullmatch(raw_time) if isinstance(raw_time, str) else None
    core.require(timestamp is not None, "SNAPSHOT_FECHA")
    instant = datetime.fromisoformat(raw_time).astimezone(UTC)
    # Restic conserva nanosegundos y datetime sólo conserva microsegundos.
    fraction = (timestamp.group(1) or "").ljust(9, "0")
    moment = instant, int(fraction[6:])
    core.require(moment < (now, 0), "SNAPSHOT_FUTURO")
    return moment


def retention_plan(execute, daily_staging, now, profile_sha):
    config = execute("cat", "config")
    core.require(
        isinstance(config, dict)
        and config.get("id") == REPOSITORY_ID
        and type(config.get("version")) is int
        and config["version"] in (1, 2),
        "REPO_R2_ID",
    )
    before = inventory(execute("snapshots"))
    times = {
        snapshot_id: moment
        for snapshot_id, row in before.items()
        if (moment := cohort_time(row, daily_staging, now)) is not None
    }
    core.require(times, "COHORTE_VACIA")
    newest, fraction = max(times.values())
    cutoff = newest - timedelta(days=30), fraction
    expected_keep = {snapshot_id for snapshot_id, moment in times.items() if moment > cutoff}

    groups = execute(*FORGET)
    core.require(
        isinstance(groups, list)
        and len(groups) == 1
        and isinstance(groups[0], dict)
        and groups[0].get("host") == HOST
        and not groups[0].get("paths")
        and not groups[0].get("tags"),
        "POLITICA_GRUPO",
    )
    group = groups[0]
    keep = inventory(group["keep"])
    remove = inventory([] if group["remove"] is None else group["remove"])
    core.require(
        keep.keys() == expected_keep
        and remove.keys() == times.keys() - expected_keep
        and all(row == before[snapshot_id] for snapshot_id, row in (keep | remove).items()),
        "POLITICA_RESULTADO",
    )
    core.require(before == inventory(execute("snapshots")), "INVENTARIO_CAMBIO")
    ordered = sorted(times, key=lambda snapshot_id: (times[snapshot_id], snapshot_id), reverse=True)
    selection = {
        "schema": 1,
        "repository_id": REPOSITORY_ID,
        "restic_sha256": RESTIC_SHA,
        "profile_sha256": profile_sha,
        "policy": FORGET,
        "inventory_sha256": hashlib.sha256(core.encode(before)).hexdigest(),
        "keep_ids": [snapshot_id for snapshot_id in ordered if snapshot_id in keep],
        "would_remove_ids": [snapshot_id for snapshot_id in ordered if snapshot_id in remove],
    }
    return {
        "plan_sha256": hashlib.sha256(core.encode(selection)).hexdigest(),
        "profile_sha256": profile_sha,
        "keep_ids": selection["keep_ids"],
        "would_remove_ids": selection["would_remove_ids"],
        "outside_cohort": len(before) - len(times),
        "newest_age_seconds": int((now - newest).total_seconds()),
        "snapshot_inventory_unchanged": True,
    }, before


def verified_restore(receipt, keep_ids, before, now):
    daily.fields(receipt, RESTORE_FIELDS, "RESTORE_CAMPOS")
    core.require(
        type(receipt["schema"]) is int and receipt["schema"] == 1 and receipt["success"] is True,
        "RESTORE_ESTADO",
    )
    core.require(
        receipt["destination"] == "r2" and receipt["repository_id"] == REPOSITORY_ID, "RESTORE_R2"
    )
    snapshot_id = receipt["snapshot_id"]
    core.require(snapshot_id in keep_ids, "RESTORE_SNAPSHOT")
    payloads = receipt["verified_payloads"]
    core.require(
        daily.pin(receipt["manifest_sha256"])
        and isinstance(payloads, list)
        and sorted(payloads) == sorted(core.FILES),
        "RESTORE_MANIFIESTO",
    )
    snapshot_time = datetime.fromisoformat(before[snapshot_id]["time"]).astimezone(UTC)
    raw = receipt["completed_at_utc"]
    core.require(
        isinstance(raw, str) and RESTIC_TIME.fullmatch(raw) and raw.endswith(("Z", "+00:00")),
        "RESTORE_FECHA",
    )
    restored_at = datetime.fromisoformat(raw)
    core.require(snapshot_time <= restored_at <= now, "RESTORE_FECHA")
    core.require(now - snapshot_time < timedelta(days=30), "RESTORE_ANTIGUO")
    return snapshot_id


def load_restore_receipt(path, expected_sha):
    core.require(daily.pin(expected_sha), "RESTORE_RECIBO_SHA")
    core.private_directory(path.parent)
    receipt, observed_sha = daily.read_json(path, modes=(0o400, 0o600))
    core.require(observed_sha == expected_sha, "RESTORE_RECIBO_SHA")
    return receipt


def apply_plan(plan, before, now, approved_sha, load_receipt, forget, list_after, result):
    remove_ids = plan["would_remove_ids"]
    result.update(forget_attempted=False, reconciliation_required=False)
    if remove_ids or approved_sha is not None:
        core.require(approved_sha == plan["plan_sha256"], "PLAN_SHA")
    if not remove_ids:
        result.update(success=True, no_op=True, removed_ids=[])
        return
    core.require(load_receipt is not None, "RECIBO_RESTORE_REQUERIDO")
    result["verified_keep_id"] = verified_restore(load_receipt(), plan["keep_ids"], before, now)
    result.update(forget_attempted=True, no_op=False, reconciliation_required=True)
    result["preflight_inventory_unchanged"] = result.pop("snapshot_inventory_unchanged", True)
    try:
        forget(remove_ids)
    finally:
        after = inventory(list_after())
        result["removed_ids"] = [
            snapshot_id for snapshot_id in remove_ids if snapshot_id not in after
        ]
        expected = {
            snapshot_id: row for snapshot_id, row in before.items() if snapshot_id not in remove_ids
        }
        result["post_apply_inventory_matches"] = after == expected
        core.require(result["post_apply_inventory_matches"], "INVENTARIO_APLICADO_DISTINTO")
    result.update(success=True, reconciliation_required=False)


def r2_context(profile):
    daily.fields(profile, daily.PROFILE_FIELDS, "PERFIL_CAMPOS")
    core.require(type(profile["schema"]) is int and profile["schema"] == 1, "PERFIL_VERSION")
    daily.fields(profile["roots"], daily.ROOT_NAMES, "PERFIL_RAICES")
    roots = [Path(profile["roots"][name]) for name in daily.ROOT_NAMES]
    staging = Path(profile["roots"]["staging"])
    core.private_directory(staging)
    daily.fields(profile["targets"], core.TARGETS, "PERFIL_DESTINOS")
    target = profile["targets"]["r2"]
    daily.fields(target, {"id", "repository", "password_file"}, "PERFIL_R2_DESTINO")
    repository = target["repository"]
    core.require(
        target["id"] == REPOSITORY_ID
        and isinstance(repository, str)
        and re.fullmatch(
            r"s3:https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com/" + PREFIX, repository
        ),
        "PERFIL_R2_REPOSITORIO",
    )
    core.require(
        profile["restic"] == str(RESTIC) and profile["restic_sha256"] == RESTIC_SHA, "PERFIL_RESTIC"
    )
    daily.trusted_install_dir(RESTIC.parent)
    password = daily.secret_file(target["password_file"], roots)
    env = {
        **core.COMMAND_ENV,
        "TZ": "UTC",
        "RESTIC_REPOSITORY": repository,
        "RESTIC_PASSWORD_FILE": str(password),
        "AWS_DEFAULT_REGION": "auto",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    daily.fields(profile["r2"], daily.R2_FIELDS, "PERFIL_R2_CREDENCIALES")
    for field, variable in (
        ("access_key_id_file", "AWS_ACCESS_KEY_ID"),
        ("secret_access_key_file", "AWS_SECRET_ACCESS_KEY"),
    ):
        path = daily.secret_file(profile["r2"][field], roots)
        raw, _ = core.read_verified(path, maximum=258, modes=(0o400, 0o600))
        value = raw.removesuffix(b"\r\n") if raw.endswith(b"\r\n") else raw.removesuffix(b"\n")
        core.require(
            re.fullmatch(rb"[!-~]{1,256}", value) and value != b"CHANGE_ME", "CREDENCIAL_R2"
        )
        env[variable] = value.decode("ascii")
    return PurePosixPath(staging), env


def run_restic(env, *args, forget_ids=()):
    if forget_ids:
        core.require(
            not args
            and all(daily.pin(snapshot_id) for snapshot_id in forget_ids)
            and len(set(forget_ids)) == len(forget_ids),
            "RESTIC_OPERACION",
        )
        options = RESTIC_OPTIONS
        args = ("forget", "--", *forget_ids)
    else:
        core.require(args in (("cat", "config"), ("snapshots",), FORGET), "RESTIC_OPERACION")
        options = ("--no-lock", *RESTIC_OPTIONS)
    daily.verified_pin(RESTIC, RESTIC_SHA, RESTIC_MODES)
    child = subprocess.run(
        [str(RESTIC), *options, *args],
        env=env,
        cwd="/",
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=180,
    )
    # El listado de restic puede omitir un snapshot ilegible y salir con código 0.
    core.require(
        child.returncode == 0 and not child.stderr,
        "RESTIC_FORGET" if forget_ids else "RESTIC_LECTURA",
    )
    if forget_ids:
        return None
    core.require(len(child.stdout) <= 16 * 1024**2, "RESTIC_SALIDA_COTA")
    return json.loads(child.stdout, object_pairs_hook=core.unique_fields)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("dry-run", "apply"), default="dry-run")
    parser.add_argument("--profile", type=Path, default=daily.DEFAULT_CONFIG / "profile.json")
    parser.add_argument("--profile-pin", type=Path, default=daily.DEFAULT_CONFIG / "profile.sha256")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--restore-receipt", type=Path)
    parser.add_argument("--restore-receipt-sha256")
    args = parser.parse_args()
    result = {"success": False, "action": args.action}
    try:
        host = os.uname() if hasattr(os, "uname") else None
        core.require(
            host is not None
            and host.sysname == "Linux"
            and host.nodename == HOST
            and os.geteuid() == os.getegid() == 0,
            "HOST_O_IDENTIDAD",
        )
        core.require(
            (args.restore_receipt is None) == (args.restore_receipt_sha256 is None),
            "RESTORE_ARGUMENTOS",
        )
        core.require(
            args.action == "apply" or (args.plan_sha256 is None and args.restore_receipt is None),
            "APPLY_ARGUMENTOS",
        )
        core.require(args.plan_sha256 is None or daily.pin(args.plan_sha256), "PLAN_SHA")
        profile, digest = daily.load_profile(args.profile, args.profile_pin)
        staging, env = r2_context(profile)

        def execute(*command):
            return run_restic(env, *command)

        plan, before = retention_plan(execute, staging, datetime.now(UTC), digest)
        result.update(plan)
        if args.action == "dry-run":
            result["success"] = True
        else:
            candidates = plan["would_remove_ids"]
            if candidates:
                core.require(args.plan_sha256 == plan["plan_sha256"], "PLAN_SHA")
                signal.signal(signal.SIGTERM, core.interrupted)
                signal.signal(signal.SIGHUP, core.interrupted)
            with core.exclusive() if candidates else nullcontext():
                if candidates:
                    plan, before = retention_plan(execute, staging, datetime.now(UTC), digest)
                    result.update(plan)

                def load_receipt():
                    return load_restore_receipt(args.restore_receipt, args.restore_receipt_sha256)

                apply_plan(
                    plan,
                    before,
                    datetime.now(UTC),
                    args.plan_sha256,
                    load_receipt if args.restore_receipt else None,
                    lambda ids: run_restic(env, forget_ids=ids),
                    lambda: execute("snapshots"),
                    result,
                )
    except COMMAND_ERRORS as error:
        result.update(success=False, error=core.output_code(error))
    print(core.encode(result).decode(), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
