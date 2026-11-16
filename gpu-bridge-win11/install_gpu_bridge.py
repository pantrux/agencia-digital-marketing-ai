"""Instalación incremental propia del puente: plan por defecto y secretos mínimos."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import venv
from pathlib import Path

from bridge_common import BridgeConfig, safe_media_path
from gpu_artifacts import fetch_artifact, verify_artifact

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parent
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
from check_env import compare_keys, parse_env, validate_comments, validate_template  # noqa: E402

OWNER_FILE = ".agencia-gpu-owner"
BASE_FILES = ("bridge_common.py", "metrics_service.py", "requirements-metrics.lock")
SERVICE_FILES = (
    "bridge_common.py",
    "metrics_service.py",
    "bridge_supervisor.py",
    "launch_service.py",
    "immich_service.py",
    "immich_portability.py",
    "immich-artifacts.json",
    "whisper_service.py",
    "whisper-artifacts.json",
    "face_adapter.py",
    "face_models.py",
    "face-artifacts.json",
)


def check_owned_root(root: Path, owner: str) -> bool:
    """Nunca adopta un directorio existente sin marcador, aunque parezca vacío."""
    if not root.exists():
        return False
    metadata = root.lstat()
    if (
        not root.is_dir()
        or stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("La instalación no puede ser un enlace o punto de reanálisis.")
    marker = root / OWNER_FILE
    if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8") != owner:
        raise ValueError("El directorio existente no pertenece a este puente; se conserva.")
    return True


def runtime_values(values: dict[str, str]) -> dict[str, str]:
    """Publica solo referencias GPU; la bóveda NAS/nube/GitHub no se copia."""
    return {key: value for key, value in values.items() if key.startswith("GPU_")}


def load_vault(vault: Path) -> dict[str, str]:
    content = vault.read_text(encoding="utf-8-sig")
    values, errors = parse_env(content)
    template_text = vault.with_name(".env.example").read_text(encoding="utf-8-sig")
    template, template_errors = parse_env(template_text)
    errors += template_errors + compare_keys(values, template) + validate_template(template)
    errors += validate_comments(content) + validate_comments(template_text)
    if errors:
        raise ValueError("La bóveda o su plantilla no cumplen el gobierno de variables.")
    return values


def private_acl(root: Path, sid: str):
    """Protege el árbol propio y comprueba los principales con identidades SID."""
    allowed = {sid, "S-1-5-18", "S-1-5-32-544"}
    script = """$ErrorActionPreference='Stop'
