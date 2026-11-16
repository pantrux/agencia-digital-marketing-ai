<#
.SYNOPSIS
Observa el certificado público del vCenter sin enviar credenciales ni confiar en él.
.DESCRIPTION
El callback de validación devuelve siempre falso: se captura el certificado y se
rechaza la sesión TLS. Su huella debe compararse con una fuente conocida antes de
registrarla en la bóveda y preparar una CA o una excepción PCTCS para ese servidor.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet('192.168.0.2')]
    [string] $Server = '192.168.0.2'
)

$ErrorActionPreference = 'Stop'
$capturedCertificate = @{ RawData = $null; PolicyErrors = $null }
$captureCallback = {
    param($connectionSender, $remoteCertificate, $certificateChain, $policyErrors)
    # No se permite completar el handshake ni enviar credenciales a este canal.
    $null = $connectionSender
    $null = $certificateChain
    $capturedCertificate.RawData = $remoteCertificate.GetRawCertData()
    $capturedCertificate.PolicyErrors = [string] $policyErrors
    return $false
}.GetNewClosure()
$client = [System.Net.Sockets.TcpClient]::new()
$tls = $null
try {
    $pendingConnection = $client.ConnectAsync($Server, 443)
    if (-not $pendingConnection.Wait(3000)) { throw 'El vCenter no respondió en el plazo de conexión TLS.' }
    $tls = [System.Net.Security.SslStream]::new($client.GetStream(), $false, $captureCallback)
    $tls.ReadTimeout = 3000
    $tls.WriteTimeout = 3000
    try { $tls.AuthenticateAsClient($Server) }
    catch [System.Security.Authentication.AuthenticationException] {
        if (-not $capturedCertificate.RawData) { throw 'El vCenter no presentó un certificado público inspeccionable.' }
    }
    if (-not $capturedCertificate.RawData) { throw 'No se capturó ningún certificado; no se registra confianza.' }
    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new([byte[]] $capturedCertificate.RawData)
    try {
        [pscustomobject]@{
            Server = $Server
            Subject = $certificate.Subject
            Issuer = $certificate.Issuer
            NotBeforeUtc = $certificate.NotBefore.ToUniversalTime().ToString('o')
            NotAfterUtc = $certificate.NotAfter.ToUniversalTime().ToString('o')
            ObservedSha256 = $certificate.GetCertHashString([System.Security.Cryptography.HashAlgorithmName]::SHA256)
            ObservedSha1 = $certificate.Thumbprint
            PolicyErrors = $capturedCertificate.PolicyErrors
            Authenticated = $false
            TrustInstalled = $false
        }
    }
    finally { $certificate.Dispose() }
}
finally {
    if ($tls) { $tls.Dispose() }
    $client.Dispose()
}
