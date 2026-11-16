"""Auditor del SDK exportado: plan por defecto, captura de datos y SCA separadas."""

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import stat
import struct
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PREFIX = "MEDIA_ZOU_OPENCV_SDK_AUDIT_"
LOCAL_CONTROLLER = "222e30e5d78dd494482a61c54c906846c13156eb3031466990df5ecb538958bf"
REMOTE_CONTROLLER = "77f442612a9c14df8f0685474ce5e3f47c8c0da16e828888ee8250dc1d7e002b"
SDK_PROFILE = "aa4d28b1ac14c23ceae011fbf22e36a8060ec8e086af0da64aaa4d3ffe785640"
JOB_SHA = "dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44"
TRIVY_SHA = "dd07adf881a72b54d1a922191d62fa939938df2155be85a727da93b67b24e617"
CACHE = ROOT / ".tools/trivy-cache-current-20261106"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def load(name, path, expected=None):
    raw = Path(path).read_bytes()
    if expected and digest(raw) != expected:
        raise ValueError("Una fuente del auditor difiere del pin revisado.")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def dependencies():
    local = load(
        "opencv_audit_controller_cli", HERE / "run_opencv_minimal_sdk.py", LOCAL_CONTROLLER
    )
    if (
        digest(local.read_source(HERE / "opencv_minimal_sdk_remote.py", 128 * 1024))
        != REMOTE_CONTROLLER
    ):
        raise ValueError("El remoto SDK difiere de la revisión 2.")
    return local, load(
        "opencv_audit_fixed_contract", HERE / "opencv_minimal_sdk_remote.py", REMOTE_CONTROLLER
    )


def source_pins():
    return {
        name: digest(path.read_bytes())
        for name, path in {
            "local": Path(__file__),
            "remote": HERE / "opencv_sdk_audit_remote.py",
            "sbom": HERE / "opencv_sdk_sbom.py",
        }.items()
    }


def bind_audit(values=None):
    local, remote = dependencies()
    proposal = local.proposal()
    if proposal["profile_sha256"] != SDK_PROFILE:
        raise ValueError("El perfil SDK no es la revisión congelada.")
    values = local.read_values(proposal) if values is None else values
    sdk = local.bind_plan(proposal, values)
    if local.decode(values[local.KEYS["plan"]]) != sdk:
        raise ValueError("El plan SDK no coincide con su respaldo literal.")
    backed = local.Controller(values, sdk, writer=None).saved()
    for stage in ("prepare", "create", "start", "sdk", "export"):
        remote.require_backed_receipt(stage, backed, backed)
    attempt = values.get(PREFIX + "ATTEMPT", "")
    if not re.fullmatch(r"v[1-9][0-9]{0,2}", attempt):
        raise ValueError("Falta un intento de auditoría respaldado.")
    stem = (
        ROOT
        / ".tools/evidence"
        / ("phase4-zou-opencv-sdk-installed-" + sdk["kit_sha256"][:16] + "-" + attempt)
    )
    if values.get(PREFIX + "REPORT_PREFIX") != str(stem):
        raise ValueError("El prefijo privado de informes no está respaldado.")
    return {
        "schema_version": 1,
        "kind": "opencv-installed-sdk-audit-v1",
        "sdk_plan": sdk,
        "backed": {
            stage: backed[stage] for stage in ("prepare", "create", "start", "sdk", "export")
        },
        "sources": source_pins(),
        "report_prefix": str(stem),
        "job_sha256": JOB_SHA,
        "trivy_sha256": TRIVY_SHA,
        "trivy_version": "0.76.2",
        "cache": str(CACHE),
    }


def verify_audit(audit, expected, values):
    if digest(canonical(audit)) != expected or audit != bind_audit(values):
        raise ValueError("El plan auditor no coincide con el hash externo/estado respaldado.")
    if (
        values.get(PREFIX + "PLAN_SHA256") != expected
        or values.get(PREFIX + "PLAN_B64") != base64.b64encode(canonical(audit)).decode()
    ):
        raise ValueError("El plan auditor y las fuentes deben respaldarse antes de ejecutarlo.")


