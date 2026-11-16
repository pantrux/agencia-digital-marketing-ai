# El SDK 13.5 utiliza servidor,huella por línea, sin encabezado ni comillas CSV.

function ConvertFrom-MediaTrustStore {
    <# .SYNOPSIS
    Lee el formato nativo y reconoce la plantilla CSV antigua para migrarla sin perder registros.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [string[]] $Lines
    )
    $nonEmptyLines = @($Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $records = @()
    $isNative = $true
    if ($nonEmptyLines.Count -gt 0) {
        $header = $nonEmptyLines[0].Trim().Trim([char] 0xFEFF)
        if ($header -cin @('server_address,certificate_thumbprint', '"server_address","certificate_thumbprint"')) {
            $records = @($nonEmptyLines | ConvertFrom-Csv)
            $isNative = $false
        }
        else {
            foreach ($line in $nonEmptyLines) {
                $nativeLine = $line.Trim([char] 0xFEFF)
                if ($nativeLine -notmatch '^([^,"\r\n]+),([A-Fa-f0-9]{40})$') {
                    throw 'El PCTCS contiene una línea de formato desconocido; se conserva sin modificarlo.'
                }
                $records += [pscustomobject]@{
                    server_address = $Matches[1]
                    certificate_thumbprint = $Matches[2]
                }
            }
        }
    }
    foreach ($record in $records) {
        if ($record.server_address -notmatch '^[^,"\r\n]+$' -or
            $record.certificate_thumbprint -notmatch '^[A-Fa-f0-9]{40}$') {
            throw 'El PCTCS contiene un servidor o una huella inválidos; se conserva sin modificarlo.'
        }
    }
    [pscustomobject]@{ Records = @($records); IsNative = $isNative }
}

function ConvertTo-MediaTrustStore {
    <# .SYNOPSIS
    Produce el formato servidor,huella que consume el SDK instalado.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [object[]] $Records
    )
    foreach ($record in $Records) {
        if ($record.server_address -notmatch '^[^,"\r\n]+$' -or
            $record.certificate_thumbprint -notmatch '^[A-Fa-f0-9]{40}$') {
            throw 'No se puede escribir un registro de confianza inválido.'
        }
        $record.server_address + ',' + $record.certificate_thumbprint
    }
}
