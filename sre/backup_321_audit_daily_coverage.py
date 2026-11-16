"""Mide ventanas de captura y cobertura diaria desde recibos locales 3-2-1."""

import argparse
import os
import re
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from . import backup_321_daily as daily
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import backup_321_daily as daily


core = daily.core
COMPONENTS = (*daily.PG, "d1", "openclaw")
ROOTS = ("daily", "journal", "postgres", "d1", "openclaw")
ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError)
MAX_DAYS = 366
DAILY_TIME = time(3, 30)
ACCEPTANCE_ID = re.compile(r"daily-acceptance-[A-Za-z0-9][A-Za-z0-9._-]{0,46}")


def calendar_dates(start, end):
    core.require(
        all(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day) for day in (start, end)),
        "FECHA_FORMATO",
    )
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    count = (last - first).days + 1
    core.require(0 < count <= MAX_DAYS, "FECHA_RANGO")
    return [first + timedelta(days=offset) for offset in range(count)]


def utc_time(value):
    instant = datetime.fromisoformat(value)
    core.require(instant.utcoffset() == timedelta(0), "RECIBO_FECHA_UTC")
    return instant.astimezone(UTC)


def verify_pair(run_id, row, journal, journal_sha):
    core.require(
        row.get("run_id") == journal.get("run_id") == run_id
        and row.get("success") is True
        and journal.get("success") is True
        and row.get("backup_set_complete") is True
        and journal.get("backup_set_complete") is True,
        "RECIBO_DIARIO_ESTADO",
    )
    stages = row["stages"]
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
    core.require(snapshots["nas"] != snapshots["r2"], "RECIBO_DIARIO_DESTINOS")
    for name, target in destinations.items():
        core.require(
            target.get("success") is True
            and target.get("reconciliation_required") is False
            and daily.pin(target.get("receipt_sha256"))
            and daily.pin(snapshots[name])
            and target.get("snapshot_id") == snapshots[name],
            "RECIBO_DIARIO_DESTINOS",
        )


def read_private(path):
    core.private_directory(path.parent)
    return daily.read_json(path)


def read_capture(path, reference):
    core.require(reference["receipt"] == str(path), "RECIBO_CAPTURA_RUTA")
    receipt, digest = read_private(path)
    core.require(
        digest == reference["sha256"] and receipt.get("success") is True, "RECIBO_CAPTURA_SHA"
    )
    return receipt


def capture_windows(roots, run_id, stages, started, confirmed):
    pg = read_capture(roots["postgres"] / run_id / "result.json", stages["postgres"])
    core.require(
        pg.get("run_id") == run_id
        and pg.get("scope") == "production"
        and pg.get("step") == "captured",
        "RECIBO_POSTGRES",
    )
    daily.fields(pg["captures"], daily.PG, "RECIBO_POSTGRES")
    d1 = read_capture(roots["d1"] / run_id / "result.json", stages["d1"])
    core.require(
        d1.get("run_id") == run_id
        and d1.get("step") == "captured"
        and d1.get("owner_verified") is True
        and d1.get("sqlite_integrity_verified") is True,
        "RECIBO_D1",
    )
    openclaw = stages["openclaw"]
    daily.fields(openclaw, daily.OPENCLAW_FIELDS, "RECIBO_OPENCLAW")
    oc_capture = read_capture(roots["openclaw"] / run_id / "capture.json", openclaw)
    core.require(
        openclaw["hostname"] == "openclaw-ia"
        and daily.pin(openclaw["manifest_sha256"])
        and oc_capture.get("action") == "capture"
        and oc_capture.get("scope") == "agency-state"
        and oc_capture.get("synthetic") is False
        and oc_capture.get("manifest_sha256") == openclaw["manifest_sha256"],
        "RECIBO_OPENCLAW",
    )
    windows = {}
    for name, capture in {**pg["captures"], "d1": d1, "openclaw": openclaw}.items():
        begin, end = utc_time(capture["started_at_utc"]), utc_time(capture["completed_at_utc"])
        # El runner permite 60 s de diferencia de reloj con el host de OpenClaw.
        lower = started - timedelta(seconds=60) if name == "openclaw" else started
        core.require(lower <= begin <= end <= confirmed, "RECIBO_CAPTURA_FECHA")
        windows[name] = begin, end
    return windows


def observe_run(run_id, record, roots, zone, now, day=None):
    path = roots["daily"] / run_id / "result.json"
    if not os.path.lexists(path.parent):
        santiago_now = now.astimezone(zone)
        status = (
            "pending"
            if day == santiago_now.date() and santiago_now.time() < DAILY_TIME
            else "missing"
        )
        return {**record, "status": status}
    try:
        core.private_directory(path.parent)
        if not os.path.lexists(path):
            return {**record, "status": "incomplete"}
        row, _ = daily.read_json(path)
        core.require(
            row.get("run_id") == run_id and type(row.get("success")) is bool, "RECIBO_DIARIO_ESTADO"
        )
        if row["success"] is False:
            return {**record, "status": "failed"}
        started, confirmed = utc_time(row["started_at_utc"]), utc_time(row["completed_at_utc"])
        local_date = started.astimezone(zone).date()
        core.require(
            (day is None or local_date == day) and started <= confirmed <= now,
            "RECIBO_DIARIO_FECHA",
        )
        journal, journal_sha = read_private(roots["journal"] / run_id / "result.json")
        verify_pair(run_id, row, journal, journal_sha)
        captures = capture_windows(roots, run_id, row["stages"], started, confirmed)
        return {
            **record,
            "date": local_date.isoformat(),
            "status": "complete",
            "started": started,
            "confirmed_by": confirmed,
            "captures": captures,
        }
    except ERRORS as error:
        return {**record, "status": "unverified", "error": core.output_code(error)}


