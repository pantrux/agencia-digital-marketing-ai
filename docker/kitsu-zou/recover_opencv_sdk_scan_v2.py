"""Recuperación v2 con OS físico explícito; no repite captura, SSH o ejecución SDK."""

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PREFIX = "MEDIA_ZOU_OPENCV_SDK_SCAN_RECOVERY_V2_"
OLD_SCANNER = "1455c74e1dd547d1fb0068b681f67c0de1208a46bdd6d1a9acd6f060c511c01e"
REFS = HERE / "opencv-sdk-scan-recovery-v2-refs.json"
PREVIOUS_RECOVERY = "9ce903e38ebab3a33d19d1c27bfc5eeacd5717d964bbb0850089f32c6421335d"
KNOWN_WARNINGS = {
    "Third-party SBOM may lead to inaccurate vulnerability detection": "sbom-externo-propiedades-cotejadas",
    "Recommend using Trivy to generate SBOMs": "sbom-externo-cobertura-cotejada",
    "Using severities from other vendors for some vulnerabilities. Read https://trivy.dev/docs/v0.76/guide/scanner/vulnerability#severity-selection for details.": "severidad-proveedor-conservada",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def bridge(info):
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "nlink": info.st_nlink,
        "type": stat.S_IFMT(info.st_mode),
    }


def signature(info):
    return {
        **bridge(info),
        "ctime_ns": info.st_ctime_ns,
        "mode": info.st_mode,
        "attributes": getattr(info, "st_file_attributes", 0),
    }


def regular(info):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("Archivo no ordinario, enlazado o con reparse point.")


def validate_signatures(path_before, fd_before, fd_after, path_after):
    for value in (path_before, fd_before, fd_after, path_after):
        regular(value)
    if signature(path_before) != signature(path_after) or signature(fd_before) != signature(
        fd_after
    ):
        raise ValueError("Una firma cambió dentro de su propia API.")
    if not bridge(path_before) == bridge(fd_before) == bridge(fd_after) == bridge(path_after):
        raise ValueError("El puente dev/ino/tamaño/mtime/nlink/tipo no acredita el mismo archivo.")


def hash_file(path, maximum):
    path = Path(path).absolute()
    if type(maximum) is not int or maximum <= 0 or path.resolve(strict=True) != path:
        raise ValueError("Ruta no canónica o límite inválido.")
    for parent in (*reversed(path.parents), path):
        item = parent.lstat()
        if stat.S_ISLNK(item.st_mode) or getattr(item, "st_file_attributes", 0) & 0x400:
            raise ValueError("La ruta contiene enlaces o reparse points.")
    named_before = path.lstat()
    regular(named_before)
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        regular(opened)
        if opened.st_size > maximum or bridge(opened) != bridge(named_before):
            raise ValueError("Archivo excesivo o sustituido antes de abrirlo.")
        checksum, count, remaining = hashlib.sha256(), 0, opened.st_size
        while remaining:
            block = stream.read(min(1024**2, remaining))
            if not block:
                raise ValueError("Lectura truncada antes del tamaño acreditado.")
            checksum.update(block)
            remaining -= len(block)
            count += len(block)
        if stream.read(1) or count != opened.st_size:
            raise ValueError("El archivo creció o el conteo no es exacto.")
        final = os.fstat(stream.fileno())
        named_after = path.lstat()
        validate_signatures(named_before, opened, final, named_after)
        return {
            "sha256": checksum.hexdigest(),
            "bytes": count,
            "fstat": signature(final),
            "lstat": signature(named_after),
        }


def read_exact(path, maximum, expected=None):
    evidence = hash_file(path, maximum)
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        regular(info)
        if info.st_size > maximum:
            raise ValueError("El archivo creció entre las dos lecturas.")
        raw = stream.read(maximum + 1)
    if (
        len(raw) != evidence["bytes"]
        or digest(raw) != evidence["sha256"]
        or expected
        and digest(raw) != expected
    ):
        raise ValueError("Los bytes no coinciden con la referencia verificada.")
    return raw


