<#
.SYNOPSIS
Comprueba parámetros de comandos PowerCLI contra el SDK instalado sin conectar infraestructura.
.DESCRIPTION
Analiza el AST de los scripts locales y contrasta los parámetros nombrados con
los metadatos reales del módulo. Rechaza parámetros inexistentes y combinaciones
que no pertenecen a un único conjunto de parámetros. No ejecuta los comandos.
#>
[CmdletBinding()]
param(
    [Parameter()]
    [string] $Directory = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'
Import-Module VMware.VimAutomation.Core -ErrorAction Stop
$failures = [Collections.Generic.List[string]]::new()
$checked = 0
foreach ($file in Get-ChildItem -LiteralPath $Directory -Filter '*.ps1' -File) {
    $tokens = $null
    $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref] $tokens, [ref] $parseErrors)
    if ($parseErrors) { throw "El archivo $($file.Name) no tiene sintaxis válida." }
    $commands = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] }, $true))
    foreach ($command in $commands) {
        $name = $command.GetCommandName()
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        $metadata = Get-Command -Name $name -ErrorAction SilentlyContinue
        if (-not $metadata -or $metadata.ModuleName -notlike 'VMware.*') { continue }
        # Los alias de comandos no exponen sus conjuntos; se inspecciona el comando resuelto.
        while ($metadata -is [Management.Automation.AliasInfo]) { $metadata = $metadata.ResolvedCommand }
        $namedParameters = @($command.CommandElements | Where-Object { $_ -is [Management.Automation.Language.CommandParameterAst] } | ForEach-Object ParameterName)
        $canonicalParameters = [Collections.Generic.List[string]]::new()
        foreach ($parameter in $namedParameters) {
            $canonical = @($metadata.Parameters.Values | Where-Object { $_.Name -ieq $parameter -or $parameter -iin $_.Aliases } | Select-Object -ExpandProperty Name -Unique)
            if ($canonical.Count -eq 0) {
                $canonical = @($metadata.Parameters.Values | Where-Object {
                    $names = @($_.Name) + @($_.Aliases)
                    @($names | Where-Object { $_.StartsWith($parameter, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
                } | Select-Object -ExpandProperty Name -Unique)
            }
            if ($canonical.Count -ne 1) {
                $failures.Add("$($file.Name):$($command.Extent.StartLineNumber): $name no acepta -$parameter.")
            }
            else { $canonicalParameters.Add($canonical[0]) }
        }
        $compatibleSets = @($metadata.ParameterSets | Where-Object {
            $setNames = @($_.Parameters.Name)
            @($canonicalParameters | Where-Object { $_ -notin $setNames }).Count -eq 0
        })
        if ($compatibleSets.Count -eq 0) {
            $failures.Add("$($file.Name):$($command.Extent.StartLineNumber): $name combina conjuntos incompatibles: $($namedParameters -join ', ').")
        }
        $checked++
    }
}
if ($failures.Count -gt 0) { throw ($failures -join [Environment]::NewLine) }
[pscustomobject]@{
    PowerCliVersion = [string] (Get-Module VMware.VimAutomation.Core).Version
    CommandsChecked = $checked
    InfrastructureContacted = $false
    ContractsValid = $true
}
