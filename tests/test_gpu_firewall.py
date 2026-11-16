"""Contrato del cortafuegos GPU con cmdlets simulados; no cambia reglas ni usa red."""

import ipaddress
import json
import os
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "gpu-bridge-win11/Configure-GpuFirewall.ps1"
MOCKS = r"""
$script:rules=@{}; $script:writes=@(); $script:isAdmin=$true
$script:profileEnabled=$true; $script:activeMissing=$false; $script:activeForeign=$null
function Test-AgencyGpuFirewallAdministrator { return $script:isAdmin }
function Get-NetFirewallRule {
 [CmdletBinding()] param([string]$PolicyStore)
 if ($PolicyStore -eq 'ActiveStore' -and $script:activeMissing) {return}
 if ($PolicyStore -eq 'ActiveStore' -and $null -ne $script:activeForeign) {return @($script:rules.Values)+@($script:activeForeign)}
 @($script:rules.Values)
}
function Get-NetFirewallProfile {
 [CmdletBinding()] param([string]$PolicyStore)
 foreach($n in @('Domain','Private','Public')) {[pscustomobject]@{Name=$n;Enabled=$script:profileEnabled;AllowLocalFirewallRules='NotConfigured'}}
}
function Get-NetFirewallPortFilter {
 [CmdletBinding()] param([object]$AssociatedNetFirewallRule)
 [pscustomobject]@{Protocol=$AssociatedNetFirewallRule.Protocol;LocalPort=$AssociatedNetFirewallRule.LocalPort;RemotePort='Any'}
}
function Get-NetFirewallAddressFilter {
 [CmdletBinding()] param([object]$AssociatedNetFirewallRule)
 [pscustomobject]@{LocalAddress=$AssociatedNetFirewallRule.LocalAddress;RemoteAddress=$AssociatedNetFirewallRule.RemoteAddress}
}
function New-NetFirewallRule {
 [CmdletBinding(SupportsShouldProcess)]
 param([string]$PolicyStore,[string]$Name,[string]$DisplayName,[string]$Description,[string]$Group,[string]$Enabled,[string]$Profile,[string]$Direction,[string]$Action,[string]$EdgeTraversalPolicy,[string[]]$LocalAddress,[string[]]$RemoteAddress,[string]$Protocol,[string[]]$LocalPort,[string[]]$RemotePort)
 if($PolicyStore -ne 'PersistentStore' -or $script:rules.ContainsKey($Name)) {throw 'Creación simulada inválida'}
 $script:writes+=('new:'+ $Name)
 $script:rules[$Name]=[pscustomobject]@{Name=$Name;DisplayName=$DisplayName;Description=$Description;Group=$Group;Enabled=$Enabled;Profile=$Profile;Direction=$Direction;Action=$Action;EdgeTraversalPolicy=$EdgeTraversalPolicy;LocalAddress=$LocalAddress;RemoteAddress=$RemoteAddress;Protocol=$Protocol;LocalPort=$LocalPort}
}
function Set-NetFirewallRule {
 [CmdletBinding(SupportsShouldProcess)]
 param([string]$PolicyStore,[string]$Name,[string]$NewDisplayName,[string]$Description,[string]$Group,[string]$Enabled,[string]$Profile,[string]$Direction,[string]$Action,[string]$EdgeTraversalPolicy,[string[]]$LocalAddress,[string[]]$RemoteAddress,[string]$Protocol,[string[]]$LocalPort,[string[]]$RemotePort)
 if($PolicyStore -ne 'PersistentStore' -or -not $script:rules.ContainsKey($Name)) {throw 'Actualización simulada inválida'}
 $script:writes+=('set:'+ $Name)
 $script:rules[$Name]=[pscustomobject]@{Name=$Name;DisplayName=$NewDisplayName;Description=$Description;Group=$Group;Enabled=$Enabled;Profile=$Profile;Direction=$Direction;Action=$Action;EdgeTraversalPolicy=$EdgeTraversalPolicy;LocalAddress=$LocalAddress;RemoteAddress=$RemoteAddress;Protocol=$Protocol;LocalPort=$LocalPort}
}
"""


