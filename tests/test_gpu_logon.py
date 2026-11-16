"""Comprueba la reconexión de P: y el diagnóstico de inicio del puente GPU."""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "gpu-bridge-win11/Start-GpuBridgeAtLogon.ps1"
HARNESS = r"""
$ErrorActionPreference = 'Stop'
$source = [IO.File]::ReadAllText($env:GPU_LOGON_TEST_SCRIPT)
$begin = $source.IndexOf('$mappingReady = $false', [StringComparison]::Ordinal)
$end = $source.IndexOf('$startedByTask = $false', [StringComparison]::Ordinal)
if ($begin -lt 0 -or $end -le $begin) { throw 'No se encontró la espera de P: en el controlador.' }
$script:reads = 0
$script:directoryReads = 0
$script:recreated = 0
$script:removed = 0
$script:fixture = $env:GPU_LOGON_TEST_CASE
$context = @{ InstallRoot='C:\GPU-Bridge'; UserSid='usuario-de-prueba' }
function Join-Path {
    param([string]$Path, [string]$ChildPath)
    if ($Path -ine 'C:\GPU-Bridge' -or $ChildPath -cne 'smb-logon-credential.xml') {
        throw 'Ruta de credencial inesperada.'
    }
    return "$Path\$ChildPath"
}
function Get-SmbMapping {
    param([string]$LocalPath, $ErrorAction)
    if ($LocalPath -ne 'P:') { throw 'Unidad inesperada' }
    $script:reads++
    if ($script:fixture -eq 'perfil_sin_mapeo' -and $script:recreated -eq 0) { return }
    $wrong = $script:fixture -eq 'otro_recurso' -or
        ($script:fixture -eq 'cambia_recurso' -and $script:directoryReads -gt 0)
    $legacy = $script:fixture -in @('perfil_persistente', 'perfil_ajeno', 'credencial_ajena') -and
        $script:recreated -eq 0
    [pscustomobject]@{
        RemotePath = if ($wrong) { '\\192.168.0.8\otro' } else { '\\192.168.0.8\producciones' }
        RequirePrivacy = if ($legacy) { $null } else { $script:fixture -ne 'sin_cifrado' }
        Status = if ($legacy) { 6 } elseif ($script:directoryReads -gt 0) { 'OK' } else { 'Unavailable' }
    }
}
function Get-ItemProperty {
    param([string]$LiteralPath, $ErrorAction)
    if ($LiteralPath -ne 'HKCU:\Network\P') { throw 'Perfil inesperado' }
    $target = if ($script:fixture -eq 'perfil_ajeno') { '\\192.168.0.8\otro' } else { '\\192.168.0.8\producciones' }
    [pscustomobject]@{ RemotePath=$target }
}
function Assert-AgencyGpuPrivatePath {
    param([string]$LiteralPath, [string]$UserSid, [string]$InstallRoot)
    if ($LiteralPath -ine 'C:\GPU-Bridge\smb-logon-credential.xml' -or $UserSid -cne 'usuario-de-prueba' -or
        $InstallRoot -ine 'C:\GPU-Bridge') { throw 'Ruta privada inesperada' }
}
function Import-Clixml {
    param([string]$LiteralPath)
    if ($script:fixture -eq 'credencial_ajena') { return 'valor ajeno' }
    return [pscredential]::new('ensayo', [Security.SecureString]::new())
}
function Remove-SmbMapping {
    param($InputObject, [switch]$Confirm)
    if ($InputObject.RemotePath -ine '\\192.168.0.8\producciones' -or $InputObject.Status -ne 6 -or
        $InputObject.RequirePrivacy -ne $null) { throw 'Retiro no admitido' }
    $script:removed++
}
function New-SmbMapping {
    param($LocalPath, $RemotePath, $Credential, [bool]$Persistent, [bool]$RequirePrivacy, [switch]$Confirm)
    if ($LocalPath -ne 'P:' -or $RemotePath -ine '\\192.168.0.8\producciones' -or
        !$Persistent -or !$RequirePrivacy -or $Credential -isnot [pscredential]) { throw 'Mapeo no admitido' }
    $script:recreated++
}
function Test-Path {
    param([string]$LiteralPath, [string]$PathType, $ErrorAction)
    if ($LiteralPath -ne 'P:\proyectos' -or $PathType -ne 'Container') { throw 'Consulta de ruta inesperada' }
    $script:directoryReads++
    return $true
}
function Start-Sleep { param([int]$Seconds) throw 'REINTENTO' }
$ready = $false
try { . ([scriptblock]::Create($source.Substring($begin, $end - $begin))); $ready = $mappingReady } catch { }
@{ listo = $ready; lecturas = $script:reads; accesos = $script:directoryReads
   reconstrucciones = $script:recreated; retiros = $script:removed } | ConvertTo-Json -Compress
"""


