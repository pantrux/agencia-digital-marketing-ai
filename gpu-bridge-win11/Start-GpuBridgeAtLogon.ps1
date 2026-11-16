#requires -Version 7.4
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$taskStartedUtc = [datetime]::UtcNow

try {
    if (-not $IsWindows -or $env:COMPUTERNAME -ine 'W11-HOMELAB' -or $PSScriptRoot -ine 'C:\GPU-Bridge') {
        throw 'La tarea requiere la instalación privada del puente GPU.'
    }

    . (Join-Path $PSScriptRoot 'Stop-GpuBridge.ps1')
    $context = Get-AgencyGpuStopContext
    $mappingReady = $false
    $mappingRebuilt = $false
    $smbTarget = '\\192.168.0.8\producciones'
    $credentialPath = Join-Path $context.InstallRoot 'smb-logon-credential.xml'
    $clock = [Diagnostics.Stopwatch]::StartNew()
    do {
        $mapping = @(Get-SmbMapping -LocalPath 'P:' -ErrorAction SilentlyContinue)
        if ($mapping.Count -gt 1 -or ($mapping.Count -eq 1 -and $mapping[0].RemotePath -ine $smbTarget)) {
            throw 'La unidad P: pertenece a otro recurso.'
        }
        if ($mapping.Count -eq 1 -and $mapping[0].RequirePrivacy -ne $true -and
            ($null -ne $mapping[0].RequirePrivacy -or $mapping[0].Status -notin @(6, 'Unavailable', 'Disconnected'))) {
            throw 'La unidad P: carece de cifrado exigido y no acredita una reserva desconectada.'
        }
        if ($mapping.Count -eq 0 -or $null -eq $mapping[0].RequirePrivacy) {
            $persistentMapping = Get-ItemProperty -LiteralPath 'HKCU:\Network\P' -ErrorAction SilentlyContinue
            if ($null -ne $persistentMapping) {
                if ($persistentMapping.RemotePath -ine $smbTarget) { throw 'El perfil persistente de P: pertenece a otro recurso.' }
                Assert-AgencyGpuPrivatePath -LiteralPath $credentialPath -UserSid $context.UserSid -InstallRoot $context.InstallRoot
                $credential = Import-Clixml -LiteralPath $credentialPath
                if ($credential -isnot [pscredential]) { throw 'La credencial privada de reconexión no es válida.' }
                if ($mapping.Count -eq 1) {
                    # Conserva la reserva de usuario para que un fallo pueda reintentarse.
                    Remove-SmbMapping -InputObject $mapping[0] -Confirm:$false | Out-Null
                }
                New-SmbMapping -LocalPath 'P:' -RemotePath $smbTarget -Credential $credential -Persistent:$true -RequirePrivacy:$true -Confirm:$false | Out-Null
                $mappingRebuilt = $true
                $mapping = @(Get-SmbMapping -LocalPath 'P:' -ErrorAction SilentlyContinue)
            }
        }
        if ($mapping.Count -eq 1 -and $mapping[0].RemotePath -ieq $smbTarget -and $mapping[0].RequirePrivacy -eq $true) {
            # El primer acceso puede reconectar el recurso persistente al iniciar sesión.
            if (Test-Path -LiteralPath 'P:\proyectos' -PathType Container -ErrorAction SilentlyContinue) {
                $connected = @(Get-SmbMapping -LocalPath 'P:' -ErrorAction SilentlyContinue)
                if ($connected.Count -eq 1 -and $connected[0].RemotePath -ieq $smbTarget -and
                    $connected[0].RequirePrivacy -eq $true -and $connected[0].Status -in @(0, 'OK')) {
                    $mappingReady = $true
                    break
                }
            }
        }
        Start-Sleep -Seconds 3
    } while ($clock.Elapsed.TotalSeconds -lt 75)
    if (-not $mappingReady) { throw 'El recurso cifrado P: no está disponible.' }

    $startedByTask = $false
    if (-not (Get-NetTCPConnection -State Listen -LocalPort 9835 -ErrorAction SilentlyContinue)) {
        $adapters = @(Get-CimInstance -ClassName Win32_PnPEntity -Filter "PNPClass='Display' AND Present=True" -OperationTimeoutSec 5 -ErrorAction SilentlyContinue |
            Where-Object { $_.DeviceID -like 'PCI\VEN_10DE&DEV_*' -and $_.Name -ieq 'NVIDIA GeForce RTX 3060 Ti' })
        if ($adapters.Count -eq 1 -and $adapters[0].ConfigManagerErrorCode -eq 43) {
            [Console]::Error.WriteLine('Windows informa el error 43 para la NVIDIA GeForce RTX 3060 Ti.')
            exit 43
        }
        $outcome = & (Join-Path $PSScriptRoot 'start-gpu-bridge.ps1') -Start
        if ($outcome.Valid -ne $true -or $outcome.Started -ne $true) {
            throw 'El lanzador no confirmó el arranque.'
        }
        $startedByTask = $true
    }

    $running = Invoke-AgencyGpuStop
    $supervisor = Read-AgencyGpuStartReceipt -Context $context
    if ($running.IdentityVerified -ne $true -or $running.Stopped -or $running.ProcessId -ne $supervisor.ProcessId) {
        throw 'El listener actual no acredita la identidad del supervisor.'
    }
    $bootUtc = (Get-CimInstance -ClassName Win32_OperatingSystem -OperationTimeoutSec 5).LastBootUpTime.ToUniversalTime()
    $supervisorUtc = [datetime]::FromFileTimeUtc([long]$supervisor.CreationFileTimeUtc)
    if ($supervisorUtc -lt $bootUtc -or $taskStartedUtc -lt $bootUtc -or ($startedByTask -and $supervisorUtc -lt $taskStartedUtc)) {
        throw 'Las horas del supervisor y del arranque de Windows no son coherentes.'
    }
    $proof = [ordered]@{
        Esquema = 1
        ArranqueWindowsUtc = $bootUtc.ToString('o')
        InicioTareaUtc = $taskStartedUtc.ToString('o')
        InicioSupervisorUtc = $supervisorUtc.ToString('o')
        SupervisorPid = $running.ProcessId
        SupervisorIniciadoPorTarea = $startedByTask
        SupervisorIdentidadVerificada = $true
        RecursoSmbCifradoDisponible = $true
        MapeoSmbReconstruido = $mappingRebuilt
        PowerShell = $PSVersionTable.PSVersion.ToString()
    }
    $destination = Join-Path $PSScriptRoot 'logon-start.json'
    if (Test-Path -LiteralPath $destination) {
        Assert-AgencyGpuPrivatePath -LiteralPath $destination -UserSid $context.UserSid -InstallRoot $context.InstallRoot
        $previous = Get-Item -LiteralPath $destination
        if ($previous.PSIsContainer) { throw 'El recibo de inicio no es un archivo.' }
    }
    $temporary = Join-Path $PSScriptRoot ('.logon-start-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, ($proof | ConvertTo-Json -Compress) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($temporary, $destination, $true)
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
} catch {
    [Console]::Error.WriteLine('El arranque programado del puente GPU no quedó verificado.')
    exit 2
}
