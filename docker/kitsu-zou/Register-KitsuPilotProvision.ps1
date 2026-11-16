[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$name = 'Agency-Kitsu-Pilot-Provision'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $root '.tools\venvs\synology-audit\Scripts\pythonw.exe'
$provision = Join-Path $PSScriptRoot 'provision_order.py'
if (!(Test-Path -LiteralPath $python -PathType Leaf) -or
    !(Test-Path -LiteralPath $provision -PathType Leaf)) {
    throw 'Falta el entorno de aprovisionamiento de Kitsu.'
}

$arguments = '-B "{0}" --pendientes' -f $provision
$task = Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
if (!$task) {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $root
    $time = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 12)
    if ($PSCmdlet.ShouldProcess($name, 'Registrar el aprovisionamiento de Kitsu')) {
        Register-ScheduledTask -TaskPath '\' -TaskName $name -Action $action -Trigger @($time, $logon) `
            -Principal $principal -Settings $settings -Description 'Aprovisiona Kitsu para órdenes elegibles de Studio.'
    }
    return
}

$actions = @($task.Actions)
$periodic = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' })
if ($actions.Count -ne 1 -or $actions[0].Execute -ne $python -or
    $actions[0].Arguments -ne $arguments -or $actions[0].WorkingDirectory -ne $root -or
    $periodic.Count -ne 1 -or !$periodic[0].Repetition.Interval) {
    throw 'La tarea instalada difiere del aprovisionador esperado.'
}
if ($periodic[0].Repetition.Interval -eq 'PT1M') { return }
if ($PSCmdlet.ShouldProcess($name, 'Actualizar el intervalo a un minuto')) {
    $periodic[0].Repetition.Interval = 'PT1M'
    Set-ScheduledTask -TaskPath '\' -TaskName $name -Trigger $task.Triggers
}
