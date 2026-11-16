import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sre import backup_321_orchestrator as backup


class BackupOrchestratorTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory).resolve()
        self.staging = self.root / "staging"
        self.journal = self.root / "journal"
        self.staging.mkdir()
        self.journal.mkdir()
        for name in backup.FILES:
            (self.staging / name).write_bytes(name.encode())
        manifest = "".join(
            f"{self.sha((self.staging / name).read_bytes())}  {name}\n" for name in backup.FILES
        ).encode()
        (self.staging / "SHA256SUMS").write_bytes(manifest)
        restic = self.root / "restic"
        restic.write_bytes(b"fixture binary")
        self.request = {
            "staging": str(self.staging),
            "manifest_sha256": self.sha(manifest),
            "run_id": "fixture",
            "journal": str(self.journal),
            "restic": str(restic),
            "restic_sha256": self.sha(restic.read_bytes()),
            "timeout_seconds": 30,
            "targets": {
                "nas": {
                    "id": "b" * 64,
                    "repository": "/mnt/producciones/_backups/fixture",
                    "password_file": str(self.root / "nas.pass"),
                },
                "r2": {
                    "id": "c" * 64,
                    "repository": f"s3:https://{'d' * 32}.r2.cloudflarestorage.com/studio-backups-bucket/fixture",
                    "password_file": str(self.root / "r2.pass"),
                },
            },
        }
        self.calls = []
        self.enterContext(patch.object(backup, "ROOT", self.root))
        self.enterContext(patch.object(backup, "private_directory"))
        self.enterContext(
            patch.object(backup, "private", side_effect=lambda path, *args: path.lstat())
        )
        self.enterContext(patch.object(backup, "read_verified", side_effect=self.read_fixture))
        self.enterContext(patch.object(backup, "sync_directory"))
        self.disk = self.enterContext(
            patch.object(
                backup.os,
                "statvfs",
                create=True,
                return_value=SimpleNamespace(f_frsize=4096, f_bavail=65536),
            )
        )
        self.enterContext(patch.object(backup, "exclusive", side_effect=nullcontext))
        self.creds = self.enterContext(
            patch.object(backup, "credentials", return_value={"nas": {}, "r2": {}})
        )
        self.invoke = self.enterContext(
            patch.object(backup, "invoke", side_effect=self.invoke_fixture)
        )

    @staticmethod
    def sha(raw):
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def read_fixture(path, maximum=None, modes=None):
        raw = path.read_bytes()
        return (raw if maximum is not None else hashlib.sha256(raw).hexdigest()), backup.identity(
            path.lstat()
        )

    def invoke_fixture(self, journal, name, arguments, script, env, timeout):
        self.calls.append(name)
        self.assertTrue((journal / f"{name}.intent.json").is_file())
        self.assertEqual(self.sha(script), backup.EXECUTOR_SHA)
        self.assertEqual(timeout, 30)
        if name == "r2":
            self.assertTrue((journal / "nas.result.json").is_file())
        _, staging, run_id, _, _, _, repo_id, destination, *options = arguments
        self.assertEqual(Path(staging), self.staging)
        child = {
            "success": True,
            "destination": name,
            "run_id": run_id,
            "repository_id": repo_id,
            "manifest_sha256": self.request["manifest_sha256"],
            "snapshot_id": "e" * 64,
            "backup_attempted": True,
            "reconciliation_required": False,
        }
        if options:
            self.assertEqual(len(options), 1)
            self.assertIn(options[0], ("--d1-clock", "--openclaw-clock"))
            child["scope"] = options[0][2:]
        directory = Path(destination)
        directory.mkdir()
        raw = backup.encode(child)
        (directory / "result.json").write_bytes(raw + b"\n")
        stdout = journal / f"{name}.stdout"
        stdout.write_bytes(raw)
        return 0, stdout

    def run_fixture(self):
        backup.request_settings(self.request, "run")
        out = {"success": False, "backup_set_complete": False, "restoration_verified": False}
        backup.run_backup(self.request, out)
        return out

    def test_complete_set_is_written_to_both_destinations_in_order(self):
        out = self.run_fixture()
        self.assertEqual(self.calls, ["nas", "r2"])
        self.assertTrue(out["backup_set_complete"])
        self.assertFalse(out["restoration_verified"])
        result = self.journal / "fixture" / "result.json"
        self.assertEqual(json.loads(result.read_bytes()), out)

    def test_d1_scope_writes_both_destinations_without_claiming_the_full_backup(self):
        for name in set(backup.FILES) - {"d1.sql.zst"}:
            (self.staging / name).unlink()
        digest = self.sha((self.staging / "d1.sql.zst").read_bytes())
        manifest = f"{digest}  d1.sql.zst\n".encode()
        (self.staging / "SHA256SUMS").write_bytes(manifest)
        self.request["manifest_sha256"] = self.sha(manifest)
        out = {"success": False, "backup_set_complete": False, "restoration_verified": False}
        backup.run_backup(self.request, out, scope="d1-clock")
        self.assertEqual(self.calls, ["nas", "r2"])
        self.assertTrue(out["d1_backup_complete"])
        self.assertFalse(out["backup_set_complete"])

    def test_missing_openclaw_stops_before_credentials_or_destinations(self):
        (self.staging / "openclaw.tar.zst").unlink()
        with self.assertRaisesRegex(ValueError, "OPENCLAW_FALTANTE"):
            self.run_fixture()
        self.creds.assert_not_called()
        self.invoke.assert_not_called()
        self.assertFalse((self.journal / "fixture").exists())

    def test_openclaw_scope_is_distinct_from_d1_and_from_the_full_backup(self):
        for name in set(backup.FILES) - {"openclaw.tar.zst"}:
            (self.staging / name).unlink()
        digest = self.sha((self.staging / "openclaw.tar.zst").read_bytes())
        manifest = f"{digest}  openclaw.tar.zst\n".encode()
        (self.staging / "SHA256SUMS").write_bytes(manifest)
        self.request["manifest_sha256"] = self.sha(manifest)
        out = {"success": False, "backup_set_complete": False}
        backup.run_backup(self.request, out, scope="openclaw-clock")
        self.assertEqual(self.calls, ["nas", "r2"])
        self.assertTrue(out["openclaw_backup_complete"])
        self.assertFalse(out["backup_set_complete"])
        self.assertNotIn("d1_backup_complete", out)

    def test_r2_timeout_preserves_nas_and_records_need_to_reconcile(self):
        def timeout_on_r2(journal, name, *args):
            if name == "r2":
                raise subprocess.TimeoutExpired("fixture", 30)
            return self.invoke_fixture(journal, name, *args)

        self.invoke.side_effect = timeout_on_r2
        out = self.run_fixture()
        self.assertFalse(out["backup_set_complete"])
        self.assertTrue(out["destinations"]["nas"]["success"])
        self.assertTrue(out["destinations"]["r2"]["reconciliation_required"])
        self.assertEqual(out["destinations"]["r2"]["error"], "TimeoutExpired")
        self.assertTrue((self.journal / "fixture" / "r2.result.json").is_file())
        with self.assertRaises(FileExistsError):
            self.run_fixture()

    def test_modified_staging_stops_before_second_destination(self):
        def mutate_after_nas(journal, name, *args):
            result = self.invoke_fixture(journal, name, *args)
            (self.staging / "d1.sql.zst").write_bytes(b"changed after NAS")
            return result

        self.invoke.side_effect = mutate_after_nas
        out = self.run_fixture()
        self.assertEqual(out["error"], "STAGING_CAMBIO")
        self.assertEqual(self.calls, ["nas"])
        self.assertTrue(out["destinations"]["nas"]["success"])
        self.assertFalse(out["backup_set_complete"])

    def test_persisted_child_receipt_is_required(self):
        def missing_receipt(journal, name, *args):
            result = self.invoke_fixture(journal, name, *args)
            (journal / name / "result.json").unlink()
            return result

        self.invoke.side_effect = missing_receipt
        out = self.run_fixture()
        self.assertEqual(self.calls, ["nas"])
        self.assertTrue(out["destinations"]["nas"]["reconciliation_required"])
        self.assertFalse(out["backup_set_complete"])

    def test_wrong_restic_hash_stops_before_any_destination(self):
        self.request["restic_sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "SHA_RESTIC"):
            self.run_fixture()
        self.invoke.assert_not_called()

    def test_journal_reserve_precedes_any_destination(self):
        self.disk.return_value = SimpleNamespace(f_frsize=4096, f_bavail=1)
        with self.assertRaisesRegex(ValueError, "DISCO_RECIBOS"):
            self.run_fixture()
        self.creds.assert_not_called()
        self.invoke.assert_not_called()


@unittest.skipUnless(os.name == "posix" and Path("/usr/bin/bash").is_file(), "Linux only")
class BackupLocalProcessTests(unittest.TestCase):
    def test_executor_receives_arguments_and_output_is_captured(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory)
            code, output = backup.invoke(
                journal, "nas", ["sample"], b'printf "%s" "$1"\n', backup.COMMAND_ENV, 3
            )
            self.assertEqual(code, 0)
            self.assertEqual(output.read_bytes(), b"sample")

    def test_timeout_kills_local_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory)
            with self.assertRaises(subprocess.TimeoutExpired):
                backup.invoke(journal, "nas", [], b"sleep 30 & wait\n", backup.COMMAND_ENV, 0.1)

    @unittest.skipUnless(
        hasattr(os, "geteuid") and os.geteuid() == 0, "requires local root fixture"
    )
    def test_global_lock_rejects_a_second_run(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(backup, "ROOT", Path(directory).resolve()), backup.exclusive():
                with self.assertRaisesRegex(ValueError, "CORRIDA_EN_CURSO"), backup.exclusive():
                    self.fail("second lock acquired")


if __name__ == "__main__":
    unittest.main()
