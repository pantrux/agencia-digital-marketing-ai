$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.tools\venvs\synology-audit\Scripts\pythonw.exe'
$script = Join-Path $PSScriptRoot 'audit_studio_kit_retention.py'
if (!(Test-Path -LiteralPath $python -PathType Leaf) -or
    !(Test-Path -LiteralPath $script -PathType Leaf)) {
    throw 'Falta el entorno de retención de Studio.'
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$name = 'Agency-Studio-KitRetention'
$action = New-ScheduledTaskAction -Execute $python -Argument (
    '-I -B "{0}" --retirar-vencidos --registrar' -f $script
) -WorkingDirectory $root
$daily = New-ScheduledTaskTrigger -Daily -At '06:15'
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$description = 'Retiene los ZIP publicados según su plan en Studio.'
Register-ScheduledTask -TaskName $name -Action $action -Trigger @($daily, $logon) -Principal $principal -Settings $settings -Description $description -Force
