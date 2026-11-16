import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from sre import backup_321_audit_daily_coverage as audit

AS_OF = datetime(2026, 11, 17, 10, tzinfo=UTC)
MANIFEST = "a" * 64
JOURNAL_SHA = "b" * 64
NAS, R2 = "c" * 64, "d" * 64


def fixture(day, start, finish, *, durable=15):
    captures = {
        name: (start + timedelta(seconds=index), finish + timedelta(seconds=index))
        for index, name in enumerate(audit.COMPONENTS)
    }
    return {
        "date": day.isoformat(),
        "status": "complete",
        "captures": captures,
        "confirmed_by": finish + timedelta(seconds=durable),
    }


def test_elapsed_24h_calendar_pairs_and_gap_across_missing_days():
    first = date(2026, 11, 13)
    begin = datetime(2026, 11, 13, 6, 30, tzinfo=UTC)
    results = [
        fixture(first, begin, begin + timedelta(seconds=10)),
        fixture(
            first + timedelta(days=1),
            begin + timedelta(hours=24, seconds=13),
            begin + timedelta(hours=24, seconds=24),
        ),
        {"date": "2026-11-15", "status": "missing"},
        fixture(
            first + timedelta(days=3),
            begin + timedelta(hours=72),
            begin + timedelta(hours=72, seconds=10),
        ),
        {"date": "2026-11-17", "status": "failed"},
    ]
    coverage = audit.coverage_summary(results, AS_OF)
    assert coverage["complete_days"] == 3
    assert coverage["missing_days"] == ["2026-11-15"]
    assert coverage["failed_days"] == ["2026-11-17"]
    sample = coverage["components"]["postgres-apps"]
    adjacent, separated = sample["gaps"]
    assert adjacent["calendar_days"] == 1 and adjacent["capture_start_elapsed_seconds"] == 86413
    assert separated["calendar_days"] == 2 and separated["capture_start_elapsed_seconds"] == 172787
    assert sample["largest_adjacent_capture_start_elapsed_seconds"] == 86413
    assert sample["largest_observed_capture_start_elapsed_seconds"] == 172787
    assert sample["latest_capture_start_age_seconds"] == 99000


def test_durability_window_is_bounded_by_evidence_not_a_synthetic_commit_timestamp():
    begin = datetime(2026, 11, 13, 6, 30, tzinfo=UTC)
    row = fixture(begin.date(), begin, begin + timedelta(seconds=10))
    out = audit.day_record(row)
    assert out["both_destinations_confirmed_by_utc"] == (begin + timedelta(seconds=25)).isoformat()
    assert out["last_capture_completed_at_utc"] == (begin + timedelta(seconds=13)).isoformat()
    assert out["captures"]["postgres-apps"]["from_capture_start_to_both_destinations_seconds"] == {
        "lower_bound": 13,
        "upper_bound": 25,
    }
    assert out["captures"]["postgres-apps"][
        "from_capture_completion_to_both_destinations_seconds"
    ] == {
        "lower_bound": 3,
        "upper_bound": 15,
    }


def test_date_range_uses_exact_iso_dates_and_has_a_finite_bound():
    assert list(audit.calendar_dates("2026-11-13", "2026-11-15")) == [
        date(2026, 11, day) for day in (13, 14, 15)
    ]
    for start, end in (
        ("2026-11-14", "2026-11-13"),
        ("20261113", "2026-11-15"),
        ("2024-01-01", "2026-11-13"),
    ):
        with pytest.raises(ValueError):
            list(audit.calendar_dates(start, end))


def test_calendar_day_is_one_but_real_elapsed_may_be_25_hours():
    try:
        santiago = ZoneInfo("America/Santiago")
    except ZoneInfoNotFoundError:
        pytest.skip("tzdata no instalado")
    before = datetime(2026, 4, 4, 3, 30, tzinfo=santiago).astimezone(UTC)
    after = datetime(2026, 4, 5, 3, 30, tzinfo=santiago).astimezone(UTC)
    data = [fixture(date(2026, 4, 4), before, before), fixture(date(2026, 4, 5), after, after)]
    coverage = audit.coverage_summary(data, after + timedelta(hours=1))
    (sample,) = coverage["components"]["postgres-apps"]["gaps"]
    assert sample["calendar_days"] == 1 and sample["capture_start_elapsed_seconds"] == 90000


def test_noncomplete_days_and_unobserved_component_freshness_remain_explicit():
    results = [
        {"date": "2026-11-13", "status": "unverified"},
        {"date": "2026-11-14", "status": "incomplete"},
    ]
    out = audit.coverage_summary(results, AS_OF)
    assert out["complete_days"] == 0
    assert out["unverified_days"] == ["2026-11-13"] and out["incomplete_days"] == ["2026-11-14"]
    assert out["components"]["d1"]["latest_capture_start_age_seconds"] is None


def test_paired_local_receipts_reject_reconciled_or_different_destination():
    run_id = "daily-2026-11-13"
    row = {
        "run_id": run_id,
        "success": True,
        "backup_set_complete": True,
        "stages": {
            "assemble": {"manifest_sha256": MANIFEST},
            "plan": {"manifest_sha256": MANIFEST},
            "run": {"sha256": JOURNAL_SHA, "snapshots": {"nas": NAS, "r2": R2}},
        },
    }
    journal = {
        "run_id": run_id,
        "success": True,
        "backup_set_complete": True,
        "manifest_sha256": MANIFEST,
        "destinations": {
            name: {
                "success": True,
                "reconciliation_required": False,
                "receipt_sha256": "e" * 64,
                "snapshot_id": value,
            }
            for name, value in (("nas", NAS), ("r2", R2))
        },
    }
    audit.verify_pair(run_id, row, journal, JOURNAL_SHA)
    journal["destinations"]["r2"]["reconciliation_required"] = True
    with pytest.raises(ValueError, match="^RECIBO_DIARIO_DESTINOS$"):
        audit.verify_pair(run_id, row, journal, JOURNAL_SHA)


