<#
.SYNOPSIS
Registra únicamente el par vCenter/certificado previamente fijado en la bóveda.
.DESCRIPTION
Vuelve a observar el certificado sin autenticar y compara SHA256, SHA1 y vigencia.
La actualización del PCTCS se limita al usuario actual y conserva las demás filas.
No instala una CA global, no usa Ignore y no modifica certificados del vCenter.
El pin inicial ofrece confianza de primer uso; no acredita un cotejo independiente.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter()]
    [string] $VaultPath = (Join-Path $PSScriptRoot '../.env')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'media-vcenter-trust-format.ps1')
$requiredKeys = @('VSPHERE_SERVER', 'VSPHERE_CERT_SHA256', 'VSPHERE_CERT_SHA1')
$pins = @{}
foreach ($line in Get-Content -LiteralPath $VaultPath -Encoding utf8) {
    if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { continue }
    $key = $Matches[1]
    if ($key -notin $requiredKeys) { continue }
    if ($pins.ContainsKey($key)) { throw "Clave duplicada en la bóveda: $key" }
    $value = $Matches[2].Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    $pins[$key] = $value
}
if ($pins.VSPHERE_SERVER -cne '192.168.0.2' -or
    $pins.VSPHERE_CERT_SHA256 -notmatch '^[A-Fa-f0-9]{64}$' -or
    $pins.VSPHERE_CERT_SHA1 -notmatch '^[A-Fa-f0-9]{40}$') {
    throw 'La bóveda debe contener el servidor .2 y sus dos huellas reales respaldadas antes de configurar confianza.'
}
$observed = & (Join-Path $PSScriptRoot 'Get-VCenterCertificate.ps1') -Server $pins.VSPHERE_SERVER
if ($observed.Server -cne $pins.VSPHERE_SERVER -or
    $observed.ObservedSha256 -ine $pins.VSPHERE_CERT_SHA256 -or
    $observed.ObservedSha1 -ine $pins.VSPHERE_CERT_SHA1) {
    throw 'El certificado observado no coincide con las dos huellas de la bóveda; no se modifica la confianza.'
}
$nowUtc = [DateTimeOffset]::UtcNow
if ($nowUtc -lt [DateTimeOffset]::Parse($observed.NotBeforeUtc) -or
    $nowUtc -gt [DateTimeOffset]::Parse($observed.NotAfterUtc)) {
    throw 'El certificado fijado todavía no es válido o ya caducó; no se modifica la confianza.'
}
$applicationData = [Environment]::GetFolderPath('ApplicationData')
if ([string]::IsNullOrWhiteSpace($applicationData)) { throw 'No se pudo determinar el almacén del usuario actual.' }
$storeDirectory = Join-Path $applicationData 'VMware/PowerCLI'
$storePath = Join-Path $storeDirectory 'SslCertificateExceptions.csv'
$rows = @()
$nativeFormat = $true
if (Test-Path -LiteralPath $storePath) {
    $storeFile = Get-Item -LiteralPath $storePath
    if ($storeFile.PSIsContainer -or ($storeFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'El PCTCS debe ser un archivo regular del usuario; no se sigue un enlace ni se escribe un directorio.'
    }
    $storeState = ConvertFrom-MediaTrustStore -Lines @(Get-Content -LiteralPath $storePath -Encoding utf8)
    $rows = @($storeState.Records)
    $nativeFormat = $storeState.IsNative
}
$targetRows = @($rows | Where-Object server_address -CEQ $pins.VSPHERE_SERVER)
$alreadyRegistered = $nativeFormat -and $targetRows.Count -eq 1 -and $targetRows[0].certificate_thumbprint -ieq $pins.VSPHERE_CERT_SHA1
$changed = $false
if (-not $alreadyRegistered -and $PSCmdlet.ShouldProcess($storePath, 'Registrar solamente el par 192.168.0.2/certificado fijado y conservar los demás servidores')) {
    $remainingRows = @($rows | Where-Object server_address -CNE $pins.VSPHERE_SERVER)
    $newRow = [pscustomobject]@{
        server_address = $pins.VSPHERE_SERVER
        certificate_thumbprint = $pins.VSPHERE_CERT_SHA1.ToUpperInvariant()
    }
    $newRows = @($remainingRows) + @($newRow)
    $csvLines = @(ConvertTo-MediaTrustStore -Records $newRows)
    [IO.Directory]::CreateDirectory($storeDirectory) | Out-Null
    $temporaryPath = Join-Path $storeDirectory ('.SslCertificateExceptions.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        # El formato nativo no lleva encabezado, comillas ni BOM antes del servidor.
        [IO.File]::WriteAllLines($temporaryPath, $csvLines, [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temporaryPath, $storePath, $true)
        $changed = $true
    }
    finally {
        if ([IO.File]::Exists($temporaryPath)) { [IO.File]::Delete($temporaryPath) }
    }
}
[pscustomobject]@{
    Server = $pins.VSPHERE_SERVER
    CertificateMatchesVault = $true
    CertificateValidNow = $true
    StorePath = $storePath
    Changed = $changed
    AlreadyRegistered = $alreadyRegistered
    Simulation = [bool] $WhatIfPreference
    OtherRowsPreserved = @($rows | Where-Object server_address -CNE $pins.VSPHERE_SERVER).Count
    TrustBasis = 'Pin de primer uso respaldado y reobservado; no validado por un canal independiente.'
    NextAction = 'Ejecutar 01-deploy-media-vm.ps1 -AuditOnly; PowerCLI mantendrá InvalidCertificateAction=Fail.'
}
