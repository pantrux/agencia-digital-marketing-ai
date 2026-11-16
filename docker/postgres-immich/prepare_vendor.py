"""Prepara fuentes Cargo verificadas; no ejecuta código descargado.

El vendor se publica como TAR para Linux. No se extrae código en Windows ni
se ejecutan Cargo, Git, SDK, build scripts o compiladores.
"""

import argparse
import base64
import concurrent.futures
import gzip
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

WORKSPACE = Path(__file__).resolve().parents[2]
MANIFEST_SHA256 = "8d5a521ef8b2827f5198b9dcb5148c05f0aad743c7ce39912c75491139f5f745"
OWNER = "agencia-digital-marketing-ai:postgres-immich-build:v1"
MARKER = ".agency-postgres-immich-build-owner"
ROOT = WORKSPACE / ".tools/postgres-immich-build"
MAX_ARCHIVE = 64 * 1024**2
ALLOWED_HOSTS = {"static.crates.io", "codeload.github.com"}
SHA = re.compile(r"[a-f0-9]{64}")
NAME = re.compile(r"[A-Za-z0-9_-]+")
VERSION = re.compile(r"[A-Za-z0-9_.+-]+")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_manifest(path):
    data = Path(path).read_bytes()
    if digest(data) != MANIFEST_SHA256:
        raise ValueError("El manifiesto difiere del pin respaldado.")
    value = json.loads(data)
    crates = value["cargo"]["registry_crates"]
    if len(crates) != 244 or len({(c["name"], c["version"]) for c in crates}) != 244:
        raise ValueError("El inventario Cargo no contiene los 244 crates exactos.")
    return value


def validate_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("La descarga no pertenece a una URL HTTPS pública autorizada.")


def validate_acl(snapshot, sid):
    allowed = {sid, "S-1-5-18", "S-1-5-32-544"}
    rules = snapshot.get("rules", [])
    if (
        snapshot.get("owner") != sid
        or snapshot.get("protected") is not True
        or {r.get("sid") for r in rules} != allowed
        or any(r.get("allow") is not True or r.get("inherits") is not True for r in rules)
    ):
        raise ValueError("La raíz requiere propietario y ACL privada explícita heredable.")


def acl_command(root, system):
    encoded = base64.b64encode(str(root).encode()).decode()
    script = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('RUTA'))
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = Get-Acl -LiteralPath $path
$rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object {
    @{sid=$_.IdentityReference.Value; allow=($_.AccessControlType -eq 'Allow');
      inherits=(($_.InheritanceFlags -band 3) -eq 3)}
})
@{sid=$sid; owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value;
  protected=$acl.AreAccessRulesProtected; rules=$rules} | ConvertTo-Json -Depth 4 -Compress
