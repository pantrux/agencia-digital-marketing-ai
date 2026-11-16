<#
.SYNOPSIS
Prepara o verifica exclusivamente P: hacia el recurso de producciones, con cifrado obligatorio.
.DESCRIPTION
Importar con dot-source solo define funciones. -AuditOnly y -WhatIf no autentican ni
mapean. La operación real requiere credenciales ya respaldadas y SHA256 del archivo
P:\.probe preparado desde Linux; nunca crea ese archivo ni elimina conexiones ajenas.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter()][Alias('VaultPath')][string] $AgencySmbVaultPath = (Join-Path $PSScriptRoot '../.env'),
    [Parameter()][Alias('AuditOnly')][switch] $AgencySmbAuditOnly,
    [Parameter()][Alias('ExpectedProbeSha256')][string] $AgencySmbExpectedProbeSha256 = ''
)

function Get-AgencySmbConfiguration {
    <# .SYNOPSIS
    Lee credenciales persistidas y devuelve PSCredential sin publicar su contenido.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $VaultPath)
    # Mismo contrato literal que check_env.py: comillas exteriores o comentario
    # precedido de espacio fuera de comillas; no se interpretan sustituciones.
    $readValues = {
        param([string] $LiteralPath)
        $parsed = @{}
        foreach ($rawLine in Get-Content -LiteralPath $LiteralPath -Encoding utf8 -ErrorAction Stop) {
            $line = $rawLine.Trim()
            if (-not $line -or $line.StartsWith('#')) { continue }
            if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { throw 'El archivo de variables contiene una asignación inválida.' }
            $key = $Matches[1]
            if ($key -notin @('SYNOLOGY_USER','SYNOLOGY_PASSWORD')) { continue }
            if ($parsed.ContainsKey($key)) { throw 'El archivo de variables contiene una clave SMB duplicada.' }
            $value = $Matches[2].Trim()
            if ($value.StartsWith('"') -or $value.StartsWith("'")) {
                if ($value.Length -lt 2 -or $value[$value.Length - 1] -cne $value[0]) { throw "Comillas sin cerrar en $key; el contenido permanece oculto." }
                $value = $value.Substring(1, $value.Length - 2)
            }
            else { $value = ($value -split '\s+#', 2)[0].TrimEnd() }
            $parsed[$key] = $value
        }
        return $parsed
    }
    $values = & $readValues -LiteralPath $VaultPath
    $pending = [Collections.Generic.List[string]]::new()
    foreach ($key in @('SYNOLOGY_USER','SYNOLOGY_PASSWORD')) {
        if (-not $values.ContainsKey($key) -or [string]::IsNullOrWhiteSpace($values[$key]) -or
            $values[$key] -match '^(?:CHANGE_ME|TODO|PENDIENTE|GENERAR|CREAR|COMPLETAR|REEMPLAZAR|REPLACE_ME)(?:$|[_ -])|^<.*>$') { $pending.Add($key) }
    }
    $examplePath = Join-Path (Split-Path -Parent ([IO.Path]::GetFullPath($VaultPath))) '.env.example'
    $template = if (Test-Path -LiteralPath $examplePath -PathType Leaf) { & $readValues -LiteralPath $examplePath } else { @{} }
    foreach ($key in @('SYNOLOGY_USER','SYNOLOGY_PASSWORD')) {
        if (-not $template.ContainsKey($key) -or $template[$key] -cne 'CHANGE_ME') { $pending.Add("Plantilla:$key") }
    }
    $credential = $null
    if ($pending.Count -eq 0) {
        $secure = [Security.SecureString]::new()
        foreach ($character in $values.SYNOLOGY_PASSWORD.ToCharArray()) { $secure.AppendChar($character) }
        $secure.MakeReadOnly()
        $credential = [pscredential]::new($values.SYNOLOGY_USER, $secure)
    }
    [pscustomobject]@{ CredentialReady=($pending.Count -eq 0); Credential=$credential; PendingKeys=@($pending) }
}

