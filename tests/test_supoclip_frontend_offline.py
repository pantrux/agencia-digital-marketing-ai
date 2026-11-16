import importlib.util
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "docker/supoclip/frontend_offline_linux.py"
SPEC = importlib.util.spec_from_file_location("supoclip_frontend_offline", SOURCE)
offline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(offline)


class FrontendOfflineTests(unittest.TestCase):
    def test_child_has_a_private_home_without_a_system_account(self):
        with tempfile.TemporaryDirectory() as path:
            stage = Path(path)
            with patch.dict("os.environ", {}, clear=True):
                env = offline.environment(stage, "http://127.0.0.1:8000", offline=True)
            self.assertEqual(Path(env["HOME"]), stage / "home")
            self.assertTrue(Path(env["HOME"]).is_dir())
            self.assertEqual(env["npm_config_offline"], "true")

    def test_exact_scope_for_npm_tarball(self):
        self.assertEqual(
            offline.tarball_route("@prisma/adapter-pg@6.19.3"),
            "/@prisma/adapter-pg/-/adapter-pg-6.19.3.tgz",
        )
        for value in ("../package@1.2.3", "name@latest", "git+https://host/name@1.2.3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                offline.tarball_route(value)

    def test_only_public_origins_without_credentials(self):
        self.assertEqual(
            offline.public_origin("http://192.168.0.85:3107"), "http://192.168.0.85:3107"
        )
        for value in (
            "http://private@host:3107",
            "https://host/frontend",
            "http://host?search=x",
            "http://host#other",
            "http://host:",
            "file:///temp",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                offline.public_origin(value)

    def test_mirror_serves_only_a_pinned_tarball_once(self):
        raw = b"tarball-fixture"
        identity = "fixture@1.2.3"
        route = offline.tarball_route(identity)
        row = {
            "identity": identity,
            "route": route,
            "path": "fixture.tgz",
            "bytes": len(raw),
            "sha256": offline.sha(raw),
        }
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            (root / "fixture.tgz").write_bytes(raw)
            with patch.object(offline, "SEED", root), offline.Mirror({identity: row}) as mirror:
                thread = threading.Thread(target=mirror.serve_forever)
                thread.start()
                try:
                    conn = HTTPConnection("127.0.0.1", mirror.server_port, timeout=3)
                    try:
                        for target, status in ((route, 200), (route, 404), ("/fixture", 404)):
                            conn.request("GET", target)
                            response = conn.getresponse()
                            self.assertEqual(response.status, status)
                            body = response.read()
                            if status == 200:
                                self.assertEqual(body, raw)
                    finally:
                        conn.close()
                    self.assertEqual(mirror.served, {identity})
                    self.assertEqual(mirror.rejected, 2)
                finally:
                    mirror.shutdown()
                    thread.join()
