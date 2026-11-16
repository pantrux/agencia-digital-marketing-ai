<#
.SYNOPSIS
Audita o configura exclusivamente las cinco reglas propias del puente GPU.
.DESCRIPTION
Audit es el modo predeterminado. Apply exige elevación y admite WhatIf.
Importar con dot-source solo define funciones. No modifica perfiles, reglas
ajenas, credenciales ni servicios; no solicita UAC ni abre conexiones.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param([Parameter()][Alias('Mode')][ValidateSet('Audit','Apply')][string] $AgencyGpuFirewallMode = 'Audit')

function ConvertTo-AgencyGpuIpv4Number {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Address)
    if ($Address -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw 'El complemento requiere direcciones IPv4 completas.' }
    $number = [uint64]0
    foreach ($part in $Address.Split('.')) {
        if ([int]$part -gt 255) { throw 'El complemento contiene una dirección IPv4 inválida.' }
        $number = $number * 256 + [uint64]$part
    }
    return $number
}

function Test-AgencyGpuFirewallComplement {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]] $Ranges)
    # Los intervalos excluidos son exactamente 127/8 y los tres clientes del laboratorio.
    $excluded = @(
        [pscustomobject]@{Start=(ConvertTo-AgencyGpuIpv4Number '127.0.0.0'); End=(ConvertTo-AgencyGpuIpv4Number '127.255.255.255')}
        [pscustomobject]@{Start=(ConvertTo-AgencyGpuIpv4Number '192.168.0.7'); End=(ConvertTo-AgencyGpuIpv4Number '192.168.0.7')}
        [pscustomobject]@{Start=(ConvertTo-AgencyGpuIpv4Number '192.168.0.8'); End=(ConvertTo-AgencyGpuIpv4Number '192.168.0.8')}
        [pscustomobject]@{Start=(ConvertTo-AgencyGpuIpv4Number '192.168.0.85'); End=(ConvertTo-AgencyGpuIpv4Number '192.168.0.85')}
    )
    $expected = @(); $cursor = [uint64]0
    foreach ($interval in $excluded) {
        if ($cursor -lt $interval.Start) { $expected += [pscustomobject]@{Start=$cursor; End=($interval.Start - 1)} }
        $cursor = $interval.End + 1
    }
    $expected += [pscustomobject]@{Start=$cursor; End=[uint64]4294967295}
    $actual = @($Ranges | ForEach-Object {
        $parts = $_.Split('-')
        if ($parts.Count -ne 2) { throw 'El complemento exige intervalos IPv4 explícitos.' }
        $start = ConvertTo-AgencyGpuIpv4Number $parts[0]; $end = ConvertTo-AgencyGpuIpv4Number $parts[1]
        if ($start -gt $end) { throw 'El complemento contiene un intervalo invertido.' }
        [pscustomobject]@{Start=$start; End=$end}
    } | Sort-Object Start)
    if ($actual.Count -ne $expected.Count) { throw 'El complemento IPv4 no conserva exactamente clientes y loopback.' }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if ($actual[$index].Start -ne $expected[$index].Start -or $actual[$index].End -ne $expected[$index].End) { throw 'El complemento IPv4 no conserva exactamente clientes y loopback.' }
    }
}

function Get-AgencyGpuFirewallContract {
    [CmdletBinding()]
    param()
    $group = 'Agencia Digital AI - Puente GPU v1'
    $description = 'Propiedad: Agencia Digital de Marketing AI; recurso: gpu-bridge-win11; contrato: 1.'
    $ports = @('3003','8000','8090','9835')
    $clients = @('192.168.0.85','192.168.0.7','192.168.0.8')
    $ranges = @('0.0.0.0-126.255.255.255','128.0.0.0-192.168.0.6','192.168.0.9-192.168.0.84','192.168.0.86-255.255.255.255')
    Test-AgencyGpuFirewallComplement -Ranges $ranges
    # Bloquear primero impide que reglas Allow amplias ensanchen estos puertos exclusivos.
    $rules = @([pscustomobject]@{Name='AgencyGPU-Block-IPv4-v1'; DisplayName='Puente GPU: bloquear otros clientes IPv4'; Group=$group; Description=$description; Action='Block'; LocalAddress=@('Any'); RemoteAddress=$ranges; LocalPort=$ports})
    foreach ($port in $ports) {
        $rules += [pscustomobject]@{Name="AgencyGPU-Allow-TCP-$port-v1"; DisplayName="Puente GPU: clientes autorizados TCP $port"; Group=$group; Description=$description; Action='Allow'; LocalAddress=@('Any'); RemoteAddress=$clients; LocalPort=@($port)}
    }
    return [pscustomobject]@{SchemaVersion=1; Rules=$rules; LoopbackExcluded='127.0.0.0/8'; PolicyStore='PersistentStore'}
}

