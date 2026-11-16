<#
.SYNOPSIS
Prueba cloud-init VMware en el clon propio con la interfaz físicamente desconectada.
.DESCRIPTION
Solo la ejecución directa contacta vCenter. Importar con dot-source define funciones
reutilizables para el aprovisionador. La bóveda nunca se modifica automáticamente.
La prueba termina con la VM propia apagada y la interfaz desconectada.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter()]
    [Alias('VaultPath')]
    [string] $CloudInitVaultPath = (Join-Path $PSScriptRoot '../.env'),

    [Parameter()]
    [ValidateRange(30, 900)]
    [Alias('TimeoutSeconds')]
    [int] $CloudInitTimeoutSeconds = 300
)

function Get-MediaSshHostIdentity {
    <# .SYNOPSIS
    Lee la identidad SSH del servidor ya persistida; no genera ni imprime llaves.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $VaultPath)
    $keys = @('MEDIA_VM_SSH_HOST_PRIVATE_KEY_B64', 'MEDIA_VM_SSH_HOST_PUBLIC_KEY', 'MEDIA_VM_SSH_HOST_FINGERPRINT')
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $VaultPath -Encoding utf8 -ErrorAction Stop) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { continue }
        $key = $Matches[1]
        if ($key -notin $keys) { continue }
        if ($values.ContainsKey($key)) { throw 'Hay una clave de identidad SSH duplicada en la bóveda.' }
        $value = $Matches[2].Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    foreach ($key in $keys) {
        if (-not $values.ContainsKey($key) -or [string]::IsNullOrWhiteSpace($values[$key]) -or $values[$key] -match '^CHANGE_ME') {
            throw "Falta identidad SSH respaldada: $key."
        }
    }
    try {
        $private = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($values.MEDIA_VM_SSH_HOST_PRIVATE_KEY_B64)).Replace("`r`n", "`n")
        $beginMarker = ('-' * 5) + 'BEGIN OPENSSH PRIVATE KEY' + ('-' * 5)
        $endMarker = ('-' * 5) + 'END OPENSSH PRIVATE KEY' + ('-' * 5)
        if (-not $private.StartsWith($beginMarker + "`n") -or -not $private.TrimEnd().EndsWith("`n" + $endMarker)) {
            throw 'Formato de llave privada no admitido.'
        }
        if ($values.MEDIA_VM_SSH_HOST_PUBLIC_KEY -notmatch '^ssh-ed25519 ([A-Za-z0-9+/]+=*)(?: [^\r\n]*)?$') {
            throw 'Formato de llave pública no admitido.'
        }
        $publicBytes = [Convert]::FromBase64String($Matches[1])
        if ($publicBytes.Length -ne 51 -or [Convert]::ToHexString($publicBytes[0..18]) -cne '0000000B7373682D6564323535313900000020') {
            throw 'La llave pública no contiene un objeto Ed25519 válido.'
        }
        $fingerprint = 'SHA256:' + [Convert]::ToBase64String([Security.Cryptography.SHA256]::HashData($publicBytes)).TrimEnd('=')
        if ($fingerprint -cne $values.MEDIA_VM_SSH_HOST_FINGERPRINT) { throw 'La huella no coincide.' }
        $markerExpression = [regex]::Escape($beginMarker) + '|' + [regex]::Escape($endMarker) + '|\s'
        $privateEncoded = ($private -replace $markerExpression, '')
        $privateBytes = [Convert]::FromBase64String($privateEncoded)
        if (-not [Convert]::ToHexString($privateBytes).Contains([Convert]::ToHexString($publicBytes))) {
            throw 'Las identidades pública y privada no coinciden.'
        }
    }
    catch { throw 'La identidad SSH del servidor no supera la validación; no se mostrará su contenido.' }
    [pscustomobject]@{ PrivateKey = $private; PublicKey = $values.MEDIA_VM_SSH_HOST_PUBLIC_KEY; Fingerprint = $fingerprint }
}