def write_new(path, data):
    path = Path(path)
    if path.parent != ROOT / ".tools/evidence" or path.parent.resolve() != path.parent:
        raise ValueError("La salida no está bajo la evidencia privada canónica.")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def capture(client, audit):
    local, _ = dependencies()
    source = local.read_source(HERE / "opencv_sdk_audit_remote.py", 64 * 1024)
    if digest(source) != audit["sources"]["remote"]:
        raise ValueError("El lector de datos cambió antes de transmitir.")
    controller = local.read_source(HERE / "opencv_minimal_sdk_remote.py", 128 * 1024)
    layout = local.read_source(ROOT / "docker/host_layout.py")
    if (
        digest(controller) != REMOTE_CONTROLLER
        or digest(layout) != audit["sdk_plan"]["layout_helper_sha256"]
    ):
        raise ValueError("Un helper fijado cambió.")
    request = {
        "audit": audit,
        "audit_sha256": digest(canonical(audit)),
        "controller_source_b64": base64.b64encode(controller).decode(),
        "layout_source_b64": base64.b64encode(layout).decode(),
    }
    data = canonical(request)
    if len(data) > 2 * 1024**2:
        raise ValueError("Petición de lectura excesiva.")
    code = (
        "import sys,struct,hashlib\ns=sys.stdin.buffer.raw\n"
        "def read(n):\n b=bytearray()\n while len(b)<n:\n  x=s.read(n-len(b))\n  if not x:raise ValueError('FRAME_TRUNCATED')\n  b.extend(x)\n return bytes(b)\n"
        "n=struct.unpack('>I',read(4))[0]\n"
        "if not 0<n<=65536:raise ValueError('SOURCE_LIMIT')\n"
        "d=read(n)\nh=hashlib.sha256(d).hexdigest()\n"
        "if h!=" + repr(audit["sources"]["remote"]) + ":raise ValueError('SOURCE_PIN')\n"
        "g={'__name__':'__main__','_AUDIT_SOURCE_SHA256':h}\nexec(compile(d,'<lector-datos-fijado>','exec'),g)"
    )
    channel = client.get_transport().open_session(timeout=5)
    try:
        channel.settimeout(5)
        channel.exec_command(
            "sudo -n /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root /usr/bin/python3 -I -B -c "
            + shlex.quote(code)
        )
        channel.sendall(
            struct.pack(">I", len(source)) + source + struct.pack(">I", len(data)) + data
        )
        channel.shutdown_write()
        response = local.collect(channel, time.monotonic() + 190, 8 * 1024**2)
    finally:
        channel.close()
    if response.get("success") is not True:
        failure = ValueError("La lectura de datos fue rechazada; el SDK no se ejecutó.")
        failure.audit_detail = str(response.get("detail", response.get("error_type", "")))[:240]
        raise failure
    validate_capture(response["result"], audit)
    return response["result"]


def validate_capture(value, audit):
    exported = audit["backed"]["export"]
    if (
        value.get("audit_sha256") != digest(canonical(audit))
        or value.get("source_sha256") != audit["sources"]["remote"]
        or value.get("export_receipt_sha256") != exported["receipt_sha256"]
        or value.get("archive_sha256") != exported["receipt"]["sha256"]
        or value.get("archive", {}).get("size") != exported["receipt"]["bytes"]
        or value.get("cid") != audit["backed"]["create"]["receipt"]["cid"]
        or value.get("material_proofs") != audit["backed"]["prepare"]["receipt"]["material_proofs"]
        or value.get("sdk_executed") is not False
        or value.get("container_mutated") is not False
        or value.get("full_archive_transferred") is not False
    ):
        raise ValueError("La captura no acredita exportación, código y mounts propios.")
    for kind in ("database", "os_release", "manifest"):
        raw = base64.b64decode(value[kind + "_b64"], validate=True)
        if digest(raw) != value[kind + "_sha256"]:
            raise ValueError("Metadato transferido alterado.")
    if value["manifest_sha256"] != audit["sdk_plan"]["sdk_manifest_sha256"]:
        raise ValueError("El manifiesto no corresponde al SDK leído.")