def load_exact(name, path, expected):
    raw = read_exact(path, 256 * 1024, expected)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def proposal():
    refs_raw = read_exact(REFS, 16384)
    refs = json.loads(refs_raw)
    if (
        refs.get("kind") != "opencv-sdk-scan-recovery-v2"
        or refs.get("original_scanner_sha256") != OLD_SCANNER
        or refs.get("original_scan_failed_preflight") is not True
        or refs.get("original_native_job_started") is not False
        or refs.get("capture_repeated") is not False
    ):
        raise ValueError(
            "Las referencias no conservan el fallo de preflight y la captura original."
        )
    return {
        "schema_version": 1,
        "kind": "opencv-sdk-scan-recovery-v2",
        "refs": refs,
        "refs_sha256": digest(refs_raw),
        "source_sha256": hash_file(Path(__file__), 128 * 1024)["sha256"],
        "hash_contract": "fstat-fstat y lstat-lstat separados; puente dev/ino/size/mtime/nlink/tipo",
        "warning_policy": "Sólo tres mensajes exactos históricos; exige biyección, origen y PURL; findings intactos",
        "os_contract": "Un OS del os-release físico; Metadata.OS Family/Name exactos; validadores sólo type=library",
        "scan_executed": False,
        "ssh_repeated": False,
        "capture_repeated": False,
        "native_job": {
            "seconds": 180,
            "processes": 8,
            "memory_bytes": 2 * 1024**3,
            "output_bytes": 1024**2,
        },
    }


def require_unused_outputs(prefix):
    prefix = Path(prefix)
    allowed = prefix.name + ".capture.json"
    if any(
        item.name.startswith(prefix.name + ".") and item.name != allowed
        for item in prefix.parent.iterdir()
    ):
        raise ValueError("El prefijo conserva otra salida; no se adopta ni sobrescribe.")


def validate_process(result):
    if result.status != "completed" or result.exit_code != 0 or result.cleanup_complete is not True:
        raise ValueError("Trivy no terminó íntegramente dentro del NativeJob.")


def libraries_only(bom):
    return {**bom, "components": [c for c in bom["components"] if c.get("type") == "library"]}


def validate_bom_os(bom, identity):
    expected = {
        "type": "operating-system",
        "name": identity["ID"],
        "version": identity["VERSION_ID"],
        "bom-ref": "os:" + identity["ID"],
    }
    operating = [c for c in bom["components"] if c.get("type") == "operating-system"]
    libraries = libraries_only(bom)["components"]
    if (
        identity.get("ID") != "wolfi"
        or not re.fullmatch(r"[0-9]{8}", identity.get("VERSION_ID", ""))
        or operating != [expected]
        or len(libraries) != 92
        or len(bom["components"]) != 93
    ):
        raise ValueError("El BOM físico no tiene un OS real único y 92 libraries.")
    return expected


def prepare_bom(packages, operating_raw, manifest, headers, sbom_module):
    identity = sbom_module.os_identity(operating_raw)
    libraries = sbom_module.make_sbom(packages, operating_raw, manifest, headers)
    descriptor = {
        "type": "operating-system",
        "name": identity["ID"],
        "version": identity["VERSION_ID"],
        "bom-ref": "os:" + identity["ID"],
    }
    physical = {**libraries, "components": [*libraries["components"], descriptor]}
    validate_bom_os(physical, identity)
    return physical, identity


def validate_report_os(report, identity):
    value = report.get("Metadata", {}).get("OS")
    if (
        not isinstance(value, dict)
        or value.get("Family") != identity["ID"]
        or value.get("Name") != identity["VERSION_ID"]
    ):
        raise ValueError("Metadata.OS no acredita Family/Name del os-release capturado.")
    return value