function Test-MediaCloudInitTarget {
    <# .SYNOPSIS
    Rechaza operar una VM ajena, una identidad no respaldada o un host diferente.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Machine,
        [Parameter(Mandatory)] $Configuration,
        [Parameter(Mandatory)][string] $TargetHostId,
        [Parameter()][switch] $RequirePoweredOff
    )
    if ($Configuration.Name -cne 'vm-media-01' -or $Configuration.IpAddress -cne '192.168.0.85') { throw 'El destino no es la VM de medios autorizada.' }
    $uuid = [guid]::Empty
    if ([string]::IsNullOrWhiteSpace($Configuration.RegisteredId) -or -not [guid]::TryParse($Configuration.RegisteredUuid, [ref] $uuid)) {
        throw 'Primero deben persistirse el ID y el UUID reales del clon en la bóveda.'
    }
    if ($Machine.Name -cne $Configuration.Name -or $Machine.Id -cne $Configuration.RegisteredId -or
        $Machine.ExtensionData.Config.InstanceUuid -cne $Configuration.RegisteredUuid -or
        $Machine.Notes -cne $Configuration.OwnerMarker -or
        $Configuration.OwnerMarker -cne 'agencia-digital-marketing-ai:vm-media-01:v4.1') {
        throw 'El nombre, ID, UUID o marcador de propiedad del clon no coincide exactamente.'
    }
    if ($Machine.VMHost.Id -cne $TargetHostId -or $Machine.ExtensionData.Config.Template -ne $false) { throw 'El clon no está en el host autorizado o es una plantilla.' }
    if ($RequirePoweredOff -and $Machine.PowerState -ne 'PoweredOff') { throw 'La prueba inicial exige que el clon propio esté apagado.' }
    return $true
}

function New-MediaCloudInitPayload {
    <# .SYNOPSIS
    Construye GuestInfo sin publicarlo; conserva la identidad SSH ya respaldada.
    #>
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '', Justification = 'Constructor puro: solo devuelve texto en memoria; no aplica cambios ni contacta infraestructura.')]
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Configuration,
        [Parameter(Mandatory)] $HostIdentity,
        [Parameter(Mandatory)][ValidatePattern('^(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}$')][string] $MacAddress,
        [Parameter()][bool] $NetworkOptional = $true
    )
    $uuid = [guid]::Empty
    if ($Configuration.Name -cne 'vm-media-01' -or $Configuration.IpAddress -cne '192.168.0.85' -or
        $Configuration.SshUser -cne 'jandrade' -or -not [guid]::TryParse($Configuration.RegisteredUuid, [ref] $uuid)) {
        throw 'La carga cloud-init requiere la identidad fija del clon propio.'
    }
    if ($HostIdentity.Fingerprint -notmatch '^SHA256:[A-Za-z0-9+/]{43}$' -or
        $Configuration.SshPublicKey -notmatch '^ssh-ed25519 [A-Za-z0-9+/]+=*(?: [^\r\n]*)?$') { throw 'Falta identidad SSH válida y persistida.' }
    $metadata = @{
        'instance-id' = $Configuration.RegisteredUuid
        'local-hostname' = $Configuration.Name
        'wait-on-network' = @{ ipv4 = $false; ipv6 = $false }
        network = @{
            version = 2
            renderer = 'networkd'
            ethernets = @{
                media0 = @{
                    match = @{ macaddress = $MacAddress.ToLowerInvariant() }
                    'set-name' = 'media0'
                    optional = $NetworkOptional
                    dhcp4 = $false
                    dhcp6 = $false
                    addresses = @('192.168.0.85/24')
                    routes = @(@{ to = '0.0.0.0/0'; via = $Configuration.Gateway })
                    nameservers = @{ addresses = @($Configuration.DnsServers) }
                }
            }
        }
    } | ConvertTo-Json -Depth 10 -Compress
    $privateIndented = (($HostIdentity.PrivateKey.Replace("`r`n", "`n").TrimEnd("`n") -split "`n") | ForEach-Object { '    ' + $_ }) -join "`n"
    $userData = @'
#cloud-config
# Toda llave de acceso y de host ya existe en la bóveda antes de este arranque.
hostname: vm-media-01
preserve_hostname: false
manage_etc_hosts: true
disable_root: true
ssh_pwauth: false
ssh_deletekeys: true
# El esquema exige una lista no vacía; cc_ssh usa ssh_keys y evita la rama de generación.
ssh_genkeytypes: [ed25519]
ssh_keys:
  ed25519_private: |
