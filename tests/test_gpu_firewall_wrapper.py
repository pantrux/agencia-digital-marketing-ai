"""Envoltorio elevado del cortafuegos: pruebas aisladas sin cambiar reglas."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "gpu-bridge-win11/Apply-GpuFirewallElevated.ps1"
CONTEXT_MOCKS = r"""
$script:checked=@(); $script:invocations=0; $script:admin=$true
$script:owner='agencia-digital-marketing-ai:gpu-bridge:v4.1'
$script:cfg=@{
 GPU_INSTALL_ROOT='C:\GPU-Bridge'; GPU_RUNTIME_CONFIG_PATH='C:\GPU-Bridge\runtime.json'
 GPU_WINDOWS_USER_SID='S-1-5-21-123'; GPU_INSTALL_OWNER_MARKER=$script:owner
 GPU_FIREWALL_SCRIPT_PATH='C:\GPU-Bridge\Configure-GpuFirewall.ps1'
 GPU_FIREWALL_SCRIPT_SHA256=('a'*64)
 GPU_FIREWALL_RECEIPT_PATH='C:\GPU-Bridge\firewall-apply-receipt.json'
}
function Test-AgencyGpuElevatedHost { return $true }
function Test-AgencyGpuElevatedAdministrator { return $script:admin }
function Get-AgencyGpuElevatedUserSid { return 'S-1-5-21-123' }
function Assert-AgencyGpuElevatedPrivatePath {
 param([string]$LiteralPath,[string]$UserSid,[string]$InstallRoot,[switch]$Directory)
 $script:checked+= $LiteralPath
 if($LiteralPath -eq $script:rejectedPath) {throw 'PROPIEDAD_AJENA'}
}
function Read-AgencyGpuElevatedJson { param([string]$LiteralPath) return $script:cfg }
function Get-Content { param([string]$LiteralPath,[switch]$Raw,[string]$Encoding) return $script:owner }
function Get-FileHash { param([string]$LiteralPath,[string]$Algorithm) [pscustomobject]@{Hash=('a'*64)} }
function Test-Path { param([string]$LiteralPath) return $false }
if (-not $IsWindows) {
 function Join-Path { param([string]$Path,[string]$ChildPath) return "$Path\$ChildPath" }
}
"""


class AplicacionElevadaFirewall(unittest.TestCase):
    def ejecutar(self, code, *, context=False, extra_env=None):
        result = subprocess.run(
            [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; . $env:GPU_WRAPPER_TEST_SCRIPT; "
                + (CONTEXT_MOCKS if context else "")
                + code,
            ],
            env=os.environ | {"GPU_WRAPPER_TEST_SCRIPT": str(SCRIPT)} | (extra_env or {}),
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_dot_source_sin_efectos(self):
        self.assertEqual(self.ejecutar("'\"importado\"'"), "importado")

    def test_contexto_valida_rutas_sin_invocar_firewall(self):
        result = self.ejecutar(
            r"$r=Get-AgencyGpuElevatedContext -RuntimeConfigPath 'C:\GPU-Bridge\runtime.json'; "
            "@{Script=$r.ScriptPath;Receipt=$r.ReceiptPath;Checked=$script:checked;"
            "Invoked=$script:invocations}|ConvertTo-Json -Compress",
            context=True,
        )
        self.assertEqual(result["Script"], r"C:\GPU-Bridge\Configure-GpuFirewall.ps1")
        self.assertEqual(result["Receipt"], r"C:\GPU-Bridge\firewall-apply-receipt.json")
        self.assertIn(r"C:\GPU-Bridge", result["Checked"])
        self.assertIn(result["Script"], result["Checked"])
        self.assertEqual(result["Invoked"], 0)

    def test_hash_y_rutas_ajenas_rechazan_antes_de_invocar(self):
        changes = (
            "$script:cfg.GPU_FIREWALL_SCRIPT_SHA256=('b'*64)",
            r"$script:cfg.GPU_INSTALL_ROOT='D:\ajeno'",
            r"$script:cfg.GPU_RUNTIME_CONFIG_PATH='C:\GPU-Bridge\otro.json'",
            r"$script:cfg.GPU_FIREWALL_SCRIPT_PATH='C:\GPU-Bridge\services\Configure-GpuFirewall.ps1'",
            r"$script:cfg.GPU_FIREWALL_RECEIPT_PATH='C:\ajeno.json'",
            "$script:cfg.GPU_WINDOWS_USER_SID='S-1-5-21-999'",
            "$script:cfg.GPU_INSTALL_OWNER_MARKER='ajeno'",
            "$script:owner='otro propietario'",
            "$script:admin=$false",
            r"$script:rejectedPath='C:\GPU-Bridge\Configure-GpuFirewall.ps1'",
        )
        for change in changes:
            with self.subTest(change=change):
                result = self.ejecutar(
                    change + "; $rejected=$false; try {"
                    r"$null=Get-AgencyGpuElevatedContext -RuntimeConfigPath 'C:\GPU-Bridge\runtime.json'"
                    "} catch {$rejected=$true}; @{Rejected=$rejected;Invoked=$script:invocations}"
                    "|ConvertTo-Json -Compress",
                    context=True,
                )
                self.assertEqual(result, {"Rejected": True, "Invoked": 0})

    def test_configuracion_no_admite_ruta_relativa_unc_ni_normalizacion(self):
        for path in (
            r"runtime.json",
            r"\\otro\GPU-Bridge\runtime.json",
            r"C:\GPU-Bridge\services\..\runtime.json",
        ):
            with self.subTest(path=path):
                result = self.ejecutar(
                    "$rejected=$false; try {$null=Get-AgencyGpuElevatedContext "
                    "-RuntimeConfigPath $env:GPU_TEST_BAD_PATH} catch {$rejected=$true}; "
                    "@{Rejected=$rejected;Checked=$script:checked.Count}|ConvertTo-Json -Compress",
                    context=True,
                    extra_env={"GPU_TEST_BAD_PATH": path},
                )
                self.assertEqual(result, {"Rejected": True, "Checked": 0})

    def test_json_literal_rechaza_duplicados_antes_de_consumir_valores(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "runtime.json"
            source.write_text('{"GPU_A":"uno","gpu_a":"dos"}', encoding="utf-8")
            result = self.ejecutar(
                "$rejected=$false; try {$null=Read-AgencyGpuElevatedJson -LiteralPath "
                "$env:GPU_TEST_JSON} catch {$rejected=$true}; @{Rejected=$rejected}"
                "|ConvertTo-Json -Compress",
                extra_env={"GPU_TEST_JSON": str(source)},
            )
            self.assertTrue(result["Rejected"])

    def test_permiso_ajeno_o_reparse_rechaza_sin_mutar_acl(self):
        result = self.ejecutar(
            r"""