class CortafuegosGpu(unittest.TestCase):
    def ejecutar(self, code):
        result = subprocess.run(
            [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; . $env:GPU_FIREWALL_TEST_SCRIPT; " + MOCKS + code,
            ],
            env=os.environ | {"GPU_FIREWALL_TEST_SCRIPT": str(SCRIPT)},
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_importacion_solo_define_funciones(self):
        self.assertEqual(self.ejecutar("'\"importado\"'"), "importado")

    def test_complemento_ipv4_exacto_preserva_clientes_y_loopback(self):
        contract = self.ejecutar(
            "Get-AgencyGpuFirewallContract | ConvertTo-Json -Depth 8 -Compress"
        )
        self.assertEqual(len(contract["Rules"]), 5)
        self.assertTrue(all(rule["LocalAddress"] == ["Any"] for rule in contract["Rules"]))
        blocked = contract["Rules"][0]
        self.assertEqual(blocked["Action"], "Block")
        intervals = [
            tuple(map(lambda s: int(ipaddress.IPv4Address(s)), item.split("-")))
            for item in blocked["RemoteAddress"]
        ]
        exclusions = [
            (int(ipaddress.IPv4Address("127.0.0.0")), int(ipaddress.IPv4Address("127.255.255.255")))
        ]
        exclusions += [
            (int(ipaddress.IPv4Address(f"192.168.0.{last}")),) * 2 for last in (7, 8, 85)
        ]
        segments = sorted(intervals + exclusions)
        cursor = 0
        for start, end in segments:
            self.assertEqual(start, cursor)
            self.assertGreaterEqual(end, start)
            cursor = end + 1
        self.assertEqual(cursor, 2**32)
        self.assertEqual(blocked["LocalPort"], ["3003", "8000", "8090", "9835"])

    def test_audit_predeterminado_no_muta_ni_exige_elevacion(self):
        result = self.ejecutar(
            "$script:isAdmin=$false; $r=Invoke-AgencyGpuFirewall; [pscustomobject]@{Mode=$r.Mode;Changes=$r.Changed;Planned=$r.PlannedChanges;Writes=$script:writes.Count}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Mode": "Audit", "Changes": 0, "Planned": 5, "Writes": 0})

    def test_apply_sin_elevacion_rechaza_antes_de_mutar(self):
        result = self.ejecutar(
            "$script:isAdmin=$false; try {Invoke-AgencyGpuFirewall -Mode Apply; throw 'NO_RECHAZO'} catch {$m=$_.Exception.Message}; [pscustomobject]@{Rejected=($m -like '*elevad*');Writes=$script:writes.Count}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Rejected": True, "Writes": 0})

    def test_whatif_previsualiza_sin_elevar_ni_muta(self):
        result = self.ejecutar(
            "$script:isAdmin=$false; $r=Invoke-AgencyGpuFirewall -Mode Apply -WhatIf; [pscustomobject]@{Changes=$r.Changed;Writes=$script:writes.Count;Verified=$r.Verified}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Changes": 0, "Writes": 0, "Verified": False})

    def test_colision_de_propiedad_rechaza_todo_antes_de_crear(self):
        for field in ("Group", "Description"):
            result = self.ejecutar(
                "$c=Get-AgencyGpuFirewallContract; $r=$c.Rules[4]; $script:rules[$r.Name]=$r; $r."
                + field
                + "='otro proyecto'; try {Invoke-AgencyGpuFirewall -Mode Apply; throw 'NO_RECHAZO'} catch {$m=$_.Exception.Message}; [pscustomobject]@{Rejected=($m -like '*ajena*');Writes=$script:writes.Count}|ConvertTo-Json -Compress"
            )
            self.assertEqual(result, {"Rejected": True, "Writes": 0})

    def test_apply_idempotente_bloquea_primero_y_no_toca_ajenas(self):
        result = self.ejecutar(
            "$script:rules['otro-proyecto']=[pscustomobject]@{Name='otro-proyecto';Group='ajeno';Description='conservar'}; $first=Invoke-AgencyGpuFirewall -Mode Apply -Confirm:$false; $second=Invoke-AgencyGpuFirewall -Mode Apply -Confirm:$false; [pscustomobject]@{First=$first.Changed;Second=$second.Changed;FirstWrite=$script:writes[0];Writes=$script:writes.Count;Foreign=$script:rules['otro-proyecto'].Description;Verified=$second.Verified}|ConvertTo-Json -Compress"
        )
        self.assertEqual(
            result,
            {
                "First": 5,
                "Second": 0,
                "FirstWrite": "new:AgencyGPU-Block-IPv4-v1",
                "Writes": 5,
                "Foreign": "conservar",
                "Verified": True,
            },
        )

    def test_deriva_propia_se_corrige_sin_recrear_reglas(self):
        result = self.ejecutar(
            "$null=Invoke-AgencyGpuFirewall -Mode Apply -Confirm:$false; $script:rules['AgencyGPU-Allow-TCP-8090-v1'].RemoteAddress=@('Any'); $r=Invoke-AgencyGpuFirewall -Mode Apply -Confirm:$false; [pscustomobject]@{Changed=$r.Changed;LastWrite=$script:writes[-1];Remote=$script:rules['AgencyGPU-Allow-TCP-8090-v1'].RemoteAddress}|ConvertTo-Json -Compress"
        )
        self.assertEqual(
            result,
            {
                "Changed": 1,
                "LastWrite": "set:AgencyGPU-Allow-TCP-8090-v1",
                "Remote": ["192.168.0.85", "192.168.0.7", "192.168.0.8"],
            },
        )

    def test_perfil_deshabilitado_rechaza_sin_cambiarlo(self):
        result = self.ejecutar(
            "$script:profileEnabled=$false; try {Invoke-AgencyGpuFirewall -Mode Apply; throw 'NO_RECHAZO'} catch {$m=$_.Exception.Message}; [pscustomobject]@{Rejected=($m -like '*perfil*');Writes=$script:writes.Count}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Rejected": True, "Writes": 0})

    def test_politica_efectiva_ausente_no_se_declara_verificada(self):
        result = self.ejecutar(
            "$script:activeMissing=$true; try {Invoke-AgencyGpuFirewall -Mode Apply -Confirm:$false; throw 'NO_RECHAZO'} catch {$m=$_.Exception.Message}; [pscustomobject]@{Rejected=($m -like '*efectiva*');Writes=$script:writes.Count}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Rejected": True, "Writes": 5})

    def test_guardia_de_rangos_rechaza_bloquear_loopback(self):
        result = self.ejecutar(
            "try {Test-AgencyGpuFirewallComplement -Ranges @('0.0.0.0-255.255.255.255'); throw 'NO_RECHAZO'} catch {$m=$_.Exception.Message}; [pscustomobject]@{Rejected=($m -like '*complemento*')}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Rejected": True})

    def test_nombre_ajeno_con_mayusculas_distintas_tambien_bloquea_todo(self):
        result = self.ejecutar(
            "$c=Get-AgencyGpuFirewallContract; $r=$c.Rules[4]; $r.Name=$r.Name.ToLowerInvariant(); $r.Group='ajeno'; $script:rules[$r.Name]=$r; try {Invoke-AgencyGpuFirewall -Mode Apply; throw 'NO_RECHAZO'} catch {$m=$_.Exception.Message}; [pscustomobject]@{Rejected=($m -like '*ajena*');Writes=$script:writes.Count}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Rejected": True, "Writes": 0})

    def test_colision_ajena_en_politica_efectiva_rechaza_antes_de_escribir(self):
        result = self.ejecutar(
            "$c=Get-AgencyGpuFirewallContract; $r=$c.Rules[4]; $r.Group='politica ajena'; $script:activeForeign=$r; try {Invoke-AgencyGpuFirewall -Mode Apply -Confirm:$false; throw 'NO_RECHAZO'} catch {$m=$_.Exception.Message}; [pscustomobject]@{Rejected=($m -like '*ajena*');Writes=$script:writes.Count}|ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"Rejected": True, "Writes": 0})


if __name__ == "__main__":
    unittest.main()
