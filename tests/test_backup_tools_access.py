"""Prueba la consulta ACL con el intérprete integrado, sin depender de instalaciones opcionales."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "backup_access", ROOT / "sre/backup_tools_access_background.py"
)
ACCESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCESS)


class WindowsInterpreterAccess(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Contrato de ruta del sistema Windows")
    def test_consulta_acl_no_depende_de_powershell7_en_programfiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "System32/WindowsPowerShell/v1.0/powershell.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture-no-ejecutable")
            data = {
                "current_sid": "S-1-5-21-123-1001",
                "owner_sid": "S-1-5-21-123-1001",
                "reparse": False,
                "access": [{"sid": "S-1-5-21-123-1001", "rights": 2032127}],
            }
            with (
                patch.dict(
                    os.environ,
                    {
                        "SystemRoot": str(root),
                        "ProgramFiles": str(root / "sin-pwsh7"),
                        "PSModulePath": str(root / "modulos-otra-version"),
                    },
                ),
                patch.object(
                    ACCESS.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout=json.dumps(data)),
                ) as launch,
            ):
                ACCESS.check_access(root, private=True)
            self.assertEqual(Path(launch.call_args.args[0][0]), executable)
            self.assertIn("-NoProfile", launch.call_args.args[0])
            self.assertEqual(
                launch.call_args.kwargs.get("creationflags"), ACCESS.subprocess.CREATE_NO_WINDOW
            )
            self.assertEqual(launch.call_args.kwargs.get("stdin"), ACCESS.subprocess.DEVNULL)
            self.assertEqual(
                launch.call_args.kwargs["env"]["AGENCY_BACKUP_TOOLS_AUDIT_PATH"], str(root)
            )
            self.assertEqual(
                launch.call_args.kwargs["env"]["PSModulePath"], str(executable.parent / "Modules")
            )


if __name__ == "__main__":
    unittest.main()