__PRIVATE_KEY__
  ed25519_public: __PUBLIC_KEY__
users:
  - name: jandrade
    uid: 1000
    groups: [adm, sudo]
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    lock_passwd: true
    ssh_authorized_keys:
      - __CLIENT_KEY__
growpart:
  mode: auto
  devices: ['/']
resize_rootfs: true
write_files:
  - path: /var/lib/cloud/scripts/per-boot/90-media-platform-proof
    owner: root:root
    permissions: '0700'
    defer: false
    content: |
      #!/usr/bin/env bash
      # La prueba se recalcula en cada arranque sin cambiar el instance-id ni limpiar la caché.
      set -eu
      test "$(hostname -s)" = 'vm-media-01'
      test "$(id -u jandrade)" = 1000
      grep -Fq '__CLIENT_KEY_BLOB__' /home/jandrade/.ssh/authorized_keys
      test "$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256 | awk '{print $2}')" = '__HOST_FINGERPRINT__'
      # Se activa la identidad persistida también en el proceso SSH antes de publicar evidencia.
      systemctl restart ssh
      # El clon de la plantilla K8s se destina exclusivamente a la plataforma Docker.
      if [[ "$(systemctl show kubelet.service --property=LoadState --value)" != not-found ]]; then
        systemctl disable --now kubelet.service
        if systemctl is-active --quiet kubelet.service; then
          printf 'No se pudo detener kubelet en el clon de medios.\n' >&2
          exit 1
        fi
      fi
      /usr/bin/vmtoolsd --cmd 'info-set guestinfo.media-cloudinit.instance-id __INSTANCE_ID__'
      /usr/bin/vmtoolsd --cmd 'info-set guestinfo.media-cloudinit.host-fingerprint __HOST_FINGERPRINT__'
      /usr/bin/vmtoolsd --cmd 'info-set guestinfo.media-cloudinit.kubelet-disabled true'
'@
    $userData = $userData.Replace('__PRIVATE_KEY__', $privateIndented).
        Replace('__PUBLIC_KEY__', ($HostIdentity.PublicKey | ConvertTo-Json -Compress)).
        Replace('__CLIENT_KEY__', ($Configuration.SshPublicKey | ConvertTo-Json -Compress)).
        Replace('__CLIENT_KEY_BLOB__', (($Configuration.SshPublicKey -split ' ')[1])).
        Replace('__HOST_FINGERPRINT__', $HostIdentity.Fingerprint).
        Replace('__INSTANCE_ID__', $Configuration.RegisteredUuid)
    $guestInfo = @{
        'guestinfo.metadata' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($metadata))
        'guestinfo.metadata.encoding' = 'base64'
        'guestinfo.userdata' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($userData))
        'guestinfo.userdata.encoding' = 'base64'
    }
    [pscustomobject]@{ MetadataJson = $metadata; UserData = $userData; GuestInfo = $guestInfo }
}

function Test-MediaCloudInitProof {
    <# .SYNOPSIS
    Exige nombre de Tools y dos pruebas de user-data del arranque observado.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Machine,
        [Parameter(Mandatory)] $Configuration,
        [Parameter(Mandatory)][string] $ExpectedFingerprint,
        [Parameter(Mandatory)][datetime] $StartedAtUtc
    )
    if ($Machine.ExtensionData.Guest.HostName -cne 'vm-media-01' -or
        $Machine.ExtensionData.Guest.ToolsRunningStatus -cne 'guestToolsRunning' -or
        -not $Machine.ExtensionData.Runtime.BootTime -or
        ([datetime] $Machine.ExtensionData.Runtime.BootTime).ToUniversalTime() -lt $StartedAtUtc.AddMinutes(-1)) { return $false }
    $proofs = @{}
    foreach ($setting in @($Machine.ExtensionData.Config.ExtraConfig)) {
        if ($setting.Key -like 'guestinfo.media-cloudinit.*') { $proofs[$setting.Key] = $setting.Value }
    }
    return $proofs['guestinfo.media-cloudinit.instance-id'] -ceq $Configuration.RegisteredUuid -and
        $proofs['guestinfo.media-cloudinit.host-fingerprint'] -ceq $ExpectedFingerprint -and
        $proofs['guestinfo.media-cloudinit.kubelet-disabled'] -ceq 'true'
}

