#requires -Version 7.4
<#
.SYNOPSIS
Valida el arranque privado del puente; -Start inicia el supervisor oculto de forma explícita.
#>
[CmdletBinding()]
param(
    [string]$RuntimeConfigPath = 'C:\GPU-Bridge\runtime.json',
    [switch]$Start
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Stop-GpuBridge.ps1')

function Assert-PrivatePath {
    param([Parameter(Mandatory)][string]$LiteralPath)
    $item = Get-Item -LiteralPath $LiteralPath -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'No se admiten enlaces ni puntos de reanálisis.'
    }
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowed = @($currentSid, 'S-1-5-18', 'S-1-5-32-544')
    $acl = Get-Acl -LiteralPath $LiteralPath
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowed) {
        throw 'El propietario no corresponde a la instalación privada.'
    }
    $unexpected = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) |
        Where-Object { $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            $_.IdentityReference.Value -notin $allowed })
    if ($unexpected.Count -gt 0) {
        throw 'La instalación concede acceso a otra identidad.'
    }
}

function Get-BridgeProcessInfo {
    param([string]$Python, [string]$Launcher, [string]$ConfigFile, [switch]$Check)
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Python
    $info.WorkingDirectory = Split-Path -Parent $Launcher
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.ArgumentList.Add('-I')
    $info.ArgumentList.Add($Launcher)
    $info.ArgumentList.Add('supervisor')
    if ($Check) {
        $info.ArgumentList.Add('--check')
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
    }
    $info.Environment.Clear()
    $permitted = @('PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
        'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PROGRAMDATA', 'PROGRAMFILES',
        'PROGRAMFILES(X86)', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE',
        'USERNAME', 'USERDOMAIN', 'HOMEDRIVE', 'HOMEPATH', 'SYSTEMDRIVE')
    foreach ($entry in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
        if ($entry.Key.ToUpperInvariant() -in $permitted) {
            $info.Environment[$entry.Key] = [string]$entry.Value
        }
    }
    $info.Environment['GPU_RUNTIME_CONFIG_PATH'] = $ConfigFile
    $info.Environment['PYTHONUTF8'] = '1'
    $info.Environment['PYTHONNOUSERSITE'] = '1'
    return $info
}

try {
    if (-not $IsWindows) { throw 'Se requiere Windows nativo.' }
    if ($env:COMPUTERNAME -ine 'W11-HOMELAB') { throw 'El host no corresponde al puente GPU propio.' }
    $configFile = [IO.Path]::GetFullPath($RuntimeConfigPath)
    Assert-PrivatePath -LiteralPath $configFile
    if ((Get-Item -LiteralPath $configFile).Length -gt 65536) { throw 'Configuración excesiva.' }
    $config = Get-Content -LiteralPath $configFile -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
    $installRoot = [IO.Path]::GetFullPath([string]$config['GPU_INSTALL_ROOT'])
    if ($configFile -ne (Join-Path $installRoot 'runtime.json')) { throw 'Ruta de configuración ajena.' }
    $services = Join-Path $installRoot 'services'
    $logs = Join-Path $installRoot 'logs'
    $python = Join-Path $installRoot 'metrics\venv\Scripts\python.exe'
    $launcher = Join-Path $services 'launch_service.py'
    if ([string]$config['GPU_METRICS_PYTHON_PATH'] -ne $python) { throw 'Intérprete ajeno.' }
    foreach ($path in @($installRoot, $services, $logs, $python, $launcher)) {
        Assert-PrivatePath -LiteralPath $path
    }
    foreach ($name in @('supervisor', 'immich', 'whisper', 'render')) {
        $logFile = Join-Path $logs ($name + '.log')
        if (Test-Path -LiteralPath $logFile) { Assert-PrivatePath -LiteralPath $logFile }
    }
    $check = [Diagnostics.Process]::new()
    try {
        $check.StartInfo = Get-BridgeProcessInfo -Python $python -Launcher $launcher -ConfigFile $configFile -Check
        if (-not $check.Start()) { throw 'No se pudo ejecutar la validación.' }
        $outputTask = $check.StandardOutput.ReadToEndAsync()
        $errorTask = $check.StandardError.ReadToEndAsync()
        if (-not $check.WaitForExit(15000)) {
            $check.Kill()
            $check.WaitForExit()
            throw 'La validación agotó su plazo.'
        }
        if ($check.ExitCode -ne 0) { throw 'La validación del lanzador fue rechazada.' }
        $result = $outputTask.GetAwaiter().GetResult() | ConvertFrom-Json -AsHashtable
        $null = $errorTask.GetAwaiter().GetResult()
        if ($result['valid'] -ne $true) { throw 'La validación no acreditó los archivos propios.' }
    }
    finally { $check.Dispose() }
    if (-not $Start) {
        [pscustomobject]@{ Valid = $true; Started = $false; Services = @('immich', 'whisper', 'render') }
        return
    }
    # Verificar la ruta del recibo y la imagen base respaldadas antes de iniciar nada.
    $receiptContext = Get-AgencyGpuStopContext
    # Comprobar disponibilidad no adopta ni termina ningún proceso que ya escuche.
    $reservation = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, 9835)
    try {
        $reservation.Server.ExclusiveAddressUse = $true
        $reservation.Start()
    }
    finally { $reservation.Stop() }
    $process = [Diagnostics.Process]::new()
    try {
        $process.StartInfo = Get-BridgeProcessInfo -Python $python -Launcher $launcher -ConfigFile $configFile
        if (-not $process.Start()) { throw 'No se pudo iniciar el supervisor.' }
        if ($process.WaitForExit(1000)) { throw 'El supervisor terminó durante el arranque.' }
        $receipt = Wait-AgencyGpuStartReceipt -Context $receiptContext -LauncherProcessId $process.Id -LauncherCreationFileTimeUtc ([string]$process.StartTime.ToFileTimeUtc())
        [pscustomobject]@{ Valid = $true; Started = $true; ProcessId = $receipt.ProcessId; LauncherPid = $process.Id; ReceiptPath = $receiptContext.ReceiptPath; Readiness = 'pending' }
    }
    finally { $process.Dispose() }
}
catch {
    [Console]::Error.WriteLine('Se detuvo el arranque del puente; revise la configuración privada y los registros propios.')
    exit 2
}
