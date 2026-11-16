"""Valida y extrae árboles nuevos; nunca ejecuta SDK, fuentes ni herramientas.

El responsable comprueba primero la propiedad/ACL de la raíz privada y el pin
externo del archivo. Esta función no descarga ni adopta destinos existentes.
"""

import hashlib
import io
import os
import re
import stat
import tarfile
import uuid
import zipfile
from pathlib import Path

RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])", re.IGNORECASE)


def relative_name(name, prefix, directory):
    if not isinstance(name, str) or not name:
        raise ValueError("Nombre de entrada ausente.")
    name = name[:-1] if directory and name.endswith("/") else name
    parts = name.split("/")
    if parts[0] != prefix:
        raise ValueError("La entrada no pertenece a la raíz esperada.")
    for part in parts:
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or any(ord(char) < 32 or char in '<>:"\\|?*' for char in part)
            or RESERVED.fullmatch(part.split(".", 1)[0])
        ):
            raise ValueError("Ruta ambigua o no permitida en Windows.")
    if len(parts) == 1 and not directory:
        raise ValueError("La raíz del paquete debe ser directorio.")
    return "/".join(parts[1:])


def plain_destination(destination):
    destination = Path(destination).absolute()
    if str(destination).startswith(("\\\\", "//")) or ".." in destination.parts:
        raise ValueError("El destino debe ser una ruta local sin escapes.")
    for parent in reversed(destination.parents):
        info = os.lstat(parent)
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("El padre no es un directorio local ordinario.")
    if destination.exists() or destination.is_symlink():
        raise ValueError("No se adopta ni sobrescribe un destino existente.")
    return destination


def extract_archive(
    content,
    kind,
    prefix,
    destination,
    *,
    max_bytes=1024**3,
    max_file=128 * 1024**2,
    max_members=40000,
):
    """Inspección completa antes de escribir y publicación de un árbol nuevo.

    Ante un fallo se conserva el staging privado para diagnóstico; no borra
    directorios recursivamente ni modifica el destino que ya existía.
    """
    destination = plain_destination(destination)
    try:
        if kind == "zip":
            package = zipfile.ZipFile(io.BytesIO(content))
            members = package.infolist()
            if not members or len(members) > max_members:
                raise ValueError("Inventario de entradas ausente o excesivo.")
        elif kind == "tar.gz":
            package = tarfile.open(fileobj=io.BytesIO(content), mode="r:gz")
            members = package
        else:
            raise ValueError("Formato de archivo no implementado.")
        with package:
            inventory, seen, spellings, roles, total = [], set(), {}, {}, 0
            for index, member in enumerate(members):
                if index >= max_members:
                    raise ValueError("Demasiadas entradas en el archivo.")
                if kind == "zip":
                    directory = member.is_dir()
                    size = member.file_size
                    mode = member.external_attr >> 16
                    if (
                        member.flag_bits & 1
                        or member.external_attr & 0x400
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                    ):
                        raise ValueError("ZIP cifrado o con enlaces/tipo especial.")
                    name = member.orig_filename
                else:
                    directory, size, name = member.isdir(), member.size, member.name
                    if not directory and not member.isfile():
                        raise ValueError("TAR con enlaces o tipo especial.")
                relative = relative_name(name, prefix, directory)
                if directory and size:
                    raise ValueError("Un directorio no debe contener bytes.")
                if size < 0 or size > max_file:
                    raise ValueError("Entrada fuera del límite de tamaño.")
                total += size
                if total > max_bytes or relative.casefold() in seen:
                    raise ValueError("Expansión excesiva o nombre duplicado.")
                seen.add(relative.casefold())
                parts = relative.split("/") if relative else []
                for index in range(1, len(parts) + 1):
                    current = "/".join(parts[:index])
                    folded = current.casefold()
                    role = "directory" if index < len(parts) or directory else "file"
                    if folded in spellings and spellings[folded] != current:
                        raise ValueError("Colisión por mayúsculas en un componente.")
                    if folded in roles and roles[folded] != role:
                        raise ValueError("Colisión entre directorio y archivo.")
                    spellings[folded], roles[folded] = current, role
                inventory.append((member, relative, directory, size))
            if not inventory:
                raise ValueError("Inventario de entradas ausente.")
            staging = destination.with_name(destination.name + ".partial-" + uuid.uuid4().hex)
            staging.mkdir(mode=0o700)
            results = []
            for member, relative, directory, size in inventory:
                target = staging.joinpath(*relative.split("/")) if relative else staging
                if directory:
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = package.open(member) if kind == "zip" else package.extractfile(member)
                digest, written = hashlib.sha256(), 0
                with source, target.open("xb") as output:
                    while True:
                        chunk = source.read(min(1024**2, size - written + 1))
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > size:
                            raise ValueError("La entrada excedió su tamaño declarado.")
                        digest.update(chunk)
                        output.write(chunk)
                if written != size:
                    raise ValueError("Entrada truncada.")
                results.append({"path": relative, "size": written, "sha256": digest.hexdigest()})
            os.rename(staging, destination)
            return sorted(results, key=lambda row: row["path"])
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile, RuntimeError) as error:
        raise ValueError("Archivo o publicación rechazados; no se ejecutó contenido.") from error
