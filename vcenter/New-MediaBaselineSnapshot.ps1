<#
.SYNOPSIS
Prepara una instantánea inicial o previa a un cambio en la VM de medios.
.DESCRIPTION
Admite 00-pre-bootstrap, 01-vm-limpia y pre-cambio-<identificador>.
Con un máximo de dos instantáneas, solo rota las de cambios anteriores.
Importar con dot-source solo define funciones para probar las guardas sin PowerCLI.
La ejecución directa devuelve el ID para respaldarlo inmediatamente en .env y
actualizar .env.example antes de utilizarlo.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)]
    [Alias('Name')]
    [ValidatePattern('^(00-pre-bootstrap|01-vm-limpia|pre-cambio-[a-z0-9][a-z0-9-]{0,47})\z')]
    [string] $BaselineName,

    [Parameter()]
    [Alias('VaultPath')]
    [string] $BaselineVaultPath = (Join-Path $PSScriptRoot '../.env')
)

function Test-MediaBaselineSnapshotTarget {
    <# .SYNOPSIS
    Valida exclusivamente la propiedad e identidad del clon sin modificarlo.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Machine,
        [Parameter(Mandatory)] $Configuration,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string] $TargetHostId
    )
    $uuid = [guid]::Empty
    if ($Configuration.Name -cne 'vm-media-01' -or $Configuration.IpAddress -cne '192.168.0.85' -or
        $Configuration.HostAddress -cne '192.168.0.3' -or
        $Configuration.OwnerMarker -cne 'agencia-digital-marketing-ai:vm-media-01:v4.1' -or
        [string]::IsNullOrWhiteSpace($Configuration.RegisteredId) -or
        -not [guid]::TryParse($Configuration.RegisteredUuid, [ref] $uuid)) {
        throw 'La configuración de propiedad e identidad de la VM autorizada debe estar respaldada.'
    }
    if ($Machine.Name -cne $Configuration.Name -or $Machine.Id -cne $Configuration.RegisteredId -or
        $Machine.Notes -cne $Configuration.OwnerMarker -or
        $Machine.ExtensionData.Config.InstanceUuid -cne $Configuration.RegisteredUuid -or
        $Machine.ExtensionData.Config.Template -ne $false -or $Machine.VMHost.Id -cne $TargetHostId) {
        throw 'La propiedad, el ID, el UUID o el host de la VM no coinciden; no se opera una VM ajena.'
    }
    return $true
}

function Get-MediaBaselineSnapshotPlan {
    <# .SYNOPSIS
    Valida identidad, propiedad y límites y devuelve un plan sin ejecutar operaciones.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Machine,
        [Parameter(Mandatory)] $Configuration,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string] $TargetHostId,
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]] $Snapshots,
        [Parameter(Mandatory)][ValidateSet('00-pre-bootstrap', '01-vm-limpia')][string] $Name
    )
    Test-MediaBaselineSnapshotTarget -Machine $Machine -Configuration $Configuration -TargetHostId $TargetHostId | Out-Null
    foreach ($snapshot in $Snapshots) {
        $expectedDescription = $Configuration.OwnerMarker + '|instantanea:' + $snapshot.Name
        if ($snapshot.Name -cnotmatch '^(00-pre-bootstrap|01-vm-limpia|pre-cambio-[a-z0-9][a-z0-9-]{0,47})\z' -or
            $snapshot.Description -cne $expectedDescription -or [string]::IsNullOrWhiteSpace($snapshot.Id)) {
            throw 'Existe una instantánea sin propiedad acreditada o con nombre ajeno; no se adopta ni elimina.'
        }
    }
    if ($Snapshots.Count -gt 2) { throw 'La VM supera el límite de dos instantáneas; no se elimina ninguna automáticamente.' }
    $existing = @($Snapshots | Where-Object Name -CEQ $Name)
    if ($existing.Count -gt 1) { throw 'Hay instantáneas duplicadas con el nombre solicitado; no se elige una arbitrariamente.' }
    if ($existing.Count -eq 1) {
        return [pscustomobject]@{
            Action = 'Reutilizar'
            Reason = $null
            RetireId = $null
            Name = $Name
            Id = $existing[0].Id
            CreatedAtUtc = $existing[0].Created.ToUniversalTime().ToString('o')
            Description = $existing[0].Description
            Memory = $false
            Quiesce = $false
        }
    }
    if ($Snapshots.Count -ge 2) { throw 'No se crea una tercera instantánea: el máximo permitido es dos.' }
    if ($Machine.PowerState -ne 'PoweredOff') {
        throw 'La creación exige la VM propia apagada; el responsable debe apagarla y verificarla previamente.'
    }
    [pscustomobject]@{
        Action = 'Crear'
        Reason = $null
        RetireId = $null
        Name = $Name
        Id = $null
        CreatedAtUtc = $null
        Description = $Configuration.OwnerMarker + '|instantanea:' + $Name
        Memory = $false
        Quiesce = $false
    }
}