function Test-AgencySmbCommandContract {
    <# .SYNOPSIS
    Comprueba un conjunto real compatible con PSCredential, persistencia y cifrado.
    #>
    [CmdletBinding()]
    param()
    $command = Get-Command -Name New-SmbMapping -ErrorAction Stop
    $required = @('LocalPath','RemotePath','Credential','Persistent','RequirePrivacy','Confirm')
    foreach ($name in $required) {
        if (-not $command.Parameters.ContainsKey($name)) { throw 'New-SmbMapping no expone el contrato seguro requerido.' }
    }
    if ($command.Parameters.Credential.ParameterType -ne [pscredential] -or
        $command.Parameters.RequirePrivacy.ParameterType -ne [bool] -or
        $command.Parameters.Persistent.ParameterType -ne [bool]) { throw 'Los tipos de parámetros SMB no coinciden con el contrato seguro.' }
    $sets = @($command.ParameterSets | Where-Object {
        $names = @($_.Parameters.Name)
        @($required | Where-Object { $_ -notin $names }).Count -eq 0
    })
    if ($sets.Count -eq 0) { throw 'Los parámetros SMB seguros pertenecen a conjuntos incompatibles.' }
    return $true
}

function Get-AgencySmbLocalState {
    <# .SYNOPSIS
    Consulta la ocupación local de P: sin autenticar contra el NAS.
    #>
    [CmdletBinding()]
    param()
    try {
        $mappings = @(Get-SmbMapping -ErrorAction Stop | Where-Object LocalPath -EQ 'P:')
        $disks = @(Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='P:'" -ErrorAction Stop)
        $drives = @(Get-PSDrive -PSProvider FileSystem -ErrorAction Stop | Where-Object Name -EQ 'P')
        $persistent = $null
        if (Test-Path -LiteralPath 'HKCU:\Network\P' -ErrorAction Stop) { $persistent = Get-ItemProperty -LiteralPath 'HKCU:\Network\P' -ErrorAction Stop }
        [pscustomobject]@{ Mappings=$mappings; LogicalDrives=$disks; PowerShellDrives=$drives; PersistentMapping=$persistent }
    }
    catch { throw 'No se pudo acreditar la ocupación de P:; no se mapeará.' }
}

function Test-AgencySmbDisposition {
    <# .SYNOPSIS
    Rechaza discos, recursos o reservas ajenos y exige cifrado en un mapping existente.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)] $State)
    $remote = '\\192.168.0.8\producciones'
    $targets = [Collections.Generic.List[string]]::new()
    foreach ($mapping in $State.Mappings) { $targets.Add([string] $mapping.RemotePath) }
    foreach ($disk in $State.LogicalDrives) {
        if ($disk.DriveType -ne 4) { throw 'P: está ocupada por un disco de otro uso; se conserva.' }
        $targets.Add([string] $disk.ProviderName)
    }
    foreach ($drive in $State.PowerShellDrives) {
        if ($drive.DisplayRoot) { $targets.Add([string] $drive.DisplayRoot) }
        elseif ($drive.Root -ine 'P:\') { $targets.Add([string] $drive.Root) }
    }
    if ($null -ne $State.PersistentMapping) { $targets.Add([string] $State.PersistentMapping.RemotePath) }
    foreach ($target in $targets) {
        if ($target.TrimEnd('\') -ine $remote) { throw 'P: pertenece a otro destino o proyecto; no se reemplaza.' }
    }
    $present = @($State.Mappings).Count -gt 0 -or @($State.LogicalDrives).Count -gt 0 -or @($State.PowerShellDrives).Count -gt 0 -or $null -ne $State.PersistentMapping
    if ($present) {
        if (@($State.Mappings).Count -ne 1 -or $State.Mappings[0].RequirePrivacy -ne $true) {
            throw 'P: está ocupada o reservada sin política de cifrado acreditada; no se remapea.'
        }
        if ($null -eq $State.PersistentMapping) { throw 'P: no tiene persistencia acreditada en el usuario actual; no se remapea.' }
    }
    [pscustomobject]@{ AlreadyMapped=[bool]$present; LocalPath='P:'; RemotePath=$remote; RequirePrivacy=$true; Persistent=[bool]$present }
}

function Read-AgencySmbProbe {
    <# .SYNOPSIS
    Lee únicamente el archivo de prueba existente; no escribe ni publica sus bytes.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string] $ExpectedSha256)
    $path = 'P:\.probe'
    try {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -gt 1048576) { throw 'Archivo no admitido.' }
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        if ($hash -ine $ExpectedSha256) { throw 'No coincide el archivo de prueba.' }
        [pscustomobject]@{ Path=$path; Sha256=$hash; Bytes=$item.Length; MatchesExpected=$true }
    }
    catch { throw 'No se pudo leer P:\.probe o su SHA256 no coincide con la prueba preparada en Linux.' }
}

