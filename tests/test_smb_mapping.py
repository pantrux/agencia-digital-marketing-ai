"""Pruebas PowerShell del cliente SMB con comandos simulados y sin red."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "storage/Map-AgencySmb.ps1"
MOCKS = r"""
$script:mapCalls=0
$script:probeCalls=0
$script:existing=$false
$script:remote='\\192.168.0.8\producciones'
$script:privacy=$true
$script:persistent=$true
function New-SmbMapping {
 [CmdletBinding(SupportsShouldProcess)]
 param([string]$LocalPath,[string]$RemotePath,[pscredential]$Credential,[bool]$Persistent,[bool]$RequirePrivacy)
 if ($LocalPath -cne 'P:' -or $RemotePath -cne '\\192.168.0.8\producciones' -or -not $RequirePrivacy -or -not $Persistent -or $null -eq $Credential) {throw 'Contrato de mapeo incorrecto'}
 $script:mapCalls++
 $script:existing=$true
}
function Get-AgencySmbLocalState {
 $mappings=@()
 if ($script:existing) {$mappings=@([pscustomobject]@{LocalPath='P:';RemotePath=$script:remote;RequirePrivacy=$script:privacy})}
 $persistentMapping=$null
 if ($script:existing -and $script:persistent) {$persistentMapping=[pscustomobject]@{RemotePath=$script:remote}}
 [pscustomobject]@{Mappings=$mappings;LogicalDrives=@();PowerShellDrives=@();PersistentMapping=$persistentMapping}
}
function Read-AgencySmbProbe {
 param([string]$ExpectedSha256)
 $script:probeCalls++
 [pscustomobject]@{Path='P:\.probe';Sha256=$ExpectedSha256;Bytes=37;MatchesExpected=$true}
}
"""


class ClienteSmb(unittest.TestCase):
    def ejecutar(self, code, password="prueba-local-sin-validez", username="prueba"):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / ".env"
            vault.write_text(
                f"SYNOLOGY_USER={username}\nSYNOLOGY_PASSWORD={password}\n", encoding="utf-8"
            )
            (Path(temporary) / ".env.example").write_text(
                "SYNOLOGY_USER=CHANGE_ME\nSYNOLOGY_PASSWORD=CHANGE_ME\n", encoding="utf-8"
            )
            return subprocess.run(
                [
                    "pwsh",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$ErrorActionPreference='Stop'; . $env:SMB_TEST_SCRIPT; " + MOCKS + code,
                ],
                env=os.environ | {"SMB_TEST_SCRIPT": str(SCRIPT), "SMB_TEST_VAULT": str(vault)},
                text=True,
                capture_output=True,
                check=False,
                timeout=20,
            )

    def test_importar_no_contacta_red_ni_mapea(self):
        result = self.ejecutar("Write-Output 'solo-funciones'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "solo-funciones")

    def test_importar_conserva_parametros_del_llamador(self):
        result = subprocess.run(
            [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; $AuditOnly=$true; $ExpectedProbeSha256='preservar'; . $env:SMB_TEST_SCRIPT; [pscustomobject]@{Audit=$AuditOnly;Hash=$ExpectedProbeSha256}|ConvertTo-Json -Compress",
            ],
            env=os.environ | {"SMB_TEST_SCRIPT": str(SCRIPT)},
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"Audit": True, "Hash": "preservar"})

    def test_auditoria_no_mapea_ni_lee_probe_con_password_pendiente(self):
        result = self.ejecutar(
            r"""
$audit=Invoke-AgencySmbMapping -VaultPath $env:SMB_TEST_VAULT -AuditOnly
[pscustomobject]@{Maps=$script:mapCalls;Reads=$script:probeCalls;Ready=$audit.CredentialReady}|ConvertTo-Json -Compress
""",
            password="CHANGE_ME",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"Maps": 0, "Reads": 0, "Ready": False})

    def test_password_pendiente_bloquea_sin_mostrar_valor(self):
        reserva = "COMPLETAR_EN_FASE_2"
        result = self.ejecutar(
            "Invoke-AgencySmbMapping -VaultPath $env:SMB_TEST_VAULT -ExpectedProbeSha256 ('a'*64)",
            password=reserva,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(reserva, result.stdout + result.stderr)

    def test_comentario_inline_sin_comillas_no_cambia_password_ni_usuario(self):
        result = self.ejecutar(
            r"""