def observe_day(day, roots, zone, now):
    run_id = f"daily-{day.isoformat()}"
    record = {"date": day.isoformat(), "run_id": run_id, "kind": "calendar_run_id"}
    return observe_run(run_id, record, roots, zone, now, day=day)


def observe_acceptance(run_id, roots, zone, now):
    core.require(ACCEPTANCE_ID.fullmatch(run_id), "ACEPTACION_RUN_ID")
    return observe_run(run_id, {"run_id": run_id, "kind": "acceptance"}, roots, zone, now)


def elapsed(after, before):
    return (after - before).total_seconds()


def day_record(row):
    if row["status"] != "complete":
        return row
    last_capture = max(end for _, end in row["captures"].values())
    confirmed = row["confirmed_by"]

    def bounds(reference):
        return {
            "lower_bound": elapsed(last_capture, reference),
            "upper_bound": elapsed(confirmed, reference),
        }

    return {
        **{key: row[key] for key in ("date", "run_id", "status", "kind") if key in row},
        "receipt_run_started_at_utc": row["started"].isoformat() if "started" in row else None,
        "both_destinations_confirmed_by_utc": confirmed.isoformat(),
        "last_capture_completed_at_utc": last_capture.isoformat(),
        "captures": {
            name: {
                "started_at_utc": start.isoformat(),
                "completed_at_utc": end.isoformat(),
                "from_capture_start_to_both_destinations_seconds": bounds(start),
                "from_capture_completion_to_both_destinations_seconds": bounds(end),
            }
            for name, (start, end) in row["captures"].items()
        },
    }


def component_summary(name, good, now):
    gaps = []
    for previous, current in zip(good, good[1:], strict=False):
        days = (date.fromisoformat(current["date"]) - date.fromisoformat(previous["date"])).days
        before, before_end = previous["captures"][name]
        after, after_end = current["captures"][name]
        gaps.append(
            {
                "from": previous["date"],
                "to": current["date"],
                "calendar_days": days,
                "capture_start_elapsed_seconds": elapsed(after, before),
                "capture_completion_elapsed_seconds": elapsed(after_end, before_end),
            }
        )
    observed = [gap["capture_start_elapsed_seconds"] for gap in gaps]
    adjacent = [gap["capture_start_elapsed_seconds"] for gap in gaps if gap["calendar_days"] == 1]
    latest = good[-1]["captures"][name] if good else None
    return {
        "gaps": gaps,
        "largest_adjacent_capture_start_elapsed_seconds": max(adjacent, default=None),
        "largest_observed_capture_start_elapsed_seconds": max(observed, default=None),
        "latest_capture_start_age_seconds": elapsed(now, latest[0]) if latest else None,
        "latest_capture_completion_age_seconds": elapsed(now, latest[1]) if latest else None,
    }


def coverage_summary(rows, now):
    complete = [row for row in rows if row["status"] == "complete"]
    excluded = {
        f"{status}_days": [row["date"] for row in rows if row["status"] == status]
        for status in ("missing", "pending", "failed", "incomplete", "unverified")
    }
    return {
        "complete_days": len(complete),
        **excluded,
        "latest_complete_date_in_range": complete[-1]["date"] if complete else None,
        "components": {name: component_summary(name, complete, now) for name in COMPONENTS},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", required=True)
    parser.add_argument("--to", dest="last", required=True)
    parser.add_argument("--profile", type=Path, default=daily.DEFAULT_CONFIG / "profile.json")
    parser.add_argument("--profile-pin", type=Path, default=daily.DEFAULT_CONFIG / "profile.sha256")
    parser.add_argument(
        "--include-acceptance",
        metavar="RUN_ID",
        help="audita un ensayo fuera de la cobertura diaria",
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
        dates = calendar_dates(args.first, args.last)
        core.require(dates[-1] <= now.astimezone(zone).date(), "FECHA_FUTURA")
        profile, digest = daily.load_profile(args.profile, args.profile_pin)
        daily.fields(profile, daily.PROFILE_FIELDS, "PERFIL_CAMPOS")
        daily.fields(profile["roots"], daily.ROOT_NAMES, "PERFIL_RAICES")
        core.require(type(profile["schema"]) is int and profile["schema"] == 1, "PERFIL_VERSION")
        roots = {name: Path(profile["roots"][name]) for name in ROOTS}
        for root in roots.values():
            core.private_directory(root)
        rows = [observe_day(day, roots, zone, now) for day in dates]
        result = {
            "success": True,
            "schema": 1,
            "timezone": zone.key,
            "from": args.first,
            "to": args.last,
            "observed_at_utc": now.isoformat(),
            "profile_sha256": digest,
            "scope": "local_receipts_only",
            "scheduled_local_time": DAILY_TIME.isoformat(timespec="minutes"),
            "cron_trigger_verified": False,
            "summary": coverage_summary(rows, now),
            "days": [day_record(row) for row in rows],
        }
        if args.include_acceptance:
            result["acceptance"] = day_record(
                observe_acceptance(args.include_acceptance, roots, zone, now)
            )
    except ERRORS as error:
        result = {"success": False, "error": core.output_code(error)}
    print(core.encode(result).decode(), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
