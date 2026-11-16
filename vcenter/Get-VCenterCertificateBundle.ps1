<#
.SYNOPSIS
Descarga las CA públicas de vCenter usando el pin del certificado de máquina respaldado.
.DESCRIPTION
El cliente HTTP acepta exclusivamente las dos huellas de .env, la identidad .2 y
la vigencia del certificado. No envía credenciales ni modifica almacenes de confianza.
Después construye la cadena del certificado fijado con CustomRootTrust, valida
firmas y guarda únicamente los certificados que forman esa cadena.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string] $VaultPath = (Join-Path $PSScriptRoot '../.env'),

    [Parameter()]
    [string] $EvidenceDirectory = (Join-Path $PSScriptRoot '../.tools/evidence')
)

$ErrorActionPreference = 'Stop'
$pins = @{}
foreach ($line in Get-Content -LiteralPath $VaultPath -Encoding utf8) {
    if ($line -match '^\s*(VSPHERE_SERVER|VSPHERE_CERT_SHA256|VSPHERE_CERT_SHA1)\s*=(.*)$') {
        if ($pins.ContainsKey($Matches[1])) { throw 'La bóveda contiene una clave de pin duplicada.' }
        $pins[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
    }
}
if ($pins.VSPHERE_SERVER -cne '192.168.0.2' -or
    $pins.VSPHERE_CERT_SHA256 -notmatch '^[A-Fa-f0-9]{64}$' -or
    $pins.VSPHERE_CERT_SHA1 -notmatch '^[A-Fa-f0-9]{40}$') {
    throw 'Se requiere el endpoint .2 y ambas huellas reales respaldadas en la bóveda.'
}
if (-not ('MediaVCenterPinnedDownload' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Net;
using System.Net.Http;
using System.Net.Security;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

public sealed class MediaVCenterPinnedDownloadResult
{
    public byte[] Bundle;
    public byte[] Leaf;
}

public static class MediaVCenterPinnedDownload
{
    // La excepción de cadena se sustituye por un pin exacto únicamente en este cliente.
    public static MediaVCenterPinnedDownloadResult Download(string sha256, string sha1)
    {
        var result = new MediaVCenterPinnedDownloadResult();
        using (var handler = new HttpClientHandler())
        {
            handler.AllowAutoRedirect = false;
            handler.UseProxy = false;
            handler.ServerCertificateCustomValidationCallback = (request, certificate, chain, errors) =>
            {
                if (certificate == null || request.RequestUri.Host != "192.168.0.2") return false;
                if ((errors & SslPolicyErrors.RemoteCertificateNameMismatch) != 0) return false;
                var now = DateTime.UtcNow;
                if (now < certificate.NotBefore.ToUniversalTime() || now > certificate.NotAfter.ToUniversalTime()) return false;
                if (!certificate.GetCertHashString(HashAlgorithmName.SHA256).Equals(sha256, StringComparison.OrdinalIgnoreCase)) return false;
                if (!certificate.Thumbprint.Equals(sha1, StringComparison.OrdinalIgnoreCase)) return false;
                result.Leaf = certificate.RawData;
                return true;
            };
            using (var client = new HttpClient(handler))
            {
                client.Timeout = TimeSpan.FromSeconds(15);
                client.MaxResponseContentBufferSize = 4 * 1024 * 1024;
                result.Bundle = client.GetByteArrayAsync("https://192.168.0.2/certs/download.zip").GetAwaiter().GetResult();
            }
        }
        if (result.Leaf == null) throw new InvalidOperationException("No se verificó el pin del certificado.");
        return result;
    }
}
'@
}
$download = [MediaVCenterPinnedDownload]::Download($pins.VSPHERE_CERT_SHA256, $pins.VSPHERE_CERT_SHA1)
$leaf = [Security.Cryptography.X509Certificates.X509Certificate2]::new([byte[]] $download.Leaf)
$certificates = [Collections.Generic.List[Security.Cryptography.X509Certificates.X509Certificate2]]::new()
$zipStream = [IO.MemoryStream]::new([byte[]] $download.Bundle)
$archive = [IO.Compression.ZipArchive]::new($zipStream, [IO.Compression.ZipArchiveMode]::Read)
$chain = [Security.Cryptography.X509Certificates.X509Chain]::new()
try {
    foreach ($entry in $archive.Entries) {
        if ($entry.Name -notmatch '\.[0-9]+$' -or $entry.Length -gt 65536) { continue }
        $entryStream = $entry.Open()
        $certificateBuffer = [IO.MemoryStream]::new()
        try {
            $entryStream.CopyTo($certificateBuffer)
            $certificateBytes = $certificateBuffer.ToArray()
        }
        finally { $entryStream.Dispose(); $certificateBuffer.Dispose() }
        $certificateText = [Text.Encoding]::ASCII.GetString($certificateBytes)
        $candidate = if ($certificateText.Contains('-----BEGIN CERTIFICATE-----')) {
            [Security.Cryptography.X509Certificates.X509Certificate2]::CreateFromPem($certificateText)
        }
        else { [Security.Cryptography.X509Certificates.X509Certificate2]::new([byte[]] $certificateBytes) }
        $basicConstraints = @($candidate.Extensions | Where-Object { $_ -is [Security.Cryptography.X509Certificates.X509BasicConstraintsExtension] })
        if ($basicConstraints.Count -eq 1 -and $basicConstraints[0].CertificateAuthority -and
            -not @($certificates | Where-Object Thumbprint -EQ $candidate.Thumbprint).Count) {
            $certificates.Add($candidate)
        }
        else { $candidate.Dispose() }
    }
    if ($certificates.Count -eq 0) { throw 'El paquete fijado no contiene certificados de CA válidos.' }
    $chain.ChainPolicy.TrustMode = [Security.Cryptography.X509Certificates.X509ChainTrustMode]::CustomRootTrust
    # La prueba es criptográfica y local; no afirma haber consultado revocación externa.
    $chain.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
    $chain.ChainPolicy.VerificationFlags = [Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
    $chain.ChainPolicy.DisableCertificateDownloads = $true
    foreach ($certificate in $certificates) {
        $chain.ChainPolicy.CustomTrustStore.Add($certificate) | Out-Null
        $chain.ChainPolicy.ExtraStore.Add($certificate) | Out-Null
    }
    if (-not $chain.Build($leaf)) {
        throw ('La CA descargada no verifica la cadena del certificado fijado: ' + (($chain.ChainStatus | Select-Object -ExpandProperty Status) -join ', '))
    }
    $directory = [IO.Path]::GetFullPath($EvidenceDirectory)
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $directory 'vcenter-ca-bundle.zip'), [byte[]] $download.Bundle)
    [IO.File]::WriteAllBytes((Join-Path $directory 'vcenter-leaf-pinned.cer'), $leaf.RawData)
    $verifiedCertificates = @()
    for ($index = 1; $index -lt $chain.ChainElements.Count; $index++) {
        $certificate = $chain.ChainElements[$index].Certificate
        $certificatePath = Join-Path $directory "vcenter-ca-$index.cer"
        [IO.File]::WriteAllBytes($certificatePath, $certificate.RawData)
        $verifiedCertificates += [pscustomobject]@{
            Subject = $certificate.Subject
            Issuer = $certificate.Issuer
            Sha256 = $certificate.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256)
            Sha1 = $certificate.Thumbprint
            CertificatePath = $certificatePath
            Root = $index -eq ($chain.ChainElements.Count - 1)
        }
    }
    $result = [pscustomobject]@{
        Endpoint = '192.168.0.2'
        LeafMatchesVault = $true
        CryptographicChainValid = $true
        RevocationChecked = $false
        TrustInstalled = $false
        Certificates = $verifiedCertificates
        NextAction = 'Respaldar CA DER Base64 y huellas en .env y claves CHANGE_ME en .env.example antes de instalar confianza de usuario.'
    }
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $directory 'vcenter-ca-verification.json') -Encoding utf8
    $result
}
finally {
    $chain.Dispose()
    $archive.Dispose()
    $zipStream.Dispose()
    $leaf.Dispose()
    foreach ($certificate in $certificates) { $certificate.Dispose() }
}
