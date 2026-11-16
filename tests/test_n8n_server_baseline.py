import io
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "n8n"))

import receive_server_linux_baseline as receiver  # noqa: E402
import seed_server_linux_baseline as seed  # noqa: E402


class BaselineTests(unittest.TestCase):
    def test_receiver_rejects_traversal_and_expansion(self):
        self.assertEqual(
            receiver.parts_of("tarballs/@a/b/1.2.3.tgz"), ("tarballs", "@a", "b", "1.2.3.tgz")
        )
        for path in (
            "../tarballs/x",
            "tarballs//x",
            "tarballs/../x",
            "tarballs/.env",
            "tarballs\\x",
            "/tarballs/x",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "TAR_RUTA"):
                receiver.parts_of(path)
        reader = receiver.Bounded(io.BytesIO(b"abcd"), 3)
        self.assertEqual(reader.read(3), b"abc")
        with self.assertRaisesRegex(ValueError, "TAR_LIMITE"):
            reader.read(1)

    def test_both_installs_frozen_and_only_second_offline(self):
        for offline in (False, True):
            with self.subTest(offline=offline):
                args = seed.command(
                    Path("/tool/pnpm"),
                    Path("/work/project"),
                    Path("/work/store"),
                    seed.REGISTRY,
                    offline,
                )
                self.assertIn("install", args)
                self.assertIn("--frozen-lockfile", args)
                self.assertIn("--ignore-scripts", args)
                self.assertIn("--config.package-import-method=hardlink", args)
                self.assertEqual("--config.frozen-store=true" in args, offline)
                self.assertEqual("--offline" in args, offline)

    def test_log_rejects_duplicate_policy_and_offline_download(self):
        policy = "✓ Lockfile passes supply-chain policies (3478 entries in 1s)\n".encode()
        progress = b"Progress: resolved 3000, reused 3000, downloaded 0, added 3000, done\n"
        self.assertEqual(seed.parse_log(policy + progress, True)["downloaded"], 0)
        with self.assertRaisesRegex(ValueError, "PNPM_EVIDENCIA"):
            seed.parse_log(policy + policy + progress, True)
        with self.assertRaisesRegex(ValueError, "PNPM_PROGRESO"):
            seed.parse_log(policy + progress.replace(b"downloaded 0", b"downloaded 1"), True)

    def test_real_loopback_serves_only_indexed_get_without_credentials(self):
        class Inputs:
            routes = {
                "/@scope/demo/-/demo-1.0.0.tgz": ("contained", "@scope/demo@1.0.0", {"bytes": 3})
            }
            metadata = {"@scope/demo": {}}

            def artifact(self, _):
                return io.BytesIO(b"tar")

            def project(self, name, origin):
                return f"{name} {origin}".encode()

        with seed.Registry(Inputs()) as server:
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
            worker.start()
            try:

                def request(method, path, headers=None):
                    conn = HTTPConnection(*server.server_address, timeout=2)
                    try:
                        conn.request(method, path, headers=headers or {})
                        with conn.getresponse() as response:
                            return response.status, response.read()
                    finally:
                        conn.close()

                self.assertEqual(request("GET", "/@scope/demo/-/demo-1.0.0.tgz"), (200, b"tar"))
                self.assertEqual(
                    request("GET", "/@scope%2Fdemo"), (200, f"@scope/demo {server.origin}".encode())
                )
                self.assertEqual(request("GET", "/not-indexed"), (404, b""))
                self.assertEqual(
                    request("GET", "/@scope/demo", {"Authorization": "dummy"}), (400, b"")
                )
            finally:
                server.shutdown()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(server.served, set(Inputs.routes))
            self.assertEqual(server.rejected, 2)
            self.assertEqual(server.failed, 0)


if __name__ == "__main__":
    unittest.main()
