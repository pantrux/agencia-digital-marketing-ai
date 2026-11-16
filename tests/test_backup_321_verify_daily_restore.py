import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import pytest

from sre import backup_321_verify_daily_restore as verification

NOW = datetime(2026, 11, 15, 7, tzinfo=UTC)
RUN_ID = "daily-2026-11-15"
STAGING = PurePosixPath("/var/lib/media-platform/backup-321-staging-v1")
FILES = {name: name.encode() for name in verification.core.FILES}
MANIFEST = "".join(
    f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in FILES.items()
).encode()
MANIFEST_SHA = hashlib.sha256(MANIFEST).hexdigest()
RUN = {
    "run_id": RUN_ID,
    "started_at_utc": (NOW - timedelta(minutes=1)).isoformat(),
    "completed_at_utc": NOW.isoformat(),
    "stages": {"assemble": {"manifest_sha256": MANIFEST_SHA}},
}


@pytest.mark.parametrize("name", ["nas", "r2"])
def test_verifica_todos_los_archivos_en_el_snapshot_acreditado(monkeypatch, name):
    repo, snapshot = "a" * 64, "b" * 64
    recorded = {
        "id": snapshot,
        "tags": [verification.retention.TAG, "run:" + RUN_ID],
        "hostname": verification.retention.HOST,
        "paths": [str(STAGING / RUN_ID / "staging" / file) for file in (*FILES, "SHA256SUMS")],
        "summary": {"total_files_processed": 5},
        "time": (NOW - timedelta(seconds=30)).isoformat(),
    }
    env = {"repositorio": name}
    requests = []

    def restic(received_env, *args):
        assert received_env == env
        requests.append(args)
        return {"id": repo, "version": 2} if args == ("cat", "config") else [recorded]

    def dump(received_env, received_snapshot, path, deadline):
        assert received_env == env and received_snapshot == snapshot
        assert path.parent == PurePosixPath("/") and deadline > 0
        requests.append(path.name)
        yield {**FILES, "SHA256SUMS": MANIFEST}[path.name]

    monkeypatch.setattr(verification.retention, "run_restic", restic)
    monkeypatch.setattr(verification.restore, "stream_dump", dump)
    result = verification.verify_destination(env, repo, snapshot, RUN, STAGING, NOW)
    assert result["snapshot_id"] == snapshot and result["verified_payloads"] == list(FILES)
    assert requests == [("cat", "config"), ("snapshots",), "SHA256SUMS", *FILES, ("snapshots",)]


def test_rechaza_repositorio_distinto_sin_leer_el_snapshot(monkeypatch):
    def wrong_repo(env, *args):
        assert args == ("cat", "config")
        return {"id": "c" * 64, "version": 2}

    monkeypatch.setattr(verification.retention, "run_restic", wrong_repo)
    with pytest.raises(ValueError, match="^REPO_ID$"):
        verification.verify_destination({}, "a" * 64, "b" * 64, RUN, STAGING, NOW)