function Test-AgencyGpuFirewallAdministrator {
    [CmdletBinding()]
    param()
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try { return ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
    finally { $identity.Dispose() }
}

function Test-AgencyGpuFirewallCommandContract {
    [CmdletBinding()]
    param()
    $required = @{
        'New-NetFirewallRule'=@('Name','DisplayName','Description','Group','PolicyStore','Enabled','Direction','Action','Profile','EdgeTraversalPolicy','Protocol','LocalPort','RemotePort','LocalAddress','RemoteAddress','WhatIf','Confirm')
        'Set-NetFirewallRule'=@('Name','NewDisplayName','Description','Group','PolicyStore','Enabled','Direction','Action','Profile','EdgeTraversalPolicy','Protocol','LocalPort','RemotePort','LocalAddress','RemoteAddress','WhatIf','Confirm')
        'Get-NetFirewallRule'=@('PolicyStore')
        'Get-NetFirewallProfile'=@('PolicyStore')
        'Get-NetFirewallPortFilter'=@('AssociatedNetFirewallRule')
        'Get-NetFirewallAddressFilter'=@('AssociatedNetFirewallRule')
    }
    foreach ($name in $required.Keys) {
        $command = Get-Command -Name $name -ErrorAction Stop
        foreach ($parameter in $required[$name]) {
            if (-not $command.Parameters.ContainsKey($parameter)) { throw "El cmdlet $name no cumple el contrato requerido." }
        }
    }
}

function Test-AgencyGpuFirewallStringSet {
    [CmdletBinding()]
    param([AllowNull()][object[]] $Actual, [Parameter(Mandatory)][string[]] $Expected)
    $left = @($Actual | ForEach-Object { ([string]$_).ToLowerInvariant() } | Sort-Object -Unique)
    $right = @($Expected | ForEach-Object { $_.ToLowerInvariant() } | Sort-Object -Unique)
    return [string]::Join('|', $left) -ceq [string]::Join('|', $right)
}

function Get-AgencyGpuFirewallPlan {
    [CmdletBinding()]
    param([ValidateSet('PersistentStore','ActiveStore')][string] $PolicyStore = 'PersistentStore')
    $contract = Get-AgencyGpuFirewallContract
    $all = @(Get-NetFirewallRule -PolicyStore $PolicyStore -ErrorAction Stop)
    # Validar TODAS las colisiones de nombre antes de consultar filtros o modificar algo.
    foreach ($expected in $contract.Rules) {
        $found = @($all | Where-Object Name -EQ $expected.Name)
        if ($found.Count -gt 1 -or ($found.Count -eq 1 -and ($found[0].Group -cne $expected.Group -or $found[0].Description -cne $expected.Description))) { throw "La regla $($expected.Name) es ajena o ambigua; no se modifica ninguna regla." }
    }
    foreach ($expected in $contract.Rules) {
        $found = @($all | Where-Object Name -EQ $expected.Name)
        $change = 'Create'
        if ($found.Count -eq 1) {
            $rule = $found[0]
            $port = @(Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop)
            $address = @(Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop)
            if ($port.Count -ne 1 -or $address.Count -ne 1) { throw 'Una regla propia tiene filtros ambiguos.' }
            $ruleMatches = [string]$rule.Enabled -eq 'True' -and [string]$rule.Profile -eq 'Any' -and [string]$rule.Direction -eq 'Inbound' -and [string]$rule.Action -eq $expected.Action -and [string]$rule.EdgeTraversalPolicy -eq 'Block' -and [string]$port[0].Protocol -in @('TCP','6')
            $ruleMatches = $ruleMatches -and (Test-AgencyGpuFirewallStringSet $port[0].LocalPort $expected.LocalPort) -and (Test-AgencyGpuFirewallStringSet $port[0].RemotePort @('Any')) -and (Test-AgencyGpuFirewallStringSet $address[0].LocalAddress $expected.LocalAddress) -and (Test-AgencyGpuFirewallStringSet $address[0].RemoteAddress $expected.RemoteAddress)
            $change = if ($ruleMatches) { 'None' } else { 'Update' }
        }
        [pscustomobject]@{Name=$expected.Name; Change=$change; Expected=$expected}
    }
}

function Invoke-AgencyGpuFirewall {
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
    param([ValidateSet('Audit','Apply')][string] $Mode = 'Audit')
    Test-AgencyGpuFirewallCommandContract
    if ($Mode -eq 'Apply' -and -not $WhatIfPreference -and -not (Test-AgencyGpuFirewallAdministrator)) { throw 'Apply requiere una sesión elevada; no se solicita UAC automáticamente.' }
    $plan = @(Get-AgencyGpuFirewallPlan)
    # Una regla GPO/MDM con el mismo nombre también debe rechazarse antes de escribir.
    $null = @(Get-AgencyGpuFirewallPlan -PolicyStore ActiveStore)
    $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
    $profilesReady = $profiles.Count -eq 3 -and @($profiles | Where-Object { [string]$_.Enabled -ne 'True' -or [string]$_.AllowLocalFirewallRules -eq 'False' }).Count -eq 0
    if ($Mode -eq 'Apply' -and -not $profilesReady) { throw 'Un perfil de firewall no está activo o no admite reglas locales; no se cambia el perfil.' }
    $changed = 0
    if ($Mode -eq 'Apply') {
        foreach ($item in $plan) {
            if ($item.Change -eq 'None') { continue }
            if ($PSCmdlet.ShouldProcess($item.Name, 'Aplicar contrato propio del puente GPU')) {
                $expected = $item.Expected
                $parameters = @{Name=$expected.Name; Description=$expected.Description; Group=$expected.Group; PolicyStore='PersistentStore'; Enabled='True'; Profile='Any'; Direction='Inbound'; Action=$expected.Action; EdgeTraversalPolicy='Block'; Protocol='TCP'; LocalPort=$expected.LocalPort; RemotePort=@('Any'); LocalAddress=$expected.LocalAddress; RemoteAddress=$expected.RemoteAddress; ErrorAction='Stop'; Confirm=$false}
                if ($item.Change -eq 'Create') { New-NetFirewallRule @parameters -DisplayName $expected.DisplayName | Out-Null }
                else { Set-NetFirewallRule @parameters -NewDisplayName $expected.DisplayName | Out-Null }
                $changed++
            }
        }
    }
    $verified = $false
    if ($Mode -eq 'Apply' -and -not $WhatIfPreference) {
        $after = @(Get-AgencyGpuFirewallPlan)
        $active = @(Get-AgencyGpuFirewallPlan -PolicyStore ActiveStore)
        if (@($after | Where-Object Change -NE 'None').Count -gt 0 -or @($active | Where-Object Change -NE 'None').Count -gt 0) { throw 'La política efectiva no coincide con las reglas propias persistidas; no se acredita el cortafuegos.' }
        $verified = $true
    }
    [pscustomobject]@{Mode=$Mode; Changed=$changed; PlannedChanges=@($plan | Where-Object Change -NE 'None').Count; Verified=$verified; ProfilesReady=$profilesReady; Plan=$plan; NetworkTestPerformed=$false; OtherRulesModified=$false}
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-AgencyGpuFirewall -Mode $AgencyGpuFirewallMode -WhatIf:$WhatIfPreference | ConvertTo-Json -Depth 8
}