""".replace("RUTA", encoded)
    return [
        str(Path(system) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        base64.b64encode(script.encode("utf-16le")).decode(),
    ]


def verify_root():
    if os.name != "nt":
        raise ValueError("La preparación real requiere la raíz privada Windows respaldada.")
    for path in (*reversed(ROOT.parents), ROOT):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("La cadena de la raíz contiene un enlace o tipo ajeno.")
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    response = subprocess.run(
        acl_command(ROOT, system),
        capture_output=True,
        timeout=20,
        check=True,
        env={"SystemRoot": str(system), "PATH": str(system / "System32")},
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if len(response.stdout) > 65536 or response.stderr:
        raise ValueError("La lectura de ACL no produjo evidencia acotada y completa.")
    snapshot = json.loads(response.stdout.decode("utf-8-sig"))
    validate_acl(snapshot, snapshot["sid"])
    marker = ROOT / MARKER
    info = marker.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & 0x400
        or info.st_size > 1024
        or marker.read_text(encoding="utf-8-sig").strip() != OWNER
    ):
        raise ValueError("La raíz no tiene el marcador ordinario propio esperado.")


def archive_name(name, prefix):
    if not isinstance(name, str) or any(ord(c) < 32 or c == "\\" for c in name):
        raise ValueError("Ruta de archivo ambigua.")
    parts = name.removesuffix("/").split("/")
    if parts[0] != prefix or any(p in {"", ".", ".."} or ":" in p for p in parts):
        raise ValueError("El archivo intenta salir de su raíz fijada.")
    return "/".join(parts[1:])


def inspect_crate(
    content, entry, *, max_members=20000, max_bytes=256 * 1024**2, max_file=64 * 1024**2
):
    if (
        not NAME.fullmatch(entry["name"])
        or not VERSION.fullmatch(entry["version"])
        or not SHA.fullmatch(entry["checksum"])
        or digest(content) != entry["checksum"]
    ):
        raise ValueError("El nombre, versión o checksum del archivo no coincide.")
    prefix = entry["name"] + "-" + entry["version"]
    files, directories, seen, roles, total = [], set(), set(), {}, 0
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        for index, member in enumerate(archive):
            if index >= max_members or not (member.isdir() or member.isfile()):
                raise ValueError("El TAR excede su inventario o contiene enlaces/tipos especiales.")
            relative = archive_name(member.name, prefix)
            if relative in seen or relative == ".cargo-checksum.json":
                raise ValueError("Entrada duplicada o checksum vendor preexistente.")
            seen.add(relative)
            if not relative and not member.isdir():
                raise ValueError("La raíz del crate debe ser un directorio.")
            for i in range(1, len(PurePosixPath(relative).parts) + 1):
                parts = PurePosixPath(relative).parts
                key = "/".join(parts[:i])
                role = "directory" if i < len(parts) or member.isdir() else "file"
                if key in roles and roles[key] != role:
                    raise ValueError("Colisión entre archivo y directorio.")
                roles[key] = role
            total += member.size
            if member.size < 0 or member.size > max_file or total > max_bytes:
                raise ValueError("El TAR excede el presupuesto de bytes.")
            if member.isdir():
                if member.size:
                    raise ValueError("Un directorio contiene bytes inesperados.")
                if relative:
                    directories.add(relative)
                continue
            data = archive.extractfile(member).read(max_file + 1)
            if len(data) != member.size:
                raise ValueError("Entrada truncada o de tamaño incoherente.")
            files.append(
                {
                    "path": relative,
                    "data": data,
                    "mode": 0o755 if member.mode & 0o111 else 0o644,
                    "sha256": digest(data),
                }
            )
    if not files:
        raise ValueError("El crate no contiene archivos regulares.")
    return {
        "name": entry["name"],
        "version": entry["version"],
        "checksum": entry["checksum"],
        "prefix": prefix,
        "files": files,
        "directories": sorted(directories),
    }


def audit_sources(items):
    report = {
        "manual_git_review_complete": False,
        "runtime_link_verified": False,
        "git_candidates": [],
        "process_candidates": [],
        "build_scripts": [],
        "c_sources": [],
        "embedded_c_versions": [],
    }
    for item in items:
        for file in item["files"]:
            path = item["prefix"] + "/" + file["path"]
            suffix = PurePosixPath(path).suffix.lower()
            if suffix in {".c", ".cc", ".cpp", ".h", ".hpp"}:
                report["c_sources"].append(
                    {"path": path, "sha256": file["sha256"], "selection_verified": False}
                )
            if path.endswith("sqlite3.c"):
                version = re.search(
                    rb'^#define SQLITE_VERSION\s+"([0-9.]+)"', file["data"], re.MULTILINE
                )
                if version:
                    report["embedded_c_versions"].append(
                        {
                            "name": "SQLite",
                            "path": path,
                            "version": version[1].decode(),
                            "selection_verified": False,
                        }
                    )
            if path.endswith("mimalloc.h"):
                version = re.search(
                    rb"^#define MI_MALLOC_VERSION\s+\(?([0-9]+)", file["data"], re.MULTILINE
                )
                if version:
                    value = int(version[1])
                    report["embedded_c_versions"].append(
                        {
                            "name": "mimalloc",
                            "path": path,
                            "version": f"{value // 100}.{value // 10 % 10}.{value % 10}",
                            "selection_verified": False,
                        }
                    )
            if suffix not in {".rs", ".sh", ".py", ".cmake"} and "Makefile" not in path:
                continue
            if path.endswith("/build.rs"):
                report["build_scripts"].append({"path": path, "sha256": file["sha256"]})
            for number, line in enumerate(
                file["data"].decode("utf-8", errors="replace").splitlines(), 1
            ):
                if line.lstrip().startswith(("//", "#", "*")):
                    continue
                row = {
                    "path": path,
                    "line": number,
                    "text": line[:300],
                    "file_sha256": file["sha256"],
                }
                if re.search(r"(?<![\w])git(?:\.exe)?(?![\w])", line, re.IGNORECASE):
                    report["git_candidates"].append(row)
                if re.search(r"Command::new|subprocess\.|os\.system|execute_process", line):
                    report["process_candidates"].append(row)
    return report


def source_sbom(entries, audit):
    """Inventario de fuente: los componentes C aún no acreditan selección/enlace."""
    components = []
    for entry in entries:
        purl = "pkg:cargo/" + quote(entry["name"], safe="") + "@" + quote(entry["version"], safe="")
        components.append(
            {
                "type": "library",
                "name": entry["name"],
                "version": entry["version"],
                "bom-ref": purl,
                "purl": purl,
                "hashes": [{"alg": "SHA-256", "content": entry["checksum"]}],
                "properties": [{"name": "agencia:alcance", "value": "fuente fijada; sin compilar"}],
            }
        )
    for index, source in enumerate(audit["embedded_c_versions"]):
        components.append(
            {
                "type": "library",
                "name": source["name"],
                "version": source["version"],
                "bom-ref": "c-source-" + str(index),
                "scope": "optional",
                "properties": [
                    {"name": "agencia:ruta_fuente", "value": source["path"]},
                    {"name": "agencia:enlace_runtime_verificado", "value": "false"},
                ],
            }
        )
    for source in audit["c_sources"]:
        components.append(
            {
                "type": "file",
                "name": source["path"],
                "bom-ref": "c-file-" + digest(source["path"].encode()),
                "scope": "optional",
                "hashes": [{"alg": "SHA-256", "content": source["sha256"]}],
                "properties": [
                    {"name": "agencia:alcance", "value": "fuente C/C++; selección pendiente"}
                ],
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "VectorChord-source-vendor",
                "version": "1.1.1",
                "properties": [{"name": "agencia:manifiesto_sha256", "value": MANIFEST_SHA256}],
            }
        },
        "components": components,
    }


def add_bytes(archive, path, data, mode=0o644):
    member = tarfile.TarInfo(path)
    member.size, member.mode, member.mtime = len(data), mode, 0
    archive.addfile(member, io.BytesIO(data))


def write_vendor(items, destination, *, max_bytes=2 * 1024**3):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("No se reemplaza un vendor existente.")
    crates, audit = [], audit_sources([])
    with destination.open("xb") as stream:
        with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                seen, total = set(), 0
                for item in items:
                    if item["prefix"] in seen:
                        raise ValueError("El vendor repite un crate.")
                    seen.add(item["prefix"])
                    total += sum(len(file["data"]) for file in item["files"])
                    if total > max_bytes:
                        raise ValueError("El vendor excede el presupuesto total descomprimido.")
                    checks = {f["path"]: f["sha256"] for f in item["files"]}
                    for directory in item["directories"]:
                        member = tarfile.TarInfo(item["prefix"] + "/" + directory)
                        member.type, member.mode, member.mtime = tarfile.DIRTYPE, 0o755, 0
                        archive.addfile(member)
                    for file in sorted(item["files"], key=lambda f: f["path"]):
                        add_bytes(
                            archive, item["prefix"] + "/" + file["path"], file["data"], file["mode"]
                        )
                    add_bytes(
                        archive,
                        item["prefix"] + "/.cargo-checksum.json",
                        canonical({"files": checks, "package": item["checksum"]}),
                    )
                    crates.append(
                        {
                            "name": item["name"],
                            "version": item["version"],
                            "archive_sha256": item["checksum"],
                            "files": checks,
                            "tree_sha256": digest(canonical(checks)),
                        }
                    )
                    current = audit_sources([item])
                    for key in audit:
                        if isinstance(audit[key], list):
                            audit[key].extend(current[key])
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "crate_count": len(crates),
        "crates": crates,
        "source_audit": audit,
        "vendor_sha256": digest(destination.read_bytes()),
        "vendor_size": destination.stat().st_size,
    }


def download(url, destination, expected, deadline):
    validate_url(url)
    destination = Path(destination)
    if destination.exists():
        raise ValueError("No se adopta una descarga preexistente sin recibo.")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("La descarga agotó el presupuesto compartido.")
    budget = min(30, remaining)
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    # Schannel usa la validación TLS nativa; no se desactiva revocación ni CA.
    # -q evita configuración del usuario. No se siguen redirecciones.
    command = [
        str(system / "System32/curl.exe"),
        "-q",
        "--fail",
        "--silent",
        "--show-error",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--connect-timeout",
        "15",
        "--max-time",
        str(budget),
        "--max-filesize",
        str(MAX_ARCHIVE),
        "--write-out",
        "%{stderr}%{http_code}",
        url,
    ]
    with destination.open("xb") as output:
        response = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=budget + 2,
            check=True,
            env={"SystemRoot": str(system), "PATH": str(system / "System32")},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        output.flush()
        os.fsync(output.fileno())
    if response.stderr != b"200" or destination.stat().st_size > MAX_ARCHIVE:
        raise ValueError("La descarga no acredita HTTP 200 y tamaño acotado.")
    with destination.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError("El checksum de la descarga no coincide; no se analizará.")
    return destination


def put_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def error_fields(error):
    reason = getattr(error, "reason", error)
    return {
        "type": type(error).__name__,
        "reason_type": type(reason).__name__,
        "errno": getattr(reason, "errno", None),
        "verify_code": getattr(reason, "verify_code", None),
        "http_code": getattr(error, "code", None),
        "process_exit_code": getattr(error, "returncode", None),
    }


def prepare(manifest):
    verify_root()
    final = ROOT / ("vendor-" + MANIFEST_SHA256[:16])
    if final.exists():
        raise ValueError("Ya existe una preparación; verificar su recibo antes de otra operación.")
    stage = ROOT / (".vendor-staging-" + uuid.uuid4().hex)
    stage.mkdir()
    put_json(stage / ".owner.json", {"owner": OWNER, "manifest_sha256": MANIFEST_SHA256})
    downloads = stage / "downloads"
    downloads.mkdir()
    deadline = time.monotonic() + 1200
    source = manifest["vectorchord_source"]
    source_path = download(
        source["url"], downloads / "vectorchord-source.tar.gz", source["sha256"], deadline
    )
    source_item = inspect_crate(
        source_path.read_bytes(),
        {"name": "VectorChord", "version": source["revision"], "checksum": source["sha256"]},
    )
    lock = next(f for f in source_item["files"] if f["path"] == "Cargo.lock")
    if lock["sha256"] != manifest["cargo"]["lock_sha256"]:
        raise ValueError("El lock de la fuente no coincide con el respaldo.")
    entries = sorted(manifest["cargo"]["registry_crates"], key=lambda c: (c["name"], c["version"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                download,
                e["download_url"],
                downloads / (e["name"] + "-" + e["version"] + ".crate"),
                e["checksum"],
                deadline,
            )
            for e in entries
        ]
        for number, future in enumerate(concurrent.futures.as_completed(futures), 1):
            future.result()
            if number % 25 == 0 or number == len(entries):
                print(
                    json.dumps({"descargas_verificadas": number, "total": len(entries)}), flush=True
                )
    items = (
        inspect_crate((downloads / (e["name"] + "-" + e["version"] + ".crate")).read_bytes(), e)
        for e in entries
    )
    result = write_vendor(items, stage / "vendor.tar.gz")
    source_audit = audit_sources([source_item])
    for key, value in source_audit.items():
        if isinstance(value, list):
            result["source_audit"][key].extend(value)
    audit = result.pop("source_audit")
    put_json(stage / "source-audit.json", audit)
    put_json(stage / "source-sbom.cdx.json", source_sbom(entries, audit))
    lock_result = {
        "schema_version": 1,
        "owner": OWNER,
        "manifest_sha256": MANIFEST_SHA256,
        "vectorchord_source_sha256": source["sha256"],
        "cargo_lock_sha256": lock["sha256"],
        **result,
        "sdk_executed": False,
        "compiler_executed": False,
        "runtime_verified": False,
        "manual_git_review_complete": False,
        "build_enabled": False,
    }
    put_json(stage / "vendor-lock.json", lock_result)
    receipt = {
        "success": True,
        "owner": OWNER,
        "manifest_sha256": MANIFEST_SHA256,
        "crate_count": result["crate_count"],
        "path": str(final),
        "files": {
            name: digest((stage / name).read_bytes())
            for name in (
                "vendor.tar.gz",
                "vendor-lock.json",
                "source-audit.json",
                "source-sbom.cdx.json",
            )
        },
        "sdk_executed": False,
        "compiler_executed": False,
        "build_enabled": False,
    }
    put_json(stage / "receipt.json", receipt)
    verify_root()
    if final.exists():
        raise ValueError("El destino apareció durante la preparación; se conserva el staging.")
    stage.rename(final)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    try:
        manifest = load_manifest(Path(__file__).with_name("musl-build-inputs.json"))
        if not args.prepare:
            print(
                json.dumps(
                    {
                        "mode": "plan",
                        "crate_count": 244,
                        "mutated": False,
                        "sdk_executed": False,
                        "build_enabled": False,
                    }
                )
            )
            return 0
        print(json.dumps(prepare(manifest)))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error": "PREPARACION_DETENIDA",
                    **error_fields(error),
                    "sdk_executed": False,
                    "build_enabled": False,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
