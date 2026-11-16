"""Construye fuentes Python fijadas en entorno aparte y publica runtimes sin Git."""

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import venv
from pathlib import Path

from bridge_common import BridgeConfig, safe_media_path
from bridge_supervisor import child_environment
from gpu_artifacts import extract_source, verify_artifact
from install_gpu_bridge import ROOT, SOURCE, check_owned_root, load_vault

EVIDENCE = ROOT / ".tools" / "evidence"


def local_requirements(entries: list[dict]) -> str:
    lines, names = [], set()
    for item in entries:
        name = item["name"]
        canonical = re.sub(r"[-_.]+", "-", name.lower())
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or canonical in names:
            raise ValueError("Nombre inválido o duplicado en el grafo de dependencias.")
        names.add(canonical)
        file = Path(item["local_path"])
        verify_artifact(file, item)
        lines.append(f"{name} @ {file.resolve().as_uri()} --hash=sha256:{item['sha256']}")
    return "\n".join(lines) + "\n"


def require_installation(values):
    config = BridgeConfig.from_env(values)
    if os.name != "nt" or socket.gethostname().upper() != "W11-HOMELAB":
        raise ValueError("La instalación requiere la estación GPU autorizada.")
    if not check_owned_root(config.install_root, values["GPU_INSTALL_OWNER_MARKER"]):
        raise ValueError("Falta el árbol privado propio del puente.")
    return config


def ensure_venv(environment):
    python = environment / "Scripts" / "python.exe"
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    return python


def immich_entries(values):
    manifest = json.loads((SOURCE / "immich-artifacts.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(
        json.dumps(manifest["artifacts"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    downloaded = json.loads(
        (EVIDENCE / "immich-artifacts-downloaded.json").read_text(encoding="utf-8")
    )
    if (
        digest != values["GPU_IMMICH_DEPENDENCIES_MANIFEST_SHA256"]
        or downloaded["manifest_sha256"] != digest
    ):
        raise ValueError("El grafo descargado no coincide con el respaldo de la bóveda.")
    by_name = {(item["name"], item["role"]): item for item in downloaded["artifacts"]}
    entries = []
    for item in manifest["artifacts"]:
        local = by_name[(item["name"], item["role"])]
        if any(item[key] != local[key] for key in ("version", "sha256", "size", "url")):
            raise ValueError("Un artefacto descargado difiere del manifiesto.")
        verify_artifact(Path(local["local_path"]), item)
        entries.append(local)
    return entries


def build_immich(values):
    config = require_installation(values)
    entries = immich_entries(values)
    python = ensure_venv(safe_media_path(config.install_root, "immich/build-venv"))
    build_lock = EVIDENCE / "immich-build.lock"
    build_lock.write_text(
        local_requirements([item for item in entries if item["role"] == "build"]), encoding="utf-8"
    )
    environment = child_environment(os.environ, {})
    environment["SOURCE_DATE_EPOCH"] = "1793836800"
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-index",
            "--require-hashes",
            "--no-deps",
            "-r",
            str(build_lock),
        ],
        check=True,
        env=environment,
        timeout=180,
    )
    runtime = [item for item in entries if item["role"] == "runtime" and item["kind"] == "wheel"]
    built = []
    for item in entries:
        if item["role"] != "runtime" or item["kind"] != "sdist":
            continue
        output = ROOT / ".tools" / "gpu-built-wheels" / item["sha256"]
        output.mkdir(parents=True, exist_ok=True)
        wheels = list(output.glob("*.whl"))
        if not wheels:
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "wheel",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-index",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(output),
                    item["local_path"],
                ],
                check=True,
                env=environment,
                timeout=180,
            )
            wheels = list(output.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("La construcción debe producir exactamente una rueda por fuente.")
        with wheels[0].open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        artifact = {
            "name": item["name"],
            "version": item["version"],
            "local_path": str(wheels[0]),
            "sha256": digest,
            "size": wheels[0].stat().st_size,
            "source_sha256": item["sha256"],
        }
        built.append(artifact)
        runtime.append(artifact)
    lock = EVIDENCE / "immich-runtime.lock"
    lock.write_text(local_requirements(runtime), encoding="utf-8")
    receipt = {
        "built": built,
        "runtime_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "runtime_package_count": len(runtime),
        "runtime_installed": False,
    }
    (EVIDENCE / "immich-build-receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )
    return receipt


def install_runtime(values, service):
    config = require_installation(values)
    if service == "immich":
        lock = EVIDENCE / "immich-runtime.lock"
    else:
        lock = SOURCE / "requirements-whisper.lock"
    if (
        hashlib.sha256(lock.read_bytes()).hexdigest()
        != values[f"GPU_{service.upper()}_RUNTIME_LOCK_SHA256"]
    ):
        raise ValueError("El lock completo debe respaldarse antes de instalar el runtime.")
    python = ensure_venv(safe_media_path(config.install_root, service + "/venv"))
    if Path(values[f"GPU_{service.upper()}_PYTHON_PATH"]) != python:
        raise ValueError("La identidad del intérprete no coincide con el respaldo.")
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--require-hashes",
            "--no-deps",
            "--only-binary=:all:",
            "--cache-dir",
            str(ROOT / ".tools" / "gpu-pip-cache"),
            "-r",
            str(lock),
        ],
        check=True,
        env=child_environment(os.environ, {}),
        timeout=600,
    )
    return {
        "service": service,
        "runtime_installed": True,
        "python": str(python),
        "inference_verified": False,
    }


def install_source(values, service):
    config = require_installation(values)
    if service == "immich":
        archive = (
            ROOT
            / ".tools"
            / "immich-research"
            / f"immich-{values['GPU_IMMICH_SOURCE_COMMIT']}.tar.gz"
        )
        metadata = json.loads((SOURCE / "immich-artifacts.json").read_text(encoding="utf-8"))[
            "source"
        ]
        destination = safe_media_path(
            config.install_root,
            "immich/vendor/" + values["GPU_IMMICH_SOURCE_COMMIT"] + "/machine-learning",
        )
        prefix = "machine-learning"
    else:
        archive = ROOT / ".tools" / f"speaches-{values['GPU_WHISPER_SOURCE_COMMIT'][:8]}.zip"
        metadata = json.loads((SOURCE / "whisper-artifacts.json").read_text(encoding="utf-8"))[
            "upstream"
        ]
        destination = safe_media_path(
            config.install_root, "whisper/vendor/" + values["GPU_WHISPER_SOURCE_COMMIT"]
        )
        if destination != Path(values["GPU_WHISPER_SOURCE_ROOT"]):
            raise ValueError("La ruta Speaches debe coincidir con el árbol propio fijado.")
        prefix = ""
    if metadata["sha256"] != values[f"GPU_{service.upper()}_SOURCE_SHA256"]:
        raise ValueError("La fuente no coincide con la huella ya respaldada.")
    verify_artifact(archive, metadata)
    extract_source(archive, destination, prefix)
    return {"service": service, "source_extracted": str(destination), "portability_applied": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=("immich", "whisper"), required=True)
    parser.add_argument("--mode", choices=("source", "build", "runtime"), required=True)
    args = parser.parse_args()
    values = load_vault(ROOT / ".env")
    if args.mode == "source":
        result = install_source(values, args.service)
    elif args.mode == "build" and args.service == "immich":
        result = build_immich(values)
    elif args.mode == "runtime":
        result = install_runtime(values, args.service)
    else:
        parser.error("Solo Immich requiere construir fuentes Python.")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
