import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import pytest

from sre import backup_321_verify_r2_restore as restore

NOW = datetime(2026, 11, 13, 18, tzinfo=UTC)
SNAPSHOT = "a" * 64
NAS = "b" * 64
JOURNAL_SHA = "c" * 64
RUN = "daily-acceptance-20261113-v2"
STAGING = PurePosixPath("/var/lib/media-platform/backup-321-staging-v1")
PAYLOADS = {
    name: f"test:{name}".encode()
    for name in (
        "postgres-apps.sql.zst",
        "postgres-immich.sql.zst",
        "d1.sql.zst",
        "openclaw.tar.zst",
    )
}
MANIFEST = "".join(
    f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in PAYLOADS.items()
).encode()
MANIFEST_SHA = hashlib.sha256(MANIFEST).hexdigest()


def test_streams_every_file_and_builds_exact_retention_receipt():
    calls = []

    def stream(path):
        calls.append(path)
        data = {"SHA256SUMS": MANIFEST, **PAYLOADS}[path.name]
        for start in range(0, len(data), 7):
            yield data[start : start + 7]

    stage = STAGING / RUN / "staging"
    verified = restore.verify_payloads(stream, stage, MANIFEST_SHA)
    receipt = restore.restore_receipt(SNAPSHOT, MANIFEST_SHA, verified, NOW)
    assert calls == [stage / name for name in ("SHA256SUMS", *PAYLOADS)]
    assert receipt["snapshot_id"] == SNAPSHOT
    assert receipt["manifest_sha256"] == MANIFEST_SHA
    assert receipt["verified_payloads"] == list(PAYLOADS)
    assert receipt.keys() == restore.retention.RESTORE_FIELDS


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("manifest_digest", "SHA_MANIFIESTO"),
        ("duplicate", "MANIFIESTO_NO_EXACTO"),
        ("payload", "SHA_RESTORE"),
        ("empty", "SHA_RESTORE"),
        ("limit", "RESTORE_COTA"),
    ],
)
def test_fails_on_corruption_or_aggregate_size(failure, code, monkeypatch):
    payloads = dict(PAYLOADS)
    manifest = MANIFEST
    expected = MANIFEST_SHA
    if failure == "manifest_digest":
        expected = "0" * 64
    elif failure == "duplicate":
        manifest += MANIFEST.splitlines(keepends=True)[0]
        expected = hashlib.sha256(manifest).hexdigest()
    elif failure == "payload":
        payloads["d1.sql.zst"] += b"!"
    elif failure == "empty":
        payloads["d1.sql.zst"] = b""
    elif failure == "limit":
        monkeypatch.setattr(restore.core, "MAX_STAGING_BYTES", sum(map(len, payloads.values())) - 1)

    def stream(path):
        yield {"SHA256SUMS": manifest, **payloads}[path.name]

    with pytest.raises(ValueError, match=f"^{code}$"):
        restore.verify_payloads(stream, STAGING / RUN / "staging", expected)


def observations():
    stage = STAGING / RUN / "staging"
    snapshot = {
        "id": SNAPSHOT,
        "hostname": "vm-media-01",
        "tags": ["agency-backup-321", f"run:{RUN}"],
        "paths": [str(stage / name) for name in (*PAYLOADS, "SHA256SUMS")],
        "summary": {"total_files_processed": 5},
        "time": (NOW - timedelta(minutes=2)).isoformat(),
    }
    daily = {
        "run_id": RUN,
        "success": True,
        "backup_set_complete": True,
        "started_at_utc": (NOW - timedelta(minutes=3)).isoformat(),
        "completed_at_utc": (NOW - timedelta(minutes=1)).isoformat(),
        "stages": {
            "assemble": {"manifest_sha256": MANIFEST_SHA},
            "plan": {"manifest_sha256": MANIFEST_SHA},
            "run": {"snapshots": {"nas": NAS, "r2": SNAPSHOT}, "sha256": JOURNAL_SHA},
        },
    }
    journal = {
        "run_id": RUN,
        "success": True,
        "backup_set_complete": True,
        "manifest_sha256": MANIFEST_SHA,
        "destinations": {
            name: {
                "snapshot_id": snapshot_id,
                "success": True,
                "reconciliation_required": False,
                "receipt_sha256": "d" * 64,
            }
            for name, snapshot_id in (("nas", NAS), ("r2", SNAPSHOT))
        },
    }
    return snapshot, daily, journal


def test_uses_newest_retained_daily_snapshot_and_paired_receipt():
    snapshot, daily, journal = observations()
    historical = {**snapshot, "tags": ["agency-backup-321", "run:fase4-produccion-20261113-v1"]}
    plan = {"keep_ids": ["e" * 64, SNAPSHOT]}
    before = {"e" * 64: historical, SNAPSHOT: snapshot}
    run_id, selected = restore.select_daily_snapshot(SNAPSHOT, plan, before, STAGING, NOW)
    assert run_id == RUN and selected == snapshot
    assert restore.paired_manifest(RUN, selected, daily, journal, JOURNAL_SHA, NOW) == MANIFEST_SHA


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("requested_id", "RESTORE_SNAPSHOT_DIARIO"),
        ("daily_fail", "RECIBO_DIARIO_ESTADO"),
        ("different_manifest", "RECIBO_DIARIO_MANIFIESTO"),
        ("different_journal", "RECIBO_DIARIO_JOURNAL"),
        ("nas_failed", "RECIBO_DIARIO_DESTINOS"),
        ("wrong_snapshot", "RECIBO_DIARIO_DESTINOS"),
        ("bad_time", "RECIBO_DIARIO_FECHA"),
    ],
)
def test_rejects_uncorroborated_snapshot_and_daily_result(failure, code):
    snapshot, daily, journal = observations()
    requested, journal_sha = SNAPSHOT, JOURNAL_SHA
    if failure == "requested_id":
        requested = "e" * 64
    elif failure == "daily_fail":
        daily["success"] = False
    elif failure == "different_manifest":
        daily["stages"]["assemble"]["manifest_sha256"] = "0" * 64
    elif failure == "different_journal":
        journal_sha = "0" * 64
    elif failure == "nas_failed":
        journal["destinations"]["nas"]["success"] = False
    elif failure == "wrong_snapshot":
        journal["destinations"]["r2"]["snapshot_id"] = NAS
    elif failure == "bad_time":
        daily["completed_at_utc"] = (NOW - timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match=f"^{code}$"):
        run, selected = restore.select_daily_snapshot(
            requested, {"keep_ids": [SNAPSHOT]}, {SNAPSHOT: snapshot}, STAGING, NOW
        )
        restore.paired_manifest(run, selected, daily, journal, journal_sha, NOW)


def test_publishes_receipt_exactly_once_and_preserves_existing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(restore.core, "sync_directory", lambda path: path == tmp_path)
    receipt_path = tmp_path / "restore.json"
    receipt = restore.restore_receipt(SNAPSHOT, MANIFEST_SHA, list(PAYLOADS), NOW)

    digest = restore.publish_receipt(receipt_path, receipt)

    assert digest == hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        restore.publish_receipt(receipt_path, {**receipt, "snapshot_id": NAS})
    assert hashlib.sha256(receipt_path.read_bytes()).hexdigest() == digest
    assert list(tmp_path.iterdir()) == [receipt_path]
    snapshot, _, _ = observations()
    assert (
        restore.retention.verified_restore(receipt, [SNAPSHOT], {SNAPSHOT: snapshot}, NOW)
        == SNAPSHOT
    )