def hash_file(path, maximum):
    path = Path(path)
    if path.resolve(strict=True) != path.absolute():
        raise ValueError("La ruta de un artefacto contiene enlaces.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > maximum
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Artefacto no ordinario o excesivo.")
        before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        checksum = hashlib.sha256()
        while block := stream.read(1024**2):
            checksum.update(block)
        after = os.fstat(stream.fileno())
        named = path.stat()
        if any(
            (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) != before
            for s in (after, named)
        ):
            raise ValueError("El artefacto cambió durante su hash.")
        return checksum.hexdigest()


def validate_db(value, now=None):
    now = datetime.now(UTC) if now is None else now
    updated = datetime.fromisoformat(value["UpdatedAt"].replace("Z", "+00:00"))
    next_update = datetime.fromisoformat(value["NextUpdate"].replace("Z", "+00:00"))
    if (
        value.get("Version") != 2
        or updated.tzinfo is None
        or next_update.tzinfo is None
        or not updated <= now < next_update
    ):
        raise ValueError("La DB de Trivy no tiene vigencia acreditada.")


def validate_job(result):
    if (
        result.status != "completed"
        or result.exit_code != 0
        or result.cleanup_complete is not True
        or re.search(rb"\b(?:WARN|ERROR)\b", result.stdout + result.stderr, re.I)
    ):
        raise ValueError("Trivy no terminó íntegramente o advirtió descarte/error.")


def require_capture(raw, expected):
    if (
        not isinstance(expected, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected)
        or digest(raw) != expected
    ):
        raise ValueError("La captura debe respaldarse por SHA antes de iniciar Trivy.")


def scan(audit, expected_capture):
    local, remote = dependencies()
    prefix = audit["report_prefix"]
    raw_capture = local.read_source(prefix + ".capture.json", 8 * 1024**2)
    require_capture(raw_capture, expected_capture)
    captured = remote.json_object(raw_capture)
    validate_capture(captured, audit)
    sbom_module = load(
        "opencv_sdk_audit_sbom", HERE / "opencv_sdk_sbom.py", audit["sources"]["sbom"]
    )
    database = base64.b64decode(captured["database_b64"], validate=True)
    operating = base64.b64decode(captured["os_release_b64"], validate=True)
    manifest = remote.json_object(base64.b64decode(captured["manifest_b64"], validate=True))
    packages = sbom_module.installed_packages(database, audit["sdk_plan"])
    bom = sbom_module.make_sbom(packages, operating, manifest, captured["header_records"])
    cache = Path(audit["cache"])
    if cache != CACHE:
        raise ValueError("La DB no es la caché fijada para esta unidad.")
    metadata_path, db_path = cache / "db/metadata.json", cache / "db/trivy.db"
    metadata_raw = local.read_source(metadata_path, 16384)
    metadata = remote.json_object(metadata_raw)
    validate_db(metadata)
    database_sha = hash_file(db_path, 2 * 1024**3)
    executable = ROOT / ".tools/bin/trivy.exe"
    if audit["trivy_sha256"] != TRIVY_SHA or hash_file(executable, 256 * 1024**2) != TRIVY_SHA:
        raise ValueError("Trivy no es el ejecutable 0.76.2 fijado.")
    job = load("opencv_audit_native_job", ROOT / "sre/windows_build_job.py", JOB_SHA)
    if audit["job_sha256"] != JOB_SHA:
        raise ValueError("NativeJob no coincide con el pin.")
    for suffix in (".cdx.json", ".trivy.json", ".stdout.log", ".stderr.log", ".receipt.json"):
        if os.path.lexists(prefix + suffix):
            raise ValueError("La auditoría no sobrescribe evidencia de un intento previo.")
    write_new(prefix + ".cdx.json", canonical(bom))
    environment = {
        k: os.environ[k] for k in ("SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ
    }
    environment["PATH"] = str(Path(environment["SystemRoot"]) / "System32")
    result = job.run_job(
        executable,
        [
            "sbom",
            "--cache-dir",
            str(cache),
            "--skip-db-update",
            "--offline-scan",
            "--list-all-pkgs",
            "--format",
            "json",
            "--output",
            prefix + ".trivy.json",
            prefix + ".cdx.json",
        ],
        cwd=ROOT / ".tools/evidence",
        env=environment,
        timeout=180,
        limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
        output_limit=1024**2,
    )
    write_new(prefix + ".stdout.log", result.stdout)
    write_new(prefix + ".stderr.log", result.stderr)
    validate_job(result)
    if (
        hash_file(db_path, 2 * 1024**3) != database_sha
        or local.read_source(metadata_path, 16384) != metadata_raw
    ):
        raise ValueError("La DB cambió durante la auditoría.")
    validate_db(metadata)
    report_raw = local.read_source(prefix + ".trivy.json", 16 * 1024**2)
    report = remote.json_object(report_raw)
    assessment = sbom_module.assess_report(report, bom)
    receipt = {
        "kind": "opencv-installed-sdk-audit-v1",
        "verified": not assessment["high_critical"],
        "audit_sha256": digest(canonical(audit)),
        "capture_sha256": digest(raw_capture),
        "export_receipt_sha256": captured["export_receipt_sha256"],
        "archive_sha256": captured["archive_sha256"],
        "sdk_receipt_sha256": audit["backed"]["sdk"]["receipt_sha256"],
        "kit_sha256": audit["sdk_plan"]["kit_sha256"],
        "base_manifest": audit["sdk_plan"]["base"]["manifest"],
        "report_sha256": digest(report_raw),
        "sbom_sha256": digest(canonical(bom)),
        "db_sha256": database_sha,
        "db_metadata": metadata,
        "job_sha256": JOB_SHA,
        "trivy_sha256": TRIVY_SHA,
        "job_status": result.status,
        "elapsed_seconds": result.elapsed_seconds,
        "cleanup_complete": result.cleanup_complete,
        "sdk_executed": False,
        "final_image_approved": False,
        **assessment,
    }
    write_new(prefix + ".receipt.json", canonical(receipt))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "capture", "scan"), nargs="?", default="plan")
    parser.add_argument("--bind-vault", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--root-authorized", action="store_true")
    args = parser.parse_args(argv)
    client = None
    try:
        if args.mode == "plan" and not args.bind_vault:
            print(
                json.dumps(
                    {
                        "plan_only": True,
                        "infrastructure_executed": False,
                        "sdk_executed": False,
                        "sources": source_pins(),
                        "profile_ready": False,
                        "installed_expected": 89,
                        "partial_header_apk": 2,
                        "numpy_partial": "2.4.6",
                        "tool_sha256": TRIVY_SHA,
                        "job_sha256": JOB_SHA,
                        "requires_root_backed_sdk_export": True,
                    }
                )
            )
            return 0
        local, remote = dependencies()
        if args.mode == "plan":
            if args.output is None:
                raise ValueError("El plan ligado necesita un archivo nuevo de evidencia.")
            audit = bind_audit()
            write_new(args.output.absolute(), canonical(audit))
            print(
                json.dumps(
                    {
                        "success": True,
                        "plan_only": True,
                        "audit_sha256": digest(canonical(audit)),
                        "infrastructure_executed": False,
                    }
                )
            )
            return 0
        if not args.root_authorized or not args.profile or not args.expected_plan_sha256:
            raise ValueError(
                "Capture/scan requieren autorización de raíz y plan/SHA externo respaldados."
            )
        proposal = local.proposal()
        values = local.read_values(proposal)
        raw = local.read_source(args.profile, 2 * 1024**2)
        audit = remote.json_object(raw)
        if canonical(audit) != raw:
            raise ValueError("El plan auditor debe ser canónico.")
        verify_audit(audit, args.expected_plan_sha256, values)
        if args.mode == "capture":
            if os.path.lexists(audit["report_prefix"] + ".capture.json"):
                raise ValueError("La captura anterior se conserva sin repetirla.")
            client = local.connect(values)
            result = capture(client, audit)
            write_new(audit["report_prefix"] + ".capture.json", canonical(result))
            print(
                json.dumps(
                    {
                        "success": True,
                        "capture_sha256": digest(canonical(result)),
                        "full_archive_transferred": False,
                        "sdk_executed": False,
                    }
                )
            )
            return 0
        receipt = scan(audit, values.get(PREFIX + "CAPTURE_SHA256"))
        print(
            json.dumps(
                {
                    "success": receipt["verified"],
                    "receipt_sha256": digest(canonical(receipt)),
                    "coverage_complete": receipt["coverage_complete"],
                    "high_critical": len(receipt["high_critical"]),
                    "sdk_executed": False,
                }
            )
        )
        return 0 if receipt["verified"] else 1
    except BaseException as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "stage": args.mode,
                    "detail": getattr(error, "audit_detail", None),
                    "values_hidden": True,
                    "partial_evidence_preserved": True,
                }
            )
        )
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
