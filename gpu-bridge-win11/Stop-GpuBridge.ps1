#requires -Version 7.4
<#
.SYNOPSIS
Planifica o detiene solo el supervisor propio mediante un handle retenido y verificado.
.DESCRIPTION
Plan es el modo predeterminado. -Stop es explícito; WhatIf no termina procesos.
Dot-source solo define funciones. El recibo debe estar en la ruta privada canónica;
un PID sin host, SID, bundle y FILETIME no basta para adoptar un proceso.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param([Alias('Stop')][switch] $AgencyGpuStopRequested, [ValidateRange(1,10000)][int] $AgencyGpuStopTimeoutMs = 5000)

function Initialize-AgencyGpuProcessNative {
    if ('AgencyGpuStop.Native' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
namespace AgencyGpuStop {
  [StructLayout(LayoutKind.Sequential)] public struct FileTime {
    public uint Low, High;
    public long Value { get { return (long)(((ulong)High << 32) | Low); } }
  }
  public sealed class ProcessInfo {
    public uint ProcessId; public string ProcessImagePath;
    public string CreationFileTimeUtc; public bool Alive;
  }
  public static class Native {
    [DllImport("kernel32.dll", SetLastError=true)] static extern SafeProcessHandle OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint GetProcessId(SafeProcessHandle process);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool QueryFullProcessImageName(SafeProcessHandle process, uint flags, StringBuilder name, ref uint size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetProcessTimes(SafeProcessHandle process, out FileTime creation, out FileTime exit, out FileTime kernel, out FileTime user);
    [DllImport("kernel32.dll", SetLastError=true)] public static extern uint WaitForSingleObject(SafeProcessHandle process, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError=true)] [return: MarshalAs(UnmanagedType.Bool)] public static extern bool TerminateProcess(SafeProcessHandle process, uint exitCode);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CommandLineToArgvW(string command, out int count);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
    public static SafeProcessHandle Open(int pid, bool terminate) {
      var handle = OpenProcess(0x1000u | 0x100000u | (terminate ? 1u : 0u), false, (uint)pid);
      if (handle.IsInvalid) { handle.Dispose(); throw new Win32Exception(Marshal.GetLastWin32Error()); }
      return handle;
    }
    public static ProcessInfo Read(SafeProcessHandle handle) {
      uint size=32768; var name=new StringBuilder((int)size);
      FileTime creation, exit, kernel, user;
      uint pid=GetProcessId(handle);
      if (pid==0 || !QueryFullProcessImageName(handle,0,name,ref size) || !GetProcessTimes(handle,out creation,out exit,out kernel,out user)) throw new Win32Exception(Marshal.GetLastWin32Error());
      uint wait=WaitForSingleObject(handle,0);
      if (wait!=0 && wait!=258) throw new Win32Exception(Marshal.GetLastWin32Error());
      return new ProcessInfo { ProcessId=pid, ProcessImagePath=name.ToString(), CreationFileTimeUtc=creation.Value.ToString(System.Globalization.CultureInfo.InvariantCulture), Alive=wait==258 };
    }
    public static string[] Arguments(string command) {
      int count; IntPtr argv=CommandLineToArgvW(command,out count);
      if (argv==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
      try { var result=new string[count]; for(int i=0;i<count;i++) result[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(argv,i*IntPtr.Size)); return result; }
      finally { LocalFree(argv); }
    }
  }
}
'@ -ErrorAction Stop
}

function Open-AgencyGpuProcessHandle {
    param([ValidateRange(1,2147483647)][int]$ProcessId, [switch]$ForStop)
    Initialize-AgencyGpuProcessNative
    return [AgencyGpuStop.Native]::Open($ProcessId, [bool]$ForStop)
}
function Get-AgencyGpuNativeProcess { param($Handle) return [AgencyGpuStop.Native]::Read($Handle) }
function Close-AgencyGpuProcessHandle { param($Handle) $Handle.Dispose() }
function ConvertFrom-AgencyGpuCommandLine {
    param([Parameter(Mandatory)][string]$CommandLine)
    Initialize-AgencyGpuProcessNative
    return [AgencyGpuStop.Native]::Arguments($CommandLine)
}
function Stop-AgencyGpuHandle {
    [CmdletBinding(SupportsShouldProcess)] param($Handle)
    if ($PSCmdlet.ShouldProcess('handle propio retenido', 'Terminar supervisor verificado')) {
        if (-not [AgencyGpuStop.Native]::TerminateProcess($Handle, 0)) { throw 'No se confirmó la terminación del handle propio.' }
    }
}
function Wait-AgencyGpuHandle { param($Handle, [int]$TimeoutMs) return [AgencyGpuStop.Native]::WaitForSingleObject($Handle, [uint32]$TimeoutMs) }

function Read-AgencyGpuPrivateJson {
    param([string]$LiteralPath)
    $item = Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
    if ($item.PSIsContainer -or $item.Length -gt 65536) { throw 'El JSON privado no es un archivo acotado.' }
    $text = Get-Content -LiteralPath $LiteralPath -Raw -Encoding utf8 -ErrorAction Stop
    $document = [Text.Json.JsonDocument]::Parse($text)
    try {
        if ($document.RootElement.ValueKind -ne [Text.Json.JsonValueKind]::Object) { throw 'El JSON privado debe ser un objeto.' }
        $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($property in $document.RootElement.EnumerateObject()) {
            if (-not $names.Add($property.Name)) { throw 'El JSON privado contiene claves duplicadas.' }
        }
    } finally { $document.Dispose() }
    return $text | ConvertFrom-Json -AsHashtable -ErrorAction Stop
}

function Assert-AgencyGpuPrivatePath {
    param([string]$LiteralPath, [string]$UserSid, [string]$InstallRoot)
    $current = [IO.Path]::GetFullPath($LiteralPath)
    if ($current -ine $InstallRoot -and -not $current.StartsWith($InstallRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'La ruta privada sale de la instalación propia.' }
    $allowed = @($UserSid, 'S-1-5-18', 'S-1-5-32-544')
    while ($true) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'La instalación privada contiene un punto de reanálisis.' }
        $acl = Get-Acl -LiteralPath $current -ErrorAction Stop
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowed) { throw 'La ruta privada tiene propietario ajeno.' }
        foreach ($entry in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if ($entry.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and $entry.IdentityReference.Value -notin $allowed) { throw 'La ruta privada concede acceso a otra identidad.' }
        }
        if ($current -ieq $InstallRoot) { break }
        $current = Split-Path -Parent $current
    }
}