function Set-MediaCloudInitNicDisconnected {
    <# .SYNOPSIS
    Usa únicamente los parámetros admitidos por el estado real de energía de la VM.
    #>
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
    param(
        [Parameter(Mandatory)] $Connection,
        [Parameter(Mandatory)] $Machine,
        [Parameter(Mandatory)] $NetworkAdapter
    )
    if ($Machine.PowerState -notin @('PoweredOff', 'PoweredOn')) { throw 'No se modifica una interfaz con un estado de energía inesperado.' }
    if (-not $PSCmdlet.ShouldProcess($Machine.Name, 'Conservar la interfaz de prueba desconectada según su estado de energía')) { return }
    if ($Machine.PowerState -eq 'PoweredOff') {
        # Connected exige PoweredOn; una VM apagada ya carece de conexión en ejecución.
        Set-NetworkAdapter -Server $Connection -NetworkAdapter $NetworkAdapter -StartConnected:$false -Confirm:$false | Out-Null
    }
    else {
        Set-NetworkAdapter -Server $Connection -NetworkAdapter $NetworkAdapter -Connected:$false -StartConnected:$false -Confirm:$false | Out-Null
    }
}

function Stop-MediaCloudInitMachine {
    <# .SYNOPSIS
    Apaga exclusivamente el clon respaldado y conserva su interfaz desconectada.
    #>
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
    param(
        [Parameter(Mandatory)] $Connection,
        [Parameter(Mandatory)] $Configuration,
        [Parameter(Mandatory)][string] $TargetHostId
    )
    $machine = Get-VM -Server $Connection -Id $Configuration.RegisteredId -ErrorAction Stop
    Test-MediaCloudInitTarget -Machine $machine -Configuration $Configuration -TargetHostId $TargetHostId | Out-Null
    if (-not $PSCmdlet.ShouldProcess($Configuration.Name, 'Mantener la interfaz desconectada y apagar únicamente el clon propio de prueba')) { return }
    $nics = @(Get-NetworkAdapter -Server $Connection -VM $machine)
    if ($nics.Count -ne 1) { throw 'La VM propia ya no tiene exactamente una interfaz; se rechaza operar otra topología.' }
    Set-MediaCloudInitNicDisconnected -Connection $Connection -Machine $machine -NetworkAdapter $nics[0] -Confirm:$false -WhatIf:$false
    if ($machine.PowerState -eq 'PoweredOn') {
        if ($machine.ExtensionData.Guest.ToolsRunningStatus -eq 'guestToolsRunning') {
            try { Shutdown-VMGuest -Server $Connection -VM $machine -Confirm:$false -ErrorAction Stop | Out-Null }
            catch { Write-Warning 'Tools no completó la solicitud de apagado; se verificará el estado antes de detener el clon propio.' }
            $stopDeadline = [datetime]::UtcNow.AddSeconds(30)
            do {
                Start-Sleep -Seconds 2
                $machine = Get-VM -Server $Connection -Id $Configuration.RegisteredId
                Test-MediaCloudInitTarget -Machine $machine -Configuration $Configuration -TargetHostId $TargetHostId | Out-Null
            } while ($machine.PowerState -eq 'PoweredOn' -and [datetime]::UtcNow -lt $stopDeadline)
        }
        if ($machine.PowerState -ne 'PoweredOff') {
            Test-MediaCloudInitTarget -Machine $machine -Configuration $Configuration -TargetHostId $TargetHostId | Out-Null
            Stop-VM -Server $Connection -VM $machine -Confirm:$false -ErrorAction Stop | Out-Null
        }
    }
    $machine = Get-VM -Server $Connection -Id $Configuration.RegisteredId
    Test-MediaCloudInitTarget -Machine $machine -Configuration $Configuration -TargetHostId $TargetHostId -RequirePoweredOff | Out-Null
    $nic = @(Get-NetworkAdapter -Server $Connection -VM $machine)
    # El apagado ya verificado acredita la ausencia de conexión actual; no se modifica Connected apagada.
    if ($nic.Count -ne 1 -or $nic[0].ConnectionState.StartConnected) {
        throw 'No se pudo acreditar que el clon propio quedó apagado y con la interfaz desconectada.'
    }
}