$script:badOwner=$false; $script:badAccess=$true; $script:reparse=$false
function Get-Item { param([string]$LiteralPath,[switch]$Force)
 [pscustomobject]@{Attributes=$(if($script:reparse){[IO.FileAttributes]::ReparsePoint}else{[IO.FileAttributes]::Directory});PSIsContainer=$true}
}
function Get-Acl { param([string]$LiteralPath)
 $acl=[pscustomobject]@{}
 $acl|Add-Member ScriptMethod GetOwner {param($type) [pscustomobject]@{Value=$(if($script:badOwner){'S-1-5-21-999'}else{'S-1-5-21-123'})}}
 $acl|Add-Member ScriptMethod GetAccessRules {param($a,$b,$type) if($script:badAccess){[pscustomobject]@{AccessControlType=[Security.AccessControl.AccessControlType]::Allow;IdentityReference=[pscustomobject]@{Value='S-1-1-0'}}}}
 return $acl
}
$results=@()
foreach($kind in @('access','owner','reparse')) {
 $script:badAccess=$kind-eq'access';$script:badOwner=$kind-eq'owner';$script:reparse=$kind-eq'reparse'
 try {Assert-AgencyGpuElevatedPrivatePath -LiteralPath 'C:\GPU-Bridge' -InstallRoot 'C:\GPU-Bridge' -UserSid 'S-1-5-21-123' -Directory; $results+=$false} catch {$results+=$true}
}
ConvertTo-Json -InputObject $results -Compress
"""
        )
        self.assertEqual(result, [True, True, True])

    def test_parsea_json_del_main_y_escribe_recibo_atomico_saneado(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "Configure-GpuFirewall.ps1"
            source.write_text(
                "[CmdletBinding(SupportsShouldProcess)] param([string]$Mode) "
                "@{Mode=$Mode;Verified=$true;Changed=5;PlannedChanges=5;ProfilesReady=$true;"
                "OtherRulesModified=$false;NetworkTestPerformed=$false;Plan=@('no publicar')}"
                "|ConvertTo-Json -Depth 4",
                encoding="utf-8",
            )
            receipt = Path(folder) / "receipt.json"
            result = self.ejecutar(
                r"""
