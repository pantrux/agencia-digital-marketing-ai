import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sre import backup_321_capture_postgres as capture


class CapturePostgresTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.parent = self.root / "capture"
        self.parent.mkdir()
        self.request = {
            "root": str(self.parent),
            "run_id": "fixture",
            "scope": "acceptance",
            "postgres-apps": {
                "cid": "a" * 64,
                "image": "sha256:" + "b" * 64,
                "pgdata": str(self.root / "apps-data"),
            },
            "postgres-immich": {
                "cid": "c" * 64,
                "image": capture.SOURCES["postgres-immich"]["image"],
                "pgdata": str(self.root / "immich-data"),
            },
        }
        self.commands = []
        self.enterContext(patch.object(capture.core, "ROOT", self.root))
        self.enterContext(patch.object(capture.core, "private_directory"))
        self.enterContext(
            patch.object(capture.core, "private", side_effect=lambda p, *a: p.lstat())
        )
        self.enterContext(patch.object(capture.core, "sync_directory"))
        self.enterContext(patch.object(capture.core, "exclusive", side_effect=nullcontext))
        self.disk = self.enterContext(
            patch.object(
                capture.core.os,
                "statvfs",
                create=True,
                return_value=SimpleNamespace(f_frsize=4096, f_bavail=1024 * 1024),
            )
        )
        self.enterContext(
            patch.object(capture.core, "read_verified", side_effect=self.read_fixture)
        )
        self.observe = self.enterContext(
            patch.object(capture, "observed_container", side_effect=self.observe_fixture)
        )
        self.invoke = self.enterContext(
            patch.object(capture.core, "invoke", side_effect=self.invoke_fixture)
        )

    @staticmethod
    def read_fixture(path, maximum=None, modes=None):
        raw = path.read_bytes()
        return (
            raw if maximum is not None else hashlib.sha256(raw).hexdigest()
        ), capture.core.identity(path.lstat())

    @staticmethod
    def observe_fixture(service, row, scope):
        return {**row, "pid": 500, "started": "fixture"}

    def invoke_fixture(self, run, service, arguments, script, env, timeout, *, prefix):
        self.commands.append(service)
        self.assertTrue((run / "intent.json").is_file())
        self.assertEqual(timeout, 420)
        self.assertEqual(prefix[0], "/usr/bin/prlimit")
        self.assertTrue(prefix[1].startswith("--fsize="))
        self.assertEqual(hashlib.sha256(script).hexdigest(), capture.SOURCES[service]["sha256"])
        self.assertEqual(arguments[0], self.request[service]["cid"])
        if service == "postgres-immich":
            self.assertEqual(arguments[1], self.request[service]["pgdata"])
            self.assertTrue((run / "postgres-apps.result.json").is_file())
        production = service == "postgres-immich" and self.request["scope"] == "production"
        if production:
            self.assertEqual(arguments[-1], "production")
        target = Path(arguments[-2] if production else arguments[-1])
        target.mkdir()
        payload = target / capture.SOURCES[service]["payload"]
        data = f"fixture {service}".encode()
        payload.write_bytes(data)
        stdout = run / (service + ".stdout")
        stdout.write_text(
            json.dumps(
                {
                    "cid": arguments[0],
                    "service": service,
                    "path": str(payload),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            ),
            encoding="utf-8",
        )
        return 0, stdout

    def run_fixture(self):
        capture.validate_request(self.request)
        out = {"success": False, "backup_set_complete": False}
        capture.run_captures(self.request, out)
        return out

    def test_both_new_captures_keep_original_identity_and_receipts(self):
        out = self.run_fixture()
        self.assertTrue(out["success"])
        self.assertFalse(out["backup_set_complete"])
        self.assertEqual(self.commands, ["postgres-apps", "postgres-immich"])
        self.assertEqual(out["captures"]["postgres-apps"]["staging_name"], "postgres-apps.sql.zst")
        self.assertEqual(self.observe.call_count, 6)
        with self.assertRaises(FileExistsError):
            self.run_fixture()

    def test_container_epoch_change_preserves_partial_and_stops(self):
        observations = 0

        def observed(service, row, scope):
            nonlocal observations
            observations += 1
            value = self.observe_fixture(service, row, scope)
            return {**value, "pid": 501} if observations == 4 else value

        self.observe.side_effect = observed
        out = self.run_fixture()
        self.assertFalse(out["success"])
        self.assertEqual(out["error"], "POSTGRES_CAMBIO")
        self.assertEqual(self.commands, ["postgres-apps"])
        self.assertTrue((self.parent / "fixture/postgres-apps/postgres.sql.zst").is_file())

    def test_rejects_production_with_trial_paths_and_uncredited_immich_image(self):
        self.request["scope"] = "production"
        self.request["postgres-immich"]["image"] = capture.IMMICH_PRODUCCION["image"]
        with self.assertRaisesRegex(ValueError, "POSTGRES_PRODUCCION_IDENTIDAD"):
            self.run_fixture()
        for scope, image in (
            ("production", capture.SOURCES["postgres-immich"]["image"]),
            ("acceptance", capture.IMMICH_PRODUCCION["image"]),
        ):
            with self.subTest(scope=scope):
                self.request["scope"] = scope
                self.request["postgres-immich"]["image"] = image
                with self.assertRaisesRegex(ValueError, "IMMICH_CAPTURADOR_NO_ACREDITA_IMAGEN"):
                    self.run_fixture()
        self.invoke.assert_not_called()

    def test_production_records_only_its_pinned_sources(self):
        self.request["scope"] = "production"
        self.request["postgres-apps"]["image"] = capture.APPS_PRODUCCION_IMAGE
        self.request["postgres-apps"]["pgdata"] = str(self.root / "apps-data")
        self.request["postgres-immich"]["image"] = capture.IMMICH_PRODUCCION["image"]
        pgdata = self.request["postgres-immich"]["pgdata"]
        with (
            patch.object(capture, "APPS_PRODUCCION_PGDATA", str(self.root / "apps-data")),
            patch.dict(capture.IMMICH_PRODUCCION, pgdata=pgdata),
        ):
            out = self.run_fixture()
        self.assertTrue(out["success"])
        self.assertEqual(out["scope"], "production")
        self.assertEqual(self.commands, ["postgres-apps", "postgres-immich"])
        self.assertTrue(all(args[-1] == "production" for args, _ in self.observe.call_args_list))
        self.assertEqual(out["captures"]["postgres-immich"]["source"]["pgdata"], pgdata)

    def test_rejects_payload_drift_even_when_child_claims_success(self):
        def drift(run, service, arguments, *args, **kwargs):
            code, stdout = self.invoke_fixture(run, service, arguments, *args, **kwargs)
            path = Path(arguments[-1]) / capture.SOURCES[service]["payload"]
            raw = path.read_bytes()
            path.write_bytes(b"x" * len(raw))
            return code, stdout

        self.invoke.side_effect = drift
        out = self.run_fixture()
        self.assertEqual(out["error"], "POSTGRES_SHA")
        self.assertFalse(out["backup_set_complete"])
        self.assertEqual(self.commands, ["postgres-apps"])

    def test_small_capture_can_use_available_disk_above_reserve(self):
        self.disk.return_value = SimpleNamespace(f_frsize=4096, f_bavail=150000)
        self.assertTrue(self.run_fixture()["success"])

    def test_capture_preserves_disk_reserve(self):
        self.disk.return_value = SimpleNamespace(f_frsize=4096, f_bavail=16384)
        with self.assertRaisesRegex(ValueError, "DISCO_CAPTURA"):
            self.run_fixture()
        self.invoke.assert_not_called()