def validate_sources(report, bom):
    expected = {(c["name"], c["version"]): c for c in bom["components"]}
    if len(expected) != 92:
        raise ValueError("El SBOM no contiene 91 APK y NumPy únicos.")
    observed, apks, python = set(), 0, 0
    for result in report.get("Results", []):
        for package in result.get("Packages", []):
            identity = (package.get("Name"), package.get("Version"))
            if identity in observed or identity not in expected:
                raise ValueError("Identidad de paquete repetida o ajena al SBOM.")
            observed.add(identity)
            component = expected[identity]
            purl = component["purl"]
            if package.get("Identifier", {}).get("PURL") != purl:
                raise ValueError("PURL reportado omitido o divergente del SBOM autenticado.")
            if purl.startswith("pkg:apk/wolfi/"):
                properties = {row["name"]: row["value"] for row in component.get("properties", [])}
                source = properties.get("aquasecurity:trivy:SrcName")
                version = properties.get("aquasecurity:trivy:SrcVersion")
                query = parse_qs(urlsplit(purl).query, strict_parsing=True)
                if (
                    result.get("Type") != "wolfi"
                    or result.get("Class") != "os-pkgs"
                    or set(query) != {"arch", "distro"}
                    or any(len(v) != 1 for v in query.values())
                    or not source
                    or not version
                    or package.get("SrcName") != source
                    or package.get("SrcVersion") != version
                    or package.get("Arch") != query["arch"][0]
                ):
                    raise ValueError(
                        "Origen/versión/arquitectura reportados no acreditan propiedades autenticadas."
                    )
                apks += 1
            elif (
                purl == "pkg:pypi/numpy@2.4.6"
                and result.get("Type") == "python-pkg"
                and result.get("Class") == "lang-pkgs"
            ):
                python += 1
            else:
                raise ValueError("Ecosistema no admitido para la identidad reportada.")
    if observed != set(expected) or apks != 91 or python != 1:
        raise ValueError("La prueba positiva de origen requiere 91 APK y NumPy completos.")
    return {
        "verified": True,
        "apk": apks,
        "numpy": python,
        "apk_fields": ["Name", "Version", "SrcName", "SrcVersion", "Arch", "Identifier.PURL"],
        "numpy_fields": ["Name", "Version", "Identifier.PURL"],
    }


def review_report(result, report, bom, sbom_module, identity):
    validate_process(result)
    validate_bom_os(bom, identity)
    operating = validate_report_os(report, identity)
    package_view = libraries_only(bom)
    assessment = sbom_module.assess_report(report, package_view)
    identities = validate_sources(report, package_view)
    warnings = []
    for channel, raw in (("stdout", result.stdout), ("stderr", result.stderr)):
        for line in raw.splitlines():
            text = line.decode("utf-8")
            if re.search(r"\b(?:ERROR|FATAL)\b", text, re.I):
                raise ValueError("El scanner emitió un ERROR/FATAL; no se clasifica como genérico.")
            if not re.search(r"\bWARN\b", text, re.I):
                continue
            fields = text.split("\t", 2)
            if len(fields) != 3 or fields[1] != "WARN" or fields[2] not in KNOWN_WARNINGS:
                raise ValueError("El scanner emitió un WARN desconocido.")
            warnings.append(
                {
                    "channel": channel,
                    "message": fields[2],
                    "classification": KNOWN_WARNINGS[fields[2]],
                    "line_sha256": digest(line),
                }
            )
    return {
        **assessment,
        "operating_system_verified": True,
        "reported_operating_system": operating,
        "physical_sbom_components": len(bom["components"]),
        "package_sbom_components": len(package_view["components"]),
        "source_identity_verified": identities["verified"],
        "source_identity_coverage": identities,
        "warning_classifications": warnings,
        "raw_stdout_sha256": digest(result.stdout),
        "raw_stderr_sha256": digest(result.stderr),
    }


