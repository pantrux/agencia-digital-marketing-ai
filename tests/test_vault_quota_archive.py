"""Pruebas de archivo y reanudación con candidatas sintéticas privadas."""

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "vault_quota_archive_test", ROOT / "sre/archive_vault_changes_v2.py"
)
ARCHIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVER)
OPERATION = "f" * 32
OWNER = "synthetic-owner"
REPO = "a" * 64


def encode(value):
    return json.dumps(value, sort_keys=True).encode()


class Store:
    def __init__(self, root):
        self.root = root

    def mkdir(self, relative):
        (self.root / relative).mkdir()

    def read(self, relative):
        return (self.root / relative).read_bytes()

    def json(self, relative):
        return json.loads(self.read(relative))

    def write(self, relative, raw):
        with (self.root / relative).open("xb") as stream:
            stream.write(raw)

    @contextmanager
    def lock(self, operation):
        self.write(".active.json", encode({"owner": OWNER, "operation": operation}))
        try:
            yield
        finally:
            (self.root / ".active.json").unlink()


def read_regular(path, access, maximum):
    access.check(path)
    raw = path.read_bytes()
    assert len(raw) <= maximum
    info = path.stat()
    return raw, {
        "sha256": ARCHIVER.sha(raw),
        "device": info.st_dev,
        "inode": info.st_ino,
        "bytes": len(raw),
        "mtime_ns": info.st_mtime_ns,
    }


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.store.mkdir("captures")
        self.store.mkdir("operations")
        self.addCleanup(patch.stopall)
        patch.object(ARCHIVER, "safe_stop_check", side_effect=ARCHIVER.check_watcher_locks).start()
        provider = SimpleNamespace(
            parse_config=lambda raw, repo: self.assertEqual(json.loads(raw)["id"], repo),
            parse_backup=lambda raw: json.loads(raw)["snapshot_id"],
            parse_listing=self.parse_listing,
            events=lambda raw: [json.loads(raw)],
        )
        self.runtime = SimpleNamespace(
            store=self.store,
            state=SimpleNamespace(
                decode=json.loads,
                encoded=encode,
                read_regular=read_regular,
                ancestors=lambda path: self.assertFalse(path.is_symlink()),
                node=lambda path, directory: path.stat(),
            ),
            access=SimpleNamespace(check=lambda path: self.assertFalse(path.is_symlink())),
            config={
                "owner": OWNER,
                "repository_id": REPO,
                "maximum_vault_bytes": 8 * 1024**2,
                "maximum_queue_bytes": 48,
                "maximum_generations": 500,
            },
            profile={"hostname": "fixture"},
            queue=SimpleNamespace(generations=self.generations, uploader_lock=self.uploader_lock),
            backup_helpers=lambda: provider,
        )
        self.old = self.capture(1)
        self.pending = self.capture(2, ack=False)
        self.newer = self.capture(3)
        self.latest = self.capture(4)
        self.prefix = ARCHIVER.ARCHIVE + "/" + OPERATION

    @contextmanager
    def uploader_lock(self):
        relative = "operations/uploader.lock.json"
        self.store.write(relative, encode({"owner": OWNER, "nonce": "temporary"}))
        try:
            yield
        finally:
            (self.root / relative).unlink()

    def live(self):
        return ARCHIVER.maintain_once(self.runtime)

    def test_live_archive_releases_locks_and_preserves_latest_and_pending(self):
        result = self.live()
        self.assertEqual(result["newly_moved"], 1)
        self.assertFalse((self.root / "captures" / self.old).exists())
        self.assertTrue((self.root / "captures" / self.latest).is_dir())
        self.assertTrue((self.root / "captures" / self.pending).is_dir())
        self.assertFalse((self.root / ".active.json").exists())
        self.assertFalse((self.root / "operations/uploader.lock.json").exists())
        self.assertEqual(self.live()["state"], "dentro-de-cuota")

    def test_live_archive_skips_a_busy_writer_without_moving(self):
        with self.store.lock("observador"):
            self.assertEqual(self.live()["state"], "ocupada")
        self.assertTrue((self.root / "captures" / self.old).is_dir())
        self.assertFalse((self.root / "operations/uploader.lock.json").exists())

    def test_live_batch_retries_after_the_writer_releases_its_lock(self):
        busy = self.store.lock("observador")
        busy.__enter__()

        def release_writer(_seconds):
            self.assertFalse((self.root / "operations/uploader.lock.json").exists())
            busy.__exit__(None, None, None)

        try:
            with patch("time.sleep", side_effect=release_writer):
                result = ARCHIVER.maintain_batch(self.runtime, 1)
            self.assertEqual((result["state"], result["newly_moved"]), ("archivada", 1))
            self.assertFalse((self.root / "captures" / self.old).exists())
        finally:
            if (self.root / ".active.json").exists():
                busy.__exit__(None, None, None)

    def test_live_batch_leaves_a_writer_alone_when_the_wait_expires(self):
        with self.store.lock("observador"), patch("time.monotonic", side_effect=(0, 100)):
            result = ARCHIVER.maintain_batch(self.runtime, 1)
            self.assertEqual((result["state"], result["newly_moved"]), ("ocupada", 0))
            self.assertTrue((self.root / ".active.json").exists())
        self.assertTrue((self.root / "captures" / self.old).is_dir())

    def test_live_archive_resumes_same_plan_after_failed_receipt(self):
        write = self.store.write

        def interrupt(path, raw):
            if ".done.json.pending-" in path:
                raise OSError("synthetic failure")
            write(path, raw)

        with patch.object(self.store, "write", side_effect=interrupt), self.assertRaises(OSError):
            self.live()
        self.assertFalse((self.root / "captures" / self.old).exists())
        self.assertFalse((self.root / ".active.json").exists())
        self.assertTrue(self.live()["reanudada"])
        self.assertTrue((self.root / "captures" / self.newer).is_dir())

    def parse_listing(self, raw, snapshot, capture, size):
        listing = json.loads(raw)
        self.assertEqual(
            (listing["id"], listing["path"], listing["size"]), (snapshot, capture, size)
        )

    def generations(self):
        return sorted(
            (
                json.loads((p / "prepared.json").read_bytes())
                for p in (self.root / "captures").iterdir()
            ),
            key=lambda row: row["prepared_ns"],
        )

    def capture(self, number, *, ack=True):
        generation = f"{number:032x}"
        capture = "captures/" + generation
        self.store.mkdir(capture)
        raw = f"SYNTHETIC={number}\n".encode()
        checksum = hashlib.sha256(raw).hexdigest()
        row = {
            "generation_id": generation,
            "sha256": checksum,
            "bytes": len(raw),
            "prepared_ns": number,
            "source_kind": "OBSERVED",
            "status": "OBSERVED",
        }
        self.store.write(capture + "/candidate.env", raw)
        self.store.write(capture + "/prepared.json", encode(row))
        if not ack:
            return generation
        snapshot, nonce = f"{number:064x}", "b" * 32
        observed = {
            "generation_id": generation,
            "sha256": checksum,
            "snapshot_id": snapshot,
            "publication_status": "OBSERVED",
        }
        result = {**observed, "success": True}
        attempt = {
            "owner": OWNER,
            "generation_id": generation,
            "sha256": checksum,
            "child_nonce": nonce,
            "subject": {
                "repository_id": REPO,
                "sha256": checksum,
                "publication_status": "OBSERVED",
            },
        }
        for name, value in (
            ("uploaded", {**result, "uploaded_ns": number, "publication_proved": False}),
            ("result", result),
            ("snapshot-recorded", observed),
            ("attempt", attempt),
            ("claim", {"generation_id": generation, "child_nonce": nonce}),
        ):
            self.store.write(capture + "/" + name + ".json", encode(value))
        for stage, value in (
            ("outer", result),
            ("cat", {"id": REPO}),
            ("backup", {"snapshot_id": snapshot}),
            (
                "ls",
                {
                    "id": snapshot,
                    "hostname": "fixture",
                    "tags": [generation, "OBSERVED", "vault-changes-v1"],
                    "path": str(self.root / capture / "candidate.env"),
                    "size": len(raw),
                },
            ),
        ):
            prefix = "operations/" + generation + "-" + stage
            self.store.write(prefix + ".stdout", encode(value))
            self.store.write(prefix + ".stderr", b"")
            self.store.write(
                prefix + ".job.json",
                encode({"status": "completed", "exit_code": 0, "cleanup_complete": True}),
            )
        return generation

    def plan(self):
        result = ARCHIVER.make_plan(self.runtime, OPERATION, 32)
        self.assertEqual(result["generations"], 2)
        self.assertEqual(
            {"operation", "plan_sha256", "generations", "candidate_bytes"}, set(result)
        )
        return result["plan_sha256"]

    def apply(self, pin):
        return ARCHIVER.apply_plan(self.runtime, OPERATION, pin)

    def test_resume_after_move_before_done_and_replay(self):
        pin = self.plan()
        original = self.store.read("captures/" + self.old + "/candidate.env")
        receipt = self.prefix + "/" + self.old + ".done.json"
        write = self.store.write

        def interrupt(path, raw):
            if path.startswith(receipt + ".pending-"):
                raise OSError("synthetic power loss")
            write(path, raw)

        with patch.object(self.store, "write", side_effect=interrupt), self.assertRaises(OSError):
            self.apply(pin)
        self.assertFalse((self.root / "captures" / self.old).exists())
        self.assertFalse((self.root / receipt).exists())
        self.assertEqual(self.apply(pin)["newly_moved"], 1)
        final_receipt = self.store.read(receipt)
        self.assertEqual(self.apply(pin)["newly_moved"], 0)
        self.assertEqual(self.store.read(receipt), final_receipt)
        self.assertEqual(self.store.read(self.prefix + "/" + self.old + "/candidate.env"), original)
        self.assertEqual(
            [row["generation_id"] for row in self.generations()], [self.pending, self.latest]
        )

    def test_wrong_pin_or_watcher_lock_prevents_move(self):
        pin = self.plan()
        with self.assertRaisesRegex(ARCHIVER.Rejected, "PLAN_SHA"):
            self.apply("0" * 64)
        self.store.write(".active.json", b"fixture lock")
        with self.assertRaisesRegex(ARCHIVER.Rejected, "WATCHER_LOCK_PRESENT"):
            self.apply(pin)
        self.assertTrue((self.root / "captures" / self.old).exists())

    def test_unpublished_receipt_does_not_block_resume(self):
        pin = self.plan()
        receipt = self.prefix + "/" + self.old + ".done.json"
        move = os.rename

        def interrupt(source, destination):
            if destination == self.root / receipt:
                raise OSError("synthetic power loss")
            return move(source, destination)

        with patch.object(ARCHIVER.os, "rename", side_effect=interrupt), self.assertRaises(OSError):
            self.apply(pin)
        self.assertFalse((self.root / receipt).exists())
        self.assertEqual(len(list((self.root / self.prefix).glob("*.pending-*"))), 1)
        self.assertEqual(self.apply(pin)["newly_moved"], 1)
        self.assertTrue((self.root / receipt).is_file())

    def test_changed_ack_in_last_entry_stops_before_first_move(self):
        pin = self.plan()
        (self.root / "captures" / self.newer / "uploaded.json").write_bytes(
            encode({"success": False})
        )
        with self.assertRaisesRegex(ARCHIVER.Rejected, "SNAPSHOT_RECEIPT"):
            self.apply(pin)
        self.assertTrue((self.root / "captures" / self.old).exists())

    def test_both_source_and_target_are_rejected(self):
        pin = self.plan()
        self.store.mkdir(self.prefix + "/" + self.old)
        with self.assertRaisesRegex(ARCHIVER.Rejected, "ARCHIVE_LOCATION_CONFLICT"):
            self.apply(pin)

    def test_changed_target_after_archive_rejects_replay(self):
        pin = self.plan()
        self.apply(pin)
        path = self.root / self.prefix / self.old / "candidate.env"
        path.write_bytes(b"SYNTHETIC=8\n")
        with self.assertRaisesRegex(ARCHIVER.Rejected, "CANDIDATE_SHA_OR_SIZE"):
            self.apply(pin)

    def test_plan_preserves_ack_from_different_verification_path(self):
        failed = self.root / "captures" / self.old / "result.json"
        failed.write_bytes(encode({"success": False, "requires_manual_reconciliation": True}))
        result = ARCHIVER.make_plan(self.runtime, OPERATION, 32)
        self.assertEqual(result["generations"], 1)
        self.apply(result["plan_sha256"])
        self.assertEqual(
            [row["generation_id"] for row in self.generations()],
            [self.old, self.pending, self.latest],
        )

    @unittest.skipUnless(os.name == "nt", "Bloqueo de handle Windows")
    def test_windows_handle_lock_releases_on_exit(self):
        with ARCHIVER.archive_lock(self.runtime):
            with self.assertRaises(OSError), ARCHIVER.archive_lock(self.runtime):
                self.fail("the same lock was acquired twice")
        with ARCHIVER.archive_lock(self.runtime):
            self.assertTrue((self.root / ARCHIVER.ARCHIVE_LOCK).exists())
