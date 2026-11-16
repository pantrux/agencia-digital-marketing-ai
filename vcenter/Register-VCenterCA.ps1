<#
.SYNOPSIS
Instala exclusivamente la CA de vCenter respaldada y verificada en el almacén del usuario.
.DESCRIPTION
Compara DER, SHA256 y SHA1 de la bóveda; reobserva el certificado de máquina y
verifica su cadena local con la CA fijada antes de importar CurrentUser/Root.
No modifica LocalMachine, no usa Ignore y no autentica contra vCenter.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter()]
    [string] $VaultPath = (Join-Path $PSScriptRoot '../.env'),

    [Parameter()]
    [string] $EvidenceDirectory = (Join-Path $PSScriptRoot '../.tools/evidence')
)

$ErrorActionPreference = 'Stop'
$requiredKeys = @(
    'VSPHERE_SERVER', 'VSPHERE_CERT_SHA256', 'VSPHERE_CERT_SHA1',
    'VSPHERE_CA_CERT_DER_B64', 'VSPHERE_CA_CERT_SHA256', 'VSPHERE_CA_CERT_SHA1'
)
$pins = @{}
foreach ($line in Get-Content -LiteralPath $VaultPath -Encoding utf8) {
    if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { continue }
    $key = $Matches[1]
    if ($key -notin $requiredKeys) { continue }
    if ($pins.ContainsKey($key)) { throw "Clave duplicada en la bóveda: $key" }
    $pins[$key] = $Matches[2].Trim().Trim('"').Trim("'")
}
if ($pins.VSPHERE_SERVER -cne '192.168.0.2') { throw 'Solo se permite la identidad de vCenter .2.' }
foreach ($key in @('VSPHERE_CERT_SHA256', 'VSPHERE_CA_CERT_SHA256')) {
    if ($pins[$key] -notmatch '^[A-Fa-f0-9]{64}$') { throw "Falta la huella real $key respaldada." }
}
foreach ($key in @('VSPHERE_CERT_SHA1', 'VSPHERE_CA_CERT_SHA1')) {
    if ($pins[$key] -notmatch '^[A-Fa-f0-9]{40}$') { throw "Falta la huella real $key respaldada." }
}
try { $caBytes = [Convert]::FromBase64String($pins.VSPHERE_CA_CERT_DER_B64) }
catch { throw 'La CA DER Base64 respaldada no es válida.' }
$ca = [Security.Cryptography.X509Certificates.X509Certificate2]::new([byte[]] $caBytes)
$leaf = $null
$chain = [Security.Cryptography.X509Certificates.X509Chain]::new()
try {
    if ($ca.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256) -ine $pins.VSPHERE_CA_CERT_SHA256 -or
        $ca.Thumbprint -ine $pins.VSPHERE_CA_CERT_SHA1) {
        throw 'La CA DER no coincide con las dos huellas respaldadas; no se importa confianza.'
    }
    $constraints = @($ca.Extensions | Where-Object { $_ -is [Security.Cryptography.X509Certificates.X509BasicConstraintsExtension] })
    if ($constraints.Count -ne 1 -or -not $constraints[0].CertificateAuthority -or $ca.Subject -cne $ca.Issuer) {
        throw 'El certificado respaldado debe ser la CA raíz del laboratorio.'
    }
    $observed = & (Join-Path $PSScriptRoot 'Get-VCenterCertificate.ps1') -Server $pins.VSPHERE_SERVER
    if ($observed.ObservedSha256 -ine $pins.VSPHERE_CERT_SHA256 -or $observed.ObservedSha1 -ine $pins.VSPHERE_CERT_SHA1) {
        throw 'El certificado de máquina actual no coincide con el pin de primer uso respaldado.'
    }
    $leafPath = Join-Path $EvidenceDirectory 'vcenter-leaf-pinned.cer'
    $leaf = [Security.Cryptography.X509Certificates.X509Certificate2]::new([IO.File]::ReadAllBytes([IO.Path]::GetFullPath($leafPath)))
    if ($leaf.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256) -ine $pins.VSPHERE_CERT_SHA256 -or
        $leaf.Thumbprint -ine $pins.VSPHERE_CERT_SHA1) {
        throw 'El certificado público local no coincide con el observado y respaldado.'
    }
    $chain.ChainPolicy.TrustMode = [Security.Cryptography.X509Certificates.X509ChainTrustMode]::CustomRootTrust
    $chain.ChainPolicy.CustomTrustStore.Add($ca) | Out-Null
    $chain.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
    $chain.ChainPolicy.VerificationFlags = [Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
    $chain.ChainPolicy.DisableCertificateDownloads = $true
    if (-not $chain.Build($leaf) -or $chain.ChainElements[$chain.ChainElements.Count - 1].Certificate.Thumbprint -ine $ca.Thumbprint) {
        throw 'La CA respaldada no verifica criptográficamente el certificado de máquina fijado.'
    }
    $store = [Security.Cryptography.X509Certificates.X509Store]::new('Root', 'CurrentUser')
    try {
        $store.Open([Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
        $existing = @($store.Certificates | Where-Object Thumbprint -EQ $ca.Thumbprint)
        $alreadyRegistered = $existing.Count -eq 1 -and $existing[0].GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256) -ieq $pins.VSPHERE_CA_CERT_SHA256
    }
    finally { $store.Dispose() }
    $changed = $false
    if (-not $alreadyRegistered -and $PSCmdlet.ShouldProcess('Cert:\CurrentUser\Root', 'Importar exclusivamente la CA respaldada que verifica el certificado fijado de vCenter')) {
        $caPath = Join-Path ([IO.Path]::GetFullPath($EvidenceDirectory)) 'vcenter-ca-from-vault.cer'
        [IO.File]::WriteAllBytes($caPath, $ca.RawData)
        $imported = Import-Certificate -FilePath $caPath -CertStoreLocation 'Cert:\CurrentUser\Root' -ErrorAction Stop
        if ($imported.Thumbprint -ine $pins.VSPHERE_CA_CERT_SHA1) { throw 'La importación no devolvió la huella esperada.' }
        $changed = $true
    }
    [pscustomobject]@{
        Server = $pins.VSPHERE_SERVER
        MachineCertificateMatchesVault = $true
        CryptographicChainValid = $true
        RevocationChecked = $false
        CertificateSha256 = $ca.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256)
        Store = 'Cert:\CurrentUser\Root'
        AlreadyRegistered = $alreadyRegistered
        Changed = $changed
        Simulation = [bool] $WhatIfPreference
        TrustBasis = 'CA obtenida mediante pin de primer uso y verificada como emisora del certificado fijado; sin cotejo independiente.'
        TrustScope = 'Almacén del usuario actual: certificados válidos emitidos por esta CA del laboratorio.'
        NextAction = 'Ejecutar auditoría vCenter con InvalidCertificateAction=Fail y registrar la evidencia real.'
    }
}
finally {
    $chain.Dispose()
    $ca.Dispose()
    if ($leaf) { $leaf.Dispose() }
}
