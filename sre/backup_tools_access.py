"""Comprueba acceso local al directorio propio, sin elevar ni cambiar permisos."""

import json
import os
import stat
import subprocess
from pathlib import Path

WRITE_RIGHTS = 2 | 4 | 16 | 64 | 256 | 65536 | 262144 | 524288


def validate_windows_access(data, *, private):
    """No adopta propietarios o permisos de escritura de otra identidad."""
    if not isinstance(data, dict) or not isinstance(data.get("current_sid"), str):
        raise ValueError("No se acreditó la identidad Windows.")
    allowed = {data["current_sid"], "S-1-5-18", "S-1-5-32-544"}
    if data.get("owner_sid") not in allowed or data.get("reparse") is not False:
        raise ValueError("La ruta no tiene propietario o tipo acreditados.")
    if not isinstance(data.get("access"), list):
        raise ValueError("No se acreditó la ACL Windows.")
    for entry in data["access"]:
        if not isinstance(entry, dict) or type(entry.get("rights")) is not int:
            raise ValueError("La ACL Windows no tiene un formato válido.")
        if entry.get("sid") not in allowed and (private or entry["rights"] & WRITE_RIGHTS):
            raise ValueError("La ACL permite acceso ajeno al directorio propio.")


def validate_posix_access(info, *, uid, private):
    if info.st_uid != uid or info.st_mode & (0o077 if private else 0o022):
        raise ValueError("La propiedad o permisos POSIX no son seguros.")
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        raise ValueError("Solo se admiten directorios y archivos regulares.")


def check_access(path, *, private=False):
    """Sólo consulta metadata; la aplicación Linux requiere el propietario root."""
    path = Path(path)
    if os.name != "nt":
        validate_posix_access(os.lstat(path), uid=0, private=private)
        return
    # Get-Acl y la consulta de SID funcionan en Windows PowerShell integrado.
    # La lectura de permisos no depende de una instalación opcional de PowerShell 7.
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    if not powershell.is_absolute() or not powershell.is_file() or powershell.is_symlink():
        raise ValueError("No está disponible el intérprete integrado para comprobar la ACL.")
    script = r"""
$ErrorActionPreference='Stop'
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
try {
    $item=Get-Item -LiteralPath $env:AGENCY_BACKUP_TOOLS_AUDIT_PATH -Force
    $acl=Get-Acl -LiteralPath $item.FullName
    $entries=@(foreach($entry in $acl.Access) {
        if($entry.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow) {
            @{sid=$entry.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value;rights=[int]$entry.FileSystemRights}
        }
    })
    @{current_sid=$identity.User.Value;owner_sid=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value;reparse=[bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint);access=$entries}|ConvertTo-Json -Depth 4 -Compress
} finally {$identity.Dispose()}
"""
    # PowerShell 7 puede exportar PSModulePath con ensamblados incompatibles con 5.1.
    # Se usa sólo el catálogo del intérprete integrado y se quitan variantes de mayúsculas.
    environment = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    environment.update(
        {
            "AGENCY_BACKUP_TOOLS_AUDIT_PATH": str(path),
            "PSModulePath": str(powershell.parent / "Modules"),
        }
    )
    result = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError("No se pudo comprobar la ACL privada.")
    try:
        validate_windows_access(json.loads(result.stdout), private=private)
    except (ValueError, TypeError, KeyError):
        raise ValueError("La ACL Windows no acredita el acceso requerido.") from None