def execute(profile, expected, profile_path):
    if os.name != "nt" or profile != proposal() or digest(canonical(profile)) != expected:
        raise ValueError("La recuperación requiere Windows y el perfil/SHA revisados.")
    refs = profile["refs"]
    old = load_exact("opencv_scan_recovery_frozen_v2", HERE / "audit_opencv_sdk.py", OLD_SCANNER)
    local, remote = old.dependencies()
    values = local.read_values(local.proposal())
    wanted = {
        "PROFILE_PATH": str(Path(profile_path).absolute()),
        "PROFILE_SHA256": expected,
        "LOCAL_SHA256": profile["source_sha256"],
        "REFS_SHA256": profile["refs_sha256"],
        "REPORT_PREFIX": refs["report_prefix"],
    }
    if any(values.get(PREFIX + key) != value for key, value in wanted.items()):
        raise ValueError("Raíz debe respaldar perfil, ruta y fuentes antes del scan recuperado.")
    original_raw = read_exact(
        refs["original_plan"]["path"], 2 * 1024**2, refs["original_plan"]["sha256"]
    )
    audit = remote.json_object(original_raw)
    if original_raw != old.canonical(audit):
        raise ValueError("El plan histórico no es canónico.")
    old.verify_audit(audit, refs["original_plan"]["sha256"], values)
    read_exact(refs["failure_diagnostic"]["path"], 256 * 1024, refs["failure_diagnostic"]["sha256"])
    previous = refs["previous_recovery"]
    if (
        previous.get("source_sha256") != PREVIOUS_RECOVERY
        or previous.get("scan_executed") is not True
        or previous.get("accepted") is not False
    ):
        raise ValueError("La revisión anterior no conserva su rechazo explícito.")
    read_exact(HERE / "recover_opencv_sdk_scan.py", 128 * 1024, PREVIOUS_RECOVERY)
    read_exact(previous["profile"]["path"], 32768, previous["profile"]["sha256"])
    for artifact in previous["outputs"].values():
        read_exact(artifact["path"], 16 * 1024**2, artifact["sha256"])
    prefix = refs["report_prefix"]
    if (
        audit["report_prefix"] != refs["original_report_prefix"]
        or refs["capture"]["path"] != refs["original_report_prefix"] + ".capture.json"
        or prefix != refs["original_report_prefix"][:-2] + "v2"
    ):
        raise ValueError("El prefijo no corresponde a la captura histórica.")
    raw_capture = read_exact(refs["capture"]["path"], 8 * 1024**2, refs["capture"]["sha256"])
    old.require_capture(raw_capture, values.get(old.PREFIX + "CAPTURE_SHA256"))
    captured = remote.json_object(raw_capture)
    old.validate_capture(captured, audit)
    if captured["archive_sha256"] != refs["archive_sha256"]:
        raise ValueError("La captura no acredita el TAR exportado original.")
    if (
        hash_file(HERE / "opencv_sdk_audit_remote.py", 64 * 1024)["sha256"]
        != refs["original_reader_sha256"]
    ):
        raise ValueError("El lector original fue modificado.")
    sbom_module = load_exact(
        "opencv_scan_recovery_sbom", HERE / "opencv_sdk_sbom.py", refs["original_sbom_sha256"]
    )
    packages = sbom_module.installed_packages(
        base64.b64decode(captured["database_b64"], validate=True), audit["sdk_plan"]
    )
    manifest = remote.json_object(base64.b64decode(captured["manifest_b64"], validate=True))
    bom, operating = prepare_bom(
        packages,
        base64.b64decode(captured["os_release_b64"], validate=True),
        manifest,
        captured["header_records"],
        sbom_module,
    )
    cache = Path(audit["cache"])
    if cache != old.CACHE:
        raise ValueError("La recuperación no cambia de caché DB.")
    metadata_path, database_path = cache / "db/metadata.json", cache / "db/trivy.db"
    metadata_raw = read_exact(metadata_path, 16384)
    metadata = remote.json_object(metadata_raw)
    old.validate_db(metadata)
    database_before = hash_file(database_path, 2 * 1024**3)
    executable = ROOT / ".tools/bin/trivy.exe"
    tool_before = hash_file(executable, 256 * 1024**2)
    if (
        audit["trivy_sha256"] != old.TRIVY_SHA
        or tool_before["sha256"] != old.TRIVY_SHA
        or audit["job_sha256"] != old.JOB_SHA
    ):
        raise ValueError("Herramienta o NativeJob difieren de los pins originales.")
    job = load_exact("opencv_scan_recovery_job", ROOT / "sre/windows_build_job.py", old.JOB_SHA)
    require_unused_outputs(prefix)
    intent = {
        "kind": "opencv-sdk-scan-recovery-intent-v2",
        "recovery_profile_sha256": expected,
        "refs_sha256": profile["refs_sha256"],
        "source_sha256": profile["source_sha256"],
        "original_plan_sha256": refs["original_plan"]["sha256"],
        "capture_sha256": digest(raw_capture),
        "original_failure_sha256": refs["failure_diagnostic"]["sha256"],
        "original_scan_failed_preflight": True,
        "original_native_job_started": False,
        "capture_repeated": False,
        "ssh_repeated": False,
        "sdk_executed": False,
        "previous_recovery_source_sha256": PREVIOUS_RECOVERY,
        "previous_recovery_scan_executed": True,
        "previous_recovery_accepted": False,
        "previous_recovery_report_sha256": previous["outputs"]["report"]["sha256"],
    }
    old.write_new(prefix + ".scan-recovery-intent.json", canonical(intent))
    # Copia explícita del bloque scan v2: cambian el hasher y la procedencia, no los límites.
    old.write_new(prefix + ".cdx.json", old.canonical(bom))
    environment = {
        key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ
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
    old.write_new(prefix + ".stdout.log", result.stdout)
    old.write_new(prefix + ".stderr.log", result.stderr)
    validate_process(result)
    database_after = hash_file(database_path, 2 * 1024**3)
    tool_after = hash_file(executable, 256 * 1024**2)
    if (
        database_after != database_before
        or tool_after != tool_before
        or read_exact(metadata_path, 16384) != metadata_raw
    ):
        raise ValueError("DB o herramienta cambiaron durante la auditoría.")
    old.validate_db(metadata)
    report_raw = read_exact(prefix + ".trivy.json", 16 * 1024**2)
    assessment = review_report(result, remote.json_object(report_raw), bom, sbom_module, operating)
    receipt = {
        "kind": "opencv-installed-sdk-scan-recovery-v2",
        "verified": not assessment["high_critical"],
        "scan_executed": True,
        "recovery": intent,
        "audit_sha256": refs["original_plan"]["sha256"],
        "capture_sha256": digest(raw_capture),
        "archive_sha256": captured["archive_sha256"],
        "operating_system_source_sha256": captured["os_release_sha256"],
        "export_receipt_sha256": captured["export_receipt_sha256"],
        "sdk_receipt_sha256": audit["backed"]["sdk"]["receipt_sha256"],
        "kit_sha256": audit["sdk_plan"]["kit_sha256"],
        "base_manifest": audit["sdk_plan"]["base"]["manifest"],
        "report_sha256": digest(report_raw),
        "sbom_sha256": digest(old.canonical(bom)),
        "db_sha256": database_before["sha256"],
        "db_metadata": metadata,
        "database_evidence": database_before,
        "tool_evidence": tool_before,
        "job_sha256": old.JOB_SHA,
        "trivy_sha256": old.TRIVY_SHA,
        "job_status": result.status,
        "cleanup_complete": result.cleanup_complete,
        "elapsed_seconds": result.elapsed_seconds,
        "sdk_executed": False,
        "final_image_approved": False,
        **assessment,
    }
    old.write_new(prefix + ".receipt.json", canonical(receipt))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "scan"), nargs="?", default="plan")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--root-authorized", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.mode == "plan":
            profile = proposal()
            if args.output:
                target = args.output.absolute()
                if (
                    target.parent != ROOT / ".tools/evidence"
                    or target.parent.resolve() != target.parent
                ):
                    raise ValueError("El perfil debe permanecer en la evidencia privada canónica.")
                with target.open("xb") as stream:
                    stream.write(canonical(profile))
                    stream.flush()
                    os.fsync(stream.fileno())
            print(
                json.dumps(
                    {
                        "plan_only": True,
                        "scan_executed": False,
                        "profile_sha256": digest(canonical(profile)),
                        "profile": profile,
                    }
                )
            )
            return 0
        if not args.root_authorized or not args.profile or not args.expected_profile_sha256:
            raise ValueError("Scan exige perfil/SHA externo y autorización de raíz.")
        raw = read_exact(args.profile, 32768, args.expected_profile_sha256)
        profile = json.loads(raw)
        if raw != canonical(profile):
            raise ValueError("El perfil de recuperación no es canónico.")
        receipt = execute(profile, args.expected_profile_sha256, args.profile)
        print(
            json.dumps(
                {
                    "success": receipt["verified"],
                    "receipt_sha256": digest(canonical(receipt)),
                    "coverage_complete": receipt["coverage_complete"],
                    "capture_repeated": False,
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
                    "stage": args.mode,
                    "error_type": type(error).__name__,
                    "partial_evidence_preserved": True,
                    "values_hidden": True,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
