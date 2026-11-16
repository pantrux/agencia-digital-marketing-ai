[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-f0-9]{64}$')]
    [string]$ArchiverSha256
)

$ErrorActionPreference = 'Stop'
$name = 'Agency-Vault-Queue-Capacity'
$root = Split-Path -Parent $PSScriptRoot
$archiver = Join-Path $PSScriptRoot 'archive_vault_changes_v2.py'
$watcher = Get-ScheduledTask -TaskPath '\' -TaskName 'Agency-Vault-Changes-V1'
if ($watcher.Principal.LogonType.ToString() -ne 'Interactive' -or
    $watcher.Principal.RunLevel.ToString() -ne 'Limited' -or
    @($watcher.Actions).Count -ne 1 -or
    (Split-Path -Leaf $watcher.Actions[0].Execute) -ne 'pythonw.exe') {
    throw 'La identidad del observador de la bóveda difiere.'
}
$launcher = $watcher.Actions[0].Execute
$python = Join-Path (Split-Path -Parent $launcher) 'python.exe'
if (!(Test-Path -LiteralPath $launcher -PathType Leaf) -or
    !(Test-Path -LiteralPath $python -PathType Leaf) -or
    (Get-FileHash -LiteralPath $archiver -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ArchiverSha256) {
    throw 'Falta el intérprete o la huella del archivador difiere.'
}

$pathLiteral = ConvertTo-Json -InputObject $archiver -Compress
$loader = @"
import hashlib, sys
from pathlib import Path
path = Path($pathLiteral)
source = path.read_bytes()
if hashlib.sha256(source).hexdigest() != '$ArchiverSha256':
    raise SystemExit(2)
sys.argv = [str(path), 'maintain', '--limit', '4']
exec(compile(source, str(path), 'exec'), {'__file__': str(path), '__name__': '__main__'})
"@
$payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($loader))
$pythonLiteral = ConvertTo-Json -InputObject $python -Compress
$launch = @"
import subprocess
loader = "import base64;exec(compile(base64.b64decode('$payload'), '<cuota>', 'exec'))"
raise SystemExit(subprocess.call(
    [$pythonLiteral, '-I', '-S', '-B', '-c', loader],
    creationflags=subprocess.CREATE_NO_WINDOW,
))
"@
$launchPayload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($launch))
$arguments = '-I -S -B -c "import base64;exec(base64.b64decode(''{0}''))"' -f $launchPayload
$action = New-ScheduledTaskAction -Execute $launcher -Argument $arguments -WorkingDirectory $root
$legacyPowerShell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$existing = Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
if ($existing) {
    if (@($existing.Actions).Count -ne 1 -or
        $existing.Actions[0].Execute -cnotin @($legacyPowerShell, $launcher) -or
        $existing.Principal.UserId -cne $watcher.Principal.UserId -or
        $existing.Principal.LogonType.ToString() -ne 'Interactive' -or
        $existing.Principal.RunLevel.ToString() -ne 'Limited') {
        throw 'La tarea de mantenimiento existente tiene otra identidad.'
    }
    if ($existing.Actions[0].Execute -ceq $launcher -and
        $existing.Actions[0].Arguments -ceq $arguments -and
        $existing.Actions[0].WorkingDirectory -ceq $root) { return }
    if ($PSCmdlet.ShouldProcess($name, 'Actualizar el ejecutable del archivador')) {
        Set-ScheduledTask -TaskName $name -TaskPath '\' -Action $action
    }
    return
}
$time = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $watcher.Principal.UserId
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
if ($PSCmdlet.ShouldProcess($name, 'Registrar el archivo preventivo de la bóveda')) {
    Register-ScheduledTask -TaskName $name -TaskPath '\' -Action $action -Trigger @($time, $logon) `
        -Principal $watcher.Principal -Settings $settings -Force `
        -Description 'Archiva capturas de la bóveda con acuse de R2 al superar la reserva local.'
}
