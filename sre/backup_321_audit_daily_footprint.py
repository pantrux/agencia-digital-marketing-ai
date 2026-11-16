"""Mide el espacio local asignado a corridas 3-2-1 a partir de sus recibos."""

import argparse
import os
import re
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from . import backup_321_audit_daily_coverage as coverage
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_audit_daily_coverage as coverage


core = coverage.core
ROOTS = {
    "postgres": core.ROOT / "backup-321-capturas-postgres-v2",
    "d1": core.ROOT / "d1-backup-production-v2",
    "openclaw": core.ROOT / "backup-321-openclaw-ingress-v1",
    "staging": core.ROOT / "backup-321-production-staging-v1",
    "daily": core.ROOT / "backup-321-daily-state-v1",
    "journal": core.ROOT / "backup-321-production-journal-v1",
}
PRODUCERS = ("postgres", "d1", "openclaw")
ESCAPE = re.compile(r"\\([0-7]{3})")


def mount_points(roots):
    targets = []
    for line in Path("/proc/self/mountinfo").read_text(errors="surrogateescape").splitlines():
        target = line.split(" - ", 1)[0].split()[4]
        path = Path(ESCAPE.sub(lambda match: chr(int(match[1], 8)), target))
        if any(path.is_relative_to(root) or root.is_relative_to(path) for root in roots.values()):
            targets.append(path)
    return tuple(sorted(targets))


def raise_walk_error(error):
    raise error


def tree_stats(path, mounts):
    core.require(not any(mount.is_relative_to(path) for mount in mounts), "MONTAJE_SUBARBOL")
    try:
        expected = path.lstat()
    except FileNotFoundError:
        return None
    core.require(stat.S_ISDIR(expected.st_mode), "DIRECTORIO_CORRIDA")
    entries = []
    for current, dirs, files, fd in os.fwalk(path, follow_symlinks=False, onerror=raise_walk_error):
        if current == os.fspath(path):
            observed = os.fstat(fd)
            core.require(
                (observed.st_dev, observed.st_ino) == (expected.st_dev, expected.st_ino),
                "DIRECTORIO_CAMBIO",
            )
        for name in (*dirs, *files):
            item = os.stat(name, dir_fd=fd, follow_symlinks=False)
            core.require(
                stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode), "ENTRADA_CORRIDA"
            )
            core.require(item.st_dev == expected.st_dev, "DISPOSITIVO_CAMBIO")
            entries.append(item)
    observed = path.lstat()
    core.require(
        (observed.st_dev, observed.st_ino) == (expected.st_dev, expected.st_ino),
        "DIRECTORIO_CAMBIO",
    )
    return [expected, *entries]


def root_usage(path, device, mounts, seen):
    entries = tree_stats(path, mounts)
    if entries is None:
        return {"present": False, "allocated_bytes": 0}
    total = 0
    for entry in entries:
        core.require(entry.st_dev == device, "DISPOSITIVO_CAMBIO")
        identity = entry.st_dev, entry.st_ino
        if identity not in seen:
            seen.add(identity)
            total += entry.st_blocks * 512
    return {"present": True, "allocated_bytes": total}


def run_usage(row, roots, device, mounts, seen):
    usage = {
        role: root_usage(root / row["run_id"], device, mounts, seen) for role, root in roots.items()
    }
    result = {key: row[key] for key in ("run_id", "date", "status", "error") if key in row}
    result.update(
        roots=usage,
        producer_allocated_bytes=sum(usage[role]["allocated_bytes"] for role in PRODUCERS),
        staging_allocated_bytes=usage["staging"]["allocated_bytes"],
        allocated_bytes=sum(root["allocated_bytes"] for root in usage.values()),
    )
    if row["status"] == "complete":
        started, confirmed = row["started"], row["confirmed_by"]
        result.update(
            started_at_utc=started.isoformat(),
            completed_at_utc=confirmed.isoformat(),
            duration_seconds=(confirmed - started).total_seconds(),
        )
    return result


def measure(roots, dates, zone, now, acceptance, device, mounts):
    seen = set()
    rows = [
        run_usage(coverage.observe_day(day, roots, zone, now), roots, device, mounts, seen)
        for day in dates
    ]
    result = {"days": rows, "calendar_allocated_bytes": sum(row["allocated_bytes"] for row in rows)}
    if acceptance:
        observed = coverage.observe_acceptance(acceptance, roots, zone, now)
        result["acceptance"] = run_usage(observed, roots, device, mounts, seen)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="last", required=True, metavar="YYYY-MM-DD")
    parser.add_argument(
        "--acceptance", metavar="RUN_ID", help="mide un ensayo fuera del total diario"
    )
    args = parser.parse_args()
    try:
        host = os.uname() if hasattr(os, "uname") else None
        core.require(
            host is not None
            and host.sysname == "Linux"
            and host.nodename == "vm-media-01"
            and os.geteuid() == os.getegid() == 0,
            "HOST_O_IDENTIDAD",
        )
        zone = ZoneInfo("America/Santiago")
        now = datetime.now(UTC)
        dates = coverage.calendar_dates(args.first, args.last)
        core.require(dates[-1] <= now.astimezone(zone).date(), "FECHA_FUTURA")
        core.require(
            not args.acceptance or coverage.ACCEPTANCE_ID.fullmatch(args.acceptance),
            "ACEPTACION_RUN_ID",
        )
        mounts = mount_points(ROOTS)
        device = Path("/").stat().st_dev
        for root in ROOTS.values():
            core.require(
                not any(root.is_relative_to(mount) for mount in mounts if mount != Path("/")),
                "MONTAJE_RAIZ",
            )
            core.private_directory(root)
            core.require(root.stat().st_dev == device, "DISPOSITIVO_CAMBIO")
        measured = measure(ROOTS, dates, zone, now, args.acceptance, device, mounts)
        fs = os.statvfs("/")
        core.require(mounts == mount_points(ROOTS), "MONTAJES_CAMBIO")
        result = {
            "success": True,
            "schema": 1,
            "timezone": zone.key,
            "from": args.first,
            "to": args.last,
            "started_at_utc": now.isoformat(),
            "observed_at_utc": datetime.now(UTC).isoformat(),
            "allocation": "st_blocks_x_512_unique_device_inode",
            "filesystem": {
                "device": f"{os.major(device)}:{os.minor(device)}",
                "available_bytes": fs.f_bavail * fs.f_frsize,
                "available_inodes": fs.f_favail,
            },
            **measured,
        }
    except (*coverage.ERRORS, IndexError) as error:
        result = {"success": False, "error": core.output_code(error)}
    print(core.encode(result).decode(), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