@pytest.mark.parametrize(
    ("caso", "resultado"),
    [
        ("desconectado", (True, 2, 1, 0, 0)),
        ("sin_cifrado", (False, 1, 0, 0, 0)),
        ("otro_recurso", (False, 1, 0, 0, 0)),
        ("cambia_recurso", (False, 2, 1, 0, 0)),
        ("perfil_persistente", (True, 3, 1, 1, 1)),
        ("perfil_sin_mapeo", (True, 3, 1, 1, 0)),
        ("perfil_ajeno", (False, 1, 0, 0, 0)),
        ("credencial_ajena", (False, 1, 0, 0, 0)),
    ],
)
def test_el_arranque_solo_acepta_el_recurso_cifrado_reconectado(caso, resultado):
    execution = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", HARNESS],
        env=os.environ | {"GPU_LOGON_TEST_SCRIPT": str(SCRIPT), "GPU_LOGON_TEST_CASE": caso},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert execution.returncode == 0, execution.stderr
    fields = ("listo", "lecturas", "accesos", "reconstrucciones", "retiros")
    assert json.loads(execution.stdout) == dict(zip(fields, resultado, strict=True))


LAUNCH_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$source = [IO.File]::ReadAllText($env:GPU_LOGON_TEST_SCRIPT)
$begin = $source.IndexOf('$startedByTask = $false', [StringComparison]::Ordinal)
$end = $source.IndexOf('$running = Invoke-AgencyGpuStop', [StringComparison]::Ordinal)
if ($begin -lt 0 -or $end -le $begin) { throw 'No se encontró la comprobación de arranque.' }
function Get-NetTCPConnection {
    param($State, $LocalPort, $ErrorAction)
    if ($State -ne 'Listen' -or $LocalPort -ne 9835) { throw 'Sonda inesperada.' }
    if ($env:GPU_LOGON_TEST_LISTENER -eq '1') { return [pscustomobject]@{LocalPort=9835} }
}
function Get-CimInstance {
    param($ClassName, $Filter, $OperationTimeoutSec, $ErrorAction)
    if ($ClassName -ne 'Win32_PnPEntity' -or $Filter -ne "PNPClass='Display' AND Present=True") {
        throw 'Consulta de dispositivos inesperada.'
    }
    [pscustomobject]@{
        Name='NVIDIA GeForce RTX 3060 Ti'
        DeviceID='PCI\VEN_10DE&DEV_2486\ENSAYO'
        ConfigManagerErrorCode=[int]$env:GPU_LOGON_TEST_PROBLEM
    }
    [pscustomobject]@{
        Name='Otro adaptador'
        DeviceID='PCI\VEN_15AD&DEV_0405\ENSAYO'
        ConfigManagerErrorCode=43
    }
}
function Join-Path {
    param([AllowEmptyString()]$Path, $ChildPath)
    if ($ChildPath -ne 'start-gpu-bridge.ps1') { throw 'Lanzador inesperado.' }
    return { param([switch]$Start) [pscustomobject]@{Valid=$Start.IsPresent; Started=$Start.IsPresent} }
}
. ([scriptblock]::Create($source.Substring($begin, $end - $begin)))
@{ iniciado=$startedByTask } | ConvertTo-Json -Compress
"""


@pytest.mark.parametrize(
    ("codigo", "listener", "esperado"), [(43, False, 43), (0, False, 0), (43, True, 0)]
)
def test_el_arranque_identifica_codigo_43_antes_de_lanzar(codigo, listener, esperado):
    execution = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", LAUNCH_HARNESS],
        env=os.environ
        | {
            "GPU_LOGON_TEST_SCRIPT": str(SCRIPT),
            "GPU_LOGON_TEST_PROBLEM": str(codigo),
            "GPU_LOGON_TEST_LISTENER": str(int(listener)),
        },
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert execution.returncode == esperado, execution.stderr
    if esperado == 43:
        assert "Windows informa el error 43" in execution.stderr and not execution.stdout
    else:
        assert json.loads(execution.stdout) == {"iniciado": not listener}
