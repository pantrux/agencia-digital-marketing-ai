"""Operador SCA V2: diagnóstico NativeJob de 1 MiB y reportes propios por FD."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CORE_SHA = "43ac1bbeefc602e78291c7963018003b7e5cdf83bde2d3d6829b9815ff86a923"
MANIFEST_SHA = "2bfd9ad19fd988d0b1c9accfdd4a0b2812cc018b366ce81975c76eadde8813d0"
DECLARATION_OWNER = "agencia-digital-marketing-ai:postgres-immich-final-sca:v1"
DECLARATION_ROOT = ".tools/evidence/phase4-postgres-immich-final-sca-v1"
OPERATOR_OWNER = "agencia-digital-marketing-ai:postgres-immich-final-sca:v2"
OPERATOR_ROOT = ".tools/evidence/phase4-postgres-immich-final-sca-v2"
DIAGNOSTIC_BYTES = 1024**2
REPORT_BYTES = 16 * 1024**2


def require(value, code):
    if not value:
        raise ValueError(code)


def path_for(relative):
    path = Path(relative)
    require(
        not path.is_absolute() and ".." not in path.parts and path.name != ".env", "RELATIVE_PATH"
    )
    path = ROOT / path
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "PATH_LINK",
        )
    return path


def evidence_path(relative):
    path = Path(relative)
    require(
        not path.is_absolute()
        and ".." not in path.parts
        and path.parts[:2] == (".tools", "evidence")
        and path.suffix == ".json",
        "EVIDENCE_REFERENCE_PATH",
    )
    return path_for(relative)


def file_hash(path, maximum):
    path = Path(path)
    named = path.lstat()
    require(
        stat.S_ISREG(named.st_mode)
        and named.st_nlink == 1
        and named.st_size <= maximum
        and not getattr(named, "st_file_attributes", 0) & 0x400,
        "REGULAR_BOUNDED_FILE",
    )
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        first = os.fstat(fd)
        sha, total = hashlib.sha256(), 0
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(1024**2), b""):
                total += len(block)
                require(total <= maximum, "FILE_GROWTH")
                sha.update(block)
        last, again = os.fstat(fd), path.lstat()

        def bridge(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_nlink,
                info.st_mode,
            )

        require(
            bridge(named) == bridge(first) == bridge(last) == bridge(again)
            and named.st_ctime_ns == again.st_ctime_ns
            and first.st_ctime_ns == last.st_ctime_ns
            and total == named.st_size,
            "FILE_CHANGED",
        )
        return {"sha256": sha.hexdigest(), "size": total}
    finally:
        os.close(fd)


def read_file(path, maximum, pin=None):
    before = file_hash(path, maximum)
    if pin is not None:
        require(before["sha256"] == pin, "FILE_PIN")
    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(
        len(raw) == before["size"] and hashlib.sha256(raw).hexdigest() == before["sha256"],
        "READ_CHANGED",
    )
    return raw


def load(name, path, pin):
    raw = read_file(path, 256 * 1024, pin)
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def write_new(path, raw):
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def operator_manifest(declaration):
    require(
        declaration["owner"] == DECLARATION_OWNER and declaration["run_root"] == DECLARATION_ROOT,
        "DECLARATION_IDENTITY",
    )
    return {
        **declaration,
        "owner": OPERATOR_OWNER,
        "run_root": OPERATOR_ROOT,
        "bounds": {
            **declaration["bounds"],
            "capture_bytes": DIAGNOSTIC_BYTES,
            "report_bytes": REPORT_BYTES,
        },
    }


def prepare_plan(core):
    manifest_raw = read_file(HERE / "final-security-inputs-v1.json", 128 * 1024, MANIFEST_SHA)
    declaration = core.json_object(manifest_raw)
    manifest = operator_manifest(declaration)
    require(manifest["schema_version"] == 1 and manifest["approved"] is False, "MANIFEST_POLICY")
    refs = {}
    for name, row in manifest["refs"].items():
        raw = read_file(path_for(row["path"]), 4 * 1024**2, row["sha256"])
        require(len(raw) == row["size"], "REFERENCE_SIZE")
        refs[name] = raw
    pending = []
    archive = ROOT / manifest["subject"]["archive"]["path"]
    if archive.exists():
        actual = file_hash(path_for(manifest["subject"]["archive"]["path"]), 768 * 1024**2)
        require(
            actual == {key: manifest["subject"]["archive"][key] for key in ("sha256", "size")},
            "ARCHIVE_PIN",
        )
    else:
        pending.append("local_archive_missing")
    plan = {
        "schema_version": 1,
        "owner": manifest["owner"],
        "declaration": {
            "owner": declaration["owner"],
            "run_root": declaration["run_root"],
            "bounds": declaration["bounds"],
            "sha256": MANIFEST_SHA,
        },
        "operator": {
            "owner": OPERATOR_OWNER,
            "run_root": OPERATOR_ROOT,
            "diagnostic_bytes": DIAGNOSTIC_BYTES,
            "report_bytes": REPORT_BYTES,
            "vault_prefix": "MEDIA_POSTGRES_IMMICH_FINAL_SECURITY_V2_",
        },
        "subject": manifest["subject"],
        "manifest_sha256": core.digest(manifest_raw),
        "sources": {"local": file_hash(Path(__file__), 256 * 1024)["sha256"], "core": CORE_SHA},
        "reference_hashes": {name: core.digest(raw) for name, raw in refs.items()},
        "pending": pending,
        "ready_for_inventory": not pending,
        "assessment": "pending",
        "approved": False,
        "sdk_executed": False,
        "image_executed": False,
    }
    return manifest, plan, refs


def open_run(core, manifest, plan, create=False):
    base = path_for(".tools/evidence")
    access_ref = manifest["refs"]["access"]
    access = load("pg_sca_access", path_for(access_ref["path"]), access_ref["sha256"])
    access.check_access(base, private=True)
    root = ROOT / manifest["run_root"]
    if create:
        root.mkdir(mode=0o700)
    info = root.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
        "RUN_DIRECTORY",
    )
    access.check_access(root, private=True)
    marker = {
        "owner": manifest["owner"],
        "directory": {"device": info.st_dev, "inode": info.st_ino},
        "plan_sha256": core.digest(core.canonical(plan)),
    }
    if create:
        write_new(root / ".owner.json", core.canonical(marker))
        write_new(root / "plan.json", core.canonical(plan))
    else:
        require(
            core.json_object(read_file(root / ".owner.json", 16384)) == marker
            and read_file(root / "plan.json", 65536) == core.canonical(plan),
            "RUN_BINDING",
        )
    permitted = {
        ".owner.json",
        "plan.json",
        "inventory.json",
        "cargo.cdx.json",
        ".scan-started",
        "tmp",
        "scan-receipt.json",
    } | {
        role + suffix
        for role in ("os", "cargo")
        for suffix in (".trivy.json", ".stdout.log", ".stderr.log", ".job.json")
    }
    for item in root.iterdir():
        require(
            item.name in permitted or re.fullmatch(r"assessment-[a-f0-9]{64}\.json", item.name),
            "FOREIGN_RUN_OUTPUT",
        )
        info = item.lstat()
        require(
            not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & 0x400
            and (
                stat.S_ISDIR(info.st_mode)
                if item.name == "tmp"
                else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            ),
            "RUN_OUTPUT_TYPE",
        )
    return root


def validate_fetch(core, archive, subject, fetch):
    metadata = {key: subject["archive"][key] for key in ("sha256", "size")}
    root = archive.parent
    info = root.lstat()
    marker = core.json_object(read_file(root / ".owner.json", 16384))
    require(
        marker == fetch["directory"]
        and marker["directory"] == {"device": info.st_dev, "inode": info.st_ino}
        and marker["path"] == str(root)
        and marker["owner"] == fetch["owner"]
        and fetch["local"] == str(archive),
        "FETCH_PHYSICAL_IDENTITY",
    )
    require(
        fetch["success"] is True
        and fetch["header"]["image_id"] == subject["image_id"]
        and fetch["header"]["config_digest"] == subject["config_digest"]
        and fetch["archive"]
        == fetch["header"]["archive"]
        == fetch["footer"]["archive"]
        == metadata,
        "FETCH_BINDING",
    )
    require(
        fetch["footer"]["success"] is True
        and fetch["footer"]["record_unchanged"] is True
        and fetch["footer"]["sdk_executed"] is False
        and fetch["footer"]["image_executed"] is False,
        "FETCH_COMPLETED",
    )


def inventory(core, manifest, plan, refs):
    root = open_run(core, manifest, plan, create=True)
    reader = core.load_oci_reader(refs["oci_reader"])
    cargo = core.cargo_inventory(
        core.json_object(refs["vendor_lock"]),
        refs["cargo_lock"],
        core.json_object(refs["sdk_history"]),
        core.json_object(refs["graph"]),
    )
    fetch = core.json_object(refs["fetch_receipt"])
    validate_fetch(
        core, path_for(manifest["subject"]["archive"]["path"]), manifest["subject"], fetch
    )
    model = core.image_inventory(
        reader,
        path_for(manifest["subject"]["archive"]["path"]),
        manifest["subject"],
        core.json_object(refs["runtime_prepare"]),
        core.json_object(refs["runtime_build"]),
        core.json_object(refs["gates"])["data"],
    )
    require(
        file_hash(path_for(manifest["subject"]["archive"]["path"]), 768 * 1024**2)
        == fetch["archive"],
        "ARCHIVE_CHANGED",
    )
    for row in cargo["registry"]:
        name, version = row["name"], row["version"]
        require(all(re.fullmatch(r"[A-Za-z0-9_+.-]+", v) for v in (name, version)), "CRATE_PATH")
        relative = (
            ".tools/postgres-immich-security-inputs-v1/"
            if name in {"anyhow", "crossbeam-epoch"}
            else ".tools/postgres-immich-build/vendor-8d5a521ef8b2827f/downloads/"
        )
        require(
            file_hash(path_for(relative + name + "-" + version + ".crate"), 64 * 1024**2)["sha256"]
            == row["archive_sha256"],
            "CRATE_ARCHIVE_PIN",
        )
    model["cargo"] = cargo
    model["annex_versions_expected"] = manifest["annex_versions"]
    write_new(root / "inventory.json", core.canonical(model))
    write_new(root / "cargo.cdx.json", core.canonical(core.cargo_sbom(cargo)))
    return {
        "inventory_sha256": core.digest(core.canonical(model)),
        "coverage": cargo["counts"],
        "native_assessment": "pending",
        "approved": False,
    }


def save_job(core, root, role, result):
    write_new(root / (role + ".stdout.log"), result.stdout)
    write_new(root / (role + ".stderr.log"), result.stderr)
    write_new(
        root / (role + ".job.json"),
        core.canonical(
            {
                "status": result.status,
                "exit_code": result.exit_code,
                "cleanup_complete": result.cleanup_complete,
                "elapsed_seconds": result.elapsed_seconds,
                "stdout_sha256": core.digest(result.stdout),
                "stderr_sha256": core.digest(result.stderr),
                "error_operation": result.error_operation,
                "error_code": result.error_code,
                "success": result.status == "completed"
                and result.exit_code == 0
                and result.cleanup_complete is True,
            }
        ),
    )


def run_scanner(job, executable, arguments, *, cwd, env, api=None):
    return job.run_job(
        executable,
        arguments,
        cwd=cwd,
        env=env,
        timeout=180,
        limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
        output_limit=DIAGNOSTIC_BYTES,
        _api=api,
    )


def reserve_report(path):
    fd = os.open(
        path,
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    first = os.fstat(fd)
    return fd, (first.st_dev, first.st_ino)


def read_report(path, fd, identity, *, maximum=REPORT_BYTES):
    os.fsync(fd)
    named, first = Path(path).lstat(), os.fstat(fd)

    def bridge(row):
        return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_nlink, row.st_mode

    require(
        (first.st_dev, first.st_ino) == identity
        and bridge(named) == bridge(first)
        and stat.S_ISREG(first.st_mode)
        and first.st_nlink == 1
        and not getattr(named, "st_file_attributes", 0) & 0x400
        and first.st_size <= maximum,
        "REPORT_IDENTITY_OR_SIZE",
    )
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(fd, "rb", closefd=False) as stream:
        raw = stream.read(maximum + 1)
    last, again = os.fstat(fd), Path(path).lstat()
    require(
        bridge(first) == bridge(last) == bridge(again)
        and first.st_ctime_ns == last.st_ctime_ns
        and named.st_ctime_ns == again.st_ctime_ns
        and len(raw) == first.st_size,
        "REPORT_CHANGED",
    )
    return raw


def scan(core, manifest, plan, inventory_sha):
    root = open_run(core, manifest, plan)
    model = core.json_object(read_file(root / "inventory.json", 16 * 1024**2, inventory_sha))
    require(model["subject"] == plan["subject"], "INVENTORY_SUBJECT")
    require(
        read_file(root / "cargo.cdx.json", 16 * 1024**2)
        == core.canonical(core.cargo_sbom(model["cargo"])),
        "SBOM_CHANGED",
    )
    archive_path = path_for(manifest["subject"]["archive"]["path"])
    archive_identity = file_hash(archive_path, 768 * 1024**2)
    require(
        archive_identity
        == {key: manifest["subject"]["archive"][key] for key in ("sha256", "size")},
        "SCAN_ARCHIVE_PIN",
    )
    write_new(root / ".scan-started", b"Etapa exclusiva; no repetir.\n")
    cache = path_for(manifest["db"]["root"])
    database = path_for(manifest["db"]["root"] + "/db/trivy.db")
    metadata_path = path_for(manifest["db"]["root"] + "/db/metadata.json")
    executable = path_for(manifest["tool"]["path"])
    before = file_hash(database, 2 * 1024**3)
    require(
        before["sha256"] == manifest["db"]["database_sha256"]
        and file_hash(executable, 256 * 1024**2)["sha256"] == manifest["tool"]["sha256"],
        "SCANNER_OR_DATABASE_PIN",
    )
    metadata_raw = read_file(metadata_path, 16384, manifest["db"]["metadata_sha256"])
    metadata = core.json_object(metadata_raw)
    now = datetime.now(UTC)
    require(
        metadata["Version"] == 2
        and datetime.fromisoformat(metadata["UpdatedAt"])
        <= now
        < datetime.fromisoformat(metadata["NextUpdate"]),
        "DATABASE_EXPIRED",
    )
    job_ref = manifest["refs"]["native_job"]
    job = load("pg_final_native_job", path_for(job_ref["path"]), job_ref["sha256"])
    (root / "tmp").mkdir(mode=0o700)
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env.update(
        {
            "PATH": str(Path(env["SystemRoot"]) / "System32"),
            "TEMP": str(root / "tmp"),
            "TMP": str(root / "tmp"),
        }
    )
    results = {}
    for role, target in [
        ("os", path_for(manifest["subject"]["archive"]["path"])),
        ("cargo", root / "cargo.cdx.json"),
    ]:
        args = (
            ["image", "--input", str(target), "--scanners", "vuln"]
            if role == "os"
            else ["sbom", str(target)]
        ) + [
            "--cache-dir",
            str(cache),
            "--skip-db-update",
            "--offline-scan",
            "--list-all-pkgs",
            "--format",
            "json",
        ]
        report_path = root / (role + ".trivy.json")
        report_fd, report_identity = reserve_report(report_path)
        try:
            result = run_scanner(
                job, executable, [*args, "--output", str(report_path)], cwd=root, env=env
            )
            save_job(core, root, role, result)
            require(
                result.status == "completed"
                and result.exit_code == 0
                and result.cleanup_complete is True,
                "SCANNER_PROCESS_INCOMPLETE",
            )
            report_raw = read_report(report_path, report_fd, report_identity)
        finally:
            os.close(report_fd)
        report = core.json_object(report_raw)
        expected = (
            {(row["name"], row["version"]) for row in model["cargo"]["registry"]}
            if role == "cargo"
            else {(name, row["version"]) for name, row in model["os_packages"].items()}
        )
        coverage = core.report_coverage(report, "cargo" if role == "cargo" else "alpine", expected)
        if role == "os":
            require(
                report.get("Metadata", {}).get("OS", {}).get("Family") == "alpine"
                and report["Metadata"]["OS"]["Name"] == model["os_version"],
                "REPORTED_OS",
            )
        results[role] = {
            **coverage,
            "warnings": core.scan_warnings(result.stderr),
            "report_sha256": core.digest(report_raw),
            "stdout_sha256": core.digest(result.stdout),
            "stderr_sha256": core.digest(result.stderr),
            "status": result.status,
            "cleanup_complete": result.cleanup_complete,
            "elapsed_seconds": result.elapsed_seconds,
        }
    require(
        file_hash(database, 2 * 1024**3) == before
        and read_file(metadata_path, 16384) == metadata_raw
        and file_hash(executable, 256 * 1024**2)["sha256"] == manifest["tool"]["sha256"],
        "SCANNER_INPUT_CHANGED",
    )
    require(
        file_hash(archive_path, 768 * 1024**2) == archive_identity
        and read_file(root / "cargo.cdx.json", 16 * 1024**2)
        == core.canonical(core.cargo_sbom(model["cargo"])),
        "SCAN_TARGET_CHANGED",
    )
    receipt = {
        "subject": model["subject"],
        "results": results,
        "database": metadata,
        "database_identity": before,
        "metadata_sha256": core.digest(metadata_raw),
        "scanner_sha256": manifest["tool"]["sha256"],
        "job_source_sha256": job_ref["sha256"],
        "approved": False,
    }
    write_new(root / "scan-receipt.json", core.canonical(receipt))
    return {
        "scan_receipt_sha256": core.digest(core.canonical(receipt)),
        "results": results,
        "native_assessment": "pending",
        "approved": False,
    }


def assess(core, manifest, plan, inventory_sha, scan_sha, supplement_path, supplement_sha):
    root = open_run(core, manifest, plan)
    model = core.json_object(read_file(root / "inventory.json", 16 * 1024**2, inventory_sha))
    scanned = core.json_object(read_file(root / "scan-receipt.json", 32 * 1024**2, scan_sha))
    require(scanned["subject"] == model["subject"] == plan["subject"], "ASSESSMENT_SUBJECT")
    current_metadata = read_file(
        path_for(manifest["db"]["root"]) / "db/metadata.json",
        16384,
        manifest["db"]["metadata_sha256"],
    )
    require(
        scanned["database"] == core.json_object(current_metadata)
        and scanned["database_identity"]["sha256"] == manifest["db"]["database_sha256"]
        and scanned["metadata_sha256"] == manifest["db"]["metadata_sha256"]
        and scanned["scanner_sha256"] == manifest["tool"]["sha256"]
        and scanned["job_source_sha256"] == manifest["refs"]["native_job"]["sha256"],
        "ASSESSMENT_SCAN_PROVENANCE",
    )
    require(
        datetime.now(UTC) < datetime.fromisoformat(scanned["database"]["NextUpdate"]),
        "ASSESSMENT_DB_EXPIRED",
    )
    for role in ("os", "cargo"):
        result = scanned["results"][role]
        require(
            result["status"] == "completed" and result["cleanup_complete"] is True,
            "ASSESSMENT_JOB_INCOMPLETE",
        )
        raw = read_file(root / (role + ".trivy.json"), REPORT_BYTES, result["report_sha256"])
        read_file(root / (role + ".stdout.log"), DIAGNOSTIC_BYTES, result["stdout_sha256"])
        expected = (
            {(r["name"], r["version"]) for r in model["cargo"]["registry"]}
            if role == "cargo"
            else {(name, row["version"]) for name, row in model["os_packages"].items()}
        )
        observed = core.report_coverage(
            core.json_object(raw), "cargo" if role == "cargo" else "alpine", expected
        )
        require(all(result[key] == value for key, value in observed.items()), "REPORT_CHANGED")
        logs = read_file(root / (role + ".stderr.log"), 1024**2, result["stderr_sha256"])
        require(core.scan_warnings(logs) == result["warnings"], "WARNINGS_CHANGED")
    supplement = core.json_object(
        read_file(evidence_path(supplement_path), 32 * 1024**2, supplement_sha)
    )
    require(
        supplement["image_id"] == plan["subject"]["image_id"]
        and supplement["config_digest"] == plan["subject"]["config_digest"],
        "SUPPLEMENT_SUBJECT",
    )
    osv, annexes = None, {}
    if supplement.get("osv"):
        row = supplement["osv"]
        bundle = core.json_object(
            read_file(evidence_path(row["path"]), 32 * 1024**2, row["sha256"])
        )
        require(
            bundle.get("image_id") == plan["subject"]["image_id"]
            and bundle.get("config_digest") == plan["subject"]["config_digest"],
            "OSV_SUBJECT",
        )
        expected = {(r["name"], r["version"]) for r in model["cargo"]["registry"] if r["selected"]}
        osv = core.osv_coverage(bundle, expected)
    for name, ref in supplement.get("annexes", {}).items():
        require(name in core.ANNEXES, "ANNEX_NAME")
        row = core.json_object(read_file(evidence_path(ref["path"]), 32 * 1024**2, ref["sha256"]))
        require(
            row["image_id"] == supplement["image_id"]
            and row["config_digest"] == supplement["config_digest"]
            and row["version"] == manifest["annex_versions"][name],
            "ANNEX_SUBJECT_VERSION",
        )
        observed = model["native_components"][name]
        require(
            row["binary_sha256"] == observed.get("sha256", observed.get("embedded_subject"))
            and type(row.get("evidence")) is list
            and row["evidence"]
            and re.fullmatch(r"[a-f0-9]{64}", str(row.get("source_sha256"))),
            "ANNEX_BINARY_SOURCE",
        )
        for evidence in row["evidence"]:
            read_file(evidence_path(evidence["path"]), 32 * 1024**2, evidence["sha256"])
        annexes[name] = row
    result = {
        "subject": plan["subject"],
        "os": scanned["results"]["os"],
        "cargo": scanned["results"]["cargo"],
        "osv": osv,
        "annexes": annexes,
        "assessment": core.assessment(
            scanned["results"]["os"], scanned["results"]["cargo"], osv, annexes
        ),
        "supplement_sha256": supplement_sha,
    }
    write_new(root / ("assessment-" + supplement_sha + ".json"), core.canonical(result))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("plan", "inventory", "scan", "assess"), nargs="?", default="plan"
    )
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--expected-inventory-sha256")
    parser.add_argument("--expected-scan-sha256")
    parser.add_argument("--supplement")
    parser.add_argument("--expected-supplement-sha256")
    args = parser.parse_args(argv)
    core = load("pg_final_security_core", HERE / "final_security.py", CORE_SHA)
    manifest, plan, refs = prepare_plan(core)
    if args.mode == "plan":
        print(
            json.dumps(
                {
                    "plan": plan,
                    "plan_sha256": core.digest(core.canonical(plan)),
                    "assessment": "pending",
                    "approved": False,
                },
                indent=2,
            )
        )
        return 0
    require(
        plan["ready_for_inventory"]
        and args.expected_plan_sha256 == core.digest(core.canonical(plan)),
        "EXTERNAL_PLAN_PIN",
    )
    if args.mode == "inventory":
        result = inventory(core, manifest, plan, refs)
    elif args.mode == "scan":
        require(args.expected_inventory_sha256 is not None, "INVENTORY_BACKUP_PIN")
        result = scan(core, manifest, plan, args.expected_inventory_sha256)
    else:
        require(
            args.expected_inventory_sha256
            and args.expected_scan_sha256
            and args.supplement
            and args.expected_supplement_sha256,
            "ASSESSMENT_PINS",
        )
        result = assess(
            core,
            manifest,
            plan,
            args.expected_inventory_sha256,
            args.expected_scan_sha256,
            args.supplement,
            args.expected_supplement_sha256,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        print(
            json.dumps(
                {
                    "action_completed": False,
                    "approved": False,
                    "error_type": type(error).__name__,
                    "detail": str(error)[:240],
                    "prior_evidence_preserved": True,
                }
            )
        )
        raise SystemExit(1) from None
