"""Comprueba el formato nativo de confianza sin leer ni modificar el PCTCS real."""

import os
import subprocess
import unittest
from pathlib import Path

FORMAT_SCRIPT = Path(__file__).parents[1] / "vcenter/media-vcenter-trust-format.ps1"


class VcenterTrustFormatTests(unittest.TestCase):
    def run_fixture(self, code):
        return subprocess.run(
            [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; . $env:MEDIA_TEST_TRUST_FORMAT; " + code,
            ],
            capture_output=True,
            text=True,
            check=False,
            env=os.environ | {"MEDIA_TEST_TRUST_FORMAT": str(FORMAT_SCRIPT)},
            timeout=20,
        )

    def test_formato_nativo_sin_encabezado_comillas_ni_bom(self):
        result = self.run_fixture(
            "$record=ConvertFrom-MediaTrustStore -Lines @('servidor.local," + "A" * 40 + "'); "
            "if (-not $record.IsNative) {throw 'No reconocido'}; "
            "ConvertTo-MediaTrustStore -Records $record.Records"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "servidor.local," + "A" * 40)

    def test_migra_csv_conservando_otros_servidores(self):
        result = self.run_fixture(
            "$record=ConvertFrom-MediaTrustStore -Lines @("
            '\'"server_address","certificate_thumbprint"\','
            '\'"servidor.local","' + "A" * 40 + "\"',"
            '\'"otro.local","' + "B" * 40 + "\"'); "
            "if ($record.IsNative -or $record.Records.Count -ne 2) {throw 'Migración inválida'}; "
            "ConvertTo-MediaTrustStore -Records $record.Records"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(), ["servidor.local," + "A" * 40, "otro.local," + "B" * 40]
        )

    def test_rechaza_huella_invalida(self):
        result = self.run_fixture("ConvertFrom-MediaTrustStore -Lines @('servidor.local,invalida')")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("formato desconocido", result.stderr)


if __name__ == "__main__":
    unittest.main()
