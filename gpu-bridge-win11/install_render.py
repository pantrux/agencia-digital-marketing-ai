"""Publica el renderizador propio, navegador fijado y módulos npm sin scripts de paquetes."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from bridge_common import safe_media_path
from bridge_supervisor import child_environment
from gpu_artifacts import extract_source, verify_artifact
from install_gpu_bridge import ROOT, SOURCE, load_vault
from install_inference import require_installation

RENDER_SOURCE = SOURCE / "render-worker"
TOP_FILES = {"package.json", "package-lock.json", "versions.json", "tsconfig.json", "README.md"}


def source_bundle(root: Path):
    files = {}
    if any((root / name).is_symlink() for name in ("src", "composition")):
        raise ValueError("La fuente del renderizador no admite enlaces.")
    candidates = [root / name for name in TOP_FILES if (root / name).exists()]
    candidates += list((root / "src").rglob("*"))
    candidates += list((root / "composition").rglob("*"))
    for path in candidates:
        relative = path.relative_to(root)
        if path.is_symlink() or (
            path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("La fuente del renderizador no admite enlaces.")
        if path.is_file():
            with path.open("rb") as stream:
                files[relative.as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return files, digest


def install_render(values, *, source_only=False):
    config = require_installation(values)
    destination = safe_media_path(config.install_root, "render-worker")
    if destination != Path(values["GPU_RENDER_WORKER_ROOT"]):
        raise ValueError("La ruta del renderizador no coincide con su respaldo.")
    files, digest = source_bundle(RENDER_SOURCE)
    if digest != values["GPU_RENDER_SOURCE_BUNDLE_SHA256"]:
        raise ValueError("El paquete de fuentes debe respaldarse antes de publicarse.")
    lock = RENDER_SOURCE / "package-lock.json"
    if hashlib.sha256(lock.read_bytes()).hexdigest() != values["GPU_RENDER_PACKAGE_LOCK_SHA256"]:
        raise ValueError("El lock npm difiere del respaldo.")
    lock_data = json.loads(lock.read_text(encoding="utf-8"))
    for package in lock_data["packages"].values():
        if package.get("resolved") and not package["resolved"].startswith(
            "https://registry.npmjs.org/"
        ):
            raise ValueError("El lock npm contiene un origen fuera del registro oficial.")
    previous = None
    if source_only:
        installed_lock = destination / "package-lock.json"
        if (
            not installed_lock.is_file()
            or hashlib.sha256(installed_lock.read_bytes()).hexdigest()
            != values["GPU_RENDER_PACKAGE_LOCK_SHA256"]
        ):
            raise ValueError(
                "Actualizar sólo fuente exige conservar el lock de dependencias instalado."
            )
        previous = json.loads(
            (config.install_root / "render-install-receipt.json").read_text(encoding="utf-8")
        )
        executable = Path(values["GPU_REMOTION_BROWSER_PATH"])
        with executable.open("rb") as stream:
            browser_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if (
            previous.get("installed") is not True
            or browser_sha != values["GPU_REMOTION_BROWSER_BINARY_SHA256"]
        ):
            raise ValueError(
                "Falta la instalación previa y navegador verificados para actualizar sólo fuente."
            )
    destination.mkdir(exist_ok=True)
    for name, expected in files.items():
        target = safe_media_path(destination, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = (RENDER_SOURCE / name).read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError("La fuente cambió durante la publicación.")
        temporary = safe_media_path(destination, name + ".new")
        temporary.write_bytes(payload)
        temporary.replace(target)
    if source_only:
        receipt = dict(previous, source_bundle_sha256=digest, source_files=files)
        (config.install_root / "render-install-receipt.json").write_text(
            json.dumps(receipt, indent=2), encoding="utf-8"
        )
        return receipt
    versions = json.loads((RENDER_SOURCE / "versions.json").read_text(encoding="utf-8"))
    browser = versions["browser"]
    archive = RENDER_SOURCE / browser["archive"]
    metadata = {"sha256": browser["sha256"], "size": archive.stat().st_size}
    if (
        browser["sha256"] != values["GPU_REMOTION_BROWSER_SHA256"]
        or browser["version"] != values["GPU_REMOTION_BROWSER_VERSION"]
    ):
        raise ValueError("El navegador no coincide con la versión respaldada.")
    verify_artifact(archive, metadata)
    browser_root = safe_media_path(
        config.install_root,
        "cache/remotion/chromium-headless-shell-win-x64-"
        + browser["version"]
        + "/chrome-headless-shell-win64",
    )
    extract_source(
        archive, browser_root, "", max_member_bytes=512 * 1024**2, max_total_bytes=2 * 1024**3
    )
    executable = browser_root / "chrome-headless-shell.exe"
    if executable != Path(values["GPU_REMOTION_BROWSER_PATH"]) or not executable.is_file():
        raise ValueError("Falta el navegador fijado en la ruta propia.")
    node = Path(values["GPU_NODE_PATH"])
    with node.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != values["GPU_NODE_SHA256"]:
            raise ValueError("El intérprete Node difiere del respaldo.")
    npm = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not npm.is_file():
        raise ValueError("No se encuentra npm junto al Node verificado.")
    subprocess.run(
        [
            str(node),
            str(npm),
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--cache",
            str(ROOT / ".tools" / "gpu-npm-cache"),
        ],
        cwd=destination,
        check=True,
        env=child_environment(os.environ, {}),
        timeout=600,
    )
    with executable.open("rb") as stream:
        binary_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    receipt = {
        "source_bundle_sha256": digest,
        "browser_binary_sha256": binary_sha,
        "source_files": files,
        "installed": True,
        "service_started": False,
    }
    (config.install_root / "render-install-receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    if args.apply:
        result = install_render(load_vault(ROOT / ".env"), source_only=args.source_only)
    else:
        files, digest = source_bundle(RENDER_SOURCE)
        result = {"source_bundle_sha256": digest, "source_files": files, "changes_applied": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