function Get-AgencyGpuStopContext {
    if (-not $IsWindows -or [Environment]::MachineName -ine 'W11-HOMELAB') { throw 'El host no corresponde al puente GPU propio.' }
    $root = 'C:\GPU-Bridge'; $configFile = Join-Path $root 'runtime.json'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try { $sid = $identity.User.Value } finally { $identity.Dispose() }
    Assert-AgencyGpuPrivatePath -LiteralPath $configFile -UserSid $sid -InstallRoot $root
    $config = Read-AgencyGpuPrivateJson -LiteralPath $configFile
    if ($config['GPU_INSTALL_ROOT'] -ine $root -or $config['GPU_RUNTIME_CONFIG_PATH'] -ine $configFile -or $config['GPU_WINDOWS_USER_SID'] -cne $sid -or $config['GPU_START_RECEIPT_PATH'] -ine (Join-Path $root 'supervisor-start.json')) { throw 'Las identidades privadas no coinciden con el contexto actual.' }
    $marker = Join-Path $root '.agencia-gpu-owner'
    Assert-AgencyGpuPrivatePath -LiteralPath $marker -UserSid $sid -InstallRoot $root
    if ((Get-Content -LiteralPath $marker -Raw -Encoding utf8) -cne $config['GPU_INSTALL_OWNER_MARKER']) { throw 'El marcador de instalación no coincide con su respaldo.' }
    if (Test-Path -LiteralPath $config['GPU_START_RECEIPT_PATH']) { Assert-AgencyGpuPrivatePath -LiteralPath $config['GPU_START_RECEIPT_PATH'] -UserSid $sid -InstallRoot $root }
    $python = Join-Path $root 'metrics\venv\Scripts\python.exe'; $launcher = Join-Path $root 'services\launch_service.py'
    $venvConfig = Join-Path $root 'metrics\venv\pyvenv.cfg'
    foreach ($file in @($python,$launcher,$venvConfig)) { Assert-AgencyGpuPrivatePath -LiteralPath $file -UserSid $sid -InstallRoot $root }
    if ($config['GPU_METRICS_PYTHON_PATH'] -ine $python) { throw 'El Python lógico no corresponde al entorno propio.' }
    $baseEntries = @(Get-Content -LiteralPath $venvConfig -Encoding utf8 | Where-Object { $_ -match '^executable\s*=' })
    if ($baseEntries.Count -ne 1) { throw 'El entorno no declara una imagen base canónica única.' }
    $base = ($baseEntries[0] -split '=',2)[1].Trim()
    if ($base -ine $config['GPU_BASE_PYTHON_PATH'] -or -not [IO.Path]::IsPathFullyQualified($base) -or (Get-FileHash -LiteralPath $base -Algorithm SHA256).Hash -ine $config['GPU_BASE_PYTHON_SHA256'] -or $config['GPU_SERVICES_BUNDLE_SHA256'] -notmatch '^[a-f0-9]{64}$') { throw 'La imagen base o bundle no coincide con su identidad respaldada.' }
    return @{HostName='W11-HOMELAB'; CurrentUserSid=$sid; UserSid=$sid; InstallRoot=$root; InstallationOwner=$config['GPU_INSTALL_OWNER_MARKER']; RuntimeConfigPath=$configFile; ReceiptPath=$config['GPU_START_RECEIPT_PATH']; PythonPath=$python; ImagePath=$base; LauncherPath=$launcher; SourceBundleSha256=$config['GPU_SERVICES_BUNDLE_SHA256']}
}

