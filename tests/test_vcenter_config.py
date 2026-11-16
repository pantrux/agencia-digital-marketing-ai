"""Valida la configuración de vCenter sin consultar ni modificar infraestructura."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
CONFIG_SCRIPT = PROJECT_ROOT / "vcenter/media-vm-config.ps1"
FIXTURE = """VSPHERE_SERVER=192.168.0.2
VSPHERE_USER=operador
VSPHERE_PASSWORD=prueba-local-sin-validez
ESXI_HOST=192.168.0.3
ESXI_DATASTORE_NVME=datastore1
ESXI_TEMPLATE_UBUNTU=TPL-ubuntu2404-k8s
MEDIA_VM_NAME=vm-media-01
MEDIA_VM_IP=192.168.0.85
MEDIA_VM_NETMASK=255.255.255.0
MEDIA_VM_GATEWAY=192.168.0.1
MEDIA_VM_DNS=192.168.0.1,1.1.1.1
MEDIA_VM_SSH_USER=jandrade
"""


class VcenterConfigTests(unittest.TestCase):
    def run_config(self, vault):
        with tempfile.TemporaryDirectory() as temporary:
            vault_path = Path(temporary) / ".env"
            vault_path.write_text(vault, encoding="utf-8")
            environment = os.environ | {
                "MEDIA_TEST_VAULT": str(vault_path),
                "MEDIA_TEST_CONFIG": str(CONFIG_SCRIPT),
            }
            command = """
$ErrorActionPreference = 'Stop'
. $env:MEDIA_TEST_CONFIG
$configuration = Get-MediaVmConfiguration -VaultPath $env:MEDIA_TEST_VAULT
[pscustomobject]@{
    Name = $configuration.Name
    IpAddress = $configuration.IpAddress
    Cpu = $configuration.Cpu
    MemoryGB = $configuration.MemoryGB
    DiskGB = $configuration.DiskGB
    PortGroup = $configuration.PortGroup
    DnsServers = @($configuration.DnsServers)
    IsCredential = $configuration.Credential -is [pscredential]
} | ConvertTo-Json -Compress
"""
            return subprocess.run(
                ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
                timeout=20,
            )

    def test_recursos_congelados_y_credencial_protegida(self):
        result = self.run_config(FIXTURE)
        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertEqual(configuration["Name"], "vm-media-01")
        self.assertEqual(configuration["Cpu"], 6)
        self.assertEqual(configuration["MemoryGB"], 16)
        self.assertEqual(configuration["DiskGB"], 80)
        self.assertEqual(configuration["PortGroup"], "LAN")
        self.assertTrue(configuration["IsCredential"])
        self.assertNotIn("prueba-local-sin-validez", result.stdout)

    def test_rechaza_ip_de_otro_equipo(self):
        result = self.run_config(
            FIXTURE.replace("MEDIA_VM_IP=192.168.0.85", "MEDIA_VM_IP=192.168.0.7")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("192.168.0.85", result.stderr)

    def test_rechaza_claves_duplicadas(self):
        result = self.run_config(FIXTURE + "MEDIA_VM_NAME=otra-maquina\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicada", result.stderr)

    def test_rechaza_credencial_pendiente_sin_mostrarla(self):
        result = self.run_config(FIXTURE.replace("prueba-local-sin-validez", "CHANGE_ME"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VSPHERE_PASSWORD", result.stderr)
        self.assertNotIn("prueba-local-sin-validez", result.stderr)


if __name__ == "__main__":
    unittest.main()
