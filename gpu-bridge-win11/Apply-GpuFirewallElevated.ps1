#requires -Version 7.4
<#
.SYNOPSIS
Aplica el cortafuegos propio desde una sesión que ya está elevada y guarda un recibo.
.DESCRIPTION
No solicita UAC, inicia tareas ni modifica perfiles. Verifica la instalación privada
y la huella respaldada antes de invocar únicamente Configure-GpuFirewall.ps1.
Dot-source solo define funciones. Un destino privado no acreditado no se escribe.
#>
[CmdletBinding()]
param([string] $RuntimeConfigPath = $env:GPU_RUNTIME_CONFIG_PATH)

function Test-AgencyGpuElevatedHost {
    return $IsWindows -and [Environment]::MachineName -ieq 'W11-HOMELAB'
}

function Get-AgencyGpuElevatedUserSid {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try { return $identity.User.Value } finally { $identity.Dispose() }
}

function Test-AgencyGpuElevatedAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        return ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
    } finally { $identity.Dispose() }
}

function Assert-AgencyGpuElevatedPrivatePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $LiteralPath,
        [Parameter(Mandatory)][string] $UserSid,
        [Parameter(Mandatory)][string] $InstallRoot,
        [switch] $Directory
    )
    $current = [IO.Path]::GetFullPath($LiteralPath)
    if ($current -ine $InstallRoot -and -not $current.StartsWith(
            $InstallRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'La ruta no pertenece a la instalación propia.'
    }
    $allowed = @($UserSid, 'S-1-5-18', 'S-1-5-32-544')
    $first = $true
    while ($true) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'La ruta privada contiene un punto de reanálisis.'
        }
        if (($first -and [bool]$item.PSIsContainer -ne [bool]$Directory) -or
                (-not $first -and -not $item.PSIsContainer)) {
            throw 'El tipo de la ruta privada no corresponde al contrato.'
        }
        $acl = Get-Acl -LiteralPath $current -ErrorAction Stop
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowed) {
            throw 'El propietario de la ruta privada es ajeno.'
        }
        foreach ($entry in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if ($entry.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                    $entry.IdentityReference.Value -notin $allowed) {
                throw 'La ruta privada concede acceso a otra identidad.'
            }
        }
        if ($current -ieq $InstallRoot) { break }
        $current = Split-Path -Parent $current
        $first = $false
    }
}

function Read-AgencyGpuElevatedJson {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $LiteralPath)
    $item = Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
    if ($item.PSIsContainer -or $item.Length -gt 65536) {
        throw 'La configuración privada no es un archivo acotado.'
    }
    $text = Get-Content -LiteralPath $LiteralPath -Raw -Encoding utf8 -ErrorAction Stop
    $document = [Text.Json.JsonDocument]::Parse($text)
    try {
        if ($document.RootElement.ValueKind -ne [Text.Json.JsonValueKind]::Object) {
            throw 'La configuración privada debe ser un objeto.'
        }
        $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($property in $document.RootElement.EnumerateObject()) {
            if (-not $names.Add($property.Name) -or
                    $property.Name -cnotmatch '^GPU_[A-Z0-9_]+$' -or
                    $property.Value.ValueKind -ne [Text.Json.JsonValueKind]::String) {
                throw 'La configuración privada requiere claves únicas GPU y valores literales.'
            }
        }
    } finally { $document.Dispose() }
    return $text | ConvertFrom-Json -AsHashtable -ErrorAction Stop
}

function Get-AgencyGpuElevatedContext {
    [CmdletBinding()]
    param([string] $RuntimeConfigPath = $env:GPU_RUNTIME_CONFIG_PATH)
    if (-not (Test-AgencyGpuElevatedHost)) { throw 'Se requiere el host Windows propio.' }
    if (-not (Test-AgencyGpuElevatedAdministrator)) {
        throw 'Se requiere una sesión ya elevada; no se solicita UAC.'
    }
    $root = 'C:\GPU-Bridge'
    $configFile = Join-Path $root 'runtime.json'
    if ([string]::IsNullOrEmpty($RuntimeConfigPath)) { $RuntimeConfigPath = $configFile }
    if ($RuntimeConfigPath -ine $configFile) { throw 'La configuración no usa la ruta canónica.' }
    $sid = Get-AgencyGpuElevatedUserSid
    Assert-AgencyGpuElevatedPrivatePath -LiteralPath $root -InstallRoot $root -UserSid $sid -Directory
    Assert-AgencyGpuElevatedPrivatePath -LiteralPath $configFile -InstallRoot $root -UserSid $sid
    $config = Read-AgencyGpuElevatedJson -LiteralPath $configFile
    $scriptFile = Join-Path $root 'Configure-GpuFirewall.ps1'
    $receiptFile = Join-Path $root 'firewall-apply-receipt.json'
    $owner = 'agencia-digital-marketing-ai:gpu-bridge:v4.1'
    if ($config['GPU_INSTALL_ROOT'] -ine $root -or
            $config['GPU_RUNTIME_CONFIG_PATH'] -ine $configFile -or
            $config['GPU_WINDOWS_USER_SID'] -cne $sid -or
            $config['GPU_INSTALL_OWNER_MARKER'] -cne $owner -or
            $config['GPU_FIREWALL_SCRIPT_PATH'] -ine $scriptFile -or
            $config['GPU_FIREWALL_RECEIPT_PATH'] -ine $receiptFile -or
            $config['GPU_FIREWALL_SCRIPT_SHA256'] -cnotmatch '^[a-f0-9]{64}$') {
        throw 'La configuración no coincide con las identidades respaldadas.'
    }
    $marker = Join-Path $root '.agencia-gpu-owner'
    foreach ($path in @($marker, $scriptFile)) {
        Assert-AgencyGpuElevatedPrivatePath -LiteralPath $path -InstallRoot $root -UserSid $sid
    }
    if ((Get-Content -LiteralPath $marker -Raw -Encoding utf8 -ErrorAction Stop) -cne $owner) {
        throw 'El marcador de propiedad no corresponde a esta instalación.'
    }
    if ((Get-FileHash -LiteralPath $scriptFile -Algorithm SHA256 -ErrorAction Stop).Hash -ine
            $config['GPU_FIREWALL_SCRIPT_SHA256']) {
        throw 'La huella del script no coincide con su respaldo.'
    }
    if (Test-Path -LiteralPath $receiptFile) {
        Assert-AgencyGpuElevatedPrivatePath -LiteralPath $receiptFile -InstallRoot $root -UserSid $sid
    }
    return @{
        InstallRoot=$root; RuntimeConfigPath=$configFile; ScriptPath=$scriptFile
        ScriptSha256=$config['GPU_FIREWALL_SCRIPT_SHA256']; ReceiptPath=$receiptFile; UserSid=$sid
    }
}

