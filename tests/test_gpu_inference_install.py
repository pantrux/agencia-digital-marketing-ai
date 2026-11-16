"""El instalador de inferencia conserva nombres, versiones y hashes del grafo completo."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"))
from install_inference import local_requirements  # noqa: E402


class InferenceInstallTests(unittest.TestCase):
    def test_local_lock_verifies_bytes_before_install(self):
        with tempfile.TemporaryDirectory(prefix="gpu prueba ") as directory:
            wheel = Path(directory) / "paquete-1-py3-none-any.whl"
            wheel.write_bytes(b"fixture")
            item = {
                "name": "paquete",
                "version": "1",
                "local_path": str(wheel),
                "size": 7,
                "sha256": hashlib.sha256(b"fixture").hexdigest(),
            }
            result = local_requirements([item])
            self.assertIn("%20", result)
            self.assertIn("--hash=sha256:" + item["sha256"], result)
            wheel.write_bytes(b"otro")
            with self.assertRaises(ValueError):
                local_requirements([item])

    def test_duplicate_packages_and_bad_names_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "paquete.whl"
            wheel.write_bytes(b"fixture")
            item = {
                "name": "paquete",
                "version": "1",
                "local_path": str(wheel),
                "size": 7,
                "sha256": hashlib.sha256(b"fixture").hexdigest(),
            }
            with self.assertRaises(ValueError):
                local_requirements([item, item])
            with self.assertRaises(ValueError):
                local_requirements([item | {"name": "paquete;otra"}])


if __name__ == "__main__":
    unittest.main()
