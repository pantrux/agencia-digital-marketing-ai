"""Instala validadores fijados desde sus publicaciones oficiales, sin ejecutar Git."""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

# Los hashes corresponden a los checksums oficiales consultados el 2026-11-05.
# ShellCheck publica el digest SHA256 en la API de su publicación oficial.
TOOLS = {
    "gitleaks": {
        "repository": "gitleaks/gitleaks",
        "version": "8.30.1",
        "linux": (
            "gitleaks_8.30.1_linux_x64.tar.gz",
            "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
        ),
        "windows": (
            "gitleaks_8.30.1_windows_x64.zip",
            "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e",
        ),
    },
    "actionlint": {
        "repository": "rhysd/actionlint",
        "version": "1.7.12",
        "linux": (
            "actionlint_1.7.12_linux_amd64.tar.gz",
            "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
        ),
        "windows": (
            "actionlint_1.7.12_windows_amd64.zip",
            "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9",
        ),
    },
    "trivy": {
        "repository": "aquasecurity/trivy",
        "version": "0.76.2",
        "linux": (
            "trivy_0.76.2_Linux-64bit.tar.gz",
            "bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf",
        ),
        "windows": (
            "trivy_0.76.2_windows-64bit.zip",
            "691f737a7e5566a673c3a74dab9160552ec32101e953b659e5c77443d16dddb6",
        ),
    },
    "shellcheck": {
        "repository": "koalaman/shellcheck",
        "version": "0.11.0",
        "linux": (
            "shellcheck-v0.11.0.linux.x86_64.tar.gz",
            "b7af85e41cc99489dcc21d66c6d5f3685138f06d34651e6d34b42ec6d54fe6f6",
        ),
        "windows": (
            "shellcheck-v0.11.0.zip",
            "8a4e35ab0b331c85d73567b12f2a444df187f483e5079ceffa6bda1faa2e740e",
        ),
    },
}


def verify_checksum(archive, expected):
    """Rechaza todo archivo distinto del publicado y fijado en el código."""
    with Path(archive).open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError(f"SHA256 incorrecto: {Path(archive).name}")


def extract_binary(archive, executable, target):
    """Copia únicamente un archivo regular; nunca extrae rutas del paquete."""
    archive = Path(archive)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            members = [
                item
                for item in bundle.infolist()
                if Path(item.filename).name == executable and not item.is_dir()
            ]
            if len(members) != 1:
                raise ValueError(f"El paquete no contiene un binario único: {executable}")
            mode = members[0].external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise ValueError("El binario debe ser un archivo regular")
            content = bundle.read(members[0])
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            members = [item for item in bundle.getmembers() if Path(item.name).name == executable]
            if len(members) != 1:
                raise ValueError(f"El paquete no contiene un binario único: {executable}")
            if not members[0].isfile():
                raise ValueError("El binario debe ser un archivo regular")
            with bundle.extractfile(members[0]) as stream:
                content = stream.read()
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.chmod(0o755)
    temporary.replace(target)


def download(url, destination):
    """Mantiene TLS activo incluso cuando Python no usa el almacén de Windows."""
    if platform.system() == "Windows":
        curl = shutil.which("curl.exe")
        if curl is None:
            raise RuntimeError("Se requiere curl.exe con el almacén de certificados de Windows")
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
                "180",
                "--output",
                str(destination),
                url,
            ],
            check=True,
        )
    else:
        with (
            urllib.request.urlopen(url, timeout=180) as response,
            destination.open("wb") as stream,
        ):
            shutil.copyfileobj(response, stream)


def install(name, destination, cache, operating_system):
    metadata = TOOLS[name]
    filename, checksum = metadata[operating_system]
    archive = cache / filename
    cache.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        temporary = archive.with_suffix(archive.suffix + ".part")
        url = f"https://github.com/{metadata['repository']}/releases/download/v{metadata['version']}/{filename}"
        download(url, temporary)
        verify_checksum(temporary, checksum)
        temporary.replace(archive)
    verify_checksum(archive, checksum)
    executable = name + (".exe" if operating_system == "windows" else "")
    extract_binary(archive, executable, destination / executable)
    print(f"{name} {metadata['version']}: SHA256 verificado e instalado")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools", nargs="+", choices=tuple(TOOLS), default=list(TOOLS))
    parser.add_argument("--destination", type=Path, default=Path(".tools/bin"))
    parser.add_argument("--cache", type=Path, default=Path(".tools/cache"))
    arguments = parser.parse_args()
    operating_system = platform.system().lower()
    if operating_system not in (
        "windows",
        "linux",
    ) or platform.machine().lower() not in ("amd64", "x86_64"):
        parser.error(
            "Solo se admiten Windows y Linux x86_64; no se instala un binario incompatible"
        )
    for name in arguments.tools:
        install(name, arguments.destination, arguments.cache, operating_system)
    if "GITHUB_PATH" in os.environ:
        with Path(os.environ["GITHUB_PATH"]).open("a", encoding="utf-8") as stream:
            stream.write(str(arguments.destination.resolve()) + "\n")


if __name__ == "__main__":
    main()
