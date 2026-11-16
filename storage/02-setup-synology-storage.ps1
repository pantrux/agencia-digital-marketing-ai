<#
.SYNOPSIS
Audita y prepara Synology DSM con respaldo previo y plan verificable.
.DESCRIPTION
El comportamiento predeterminado es auditoría. Plan consulta el estado sin cambios;
Backup y Apply son pasos explícitos que exigen el hash del plan revisado. Apply
también exige el SHA256 del respaldo ya persistido en la bóveda. La contraseña
solo llega a sudo por stdin, nunca por argumentos, entorno o logs. No monta NFS.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string] $VaultPath = (Join-Path $PSScriptRoot '../.env'),

    [Parameter()]
    [string] $OutputPath = (Join-Path $PSScriptRoot '../.tools/evidence/synology-storage-operation.json'),

    [Parameter()]
    [ValidateSet('Audit', 'Plan', 'Backup', 'Apply')]
    [string] $Mode = 'Audit',

    [Parameter()]
    [string] $PlanPath = (Join-Path $PSScriptRoot '../.tools/evidence/synology-storage-plan.json'),

    [Parameter()]
    [ValidatePattern('^$|^[a-f0-9]{64}$')]
    [string] $ExpectedPlanSha256 = '',

    [Parameter()]
    [ValidateSet('Password', 'Key')]
    [string] $Authentication = 'Key',

    [Parameter()]
    [ValidateRange(2, 30)]
    [int] $TimeoutSeconds = 30,

    [Parameter()]
    [string] $PythonPath = 'python',

    [Parameter()]
    [switch] $AuditOnly
)

$ErrorActionPreference = 'Stop'
if ($AuditOnly -and $Mode -ne 'Audit') { throw 'AuditOnly no puede combinarse con modos de preparación o aplicación.' }
if ($Mode -eq 'Audit') {
    $auditArguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'synology_storage_audit.py'),
        '--vault-path', $VaultPath, '--output', $OutputPath,
        '--auth-method', $Authentication.ToLowerInvariant(), '--timeout', [string] $TimeoutSeconds,
        '--audit-only')
}
else {
    if ($Authentication -ne 'Key') { throw 'Plan, Backup y Apply exigen la identidad SSH canónica respaldada.' }
    if ($Mode -in @('Backup', 'Apply') -and -not $ExpectedPlanSha256) { throw 'Falta el hash del plan revisado.' }
    $auditArguments = @('-X', 'utf8', (Join-Path $PSScriptRoot 'synology_storage_apply.py'),
        "--$($Mode.ToLowerInvariant())", '--vault-path', $VaultPath, '--output', $OutputPath,
        '--plan-path', $PlanPath, '--timeout', [string] $TimeoutSeconds)
    if ($ExpectedPlanSha256) { $auditArguments += @('--expected-plan-sha256', $ExpectedPlanSha256) }
}
& $PythonPath @auditArguments
exit $LASTEXITCODE
