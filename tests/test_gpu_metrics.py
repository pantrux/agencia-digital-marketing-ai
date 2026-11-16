"""Verifica datos NVIDIA observados, sin atribuir cero a valores desconocidos."""

import asyncio
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"))
from metrics_service import create_app, parse_gpu_csv, prometheus_metrics  # noqa: E402


class MetricsTests(unittest.TestCase):
    def test_parse_real_nvidia_shape_and_sessions(self):
        result = parse_gpu_csv(
            "NVIDIA GeForce RTX 3060 Ti, GPU-prueba, 8192, 3381, 44.53, 1, 2\n", "GPU-prueba"
        )
        self.assertEqual(result["memory_used_mib"], 3381)
        self.assertEqual(result["temperature_celsius"], 44.53)
        self.assertEqual(result["encoder_sessions"], 2)

    def test_unavailable_sessions_are_omitted(self):
        result = parse_gpu_csv("RTX, GPU-prueba, 8192, 3381, 44, 1, [N/A]\n", "GPU-prueba")
        output = prometheus_metrics(result)
        self.assertIsNone(result["encoder_sessions"])
        self.assertNotIn("gpu_encoder_sessions ", output)
        self.assertIn("gpu_encoder_sessions_available 0", output)

    def test_wrong_gpu_multiple_devices_and_impossible_values_rejected(self):
        for line in (
            "RTX, otra, 8192, 0, 44, 1, 0",
            "RTX, GPU-prueba, 8192, 9000, 44, 1, 0",
            "RTX, GPU-prueba, 8192, 0, nan, 1, 0",
            "RTX, GPU-prueba, 8192, 0, 44, 101, 0",
            "RTX, GPU-prueba, 8192, 0, 44, 1, -1",
            "fila\nfila",
        ):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_gpu_csv(line, "GPU-prueba")

    def test_supervisor_lifespan_and_degraded_health(self):
        class Supervisor:
            started = stopped = False

            async def start(self):
                self.started = True

            async def stop(self):
                self.stopped = True

            def snapshot(self):
                return {"healthy": False, "children": {"whisper": {"healthy": False}}}

        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "smi.exe"
            binary.write_bytes(b"fixture")
            mapping = {
                "GPU_BRIDGE_HOST": "192.168.0.6",
                "GPU_BRIDGE_BEARER_TOKEN": "prueba-local-sin-valor-operativo-123456",
                "GPU_INSTALL_ROOT": r"C:\GPU-Bridge",
                "GPU_MODEL_CACHE_ROOT": r"C:\GPU-Bridge\models",
                "GPU_SMI_PATH": str(binary),
                "GPU_SMI_SHA256": hashlib.sha256(b"fixture").hexdigest(),
                "GPU_UUID": "GPU-prueba",
            }
            supervisor = Supervisor()
            app = create_app(mapping, supervisor=supervisor)
            events = iter(({"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}))
            messages = []

            async def receive():
                return next(events)

            async def send(message):
                messages.append(message)

            asyncio.run(app({"type": "lifespan"}, receive, send))
            self.assertTrue(supervisor.started and supervisor.stopped)
            self.assertEqual(messages[-1]["type"], "lifespan.shutdown.complete")
            measures = parse_gpu_csv("RTX, GPU-prueba, 8192, 1000, 40, 1, 0", "GPU-prueba")
            with patch("metrics_service.read_gpu", return_value=measures):
                self.assertFalse(asyncio.run(app.readiness())["ready"])


if __name__ == "__main__":
    unittest.main()