function Invoke-MediaCloudInitTest {
    <# .SYNOPSIS
    Ejecuta un primer arranque aislado y devuelve evidencia sin modificar la bóveda.
    #>
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
    param(
        [Parameter()][string] $VaultPath = (Join-Path $PSScriptRoot '../.env'),
        [Parameter()][ValidateRange(30, 900)][int] $TimeoutSeconds = 300
    )
    $ErrorActionPreference = 'Stop'
    . (Join-Path $PSScriptRoot 'media-vm-config.ps1')
    $configuration = Get-MediaVmConfiguration -VaultPath $VaultPath
    $hostIdentity = Get-MediaSshHostIdentity -VaultPath $VaultPath
    if ($configuration.CloudInitVerified) { throw 'La bóveda ya acredita la plantilla; no se repetirá automáticamente el primer arranque.' }
    Import-Module VMware.VimAutomation.Core -ErrorAction Stop
    Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction Fail -Confirm:$false -WhatIf:$false | Out-Null
    $connection = $null
    $touched = $false
    $verified = $false
    $targetHostId = $null
    $observedAt = $null
    $startedAt = $null
    $stage = 'Validación previa del clon'
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $stage = 'Conexión TLS con vCenter'
        $connection = Connect-VIServer -Server $configuration.Server -Credential $configuration.Credential -NotDefault -ErrorAction Stop
        $targetHosts = @(Get-VMHost -Server $connection -Name $configuration.HostAddress)
        if ($targetHosts.Count -ne 1) { throw 'El host autorizado no es único.' }
        $targetHostId = $targetHosts[0].Id
        if ([string]::IsNullOrWhiteSpace($configuration.RegisteredId)) { throw 'Primero se debe persistir el ID del clon propio.' }
        $machines = @(Get-VM -Server $connection -Id $configuration.RegisteredId)
        if ($machines.Count -ne 1) { throw 'El clon respaldado no es único.' }
        $machine = $machines[0]
        Test-MediaCloudInitTarget -Machine $machine -Configuration $configuration -TargetHostId $targetHostId -RequirePoweredOff | Out-Null
        $nics = @(Get-NetworkAdapter -Server $connection -VM $machine)
        if ($nics.Count -ne 1) { throw 'El clon propio debe tener exactamente una interfaz.' }
        $payload = New-MediaCloudInitPayload -Configuration $configuration -HostIdentity $hostIdentity -MacAddress $nics[0].MacAddress
        if (-not $PSCmdlet.ShouldProcess($configuration.Name, 'Inyectar GuestInfo respaldado y realizar un arranque sin conexión de red para probar cloud-init y la identidad SSH')) {
            return [pscustomobject]@{ Verified = $false; Simulation = $true; VmName = $configuration.Name; NetworkWillRemainDisconnected = $true }
        }
        $touched = $true
        # Se desconecta físicamente antes de configurar o encender; no depende de la IP heredada.
        $stage = 'Desconectar el arranque de la interfaz con la VM apagada'
        Set-MediaCloudInitNicDisconnected -Connection $connection -Machine $machine -NetworkAdapter $nics[0] -Confirm:$false -WhatIf:$false
        $settingsToApply = @{}
        foreach ($key in $payload.GuestInfo.Keys) { $settingsToApply[$key] = $payload.GuestInfo[$key] }
        $settingsToApply['guestinfo.media-cloudinit.instance-id'] = 'pendiente'
        $settingsToApply['guestinfo.media-cloudinit.host-fingerprint'] = 'pendiente'
        $settingsToApply['guestinfo.media-cloudinit.kubelet-disabled'] = 'pendiente'
        $stage = 'Aplicar GuestInfo respaldado'
        foreach ($key in $settingsToApply.Keys) {
            $existing = @(Get-AdvancedSetting -Server $connection -Entity $machine -Name $key -ErrorAction SilentlyContinue)
            if ($existing.Count -gt 1) { throw 'Existe una clave GuestInfo duplicada.' }
            if ($existing.Count -eq 1) {
                Set-AdvancedSetting -AdvancedSetting $existing[0] -Value $settingsToApply[$key] -Confirm:$false | Out-Null
            }
            else {
                New-AdvancedSetting -Server $connection -Entity $machine -Name $key -Value $settingsToApply[$key] -Confirm:$false | Out-Null
            }
        }
        $startedAt = [datetime]::UtcNow
        $stage = 'Arrancar exclusivamente el clon aislado'
        Start-VM -Server $connection -VM $machine -Confirm:$false -ErrorAction Stop | Out-Null
        $deadline = $startedAt.AddSeconds($TimeoutSeconds)
        $stage = 'Esperar las pruebas de Tools y del script por arranque'
        do {
            Start-Sleep -Seconds 3
            $machine = Get-VM -Server $connection -Id $configuration.RegisteredId
            Test-MediaCloudInitTarget -Machine $machine -Configuration $configuration -TargetHostId $targetHostId | Out-Null
            $machine.ExtensionData.UpdateViewData('Guest.HostName', 'Guest.ToolsRunningStatus', 'Runtime.BootTime', 'Config.ExtraConfig')
            $activeNics = @(Get-NetworkAdapter -Server $connection -VM $machine)
            $runtimeConnected = $machine.PowerState -eq 'PoweredOn' -and $activeNics.Count -eq 1 -and $activeNics[0].ConnectionState.Connected
            if ($activeNics.Count -ne 1 -or $runtimeConnected -or $activeNics[0].ConnectionState.StartConnected) {
                throw 'La interfaz dejó de estar aislada durante la prueba; se interrumpe el arranque.'
            }
            $verified = Test-MediaCloudInitProof -Machine $machine -Configuration $configuration -ExpectedFingerprint $hostIdentity.Fingerprint -StartedAtUtc $startedAt
            if ($verified) { $observedAt = [datetime]::UtcNow; break }
        } while ([datetime]::UtcNow -lt $deadline -and $machine.PowerState -eq 'PoweredOn')
        if (-not $verified) { throw 'No se observaron las pruebas completas dentro del plazo de arranque aislado.' }
    }
    catch {
        # No se propagan errores de parámetros GuestInfo que pudieran incluir user-data privado.
        throw "Falló la prueba aislada en la etapa '$stage'; no se acredita soporte de la plantilla. Se intentará apagar y aislar exclusivamente el clon propio. La prueba por arranque conserva el instance-id y no limpia la caché; revisar el clon antes de reintentar."
    }
    finally {
        try {
            if ($touched -and $connection -and $targetHostId) {
                Stop-MediaCloudInitMachine -Connection $connection -Configuration $configuration -TargetHostId $targetHostId -Confirm:$false -WhatIf:$false
            }
        }
        finally {
            $watch.Stop()
            if ($connection) { Disconnect-VIServer -Server $connection -Confirm:$false -WhatIf:$false -ErrorAction Continue | Out-Null }
        }
    }
    [pscustomobject]@{
        Verified = [bool] $verified
        Simulation = $false
        VmName = $configuration.Name
        VmId = $configuration.RegisteredId
        VmUuid = $configuration.RegisteredUuid
        GuestHostNameObserved = 'vm-media-01'
        CloudInitInstanceIdMatched = [bool] $verified
        SshHostFingerprintMatched = [bool] $verified
        KubeletDisabledOrAbsent = [bool] $verified
        StartedAtUtc = $startedAt.ToString('o')
        ProofObservedAtUtc = $observedAt.ToString('o')
        FinalPowerState = 'PoweredOff'
        FinalNicConnected = $false
        FinalNicStartConnected = $false
        ElapsedSeconds = [math]::Round($watch.Elapsed.TotalSeconds, 3)
        NextAction = 'Respaldar MEDIA_TEMPLATE_CLOUDINIT_VMWARE_VERIFIED=true y su plantilla antes de reconciliar hardware y habilitar LAN con el aprovisionador.'
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-MediaCloudInitTest -VaultPath $CloudInitVaultPath -TimeoutSeconds $CloudInitTimeoutSeconds -WhatIf:$WhatIfPreference
}