$acl=Get-Acl -LiteralPath $env:AGENCY_GPU_ACL_PATH
$entries=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | ForEach-Object {
 [pscustomobject]@{sid=$_.IdentityReference.Value;allow=($_.AccessControlType -eq 'Allow');full=(([int64]$_.FileSystemRights -band 2032127) -eq 2032127)}
})
[pscustomobject]@{protected=$acl.AreAccessRulesProtected;owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value;entries=$entries} | ConvertTo-Json -Compress -Depth 4
"""
    environment = os.environ.copy()
    environment["AGENCY_GPU_ACL_PATH"] = str(root)
    powershell = shutil.which("pwsh.exe")
    if not powershell:
        raise ValueError("Se requiere PowerShell 7 para comprobar la ACL privada.")

    def query():
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return json.loads(result.stdout)

    def matches(acl):
        entries = acl.get("entries")
        return (
            acl.get("protected") is True
            and acl.get("owner") in allowed
            and isinstance(entries, list)
            and {entry["sid"] for entry in entries} == allowed
            and all(entry["allow"] and entry["full"] for entry in entries)
        )

    if matches(query()):
        return
    args = ["icacls.exe", str(root), "/inheritance:r", "/grant:r"]
    args += [f"*{principal}:(OI)(CI)F" for principal in sorted(allowed)]
    subprocess.run(args, check=True, capture_output=True, timeout=120)
    if not matches(query()):
        raise ValueError("La ACL privada no coincide; no se publicarán secretos.")


def prepare_base(values: dict[str, str]) -> dict:
    config = BridgeConfig.from_env(values)
    if platform.system() != "Windows" or socket.gethostname().upper() != "W11-HOMELAB":
        raise ValueError("Este instalador solo aplica en W11-HOMELAB.")
    if config.install_root != Path(r"C:\GPU-Bridge"):
        raise ValueError("La raíz no coincide con la ruta canónica del plan.")
    root, owner = config.install_root, values["GPU_INSTALL_OWNER_MARKER"]
    existed = check_owned_root(root, owner)
    if not existed:
        root.mkdir()
        (root / OWNER_FILE).write_text(owner, encoding="utf-8")
    private_acl(root, values["GPU_WINDOWS_USER_SID"])
    destination = root / "services"
    destination.mkdir(exist_ok=True)
    files = {}
    for name in BASE_FILES:
        content = (SOURCE / name).read_bytes()
        target = destination / name
        if target.is_symlink() or (
            target.exists() and getattr(target.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Un archivo de instalación fue sustituido por un enlace.")
        temporary = target.with_suffix(target.suffix + ".new")
        temporary.write_bytes(content)
        temporary.replace(target)
        files[name] = hashlib.sha256(content).hexdigest()
    runtime = root / "runtime.json"
    if runtime.is_symlink():
        raise ValueError("La configuración privada no puede ser un enlace.")
    temporary = root / "runtime.json.new"
    temporary.write_text(json.dumps(runtime_values(values)), encoding="utf-8")
    temporary.replace(runtime)
    environment = root / "metrics" / "venv"
    python = environment / "Scripts" / "python.exe"
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(destination / "requirements-metrics.lock"),
        ],
        check=True,
        timeout=180,
    )
    receipt = {
        "owner": owner,
        "root": str(root),
        "base_files": files,
        "metrics_python": str(python),
        "private_acl_verified": True,
        "services_started": False,
    }
    (root / "base-install-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def install_models(values: dict[str, str], cache: Path) -> dict:
    """Descarga y publica únicamente los modelos cuyos manifiestos ya están respaldados."""
    config = BridgeConfig.from_env(values)
    if not check_owned_root(config.install_root, values["GPU_INSTALL_OWNER_MARKER"]):
        raise ValueError("Primero se requiere instalar la base privada del puente.")
    root = config.install_root.resolve(strict=True)
    model_root = config.model_cache_root
    if not model_root.resolve(strict=False).is_relative_to(root):
        raise ValueError("La caché debe pertenecer al árbol privado del puente.")
    immich = json.loads((SOURCE / "immich-artifacts.json").read_text(encoding="utf-8"))["models"]
    whisper = json.loads((SOURCE / "whisper-artifacts.json").read_text(encoding="utf-8"))["model"]

    def digest(data):
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    if (
        digest(immich) != values["GPU_IMMICH_MODELS_MANIFEST_SHA256"]
        or digest(whisper) != values["GPU_WHISPER_MODELS_MANIFEST_SHA256"]
    ):
        raise ValueError("El manifiesto de modelos difiere de la bóveda respaldada.")
    artifacts = []
    for model in immich:
        artifacts += [("immich/" + item["cache_relative"], item) for item in model["files"]]
    artifacts += [
        (whisper["cache_relative"] + "/" + item["filename"], item) for item in whisper["files"]
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        fetched = list(pool.map(lambda item: fetch_artifact(item[1], cache), artifacts))
    model_root.mkdir(parents=True, exist_ok=True)
    installed = []
    for (relative, metadata), source in zip(artifacts, fetched, strict=True):
        destination = safe_media_path(model_root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            verify_artifact(destination, metadata)
        else:
            temporary = safe_media_path(model_root, relative + ".new")
            shutil.copyfile(source, temporary)
            verify_artifact(temporary, metadata)
            temporary.replace(destination)
        installed.append(
            {"relative": relative, "sha256": metadata["sha256"], "size": metadata["size"]}
        )
    probe = ROOT / ".tools" / "fixtures" / "whisper-probe.wav"
    with probe.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != values["GPU_WHISPER_PROBE_SHA256"]:
            raise ValueError("La prueba de voz no coincide con la bóveda.")
    destination = safe_media_path(root, "fixtures/whisper-probe.wav")
    destination.parent.mkdir(exist_ok=True)
    if destination.exists() and destination.read_bytes() != probe.read_bytes():
        raise ValueError("La prueba instalada difiere y se conserva sin reemplazar.")
    if not destination.exists():
        shutil.copyfile(probe, destination)
    receipt = {
        "model_files": installed,
        "probe_sha256": values["GPU_WHISPER_PROBE_SHA256"],
        "inference_verified": False,
    }
    (root / "model-install-receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )
    return receipt


def service_bundle():
    sources = {"services/" + name: SOURCE / name for name in SERVICE_FILES}
    sources["services/sre_control.py"] = ROOT / "sre" / "sre_control.py"
    for name in (
        "start-gpu-bridge.ps1",
        "Stop-GpuBridge.ps1",
        "Configure-GpuFirewall.ps1",
        "Apply-GpuFirewallElevated.ps1",
    ):
        sources[name] = SOURCE / name
    hashes = {}
    for target, source in sources.items():
        if source.is_symlink() or not source.is_file():
            raise ValueError("Falta una fuente regular del paquete de servicios.")
        hashes[target] = hashlib.sha256(source.read_bytes()).hexdigest()
    digest = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return sources, hashes, digest


def publish_services(values):
    config = BridgeConfig.from_env(values)
    if not check_owned_root(config.install_root, values["GPU_INSTALL_OWNER_MARKER"]):
        raise ValueError("Primero debe existir la base privada propia.")
    sources, hashes, digest = service_bundle()
    if digest != values["GPU_SERVICES_BUNDLE_SHA256"]:
        raise ValueError("El paquete de servicios debe respaldarse antes de publicarse.")
    private_acl(config.install_root, values["GPU_WINDOWS_USER_SID"])
    for name, source in sources.items():
        target = safe_media_path(config.install_root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != hashes[name]:
            raise ValueError("Una fuente cambió durante la publicación.")
        temporary = safe_media_path(config.install_root, name + ".new")
        temporary.write_bytes(payload)
        temporary.replace(target)
    safe_media_path(config.install_root, "logs").mkdir(exist_ok=True)
    fixture = ROOT / ".tools" / "immich-research" / "astronaut.png"
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != values["GPU_IMMICH_SMOKE_IMAGE_SHA256"]:
        raise ValueError("La imagen pública no coincide con la huella respaldada.")
    target = safe_media_path(config.install_root, "fixtures/immich-astronaut.png")
    if str(target) != values["GPU_IMMICH_SMOKE_IMAGE_PATH"]:
        raise ValueError("La ruta de la imagen no coincide con su respaldo.")
    if (
        target.exists()
        and hashlib.sha256(target.read_bytes()).hexdigest()
        != values["GPU_IMMICH_SMOKE_IMAGE_SHA256"]
    ):
        raise ValueError("La imagen existente es distinta y se conserva.")
    if not target.exists():
        shutil.copyfile(fixture, target)
    runtime = safe_media_path(config.install_root, "runtime.json.new")
    runtime.write_text(json.dumps(runtime_values(values)), encoding="utf-8")
    runtime.replace(config.install_root / "runtime.json")
    receipt = {"bundle_sha256": digest, "files": hashes, "services_started": False}
    (config.install_root / "services-install-receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("plan", "base", "models", "service-plan", "services"), default="plan"
    )
    parser.add_argument("--cache", type=Path, default=ROOT / ".tools" / "gpu-cache")
    parser.add_argument("--vault", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    values = load_vault(args.vault)
    config = BridgeConfig.from_env(values)
    if args.mode == "service-plan":
        _, hashes, digest = service_bundle()
        result = {"bundle_sha256": digest, "files": hashes, "changes_applied": False}
    elif args.mode == "plan":
        result = {
            "root": str(config.install_root),
            "owned_existing": check_owned_root(
                config.install_root, values["GPU_INSTALL_OWNER_MARKER"]
            ),
            "files": list(BASE_FILES),
            "runtime_keys": sorted(runtime_values(values)),
            "changes_applied": False,
        }
    elif args.mode == "base":
        result = prepare_base(values)
    elif args.mode == "models":
        result = install_models(values, args.cache)
    else:
        result = publish_services(values)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
