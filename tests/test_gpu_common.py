"""Contratos de acceso del puente, sin instalar dependencias ni usar la GPU."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"))
from bridge_common import BridgeConfig, GuardedAsgi, safe_media_path  # noqa: E402


def config():
    return BridgeConfig.from_env(
        {
            "GPU_BRIDGE_HOST": "192.168.0.6",
            "GPU_BRIDGE_BEARER_TOKEN": "prueba-local-sin-valor-operativo-123456",
            "GPU_INSTALL_ROOT": r"C:\GPU-Bridge",
            "GPU_MODEL_CACHE_ROOT": r"C:\GPU-Bridge\models",
        }
    )


class AccessTests(unittest.TestCase):
    def request(self, *, ip="192.168.0.85", path="/health", headers=None, ready=True):
        calls, messages = [], []

        async def upstream(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def readiness():
            if ready == "error":
                raise RuntimeError(config().token)
            return {"ready": ready, "provider": "CUDA"}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        auth = (b"authorization", f"Bearer {config().token}".encode())
        scope = {
            "type": "http",
            "client": (ip, 12345),
            "path": path,
            "method": "GET",
            "headers": [auth] if headers is None else headers,
        }
        app = GuardedAsgi(upstream, config(), "prueba", "1", readiness)
        asyncio.run(app(scope, receive, send))
        return messages, calls

    def test_health_requires_token(self):
        messages, calls = self.request(headers=[])
        self.assertEqual(messages[0]["status"], 401)
        self.assertEqual(calls, [])

    def test_duplicate_authorization_is_rejected(self):
        auth = (b"authorization", f"Bearer {config().token}".encode())
        self.assertEqual(self.request(headers=[auth, auth])[0][0]["status"], 401)

    def test_forwarded_header_cannot_bypass_ip(self):
        auth = (b"authorization", f"Bearer {config().token}".encode())
        result = self.request(
            ip="192.168.0.100", headers=[auth, (b"x-forwarded-for", b"192.168.0.85")]
        )
        self.assertEqual(result[0][0]["status"], 403)

    def test_health_reports_real_readiness_and_alias(self):
        for path in ("/health", "/healthz"):
            messages, calls = self.request(path=path)
            self.assertEqual(messages[0]["status"], 200)
            self.assertTrue(json.loads(messages[1]["body"])["ready"])
            self.assertEqual(calls, [])

    def test_unready_or_exception_is_503_without_secret(self):
        for ready in (False, "error"):
            messages, _ = self.request(ready=ready)
            self.assertEqual(messages[0]["status"], 503)
            self.assertNotIn(config().token, str(messages))

    def test_upstream_routes_preserved_and_ipv4_mapped_allowed(self):
        messages, calls = self.request(ip="::ffff:192.168.0.7", path="/predict")
        self.assertEqual(messages[0]["status"], 204)
        self.assertEqual(calls, ["/predict"])

    def test_config_rejects_pending_token_and_wrong_host(self):
        for values in (
            {"GPU_BRIDGE_HOST": "192.168.0.7"},
            {"GPU_BRIDGE_BEARER_TOKEN": "CHANGE_ME"},
        ):
            mapping = {
                "GPU_BRIDGE_HOST": "192.168.0.6",
                "GPU_BRIDGE_BEARER_TOKEN": config().token,
                "GPU_INSTALL_ROOT": r"C:\GPU-Bridge",
                "GPU_MODEL_CACHE_ROOT": r"C:\GPU-Bridge\models",
            }
            with self.assertRaises(ValueError):
                BridgeConfig.from_env(mapping | values)
        self.assertNotIn(config().token, repr(config()))


class MediaPathTests(unittest.TestCase):
    def test_existing_and_new_path_stay_in_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                safe_media_path(root, "proyectos/sansevieras/06_renders/prueba.mp4"),
                root / "proyectos/sansevieras/06_renders/prueba.mp4",
            )

    def test_escape_windows_devices_and_ambiguous_paths_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for path in (
                "../otro",
                "/otro",
                r"C:\otro",
                r"\\NAS\otro",
                "a/../b",
                "a:secret",
                "NUL.mp4",
                "a/COM1",
                "a.",
                "a ",
                "a//b",
                r"a\b",
            ):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    safe_media_path(Path(directory), path)

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / "escape"
            try:
                link.symlink_to(root.parent, target_is_directory=True)
            except OSError:
                return  # Windows sin privilegio de enlaces; la prueba de reanálisis es independiente.
            with self.assertRaises(ValueError):
                safe_media_path(root, "escape/otra.txt")


if __name__ == "__main__":
    unittest.main()