def test_real_daily_receipt_shapes_and_exclusion_of_acceptance_id(tmp_path, monkeypatch):
    roots = {name: tmp_path / name for name in audit.ROOTS}
    run_id = "daily-2026-11-13"
    day = date(2026, 11, 13)
    started = "2026-11-13T06:30:00+00:00"
    capture = {
        "started_at_utc": "2026-11-13T06:30:01+00:00",
        "completed_at_utc": "2026-11-13T06:30:12+00:00",
    }
    confirmed = "2026-11-13T06:30:30+00:00"
    zone = timezone(timedelta(hours=-3))

    def write(name, row, filename="result.json", run=run_id):
        path = roots[name] / run / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(row) + "\n").encode()
        path.write_bytes(raw)
        return {"receipt": str(path), "sha256": hashlib.sha256(raw).hexdigest()}

    def read(path):
        raw = path.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(audit.core, "private_directory", lambda _: None)
    monkeypatch.setattr(audit.daily, "read_json", read)
    pg = write(
        "postgres",
        {
            "success": True,
            "run_id": run_id,
            "scope": "production",
            "step": "captured",
            "captures": {name: capture for name in audit.daily.PG},
        },
    )
    d1 = write(
        "d1",
        {
            "success": True,
            "run_id": run_id,
            "step": "captured",
            "owner_verified": True,
            "sqlite_integrity_verified": True,
            **capture,
        },
    )
    oc = write(
        "openclaw",
        {
            "success": True,
            "action": "capture",
            "scope": "agency-state",
            "synthetic": False,
            "manifest_sha256": MANIFEST,
        },
        "capture.json",
    )
    journal = write(
        "journal",
        {
            "run_id": run_id,
            "success": True,
            "backup_set_complete": True,
            "manifest_sha256": MANIFEST,
            "destinations": {
                name: {
                    "success": True,
                    "reconciliation_required": False,
                    "receipt_sha256": "e" * 64,
                    "snapshot_id": snapshot,
                }
                for name, snapshot in (("nas", NAS), ("r2", R2))
            },
        },
    )
    daily_result = {
        "run_id": run_id,
        "success": True,
        "backup_set_complete": True,
        "started_at_utc": started,
        "completed_at_utc": confirmed,
        "stages": {
            "postgres": pg,
            "d1": d1,
            "openclaw": {**oc, **capture, "hostname": "openclaw-ia", "manifest_sha256": MANIFEST},
            "assemble": {"manifest_sha256": MANIFEST},
            "plan": {"manifest_sha256": MANIFEST},
            "run": {"sha256": journal["sha256"], "snapshots": {"nas": NAS, "r2": R2}},
        },
    }
    write("daily", daily_result, run="daily-acceptance-20261113-v2")
    assert audit.observe_day(day, roots, zone, AS_OF)["status"] == "missing"
    assert (
        audit.observe_day(day, roots, zone, datetime(2026, 11, 13, 5, tzinfo=UTC))["status"]
        == "pending"
    )
    write("daily", daily_result)
    found = audit.observe_day(day, roots, zone, AS_OF)
    assert found["status"] == "complete" and list(found["captures"]) == list(audit.COMPONENTS)
    (roots["d1"] / run_id / "result.json").write_bytes(b"{}\n")
    assert audit.observe_day(day, roots, zone, AS_OF)["error"] == "RECIBO_CAPTURA_SHA"


def test_exact_acceptance_lookup_does_not_contribute_to_date_continuity(tmp_path, monkeypatch):
    roots = {"daily": tmp_path, "journal": tmp_path}
    acceptance_id = "daily-acceptance-20261113-v2"
    acceptance_path = tmp_path / acceptance_id / "result.json"
    acceptance_path.parent.mkdir()
    raw = json.dumps(
        {
            "run_id": acceptance_id,
            "success": True,
            "started_at_utc": "2026-11-13T15:56:09.414127+00:00",
            "completed_at_utc": "2026-11-13T15:56:30.136923+00:00",
            "stages": {},
        }
    )
    acceptance_path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(audit.core, "private_directory", lambda _: None)
    monkeypatch.setattr(
        audit.daily, "read_json", lambda path: (json.loads(path.read_bytes()), JOURNAL_SHA)
    )
    monkeypatch.setattr(audit, "verify_pair", lambda *_: None)
    captured = datetime(2026, 11, 13, 15, 56, 17, tzinfo=UTC)
    monkeypatch.setattr(
        audit,
        "capture_windows",
        lambda *_: {name: (captured, captured) for name in audit.COMPONENTS},
    )

    accepted = audit.observe_acceptance(acceptance_id, roots, UTC, AS_OF)
    coverage = audit.coverage_summary(
        [audit.observe_day(date(2026, 11, 13), roots, UTC, AS_OF)], AS_OF
    )

    assert accepted["status"] == "complete" and accepted["run_id"] == acceptance_id
    assert accepted["date"] == "2026-11-13" and accepted["kind"] == "acceptance"
    assert coverage["complete_days"] == 0 and coverage["missing_days"] == ["2026-11-13"]
    for other in ("daily-2026-11-13", "daily-acceptance-../x", "daily-acceptance-20261113-*"):
        with pytest.raises(ValueError, match="^ACEPTACION_RUN_ID$"):
            audit.observe_acceptance(other, roots, UTC, AS_OF)