function Invoke-AgencySmbMapping {
    <# .SYNOPSIS
    Mapea solo P: libre o reutiliza el destino exacto y comprueba la prueba coordinada.
    #>
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
    param(
        [Parameter()][string] $VaultPath = (Join-Path $PSScriptRoot '../.env'),
        [Parameter()][switch] $AuditOnly,
        [Parameter()][string] $ExpectedProbeSha256 = ''
    )
    $ErrorActionPreference = 'Stop'
    $configuration = Get-AgencySmbConfiguration -VaultPath $VaultPath
    Test-AgencySmbCommandContract | Out-Null
    $disposition = Test-AgencySmbDisposition -State (Get-AgencySmbLocalState)
    $blockers = @($configuration.PendingKeys | ForEach-Object { "Falta credencial o plantilla preparada: $_" })
    if ($ExpectedProbeSha256 -notmatch '^[A-Fa-f0-9]{64}$') { $blockers += 'Falta SHA256 de P:\.probe coordinado desde Linux.' }
    if ($AuditOnly -or $WhatIfPreference) {
        if ($WhatIfPreference -and $blockers.Count -eq 0) { $PSCmdlet.ShouldProcess('P: -> \\192.168.0.8\producciones', 'Exigir cifrado, persistir el mapping y leer la prueba Linux') | Out-Null }
        return [pscustomobject]@{ AuditOnly=[bool]$AuditOnly; Simulation=[bool]$WhatIfPreference; AlreadyMapped=$disposition.AlreadyMapped; CredentialReady=$configuration.CredentialReady; Blockers=$blockers; Changed=$false; ProbeRead=$false }
    }
    if ($blockers.Count -gt 0) { throw ($blockers -join ' ') }
    $changed = $false
    if (-not $disposition.AlreadyMapped) {
        if (-not $PSCmdlet.ShouldProcess('P: -> \\192.168.0.8\producciones', 'Crear mapping persistente cifrado en el contexto del usuario actual')) { return }
        try {
            New-SmbMapping -LocalPath 'P:' -RemotePath '\\192.168.0.8\producciones' -Credential $configuration.Credential -Persistent:$true -RequirePrivacy:$true -Confirm:$false -ErrorAction Stop -Verbose:$false -Debug:$false | Out-Null
            $changed = $true
        }
        catch { throw 'No se pudo crear el mapping SMB cifrado. Revisar credenciales, permisos y sesiones del NAS; no se eliminaron conexiones existentes.' }
    }
    $verified = Test-AgencySmbDisposition -State (Get-AgencySmbLocalState)
    if (-not $verified.AlreadyMapped) { throw 'No se pudo acreditar el mapping esperado después de la operación.' }
    $probe = Read-AgencySmbProbe -ExpectedSha256 $ExpectedProbeSha256
    [pscustomobject]@{ Changed=$changed; LocalPath='P:'; RemotePath=$verified.RemotePath; RequirePrivacy=$true; Persistent=$verified.Persistent; CredentialManagerModified=$false; Probe=$probe; SessionDialectVerified=$false }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-AgencySmbMapping -VaultPath $AgencySmbVaultPath -AuditOnly:$AgencySmbAuditOnly -ExpectedProbeSha256 $AgencySmbExpectedProbeSha256 -WhatIf:$WhatIfPreference
}
