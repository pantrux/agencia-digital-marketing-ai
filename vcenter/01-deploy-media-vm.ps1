<#
.SYNOPSIS
Audita o aprovisiona exclusivamente vm-media-01 en el host y datastore autorizados.
.DESCRIPTION
-AuditOnly consulta inventario y disponibilidad sin modificar infraestructura.
-WhatIf muestra el plan con la misma auditoría y no ejecuta mutaciones.
La primera creación deja la VM apagada: sus identificadores deben respaldarse en
.env antes de una segunda ejecución. El arranque necesita -Start y una plantilla
verificada para cloud-init VMware; no se presupone ninguna contraseña del invitado.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter()]
    [string] $VaultPath = (Join-Path $PSScriptRoot '../.env'),

    [Parameter()]
    [switch] $AuditOnly,

    [Parameter()]
    [switch] $Start
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'media-vm-config.ps1')
$configuration = Get-MediaVmConfiguration -VaultPath $VaultPath
Import-Module VMware.VimAutomation.Core -ErrorAction Stop
# La auditoría también aplica esta política local: no hereda Ignore/Warn de otros proyectos.
# -WhatIf solo simula infraestructura; la validación TLS continúa siendo obligatoria.
Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction Fail -Confirm:$false -WhatIf:$false | Out-Null
$connection = $null