function Get-MediaPreChangeSnapshotPlan {
    <# .SYNOPSIS
    Determina si hay lugar para el punto de reversión de un cambio concreto.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] $Machine,
        [Parameter(Mandatory)] $Configuration,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string] $TargetHostId,
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]] $Snapshots,
        [Parameter(Mandatory)][string] $Name
    )
    Test-MediaBaselineSnapshotTarget -Machine $Machine -Configuration $Configuration -TargetHostId $TargetHostId | Out-Null
    if ($Name -cnotmatch '^pre-cambio-[a-z0-9][a-z0-9-]{0,47}\z') { throw 'El identificador del cambio es inválido.' }
    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($snapshot in $Snapshots) {
        if ($snapshot.Name -cnotmatch '^(00-pre-bootstrap|01-vm-limpia|pre-cambio-[a-z0-9][a-z0-9-]{0,47})\z' -or
            $snapshot.Description -cne ($Configuration.OwnerMarker + '|instantanea:' + $snapshot.Name) -or
            [string]::IsNullOrWhiteSpace($snapshot.Id) -or -not $seen.Add($snapshot.Name)) {
            throw 'El inventario de instantáneas contiene una entrada ajena, inválida o duplicada.'
        }
    }
    if ($Snapshots.Count -gt 2) { throw 'La VM supera el límite de dos instantáneas.' }

    $plan = [pscustomobject]@{
        Action = 'Bloqueado'
        Reason = $null
        Name = $Name
        Id = $null
        CreatedAtUtc = $null
        RetireId = $null
        Description = $Configuration.OwnerMarker + '|instantanea:' + $Name
    }
    $existing = $Snapshots | Where-Object Name -CEQ $Name
    $previous = $Snapshots | Where-Object { $_.Name -clike 'pre-cambio-*' } | Sort-Object Created, Id | Select-Object -First 1
    if ($Machine.ExtensionData.Runtime.ConsolidationNeeded) {
        $plan.Reason = 'CONSOLIDACION_PENDIENTE'
    } elseif (-not $existing -and $Snapshots.Count -eq 2 -and -not $previous) {
        $plan.Reason = 'CAPACIDAD_OCUPADA_POR_REFERENCIAS_INICIALES'
    } elseif ($Machine.PowerState -ne 'PoweredOff') {
        $plan.Reason = 'VM_DEBE_ESTAR_APAGADA'
    } elseif ($existing) {
        $plan.Action = 'Reutilizar'
        $plan.Id = $existing.Id
        $plan.CreatedAtUtc = $existing.Created.ToUniversalTime().ToString('o')
    } else {
        $plan.Action = 'Crear'
        if ($Snapshots.Count -eq 2) { $plan.RetireId = $previous.Id }
    }
    return $plan
}

