import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sre import backup_321_audit_daily_coverage as coverage
from sre import backup_321_daily as daily


def test_run_id_uses_santiago_local_date(monkeypatch):
    def zone(name):
        assert name == "America/Santiago"
        return timezone(timedelta(hours=-3))

    monkeypatch.setattr(daily, "ZoneInfo", zone)
    assert daily.daily_id(datetime(2026, 11, 14, 0, 59, tzinfo=UTC)) == "daily-2026-11-13"
    assert daily.daily_id(datetime(2026, 11, 14, 3, 0, tzinfo=UTC)) == "daily-2026-11-14"


@pytest.mark.parametrize(
    "hour,minute,origin", [(6, 29, None), (6, 30, "ventana-diaria"), (6, 35, "recuperacion")]
)
def test_scheduled_origin_follows_santiago(monkeypatch, hour, minute, origin):
    monkeypatch.setattr(daily, "ZoneInfo", lambda _: timezone(timedelta(hours=-3)))
    assert daily.scheduled_origin(datetime(2026, 11, 13, hour, minute, tzinfo=UTC)) == origin


@pytest.mark.parametrize("broken,scheduled", [(False, False), (True, False), (False, True)])
def test_daily_chain_uses_one_id_and_never_repeats(tmp_path, monkeypatch, broken, scheduled):
    roots = {name: tmp_path / name for name in daily.ROOT_NAMES}
    for root in roots.values():
        root.mkdir()
    profile = {
        "roots": {name: str(path) for name, path in roots.items()},
        "sources": {"postgres-apps": {}, "postgres-immich": {}},
        "d1": {"account_id": "a" * 32},
        "restic": "/opt/restic",
        "restic_sha256": "e" * 64,
        "targets": {},
        "timeout_seconds": 30,
    }
    day = "daily-2026-11-13"
    calls = []
    now = datetime(2026, 11, 13, 14, tzinfo=UTC)
    monkeypatch.setattr(daily, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(daily, "ZoneInfo", lambda _: timezone(timedelta(hours=-3)))
    monkeypatch.setattr(daily, "daily_lock", lambda _: nullcontext())
    monkeypatch.setattr(daily, "sync_directory", lambda _: None)
    monkeypatch.setattr(daily.core, "sync_directory", lambda _: None)
    monkeypatch.setattr(
        daily,
        "verify_saved_result",
        lambda path, value: hashlib.sha256(json.dumps(value).encode()).hexdigest(),
    )
    monkeypatch.setattr(daily, "verify_openclaw", lambda path, row, started: row)

    def invoke(name, payload):
        calls.append((name, payload))
        if name != "plan":
            assert payload["run_id"] == day
        row = {"success": True, "run_id": day}
        if name in ("postgres", "d1"):
            return {**row, "run_directory": str(roots[name] / day), "step": "captured"}
        if name == "openclaw":
            if broken:
                raise ValueError("OPENCLAW_FALTANTE")
            return {"receipt": str(roots[name] / day / "capture.json"), "sha256": "1" * 64}
        if name == "assemble":
            return {
                **row,
                "step": "staged",
                "staging": str(roots["staging"] / day / "staging"),
                "manifest_sha256": "2" * 64,
            }
        if name == "plan":
            return {**row, "manifest_sha256": "2" * 64}
        assert name == "run"
        return {
            **row,
            "manifest_sha256": "2" * 64,
            "backup_set_complete": True,
            "restoration_verified": False,
            "destinations": {
                name: {"success": True, "snapshot_id": "3" * 64, "receipt_sha256": "4" * 64}
                for name in ("nas", "r2")
            },
        }

    out = daily.run_daily(profile, day, invoke, scheduled=scheduled)
    assert out["success"] is not broken
    assert out["backup_set_complete"] is not broken
    assert out.get("order_origin") == ("recuperacion" if scheduled else None)
    stages = [name for name, _ in calls]
    assert stages == (
        ["postgres", "d1", "openclaw"]
        if broken
        else ["postgres", "d1", "openclaw", "assemble", "plan", "run"]
    )
    assert (roots["daily"] / day / "result.json").is_file()
    with pytest.raises(ValueError, match="CORRIDA_DIARIA_EXISTENTE"):
        daily.run_daily(profile, day, invoke)
    assert stages == [name for name, _ in calls]


@pytest.mark.parametrize("status", ["complete", "incomplete", "failed", "unverified"])
def test_scheduled_reuses_only_verified_complete_day(tmp_path, monkeypatch, status):
    roots = {name: tmp_path / name for name in daily.ROOT_NAMES}
    run_id = "daily-2026-11-13"
    now = datetime(2026, 11, 13, 14, tzinfo=UTC)
    (roots["daily"] / run_id).mkdir(parents=True)
    monkeypatch.setattr(daily, "datetime", SimpleNamespace(now=lambda _: now))
    monkeypatch.setattr(daily, "ZoneInfo", lambda _: timezone(timedelta(hours=-3)))
    monkeypatch.setattr(daily, "daily_lock", lambda _: nullcontext())
    monkeypatch.setattr(daily, "trusted_install_dir", lambda _: None)
    monkeypatch.setattr(
        daily, "preflight", lambda *_: pytest.fail("Una consulta de recibos es local")
    )
    checked = []
    monkeypatch.setattr(daily, "verified_pin", lambda *args: checked.append(args))

    def observe_day(day, current_roots, zone, observed):
        assert str(day) == "2026-11-13" and current_roots == roots and observed == now
        assert checked == [(daily.RUNTIME / daily.COVERAGE_SCRIPT, daily.COVERAGE_SHA256)]
        return {"status": status, "run_id": run_id}

    monkeypatch.setattr(coverage, "observe_day", observe_day)
    if status == "complete":
        result = daily.run_daily({"roots": roots}, run_id, scheduled=True)
        assert result["action"] == "ya-confirmado" and result["backup_set_complete"]
    else:
        with pytest.raises(ValueError, match="CORRIDA_DIARIA_POR_RECONCILIAR"):
            daily.run_daily({"roots": roots}, run_id, scheduled=True)


def test_coverage_verifier_hash_matches_repository():
    source = (daily.RUNTIME / daily.COVERAGE_SCRIPT).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(source).hexdigest() == daily.COVERAGE_SHA256


@pytest.mark.parametrize("failure,waits", [("CORRIDA_EN_CURSO", [15, 45]), ("DESTINO_FALLO", [])])
def test_only_contention_before_destination_is_retried(monkeypatch, failure, waits):
    calls = []
    sleeps = []
    request = {"run_id": "daily-2026-11-13"}

    def invoke(name, payload):
        assert name == "run" and payload is request
        calls.append(name)
        if len(calls) < 3:
            raise ValueError(failure)
        return {"success": True}

    monkeypatch.setattr(daily.time, "sleep", sleeps.append)
    if waits:
        assert daily.deliver_with_lock_retry(invoke, request) == {"success": True}
    else:
        with pytest.raises(ValueError, match=failure):
            daily.deliver_with_lock_retry(invoke, request)
    assert sleeps == waits and len(calls) == len(waits) + 1


def test_openclaw_wrong_path_stops_before_reading(monkeypatch):
    monkeypatch.setattr(
        daily, "read_json", lambda *_: pytest.fail("receipt read before scope check")
    )
    receipt = {
        "receipt": "/tmp/result.json",
        "sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "hostname": "openclaw-ia",
        "started_at_utc": "2026-11-13T06:30:02Z",
        "completed_at_utc": "2026-11-13T06:30:05Z",
    }
    with pytest.raises(ValueError, match="OPENCLAW_RECIBO_RUTA"):
        daily.verify_openclaw(
            Path("/safe/capture.json"), receipt, datetime(2026, 11, 13, 6, 30, tzinfo=UTC)
        )


@pytest.mark.parametrize(("complete", "retention_ok"), [(False, True), (True, False), (True, True)])
def test_post_copy_retention_keeps_backup_receipt_independent(
    monkeypatch, capsys, complete, retention_ok
):
    run_id = "daily-2026-11-13"
    saved = {"run_id": run_id, "success": complete, "backup_set_complete": complete}
    calls = []
    monkeypatch.setattr(daily.sys, "argv", ["backup_321_daily.py", "--retention", "apply"])
    monkeypatch.setattr(
        daily.os,
        "uname",
        lambda: SimpleNamespace(sysname="Linux", nodename="vm-media-01"),
        raising=False,
    )
    monkeypatch.setattr(daily.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(daily.os, "getegid", lambda: 0, raising=False)
    monkeypatch.setattr(daily.os, "umask", lambda _: None)
    monkeypatch.setattr(daily.signal, "signal", lambda *_: None)
    monkeypatch.setattr(daily.signal, "SIGHUP", 1, raising=False)
    monkeypatch.setattr(daily, "load_profile", lambda *_: ({}, "0" * 64))
    monkeypatch.setattr(daily, "preflight", lambda _: (None, {}))
    monkeypatch.setattr(daily, "daily_id", lambda _: run_id)
    monkeypatch.setattr(daily, "run_daily", lambda *_, scheduled: saved.copy())

    def run_retention(run_id, mode, profile, pin):
        calls.append((run_id, mode))
        assert profile == daily.DEFAULT_CONFIG / "profile.json"
        assert pin == daily.DEFAULT_CONFIG / "profile.sha256"
        return {"success": retention_ok}

    monkeypatch.setattr(daily, "run_retention", run_retention)
    assert daily.main() == (0 if complete and retention_ok else 1)
    assert calls == ([(run_id, "apply")] if complete else [])
    output = json.loads(capsys.readouterr().out)
    assert output["success"] is complete
    assert output["backup_set_complete"] is complete
    assert output.get("retention") == ({"success": retention_ok} if complete else None)
    assert "retention" not in saved
