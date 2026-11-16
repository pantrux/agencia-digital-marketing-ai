"""El instalador conserva directorios ajenos y solo publica configuración mínima."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gpu-bridge-win11"))
from install_gpu_bridge import check_owned_root, private_acl, runtime_values  # noqa: E402


class InstallTests(unittest.TestCase):
    def test_missing_root_is_valid_but_foreign_existing_root_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "bridge"
            self.assertFalse(check_owned_root(root, "propietario"))
            root.mkdir()
            with self.assertRaises(ValueError):
                check_owned_root(root, "propietario")
            (root / ".agencia-gpu-owner").write_text("propietario", encoding="utf-8")
            self.assertTrue(check_owned_root(root, "propietario"))
            with self.assertRaises(ValueError):
                check_owned_root(root, "otro")

    def test_runtime_excludes_unrelated_credentials_and_publication_secrets(self):
        values = {
            "GPU_BRIDGE_HOST": "192.168.0.6",
            "GPU_BRIDGE_BEARER_TOKEN": "local",
            "SYNOLOGY_PASSWORD": "no-copiar",
            "GITHUB_TOKEN": "no-copiar",
            "GPU_SMI_PATH": r"C:\Windows\System32\nvidia-smi.exe",
        }
        result = runtime_values(values)
        self.assertEqual(
            set(result), {"GPU_BRIDGE_HOST", "GPU_BRIDGE_BEARER_TOKEN", "GPU_SMI_PATH"}
        )

    def test_valid_private_acl_is_read_without_reapplying_inheritance(self):
        sid = "S-1-5-21-1-2-3-1001"
        acl = {
            "protected": True,
            "owner": sid,
            "entries": [
                {"sid": principal, "allow": True, "full": True}
                for principal in (sid, "S-1-5-18", "S-1-5-32-544")
            ],
        }

        def run(command, **kwargs):
            self.assertEqual(command[0], "pwsh.exe", "Una ACL válida no debe propagarse otra vez.")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(acl))

        with (
            patch("install_gpu_bridge.shutil.which", return_value="pwsh.exe"),
            patch("install_gpu_bridge.subprocess.run", side_effect=run),
        ):
            private_acl(Path(r"C:\GPU-Bridge"), sid)


if __name__ == "__main__":
    unittest.main()
