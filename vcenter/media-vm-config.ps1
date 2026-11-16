# Carga declarativa de la VM de medios. Importar este archivo no conecta a vCenter.

function Get-MediaVmConfiguration {
    <#
    .SYNOPSIS
    Lee exclusivamente las claves necesarias de la bóveda y valida el destino aislado.
    .DESCRIPTION
    No ejecuta el contenido de .env, no imprime secretos ni genera credenciales.
    La contraseña se convierte en PSCredential antes de devolver la configuración.
    #>
    [CmdletBinding()]
    param(
        [Parameter()]
        [string] $VaultPath = (Join-Path $PSScriptRoot '../.env')
    )

    $requiredKeys = @(
        'VSPHERE_SERVER', 'VSPHERE_USER', 'VSPHERE_PASSWORD', 'ESXI_HOST',
        'ESXI_DATASTORE_NVME', 'ESXI_TEMPLATE_UBUNTU', 'MEDIA_VM_NAME',
        'MEDIA_VM_IP', 'MEDIA_VM_NETMASK', 'MEDIA_VM_GATEWAY', 'MEDIA_VM_DNS',
        'MEDIA_VM_SSH_USER'
    )
    $optionalKeys = @(
        'MEDIA_VM_SSH_PUBLIC_KEY', 'MEDIA_TEMPLATE_CLOUDINIT_VMWARE_VERIFIED',
        'VSPHERE_MEDIA_VM_ID', 'VSPHERE_MEDIA_VM_UUID'
    )
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $VaultPath -Encoding utf8 -ErrorAction Stop) {
        if ($line -match '^\s*(?:#.*)?$') { continue }
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            throw 'La bóveda contiene una línea que no cumple CLAVE=valor.'
        }
        $key = $Matches[1]
        if ($key -notin $requiredKeys -and $key -notin $optionalKeys) { continue }
        if ($values.ContainsKey($key)) { throw "Clave duplicada en la bóveda: $key" }
        $value = $Matches[2].Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    foreach ($key in $requiredKeys) {
        if (-not $values.ContainsKey($key) -or [string]::IsNullOrWhiteSpace($values[$key]) -or
            $values[$key] -match '^(CHANGE_ME|TODO|PENDIENTE)(?:_|$)') {
            throw "Falta un valor real documentado para $key en la bóveda."
        }
    }
    $fixedValues = @{
        VSPHERE_SERVER = '192.168.0.2'
        ESXI_HOST = '192.168.0.3'
        ESXI_DATASTORE_NVME = 'datastore1'
        ESXI_TEMPLATE_UBUNTU = 'TPL-ubuntu2404-k8s'
        MEDIA_VM_NAME = 'vm-media-01'
        MEDIA_VM_IP = '192.168.0.85'
        MEDIA_VM_NETMASK = '255.255.255.0'
    }
    foreach ($key in $fixedValues.Keys) {
        if ($values[$key] -cne $fixedValues[$key]) {
            throw "El destino $key debe permanecer en $($fixedValues[$key]); se rechaza operar otro equipo."
        }
    }
    $dnsServers = @($values.MEDIA_VM_DNS -split '[,;\s]+' | Where-Object { $_ })
    foreach ($address in @($values.MEDIA_VM_GATEWAY) + $dnsServers) {
        $parsedAddress = $null
        if (-not [System.Net.IPAddress]::TryParse($address, [ref] $parsedAddress) -or
            $parsedAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
            throw 'La puerta de enlace y los servidores DNS deben ser direcciones IPv4 válidas.'
        }
    }
    if ($dnsServers.Count -eq 0 -or $values.MEDIA_VM_SSH_USER -notmatch '^[a-z_][a-z0-9_-]{0,31}$') {
        throw 'Debe existir al menos un DNS y un usuario Linux válido.'
    }
    $securePassword = [System.Security.SecureString]::new()
    foreach ($character in $values.VSPHERE_PASSWORD.ToCharArray()) { $securePassword.AppendChar($character) }
    $securePassword.MakeReadOnly()
    $credential = [pscredential]::new($values.VSPHERE_USER, $securePassword)
    $values.Remove('VSPHERE_PASSWORD')

    [pscustomobject]@{
        Server = $values.VSPHERE_SERVER
        Credential = $credential
        HostAddress = $values.ESXI_HOST
        Datastore = $values.ESXI_DATASTORE_NVME
        Template = $values.ESXI_TEMPLATE_UBUNTU
        Name = $values.MEDIA_VM_NAME
        IpAddress = $values.MEDIA_VM_IP
        Netmask = $values.MEDIA_VM_NETMASK
        PrefixLength = 24
        Gateway = $values.MEDIA_VM_GATEWAY
        DnsServers = $dnsServers
        SshUser = $values.MEDIA_VM_SSH_USER
        SshPublicKey = $values.MEDIA_VM_SSH_PUBLIC_KEY
        CloudInitVerified = $values.MEDIA_TEMPLATE_CLOUDINIT_VMWARE_VERIFIED -ceq 'true'
        RegisteredId = $values.VSPHERE_MEDIA_VM_ID
        RegisteredUuid = $values.VSPHERE_MEDIA_VM_UUID
        Cpu = 6
        MemoryGB = 16
        DiskGB = 80
        PortGroup = 'LAN'
        OwnerMarker = 'agencia-digital-marketing-ai:vm-media-01:v4.1'
    }
}