function Read-AgencyGpuStartReceipt {
    param($Context)
    Assert-AgencyGpuPrivatePath -LiteralPath $Context.ReceiptPath -UserSid $Context.UserSid -InstallRoot $Context.InstallRoot
    return Read-AgencyGpuPrivateJson -LiteralPath $Context.ReceiptPath
}

function Get-AgencyGpuCimProcess {
    param([ValidateRange(1,2147483647)][int]$ProcessId)
    $found = @(Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -OperationTimeoutSec 5 -ErrorAction Stop)
    if ($found.Count -ne 1) { throw 'El recibo identifica un proceso inexistente u obsoleto.' }
    $owner = Invoke-CimMethod -InputObject $found[0] -MethodName GetOwnerSid -OperationTimeoutSec 5 -ErrorAction Stop
    if ($owner.ReturnValue -ne 0) { throw 'No se acreditó el SID del proceso.' }
    return @{ProcessId=[int]$found[0].ProcessId; ParentProcessId=[int]$found[0].ParentProcessId; CreationTimeUtc=$found[0].CreationDate.ToUniversalTime(); UserSid=[string]$owner.Sid; ExecutablePath=[string]$found[0].ExecutablePath; CommandLine=[string]$found[0].CommandLine}
}

function Test-AgencyGpuProcessIdentity {
    param($Context, $Receipt, $Native, $Cim, [string[]]$Arguments)
    if ($Receipt.SchemaVersion -ne 1 -or $Receipt.Role -cne 'supervisor' -or $Receipt.Valid -isnot [bool] -or -not $Receipt.Valid -or $Receipt.Started -isnot [bool] -or -not $Receipt.Started -or ($Receipt.ProcessId -isnot [int] -and $Receipt.ProcessId -isnot [long]) -or [long]$Receipt.ProcessId -lt 1 -or [long]$Receipt.ProcessId -gt 2147483647) { throw 'El recibo no acredita un supervisor propio.' }
    if ($Context.CurrentUserSid -cne $Context.UserSid -or $Receipt.HostName -cne $Context.HostName -or $Receipt.UserSid -cne $Context.UserSid -or $Receipt.InstallationOwner -cne $Context.InstallationOwner -or $Receipt.SourceBundleSha256 -cne $Context.SourceBundleSha256 -or $Cim.UserSid -cne $Context.UserSid) { throw 'El host, SID o bundle no pertenece a la instalación propia.' }
    foreach ($pair in @(@($Receipt.InstallRoot,$Context.InstallRoot),@($Receipt.RuntimeConfigPath,$Context.RuntimeConfigPath),@($Receipt.LauncherPath,$Context.LauncherPath),@($Receipt.ProcessImagePath,$Context.ImagePath),@($Native.ProcessImagePath,$Context.ImagePath),@($Cim.ExecutablePath,$Context.ImagePath))) {
        if ($pair[0] -ine $pair[1]) { throw 'Una ruta de proceso no pertenece a la instalación propia.' }
    }
    if (-not $Native.Alive -or [long]$Receipt.ProcessId -ne [long]$Native.ProcessId -or [long]$Receipt.ProcessId -ne [long]$Cim.ProcessId -or [long]$Receipt.ParentProcessId -ne [long]$Cim.ParentProcessId -or [string]$Receipt.CreationFileTimeUtc -notmatch '^\d{16,19}$' -or [long]$Receipt.CreationFileTimeUtc -ne [long]$Native.CreationFileTimeUtc) { throw 'El PID o FILETIME del recibo es obsoleto.' }
    $nativeTime = [datetime]::FromFileTimeUtc([long]$Native.CreationFileTimeUtc)
    # ConvertFrom-Json puede devolver una fecha tipada: convertirla a texto pierde ticks y zona.
    if ($Receipt.StartTimeUtc -is [DateTimeOffset]) {
        $receiptTime = $Receipt.StartTimeUtc.UtcDateTime
    } elseif ($Receipt.StartTimeUtc -is [datetime]) {
        $receiptTime = $Receipt.StartTimeUtc.ToUniversalTime()
    } elseif ($Receipt.StartTimeUtc -is [string]) {
        $receiptTime = [DateTimeOffset]::Parse($Receipt.StartTimeUtc, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime
    } else {
        throw 'La hora de inicio del recibo no contiene una fecha válida.'
    }
    if ([math]::Abs(($Cim.CreationTimeUtc - $nativeTime).Ticks) -gt 10 -or [math]::Abs(($receiptTime - $nativeTime).Ticks) -gt 10) { throw 'CIM y el handle retenido identifican horas de inicio diferentes.' }
    if ($Arguments.Count -ne 4 -or $Arguments[0] -ine $Context.ImagePath -or $Arguments[1] -cne '-I' -or $Arguments[2] -ine $Context.LauncherPath -or $Arguments[3] -cne 'supervisor') { throw 'El argv privado no identifica el supervisor canónico.' }
    return $true
}

function Invoke-AgencyGpuStop {
    [CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
    param([switch]$Stop, [ValidateRange(1,10000)][int]$TimeoutMs = 5000)
    $context = Get-AgencyGpuStopContext
    $receipt = Read-AgencyGpuStartReceipt -Context $context
    $handle = Open-AgencyGpuProcessHandle -ProcessId $receipt.ProcessId -ForStop:($Stop -and -not $WhatIfPreference)
    try {
        $native = Get-AgencyGpuNativeProcess -Handle $handle
        $cim = Get-AgencyGpuCimProcess -ProcessId $receipt.ProcessId
        $arguments = ConvertFrom-AgencyGpuCommandLine -CommandLine $cim.CommandLine
        $null = Test-AgencyGpuProcessIdentity -Context $context -Receipt $receipt -Native $native -Cim $cim -Arguments $arguments
        $stopped = $false
        if ($Stop -and $PSCmdlet.ShouldProcess('supervisor propio verificado', 'Terminar mediante SafeProcessHandle retenido')) {
            if (-not (Get-AgencyGpuNativeProcess -Handle $handle).Alive) { throw 'El supervisor terminó antes de la operación; el recibo es obsoleto.' }
            Stop-AgencyGpuHandle -Handle $handle -Confirm:$false
            if ((Wait-AgencyGpuHandle -Handle $handle -TimeoutMs $TimeoutMs) -ne 0) { throw 'No se confirmó el cierre dentro del plazo acotado.' }
            $stopped = $true
        }
        return [pscustomobject]@{Mode=$(if($Stop){'Stop'}else{'Plan'}); IdentityVerified=$true; ProcessId=$receipt.ProcessId; Stopped=$stopped; ReadinessVerified=$false; TerminationMethod='SafeProcessHandle'; DescendantPidsUsed=$false}
    } finally { Close-AgencyGpuProcessHandle -Handle $handle }
}

function Assert-AgencyGpuLauncherLineage {
    param($Context, $Native, $Cim, [int]$LauncherProcessId, [string]$LauncherCreationFileTimeUtc)
    if ([long]$Native.CreationFileTimeUtc -lt [long]$LauncherCreationFileTimeUtc) { throw 'El recibo es anterior al lanzador propio.' }
    if ([int]$Native.ProcessId -eq $LauncherProcessId) {
        if ([string]$Native.CreationFileTimeUtc -cne $LauncherCreationFileTimeUtc) { throw 'El PID del lanzador fue reutilizado.' }
        return
    }
    $parentId = [int]$Cim.ParentProcessId
    $descendantTime = [datetime]::FromFileTimeUtc([long]$Native.CreationFileTimeUtc)
    for ($depth = 0; $depth -lt 8 -and $parentId -gt 0; $depth++) {
        $parent = Get-AgencyGpuCimProcess -ProcessId $parentId
        if ([int]$parent.ProcessId -ne $parentId -or ($parent.CreationTimeUtc - $descendantTime).Ticks -gt 10) { throw 'La ascendencia contiene un PID reutilizado.' }
        $arguments = ConvertFrom-AgencyGpuCommandLine -CommandLine $parent.CommandLine
        if ($parent.UserSid -cne $Context.UserSid -or $parent.ExecutablePath -notin @($Context.PythonPath,$Context.ImagePath) -or $arguments.Count -ne 4 -or $arguments[0] -ine $parent.ExecutablePath -or $arguments[1] -cne '-I' -or $arguments[2] -ine $Context.LauncherPath -or $arguments[3] -cne 'supervisor') { throw 'La ascendencia no pertenece al supervisor propio.' }
        if ($parentId -eq $LauncherProcessId) {
            $born = [datetime]::FromFileTimeUtc([long]$LauncherCreationFileTimeUtc)
            if ([math]::Abs(($parent.CreationTimeUtc - $born).Ticks) -gt 10) { throw 'La hora de inicio del lanzador no coincide.' }
            return
        }
        $descendantTime = $parent.CreationTimeUtc
        $parentId = [int]$parent.ParentProcessId
    }
    throw 'El recibo no identifica un descendiente del lanzador propio.'
}

function Get-AgencyGpuVerifiedStartReceipt {
    <#
    .SYNOPSIS
    Obtiene un recibo saneado para adopción manual expresamente coordinada.
    .DESCRIPTION
    No escribe archivos ni termina procesos. Requiere el PID REAL inspeccionado y el
    PID del lanzador previo propio; retiene ambos handles durante la comparación.
    El responsable guarda el resultado SOLO en GPU_START_RECEIPT_PATH ya respaldada.
    No ejecutar Get-AgencyGpuCimProcess directamente en salida: su argv es privado.
    Adopción inicial: respaldar rutas/base/bundle, inspeccionar el hijo del lanzador
    registrado sin imprimir argv, asignar este resultado a una variable y persistirlo
    en el recibo canónico. Después ejecutar primero Plan y revisar IdentityVerified.
    Esta función nunca admite el recibo mínimo antiguo como prueba de identidad.
    #>
    param([Parameter(Mandatory)][ValidateRange(1,2147483647)][int]$ProcessId,
          [Parameter(Mandatory)][ValidateRange(1,2147483647)][int]$ExpectedLauncherProcessId)
    $context = Get-AgencyGpuStopContext
    $handle = Open-AgencyGpuProcessHandle -ProcessId $ProcessId
    $launcherHandle = $null
    try {
        $native = Get-AgencyGpuNativeProcess -Handle $handle
        $cim = Get-AgencyGpuCimProcess -ProcessId $ProcessId
        $receipt = @{SchemaVersion=1; Role='supervisor'; Valid=$true; Started=$true; Readiness='pending'; ProcessId=$ProcessId; ParentProcessId=$cim.ParentProcessId; HostName=$context.HostName; UserSid=$context.UserSid; InstallRoot=$context.InstallRoot; InstallationOwner=$context.InstallationOwner; RuntimeConfigPath=$context.RuntimeConfigPath; ProcessImagePath=$native.ProcessImagePath; LauncherPath=$context.LauncherPath; CreationFileTimeUtc=[string]$native.CreationFileTimeUtc; StartTimeUtc=([datetime]::FromFileTimeUtc([long]$native.CreationFileTimeUtc)).ToString('o'); SourceBundleSha256=$context.SourceBundleSha256}
        $arguments = ConvertFrom-AgencyGpuCommandLine -CommandLine $cim.CommandLine
        $null = Test-AgencyGpuProcessIdentity -Context $context -Receipt $receipt -Native $native -Cim $cim -Arguments $arguments
        $launcherHandle = Open-AgencyGpuProcessHandle -ProcessId $ExpectedLauncherProcessId
        $launcherNative = Get-AgencyGpuNativeProcess -Handle $launcherHandle
        if (-not $launcherNative.Alive -or [int]$launcherNative.ProcessId -ne $ExpectedLauncherProcessId) { throw 'El lanzador previo no está disponible.' }
        Assert-AgencyGpuLauncherLineage -Context $context -Native $native -Cim $cim -LauncherProcessId $ExpectedLauncherProcessId -LauncherCreationFileTimeUtc $launcherNative.CreationFileTimeUtc
        return $receipt
    } finally {
        if ($null -ne $launcherHandle) { Close-AgencyGpuProcessHandle -Handle $launcherHandle }
        Close-AgencyGpuProcessHandle -Handle $handle
    }
}

function Wait-AgencyGpuStartReceipt {
    param($Context, [int]$LauncherProcessId, [string]$LauncherCreationFileTimeUtc,
          [ValidateRange(1,30000)][int]$TimeoutMs = 15000)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ($clock.ElapsedMilliseconds -lt $TimeoutMs) {
        $handle = $null
        try {
            $receipt = Read-AgencyGpuStartReceipt -Context $Context
            $handle = Open-AgencyGpuProcessHandle -ProcessId $receipt.ProcessId
            $native = Get-AgencyGpuNativeProcess -Handle $handle
            $cim = Get-AgencyGpuCimProcess -ProcessId $receipt.ProcessId
            $arguments = ConvertFrom-AgencyGpuCommandLine -CommandLine $cim.CommandLine
            $null = Test-AgencyGpuProcessIdentity -Context $Context -Receipt $receipt -Native $native -Cim $cim -Arguments $arguments
            Assert-AgencyGpuLauncherLineage -Context $Context -Native $native -Cim $cim -LauncherProcessId $LauncherProcessId -LauncherCreationFileTimeUtc $LauncherCreationFileTimeUtc
            return $receipt
        } catch { Write-Verbose 'Aún no existe un recibo nuevo verificable del supervisor propio.' }
        finally { if ($null -ne $handle) { Close-AgencyGpuProcessHandle -Handle $handle } }
        Start-Sleep -Milliseconds 100
    }
    throw 'No apareció un recibo nuevo verificado del supervisor real dentro del plazo.'
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-AgencyGpuStop -Stop:$AgencyGpuStopRequested -TimeoutMs $AgencyGpuStopTimeoutMs -WhatIf:$WhatIfPreference | ConvertTo-Json -Compress }
    catch { [Console]::Error.WriteLine('{"Stopped":false,"Error":"No se acreditó una parada propia segura; no se usa otra identidad de proceso."}'); exit 2 }
}
