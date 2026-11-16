"""Descarga HTTPS y extracción selectiva de artefactos fijados por tamaño y SHA256."""

import hashlib
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from bridge_common import safe_media_path

HOSTS = {
    "huggingface.co",
    "files.pythonhosted.org",
    "codeload.github.com",
    "aiinfra.pkgs.visualstudio.com",
    "storage.googleapis.com",
}


def verify_artifact(path: Path, metadata: dict):
    if not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"]) or type(metadata["size"]) is not int:
        raise ValueError("El artefacto carece de una huella o tamaño fijado válido.")
    if not 0 < metadata["size"] <= 4 * 1024**3 or path.is_symlink() or not path.is_file():
        raise ValueError("El artefacto no es un archivo regular permitido.")
    if path.stat().st_size != metadata["size"]:
        raise ValueError("El tamaño descargado no coincide con el manifiesto.")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != metadata["sha256"]:
        raise ValueError("SHA256 distinto del manifiesto fijado.")


def fetch_artifact(metadata: dict, cache: Path) -> Path:
    url = urlsplit(metadata["url"])
    if url.scheme != "https" or url.hostname not in HOSTS or url.username or url.password:
        raise ValueError("Origen HTTPS no permitido para los artefactos del puente.")
    if not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"]):
        raise ValueError("Huella inválida.")
    name = metadata.get("filename") or unquote(url.path.rsplit("/", 1)[-1])
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", name) or name in (".", ".."):
        raise ValueError("Nombre de artefacto inválido.")
    cache.mkdir(parents=True, exist_ok=True)
    cache = cache.resolve(strict=True)
    destination = safe_media_path(cache, metadata["sha256"] + "/" + name)
    destination.parent.mkdir(exist_ok=True)
    if destination.exists():
        verify_artifact(destination, metadata)
        return destination
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise ValueError("Se requiere curl con verificación TLS para descargar artefactos.")
    temporary = safe_media_path(cache, metadata["sha256"] + "/" + name + ".part")
    subprocess.run(
        [
            curl,
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--tlsv1.2",
            "--connect-timeout",
            "15",
            "--max-time",
            "600",
            "--max-filesize",
            str(metadata["size"]),
            "--output",
            str(temporary),
            metadata["url"],
        ],
        check=True,
        timeout=610,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    verify_artifact(temporary, metadata)
    temporary.replace(destination)
    return destination


def _relative_member(name: str, prefix: str):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError("La fuente contiene una ruta no permitida.")
    parts = path.parts[1:]
    wanted = PurePosixPath(prefix).parts if prefix else ()
    if parts[: len(wanted)] != wanted:
        return None
    remaining = parts[len(wanted) :]
    return "/".join(remaining) if remaining else None


def _write_member(destination, name, stream):
    target = safe_media_path(destination, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file():
            raise ValueError("La fuente existente difiere: se conserva sin sobrescribir.")
        expected = hashlib.file_digest(stream, "sha256").hexdigest()
        with target.open("rb") as existing:
            actual = hashlib.file_digest(existing, "sha256").hexdigest()
        if actual != expected:
            raise ValueError("La fuente existente difiere: se conserva sin sobrescribir.")
        return
    with target.open("xb") as output:
        shutil.copyfileobj(stream, output, length=1024 * 1024)


def extract_source(
    archive: Path,
    destination: Path,
    prefix: str,
    *,
    max_member_bytes=64 * 1024**2,
    max_total_bytes=512 * 1024**2,
):
    """Retira un único directorio raíz y conserva únicamente el prefijo solicitado."""
    destination.mkdir(parents=True, exist_ok=True)
    total, names = 0, set()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            selected = []
            for item in bundle.infolist():
                name = _relative_member(item.filename, prefix)
                if name and not item.is_dir():
                    if stat.S_ISLNK(item.external_attr >> 16):
                        raise ValueError("No se extraen enlaces de fuentes.")
                    selected.append((name, item, item.file_size))
            for name, _, size in selected:
                total += size
                if name in names or size > max_member_bytes or total > max_total_bytes:
                    raise ValueError("Fuente duplicada o demasiado grande.")
                names.add(name)
            for name, item, _ in selected:
                with bundle.open(item) as stream:
                    _write_member(destination, name, stream)
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            selected = []
            for item in bundle.getmembers():
                name = _relative_member(item.name, prefix)
                if name and not item.isdir():
                    if not item.isfile():
                        raise ValueError("No se extraen enlaces ni dispositivos de fuentes.")
                    selected.append((name, item, item.size))
            for name, _, size in selected:
                total += size
                if name in names or size > max_member_bytes or total > max_total_bytes:
                    raise ValueError("Fuente duplicada o demasiado grande.")
                names.add(name)
            for name, item, _ in selected:
                with bundle.extractfile(item) as stream:
                    _write_member(destination, name, stream)
    if not names:
        raise ValueError("La fuente no contiene el prefijo solicitado.")