function Invoke-MediaBaselineSnapshot {
    <# .SYNOPSIS
    Consulta únicamente las instantáneas de la VM propia y crea una si el plan lo permite.
    #>
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
    param(
        [Parameter(Mandatory)][ValidatePattern('^(00-pre-bootstrap|01-vm-limpia|pre-cambio-[a-z0-9][a-z0-9-]{0,47})\z')][string] $Name,
        [Parameter()][string] $VaultPath = (Join-Path $PSScriptRoot '../.env')
    )
    $ErrorActionPreference = 'Stop'
    . (Join-Path $PSScriptRoot 'media-vm-config.ps1')
    $configuration = Get-MediaVmConfiguration -VaultPath $VaultPath
    Import-Module VMware.VimAutomation.Core -ErrorAction Stop
    Set-PowerCLIConfiguration -Scope Session -InvalidCertificateAction Fail -Confirm:$false -WhatIf:$false | Out-Null
    $connection = $null
    try {
        $connection = Connect-VIServer -Server $configuration.Server -Credential $configuration.Credential -NotDefault -ErrorAction Stop
        $hosts = @(Get-VMHost -Server $connection -Name $configuration.HostAddress)
        if ($hosts.Count -ne 1) { throw 'El host autorizado no es único; no se crea ninguna instantánea.' }
        $machines = @(Get-VM -Server $connection -Id $configuration.RegisteredId)
        if ($machines.Count -ne 1) { throw 'La VM respaldada no es única; no se crea ninguna instantánea.' }
        $machine = $machines[0]
        # Se valida identidad antes de consultar; reutilizar no exige volver a apagar la VM.
        Test-MediaBaselineSnapshotTarget -Machine $machine -Configuration $configuration -TargetHostId $hosts[0].Id | Out-Null
        $snapshots = @(Get-Snapshot -Server $connection -VM $machine)
        if ($Name -clike 'pre-cambio-*') {
            $plan = Get-MediaPreChangeSnapshotPlan -Machine $machine -Configuration $configuration -TargetHostId $hosts[0].Id -Snapshots $snapshots -Name $Name
        } else {
            $plan = Get-MediaBaselineSnapshotPlan -Machine $machine -Configuration $configuration -TargetHostId $hosts[0].Id -Snapshots $snapshots -Name $Name
        }
        if ($plan.Action -eq 'Bloqueado' -and -not $WhatIfPreference) {
            throw ('No se puede preparar la instantánea previa al cambio. ' + $plan.Reason)
        }
        $created = $false
        $snapshotId = $plan.Id
        $createdAtUtc = $plan.CreatedAtUtc
        $operation = "Crear la instantánea propia $Name sin memoria con la VM apagada"
        if ($plan.RetireId) { $operation += " después de consolidar la instantánea anterior $($plan.RetireId)" }
        if ($plan.Action -eq 'Crear' -and $PSCmdlet.ShouldProcess($configuration.Name, $operation)) {
            if ($plan.RetireId) {
                $previous = $snapshots | Where-Object Id -CEQ $plan.RetireId
                Remove-Snapshot -Snapshot $previous -RemoveChildren:$false -Confirm:$false -ErrorAction Stop | Out-Null

                # Volver a comprobar la VM y la capacidad al terminar la consolidación.
                $machine = Get-VM -Server $connection -Id $configuration.RegisteredId
                $snapshots = @(Get-Snapshot -Server $connection -VM $machine)
                $plan = Get-MediaPreChangeSnapshotPlan -Machine $machine -Configuration $configuration -TargetHostId $hosts[0].Id -Snapshots $snapshots -Name $Name
                if ($plan.Action -ne 'Crear' -or $plan.RetireId) {
                    throw 'El inventario cambió durante la consolidación. Ejecuta la comprobación de nuevo.'
                }
            }
            $snapshot = New-Snapshot -Server $connection -VM $machine -Name $Name -Description $plan.Description -Memory:$false -Quiesce:$false -Confirm:$false -ErrorAction Stop
            $snapshotId = $snapshot.Id
            $createdAtUtc = $snapshot.Created.ToUniversalTime().ToString('o')
            $created = $true
        }
        # No se consulta ni restaura el ID recién generado antes de respaldarlo en la bóveda.
        [pscustomobject]@{
            VmName = $configuration.Name
            VmId = $configuration.RegisteredId
            Name = $Name
            Id = $snapshotId
            Action = $plan.Action
            Reason = $plan.Reason
            Created = $created
            CreatedAtUtc = $createdAtUtc
            Reused = $plan.Action -eq 'Reutilizar'
            Simulation = [bool] $WhatIfPreference
            Memory = $false
            Quiesce = $false
            NextAction = 'Respaldar inmediatamente el ID real en .env y su clave CHANGE_ME en .env.example antes de usar la instantánea.'
        }
    }
    finally {
        if ($connection) { Disconnect-VIServer -Server $connection -Confirm:$false -WhatIf:$false -ErrorAction Continue | Out-Null }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-MediaBaselineSnapshot -Name $BaselineName -VaultPath $BaselineVaultPath
}