try {
    # Una CA confiable o la excepción exacta servidor/huella deben estar preparadas previamente.
    $connection = Connect-VIServer -Server $configuration.Server -Credential $configuration.Credential -NotDefault -ErrorAction Stop
    $hostCandidates = @(Get-VMHost -Server $connection -Name $configuration.HostAddress)
    $templates = @(Get-Template -Server $connection -Name $configuration.Template | Where-Object Name -CEQ $configuration.Template)
    if ($hostCandidates.Count -ne 1 -or $templates.Count -ne 1) {
        throw 'La plantilla o el host autorizado no son únicos; no se puede elegir un destino seguro.'
    }
    $targetHost = $hostCandidates[0]
    $template = $templates[0]
    $datastores = @(Get-Datastore -Server $connection -VMHost $targetHost -Name $configuration.Datastore)
    $portGroups = @(Get-VirtualPortGroup -Server $connection -VMHost $targetHost -Name $configuration.PortGroup -Standard)
    if ($datastores.Count -ne 1 -or $portGroups.Count -ne 1) {
        throw 'El datastore o el PortGroup LAN no son únicos en el host autorizado.'
    }
    $datastore = $datastores[0]
    $portGroup = $portGroups[0]
    $templateDisks = @(Get-HardDisk -Server $connection -Template $template)
    $templateNics = @($template.ExtensionData.Config.Hardware.Device | Where-Object { $_ -is [VMware.Vim.VirtualEthernetCard] })
    $allMachines = @(Get-VM -Server $connection)
    $existingMachines = @($allMachines | Where-Object Name -CEQ $configuration.Name)
    if ($existingMachines.Count -gt 1) { throw 'Hay más de una VM con el nombre reservado; se rechaza continuar.' }
    $machine = if ($existingMachines.Count -eq 1) { $existingMachines[0] } else { $null }
    $otherIpOwners = @($allMachines | Where-Object {
        $_.Name -cne $configuration.Name -and @($_.Guest.IPAddress) -contains $configuration.IpAddress
    } | Select-Object -ExpandProperty Name)
    $icmpResponse = Test-Connection -TargetName $configuration.IpAddress -Count 1 -Quiet -TimeoutSeconds 1
    $tcpResponse = $false
    $tcpClient = [System.Net.Sockets.TcpClient]::new()
    try {
        $connectionTask = $tcpClient.ConnectAsync($configuration.IpAddress, 22)
        if ($connectionTask.Wait(1000)) { $tcpResponse = $tcpClient.Connected }
    }
    catch [System.AggregateException] { $tcpResponse = $false }
    finally { $tcpClient.Dispose() }

    $blockers = [System.Collections.Generic.List[string]]::new()
    if ($targetHost.ConnectionState -ne 'Connected' -or $targetHost.PowerState -ne 'PoweredOn') {
        $blockers.Add('El host no está conectado y encendido.')
    }
    if ($templateDisks.Count -ne 1 -or $templateDisks[0].CapacityGB -gt $configuration.DiskGB) {
        $blockers.Add('La plantilla debe tener un único disco de hasta 80 GB; nunca se reduce un disco.')
    }
    if ($templateNics.Count -ne 1) { $blockers.Add('La plantilla debe tener exactamente una interfaz de red.') }
    if ($otherIpOwners.Count -gt 0) { $blockers.Add('Otra VM del inventario anuncia la IP reservada.') }
    if (-not $machine) {
        if ($datastore.FreeSpaceGB -lt 100) { $blockers.Add('Se necesitan 100 GB libres para el disco y la reserva de snapshots.') }
        if (($targetHost.MemoryTotalGB - $targetHost.MemoryUsageGB) -lt 20) { $blockers.Add('Se necesitan 20 GB de memoria libre incluyendo la reserva del host.') }
        if ($icmpResponse -or $tcpResponse) { $blockers.Add('La IP reservada responde; se requiere identificar al ocupante antes de clonar.') }
    }
    $owned = $false
    if ($machine) {
        $owned = $machine.Notes -ceq $configuration.OwnerMarker
        if (-not $owned) { $blockers.Add('La VM existente no tiene el marcador de propiedad; no se adopta ni modifica.') }
        if ($configuration.RegisteredId -and $machine.Id -cne $configuration.RegisteredId) {
            $blockers.Add('El ID de la VM existente no coincide con la bóveda.')
        }
        if ($configuration.RegisteredUuid -and $machine.ExtensionData.Config.InstanceUuid -cne $configuration.RegisteredUuid) {
            $blockers.Add('El UUID de la VM existente no coincide con la bóveda.')
        }
        if ($machine.VMHost.Id -cne $targetHost.Id) { $blockers.Add('La VM existente está fuera del host autorizado.') }
    }
    $audit = [pscustomobject]@{
        Mode = if ($AuditOnly) { 'Solo auditoría' } elseif ($WhatIfPreference) { 'Simulación' } else { 'Aprovisionamiento' }
        VmName = $configuration.Name
        VmExists = [bool] $machine
        VmOwned = $owned
        VmId = if ($machine) { $machine.Id } else { $null }
        VmUuid = if ($machine) { $machine.ExtensionData.Config.InstanceUuid } else { $null }
        TemplateName = $template.Name
        TemplateId = $template.Id
        TemplateGuest = $template.ExtensionData.Config.GuestFullName
        TemplateDisksGB = @($templateDisks | Select-Object -ExpandProperty CapacityGB)
        TemplateNicCount = $templateNics.Count
        TemplateGuestInfoKeys = @($template.ExtensionData.Config.ExtraConfig.Key | Where-Object { $_ -like 'guestinfo.*' })
        HostName = $targetHost.Name
        HostId = $targetHost.Id
        HostFreeMemoryGB = [math]::Round(($targetHost.MemoryTotalGB - $targetHost.MemoryUsageGB), 2)
        DatastoreName = $datastore.Name
        DatastoreId = $datastore.Id
        DatastoreFreeGB = [math]::Round($datastore.FreeSpaceGB, 2)
        PortGroup = $portGroup.Name
        PortGroupKey = $portGroup.Key
        TargetIp = $configuration.IpAddress
        IcmpResponds = $icmpResponse
        SshPortResponds = $tcpResponse
        OtherIpOwners = $otherIpOwners
        CloudInitVerified = $configuration.CloudInitVerified
        PublicKeyReady = [bool] ($configuration.SshPublicKey -match '^ssh-ed25519 [A-Za-z0-9+/]+=?(?: .*)?$')
        Blockers = @($blockers)
        IpAvailabilityLimitation = 'La ausencia de respuesta ICMP/TCP y de IP en inventario no demuestra por sí sola que la IP esté libre; comprobar también DHCP y ARP del router.'
    }
    if ($AuditOnly) { return $audit }
    if ($blockers.Count -gt 0) { throw ('Auditoría bloqueante: ' + ($blockers -join ' ')) }

    if (-not $machine) {
        if ($PSCmdlet.ShouldProcess($configuration.Name, 'Clonar apagada desde la plantilla autorizada con disco Thin y red LAN')) {
            $machine = New-VM -Server $connection -Name $configuration.Name -Template $template -VMHost $targetHost -Datastore $datastore -DiskStorageFormat Thin -Portgroup $portGroup -Notes $configuration.OwnerMarker -Confirm:$false
            # No se usa el ID generado para configurar ni encender la VM antes de respaldarlo.
            return [pscustomobject]@{
                VmName = $machine.Name
                VmId = $machine.Id
                VmUuid = $machine.ExtensionData.Config.InstanceUuid
                PowerState = [string] $machine.PowerState
                PendingVaultRegistration = $true
                NextAction = 'Respaldar VSPHERE_MEDIA_VM_ID y VSPHERE_MEDIA_VM_UUID en .env y .env.example; volver a ejecutar antes de encender.'
            }
        }
        return $audit
    }
    if (-not $configuration.RegisteredId -or -not $configuration.RegisteredUuid) {
        throw 'Primero se deben respaldar VSPHERE_MEDIA_VM_ID y VSPHERE_MEDIA_VM_UUID reales en .env y sus claves en .env.example.'
    }
    if ($machine.PowerState -ne 'PoweredOff') {
        return [pscustomobject]@{
            VmName = $machine.Name
            PowerState = [string] $machine.PowerState
            Changed = $false
            NextAction = 'La VM está encendida; no se modifica hardware ni se reinicia cloud-init. Verificar SSH y la fase de instalación.'
        }
    }
    if (-not $configuration.CloudInitVerified -or -not $audit.PublicKeyReady) {
        if ($WhatIfPreference) {
            $audit.Blockers += 'La personalización real requiere clave pública Ed25519 respaldada y soporte cloud-init VMware de la plantilla verificado.'
            return $audit
        }
        throw 'Falta clave pública Ed25519 respaldada o soporte cloud-init VMware de la plantilla verificado; no se enciende la VM.'
    }
    $disks = @(Get-HardDisk -Server $connection -VM $machine)
    $nics = @(Get-NetworkAdapter -Server $connection -VM $machine)
    if ($disks.Count -ne 1 -or $disks[0].CapacityGB -gt $configuration.DiskGB -or $nics.Count -ne 1) {
        throw 'La VM existente no tiene la topología segura de un disco de hasta 80 GB y una interfaz.'
    }
    # Se usa exactamente la carga probada en el arranque aislado y su identidad de host respaldada.
    # Importar este archivo solo define funciones; la prueba aislada no se ejecuta aquí.
    . (Join-Path $PSScriptRoot 'Test-MediaCloudInit.ps1') -VaultPath $VaultPath
    $hostIdentity = Get-MediaSshHostIdentity -VaultPath $VaultPath
    $cloudInitPayload = New-MediaCloudInitPayload -Configuration $configuration -HostIdentity $hostIdentity -MacAddress $nics[0].MacAddress -NetworkOptional:$false
    $guestInfo = $cloudInitPayload.GuestInfo
    if ($PSCmdlet.ShouldProcess($configuration.Name, 'Reconciliar 6 vCPU, 16 GB RAM y disco Thin de 80 GB sin encender')) {
        if ($machine.NumCpu -ne $configuration.Cpu -or $machine.MemoryGB -ne $configuration.MemoryGB) {
            $machine = Set-VM -Server $connection -VM $machine -NumCpu $configuration.Cpu -MemoryGB $configuration.MemoryGB -Confirm:$false
        }
        if ($disks[0].CapacityGB -lt $configuration.DiskGB) {
            Set-HardDisk -Server $connection -HardDisk $disks[0] -CapacityGB $configuration.DiskGB -Confirm:$false | Out-Null
        }
        if ($disks[0].StorageFormat -ne 'Thin') { throw 'El disco no es Thin; no se transforma ni migra automáticamente.' }
    }
    if ($PSCmdlet.ShouldProcess($configuration.Name, 'Aplicar GuestInfo cloud-init con IP .85 y las identidades SSH de cliente y host ya respaldadas')) {
        foreach ($key in $guestInfo.Keys) {
            $setting = Get-AdvancedSetting -Server $connection -Entity $machine -Name $key -ErrorAction SilentlyContinue
            if ($setting) {
                if ($setting.Value -cne $guestInfo[$key]) {
                    # El objeto AdvancedSetting ya vincula el servidor; este cmdlet no acepta -Server.
                    Set-AdvancedSetting -AdvancedSetting $setting -Value $guestInfo[$key] -Confirm:$false | Out-Null
                }
            }
            else {
                New-AdvancedSetting -Server $connection -Entity $machine -Name $key -Value $guestInfo[$key] -Confirm:$false | Out-Null
            }
        }
        # PowerCLI separa cambio de PortGroup y conexión inicial en conjuntos de parámetros distintos.
        $updatedNic = Set-NetworkAdapter -Server $connection -NetworkAdapter $nics[0] -Portgroup $portGroup -Confirm:$false
        Set-NetworkAdapter -Server $connection -NetworkAdapter $updatedNic -StartConnected:$true -Confirm:$false | Out-Null
    }
    if ($Start -and $PSCmdlet.ShouldProcess($configuration.Name, 'Encender únicamente la VM de medios tras reconciliar su configuración')) {
        Start-VM -Server $connection -VM $machine -Confirm:$false | Out-Null
    }
    [pscustomobject]@{
        VmName = $machine.Name
        VmId = $machine.Id
        VmUuid = $machine.ExtensionData.Config.InstanceUuid
        StartRequested = [bool] $Start
        Simulation = [bool] $WhatIfPreference
        NextAction = 'Verificar IP .85, SSH con clave persistida y cloud-init status --wait antes de instalar el runner y tomar 01-vm-limpia.'
    }
}
finally {
    # La limpieza de la sesión autenticada también debe ejecutarse durante -WhatIf.
    if ($connection) { Disconnect-VIServer -Server $connection -Confirm:$false -WhatIf:$false -ErrorAction Continue | Out-Null }
}