$config=Get-AgencySmbConfiguration -VaultPath $env:SMB_TEST_VAULT
[pscustomobject]@{UserOk=($config.Credential.UserName -ceq 'prueba');ValueOk=($config.Credential.GetNetworkCredential().Password -ceq 'valor')}|ConvertTo-Json -Compress
""",
            password="valor # comentario",
            username="prueba # comentario",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"UserOk": True, "ValueOk": True})

    def test_hash_dentro_de_comillas_se_conserva_literal(self):
        result = self.ejecutar(
            r"""
$config=Get-AgencySmbConfiguration -VaultPath $env:SMB_TEST_VAULT
[pscustomobject]@{ValueOk=($config.Credential.GetNetworkCredential().Password -ceq 'valor # literal')}|ConvertTo-Json -Compress
""",
            password='"valor # literal"',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ValueOk": True})

    def test_comillas_sin_cierre_bloquean_y_no_exponen_contenido(self):
        reserva = '"NO_CERRADA'
        result = self.ejecutar(
            "Get-AgencySmbConfiguration -VaultPath $env:SMB_TEST_VAULT",
            password=reserva,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("NO_CERRADA", result.stdout + result.stderr)

    def test_sustituciones_se_conservan_como_texto_sin_ejecutarse(self):
        reserva = '$(throw "NO_EJECUTAR")'
        result = self.ejecutar(
            r"""
$config=Get-AgencySmbConfiguration -VaultPath $env:SMB_TEST_VAULT
[pscustomobject]@{ValueOk=($config.Credential.GetNetworkCredential().Password -ceq '$(throw "NO_EJECUTAR")')}|ConvertTo-Json -Compress
""",
            password=reserva,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"ValueOk": True})

    def test_whatif_no_autentica_mapea_ni_lee_probe(self):
        result = self.ejecutar(r"""
$null=Invoke-AgencySmbMapping -VaultPath $env:SMB_TEST_VAULT -ExpectedProbeSha256 ('a'*64) -WhatIf
[pscustomobject]@{Maps=$script:mapCalls;Reads=$script:probeCalls}|ConvertTo-Json -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {"Maps": 0, "Reads": 0})

    def test_rechaza_p_de_otro_proyecto_antes_de_mapear(self):
        result = self.ejecutar(r"""
$script:existing=$true;$script:remote='\\192.168.0.8\otro-proyecto'
Invoke-AgencySmbMapping -VaultPath $env:SMB_TEST_VAULT -ExpectedProbeSha256 ('a'*64)
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("P:", result.stderr)

    def test_existente_exacto_cifrado_es_idempotente(self):
        result = self.ejecutar(r"""
$script:existing=$true
$null=Invoke-AgencySmbMapping -VaultPath $env:SMB_TEST_VAULT -ExpectedProbeSha256 ('a'*64)
[pscustomobject]@{Maps=$script:mapCalls;Reads=$script:probeCalls}|ConvertTo-Json -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"Maps": 0, "Reads": 1})

    def test_existente_sin_requireprivacy_no_se_reemplaza(self):
        result = self.ejecutar(r"""
$script:existing=$true;$script:privacy=$false
Invoke-AgencySmbMapping -VaultPath $env:SMB_TEST_VAULT -ExpectedProbeSha256 ('a'*64)
""")
        self.assertNotEqual(result.returncode, 0)

    def test_existente_no_persistente_no_se_declara_listo(self):
        result = self.ejecutar(r"""
$script:existing=$true;$script:persistent=$false
Invoke-AgencySmbMapping -VaultPath $env:SMB_TEST_VAULT -ExpectedProbeSha256 ('a'*64)
""")
        self.assertNotEqual(result.returncode, 0)

    def test_libre_mapea_con_credential_privacy_y_lee_probe_una_vez(self):
        result = self.ejecutar(r"""
$done=Invoke-AgencySmbMapping -VaultPath $env:SMB_TEST_VAULT -ExpectedProbeSha256 ('a'*64)
[pscustomobject]@{Maps=$script:mapCalls;Reads=$script:probeCalls;Ready=$done.Probe.MatchesExpected}|ConvertTo-Json -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"Maps": 1, "Reads": 1, "Ready": True})
        self.assertNotIn("prueba-local-sin-validez", result.stdout + result.stderr)

    def test_no_mapea_si_no_se_coordino_hash_del_probe_linux(self):
        result = self.ejecutar("Invoke-AgencySmbMapping -VaultPath $env:SMB_TEST_VAULT")
        self.assertNotEqual(result.returncode, 0)

    def test_contrato_rechaza_cmdlet_sin_opcion_de_cifrado(self):
        result = self.ejecutar(r"""
function New-SmbMapping {[CmdletBinding(SupportsShouldProcess)] param([string]$LocalPath,[string]$RemotePath,[pscredential]$Credential,[bool]$Persistent)}
Test-AgencySmbCommandContract
""")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