function Write-AgencyGpuElevatedReceipt {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Context, [Parameter(Mandatory)] $Receipt)
    Assert-AgencyGpuElevatedPrivatePath -LiteralPath $Context.InstallRoot -InstallRoot $Context.InstallRoot -UserSid $Context.UserSid -Directory
    if (Test-Path -LiteralPath $Context.ReceiptPath) {
        Assert-AgencyGpuElevatedPrivatePath -LiteralPath $Context.ReceiptPath -InstallRoot $Context.InstallRoot -UserSid $Context.UserSid
    }
    $temporary = Join-Path $Context.InstallRoot ('.firewall-receipt-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Receipt | ConvertTo-Json -Depth 4 -Compress))
        $stream = [IO.FileStream]::new($temporary, [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
        finally { $stream.Dispose() }
        Assert-AgencyGpuElevatedPrivatePath -LiteralPath $temporary -InstallRoot $Context.InstallRoot -UserSid $Context.UserSid
        # Mismo directorio y volumen: nunca se publica un JSON parcialmente escrito.
        [IO.File]::Move($temporary, $Context.ReceiptPath, $true)
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Invoke-AgencyGpuElevatedApply {
    [CmdletBinding()]
    param([string] $RuntimeConfigPath = $env:GPU_RUNTIME_CONFIG_PATH)
    $ErrorActionPreference = 'Stop'
    $context = $null
    $receipt = [ordered]@{
        SchemaVersion=1; Operation='apply-gpu-firewall'; Success=$false; Verified=$false
        Stage='preflight'; ErrorCode='PREFLIGHT_REJECTED'; Changed=0; ReceiptWritten=$false
        TimestampUtc=[datetime]::UtcNow.ToString('o')
    }
    try {
        $context = Get-AgencyGpuElevatedContext -RuntimeConfigPath $RuntimeConfigPath
        $receipt['ScriptSha256'] = $context.ScriptSha256
        $receipt.Stage = 'apply'
        $receipt.ErrorCode = 'FIREWALL_APPLY_FAILED'
        # El main revisado ya devuelve JSON: capturarlo y analizarlo una única vez.
        $output = @(& $context.ScriptPath -Mode Apply -Confirm:$false)
        $json = [string]::Join([Environment]::NewLine, [string[]]$output)
        if ($json.Length -gt 65536) { throw 'La respuesta del cortafuegos es excesiva.' }
        $result = $json | ConvertFrom-Json -AsHashtable -ErrorAction Stop
        if ($result -isnot [System.Collections.IDictionary] -or
                $result['Mode'] -cne 'Apply' -or
                $result['Verified'] -isnot [bool] -or $result['Verified'] -ne $true -or
                $result['ProfilesReady'] -isnot [bool] -or $result['ProfilesReady'] -ne $true -or
                $result['OtherRulesModified'] -isnot [bool] -or $result['OtherRulesModified'] -ne $false -or
                $result['NetworkTestPerformed'] -isnot [bool] -or $result['NetworkTestPerformed'] -ne $false -or
                $result['Changed'] -isnot [long] -or $result['Changed'] -lt 0 -or $result['Changed'] -gt 5) {
            throw 'La respuesta no acredita exclusivamente las reglas propias.'
        }
        $receipt.Success = $true; $receipt.Verified = $true; $receipt.Changed = $result['Changed']
        $receipt.Stage = 'complete'; $receipt.ErrorCode = $null
    } catch {
        # No registrar Exception, argv, salida del hijo ni valores del JSON privado.
        $receipt.Success = $false; $receipt.Verified = $false
    }
    if ($null -ne $context) {
        try {
            $receipt.ReceiptWritten = $true
            Write-AgencyGpuElevatedReceipt -Context $context -Receipt $receipt
        } catch {
            $receipt.Success = $false; $receipt.Verified = $false; $receipt.ReceiptWritten = $false
            $receipt.Stage = 'receipt'; $receipt.ErrorCode = 'RECEIPT_WRITE_FAILED'
        }
    }
    return [pscustomobject]$receipt
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-AgencyGpuElevatedApply -RuntimeConfigPath $RuntimeConfigPath
    $result | ConvertTo-Json -Depth 4 -Compress
    if (-not $result.Success) { exit 2 }
}