function Get-AgencyGpuElevatedContext {param($RuntimeConfigPath) @{
 ScriptPath=$env:GPU_TEST_SOURCE;ReceiptPath=$env:GPU_TEST_RECEIPT
 InstallRoot=$env:GPU_TEST_ROOT;UserSid='S-1-5-21-123';ScriptSha256=('a'*64)
}}
function Assert-AgencyGpuElevatedPrivatePath {param($LiteralPath,$UserSid,$InstallRoot,[switch]$Directory)}
Invoke-AgencyGpuElevatedApply -RuntimeConfigPath 'no se consume'|ConvertTo-Json -Compress
""",
                extra_env={
                    "GPU_TEST_SOURCE": str(source),
                    "GPU_TEST_RECEIPT": str(receipt),
                    "GPU_TEST_ROOT": folder,
                },
            )
            self.assertTrue(result["Success"])
            self.assertTrue(result["Verified"])
            self.assertEqual(result["Changed"], 5)
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(saved, result)
            self.assertNotIn("Plan", saved)
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_error_saneado_y_verified_falso_nunca_acreditan_exito(self):
        for output in (
            "throw 'TOKEN_SINTETICO_PRIVADO'",
            "'not JSON'",
            '\'{"Mode":"Apply","Verified":"true"}\'',
            '\'{"Mode":"Apply","Verified":false}\'',
        ):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / "mock.ps1"
                source.write_text(
                    "[CmdletBinding(SupportsShouldProcess)]param($Mode) " + output, encoding="utf-8"
                )
                receipt = Path(folder) / "receipt.json"
                result = self.ejecutar(
                    r"""
function Get-AgencyGpuElevatedContext {param($RuntimeConfigPath) @{
 ScriptPath=$env:GPU_TEST_SOURCE;ReceiptPath=$env:GPU_TEST_RECEIPT
 InstallRoot=$env:GPU_TEST_ROOT;UserSid='S-1-5-21-123';ScriptSha256=('a'*64)
}}
function Assert-AgencyGpuElevatedPrivatePath {param($LiteralPath,$UserSid,$InstallRoot,[switch]$Directory)}
Invoke-AgencyGpuElevatedApply|ConvertTo-Json -Compress
""",
                    extra_env={
                        "GPU_TEST_SOURCE": str(source),
                        "GPU_TEST_RECEIPT": str(receipt),
                        "GPU_TEST_ROOT": folder,
                    },
                )
                self.assertFalse(result["Success"])
                self.assertFalse(result["Verified"])
                self.assertNotIn("TOKEN_SINTETICO_PRIVADO", json.dumps(result))
                self.assertEqual(json.loads(receipt.read_text(encoding="utf-8")), result)

    def test_contexto_inseguro_no_escribe_recibo(self):
        result = self.ejecutar(
            "function Get-AgencyGpuElevatedContext {param($RuntimeConfigPath) throw 'TOKEN_PRIVADO'}; "
            "function Write-AgencyGpuElevatedReceipt {throw 'NO_ESCRIBIR'}; "
            "Invoke-AgencyGpuElevatedApply|ConvertTo-Json -Compress"
        )
        self.assertFalse(result["Success"])
        self.assertEqual(result["Stage"], "preflight")
        self.assertFalse(result["ReceiptWritten"])
        self.assertNotIn("TOKEN_PRIVADO", json.dumps(result))

    def test_fallo_al_publicar_recibo_no_devuelve_exito(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "mock.ps1"
            source.write_text(
                "[CmdletBinding(SupportsShouldProcess)]param($Mode) "
                "@{Mode='Apply';Verified=$true;Changed=0;ProfilesReady=$true;"
                "OtherRulesModified=$false;NetworkTestPerformed=$false}|ConvertTo-Json",
                encoding="utf-8",
            )
            result = self.ejecutar(
                r"""
function Get-AgencyGpuElevatedContext {param($RuntimeConfigPath) @{
 ScriptPath=$env:GPU_TEST_SOURCE;ReceiptPath='no se usa';ScriptSha256=('a'*64)
}}
function Write-AgencyGpuElevatedReceipt {param($Context,$Receipt) throw 'DETALLE_PRIVADO'}
Invoke-AgencyGpuElevatedApply|ConvertTo-Json -Compress
""",
                extra_env={"GPU_TEST_SOURCE": str(source)},
            )
            self.assertFalse(result["Success"])
            self.assertFalse(result["Verified"])
            self.assertFalse(result["ReceiptWritten"])
            self.assertEqual(result["Stage"], "receipt")
            self.assertEqual(result["ErrorCode"], "RECEIPT_WRITE_FAILED")
            self.assertNotIn("DETALLE_PRIVADO", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
