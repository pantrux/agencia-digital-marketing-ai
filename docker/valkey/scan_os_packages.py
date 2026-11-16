"""Plan local y SCA explícito de 23 APK; sin aprobar el binario ni el servicio."""

import argparse
import hashlib
import json
import os
import re
import stat
import types
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
PUBLICATION_ROOT = ROOT / ".tools/evidence/phase4-valkey-inventory-v2"
PUBLICATION_OWNER = "agencia-digital-marketing-ai:valkey-inventory:v2"
PUBLICATION_IDENTITY = {"device": 8121725096129312908, "inode": 1125899907501674}
PUBLICATION_PLAN_SHA = "71e67caf06bbf1c84b63ad724157cb9ab8cbcfbbf6de1c089531d4737bb48ae3"
PUBLICATION_FILES = {
    ".owner.json": "5b732274576df52b8a0c799f353e0b18f9b8d7892c2b090323629c4eb8ea8519",
    "plan.json": PUBLICATION_PLAN_SHA,
    "receipt.json": "5e8a419535f6fdc49785cfe6d8321331adcb268d0eb3c2496ecae58b8f634afb",
    "inventory.json": "7ebf54457de5fa7c6413d6d5f7c64fed94517f81894cd3e5da5596efc911ccfd",
    "os-packages.cdx.json": "abe00bc00ffb948e71874952db76058a4db13b458e2f21e905cbe45c4bc8dd3d",
    "binary-materials.json": "98c94ae22fa9da87f3a1d7aab2f2cdebae2061f175849f26a534b2c4f21c8246",
    "file-catalog.json": "a9aa006f02d78ac917f2f267474ffe0f31b4b2867b80f3ed3ac6e03a35f4a7ce",
    "provenance.json": "e08dfe53ff7d6ef631835828f5ac1830a0a4c28bde852bb432a264910c7c20bd",
}
PUBLICATION_SOURCE_PINS = {
    "local": "042849c2823519de87b1d92ab2edcd8685aadf66ce7ca316e57d5e4c028203b5",
    "core": "2d1f335fbb458d34c80b3a2c0fa993d75bc5cf0858377ac9bb1a25966976ee6d",
    "parser": "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49",
    "reader": "b15f8ed20e19b0b1b19dfdd5ffd81ea60667175a014ab63409b29012fa6f253c",
}
READER_PATH = "docker/kitsu-zou/recover_opencv_sdk_scan_v2.py"
READER_SHA = PUBLICATION_SOURCE_PINS["reader"]
JOB_PATH = "sre/windows_build_job.py"
JOB_SHA = "dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44"
TRIVY = ROOT / ".tools/bin/trivy.exe"
TRIVY_SHA = "dd07adf881a72b54d1a922191d62fa939938df2155be85a727da93b67b24e617"
CACHE = ROOT / ".tools/trivy-cache-current-20261106"
CACHE_OWNER = "agencia-digital-marketing-ai:trivy-current-db:v2"
DB_SHA = "7520aa80bfb8db4a230514baa510ec6b1d7de9099b74f7879be65450407a6f1f"
METADATA_SHA = "475ea2061a7f343953b880fd0eb2ce56253cca495d48f12661f3bd3e5e95c125"
RUN_ROOT = ROOT / ".tools/evidence/phase4-valkey-os-sca-v1"
OWNER = "agencia-digital-marketing-ai:valkey-os-sca:v1"
MAX_OUTPUT = 1024**2
SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
KNOWN_WARNINGS = {
    "Third-party SBOM may lead to inaccurate vulnerability detection": "SBOM externo con identidades cotejadas",
    "Recommend using Trivy to generate SBOMs": "SBOM externo con cobertura cotejada",
    "Using severities from other vendors for some vulnerabilities. Read https://trivy.dev/docs/v0.76/guide/scanner/vulnerability#severity-selection for details.": "Severidad de proveedor conservada",
}

# Copias explícitas de funciones de embedded_js_sca_v2.py (SHA d084097c…);
# no se importa ese controlador ni se modifican sus variables globales.


def require(value, code):
    if not value:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def json_object(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "JSON_DUPLICATE")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("JSON_NONFINITE")

    value = json.loads(
        raw.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=invalid
    )
    require(type(value) is dict, "JSON_OBJECT")
    return value


def directory_chain(path):
    path = Path(path).absolute()
    require(path.resolve(strict=True) == path, "PATH_CANONICAL")
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "DIRECTORY_LINK",
        )
    return path


def load_reader():
    # Bootstrap mínimo: sólo compila los bytes cuyo SHA coincide, nunca un .pyc.
    path = ROOT / READER_PATH
    directory_chain(path.parent)
    before = path.lstat()
    require(
        stat.S_ISREG(before.st_mode)
        and before.st_nlink == 1
        and not getattr(before, "st_file_attributes", 0) & 0x400
        and before.st_size <= 256 * 1024,
        "READER_FILE",
    )
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(256 * 1024 + 1)
        after = path.lstat()
    require(
        (before.st_dev, before.st_ino, before.st_size)
        == (opened.st_dev, opened.st_ino, opened.st_size)
        == (after.st_dev, after.st_ino, after.st_size),
        "READER_CHANGED",
    )
    require(digest(raw) == READER_SHA, "READER_PIN")
    module = types.ModuleType("embedded_js_fixed_reader")
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def validate_stderr(raw):
    require(len(raw) <= MAX_OUTPUT, "STDERR_LIMIT")
    text = raw.decode("utf-8", "strict").replace("\r\n", "\n")
    require(not any(ord(char) < 32 and char not in "\n\t" for char in text), "LOG_CONTROL")
    warnings = []
    for line in text.splitlines():
        if not re.search(r"\b(?:WARN|WARNING|ERROR|FATAL)\b", line, re.I):
            continue
        fields = line.split("\t", 2)
        require(
            len(fields) == 3 and fields[1] == "WARN" and fields[2] in KNOWN_WARNINGS,
            "SCANNER_DIAGNOSTIC",
        )
        warnings.append({"message": fields[2], "classification": KNOWN_WARNINGS[fields[2]]})
    return warnings


def validate_db(metadata, now=None):
    now = now or datetime.now(UTC)
    require(type(metadata.get("Version")) is int and metadata["Version"] == 2, "DB_VERSION")
    dates = []
    for key in ("UpdatedAt", "DownloadedAt", "NextUpdate"):
        value = metadata[key]
        require(type(value) is str and value.endswith("Z"), "DB_DATE")
        dates.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
    updated, downloaded, next_update = dates
    require(updated <= downloaded <= now < next_update, "DB_EXPIRED_OR_FUTURE")


def capture_snapshot(reader):
    directory_chain(CACHE)
    owner = reader.read_exact(CACHE / ".owner", 256)
    require(owner == CACHE_OWNER.encode(), "CACHE_OWNER")
    raw = reader.read_exact(CACHE / "db/metadata.json", 16384, METADATA_SHA)
    metadata = json_object(raw)
    validate_db(metadata)
    database = reader.hash_file(CACHE / "db/trivy.db", 2 * 1024**3)
    tool = reader.hash_file(TRIVY, 256 * 1024**2)
    require(database["sha256"] == DB_SHA and tool["sha256"] == TRIVY_SHA, "DB_TOOL_PIN")
    directories, files = {}, {}

    def observe(path, depth=0):
        require(depth <= 12 and len(directories) + len(files) < 512, "CACHE_TREE_LIMIT")
        info = path.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "CACHE_LINK",
        )
        identity = {
            key: getattr(info, key)
            for key in ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")
        }
        directories[str(path)] = identity
        for child in sorted(path.iterdir()):
            node = child.lstat()
            require(
                not stat.S_ISLNK(node.st_mode)
                and not getattr(node, "st_file_attributes", 0) & 0x400,
                "CACHE_LINK",
            )
            if stat.S_ISDIR(node.st_mode):
                observe(child, depth + 1)
            else:
                require(len(directories) + len(files) < 512, "CACHE_TREE_LIMIT")
                files[str(child.relative_to(CACHE))] = (
                    database
                    if child == CACHE / "db/trivy.db"
                    else reader.hash_file(child, 32 * 1024**2)
                )
        require(
            identity == {key: getattr(path.lstat(), key) for key in identity},
            "CACHE_DIRECTORY_CHANGED",
        )

    observe(CACHE)
    return {
        "db_metadata": metadata,
        "metadata_sha256": digest(raw),
        "owner_sha256": digest(owner),
        "database": database,
        "tool": tool,
        "directories": directories,
        "files": files,
    }


def scanner_environment():
    environment = {
        key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ
    }
    require("SystemRoot" in environment, "WINDOWS_ENVIRONMENT")
    environment["PATH"] = str(Path(environment["SystemRoot"]) / "System32")
    environment["NO_COLOR"] = "1"
    return environment


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
    return {"path": str(path), "sha256": digest(raw), "bytes": len(raw)}


def validate_inputs(inventory, bom):
    require(
        inventory["os"]["identity"].get("ID") == "alpine"
        and inventory["os"]["identity"].get("VERSION_ID") == "3.24.3"
        and inventory.get("binary_accepted") is False
        and inventory.get("binary_sca_verified") is False
        and inventory.get("os_sca_verified") is False,
        "INVENTORY_SCOPE",
    )
    rows = inventory["os"]["packages"]
    require(type(rows) is list and len(rows) == 23, "INVENTORY_COUNT")
    expected = {}
    for row in rows:
        require(
            type(row) is dict
            and all(
                type(row.get(key)) is str
                and row[key]
                and row[key].isascii()
                and all(32 < ord(char) < 127 for char in row[key])
                for key in ("name", "version", "architecture", "source_name", "source_version")
            )
            and row["architecture"] == "x86_64",
            "INVENTORY_PACKAGE",
        )
        pair = (row["name"], row["version"])
        require(pair not in expected, "INVENTORY_DUPLICATE")
        expected[pair] = {
            key: row[key]
            for key in ("name", "version", "architecture", "source_name", "source_version")
        }
        expected[pair]["purl"] = (
            "pkg:apk/alpine/"
            + quote(row["name"], safe="")
            + "@"
            + quote(row["version"], safe=".-")
            + "?arch=x86_64&distro=3.24.3"
        )
    require(
        bom.get("bomFormat") == "CycloneDX"
        and bom.get("specVersion") == "1.5"
        and bom.get("version") == 1
        and type(bom.get("components")) is list
        and len(bom["components"]) == 24,
        "SBOM_SCHEMA",
    )
    operating = [row for row in bom["components"] if row.get("type") == "operating-system"]
    require(
        operating
        == [
            {
                "type": "operating-system",
                "name": "alpine",
                "version": "3.24.3",
                "bom-ref": "os:alpine",
            }
        ],
        "SBOM_OS",
    )
    observed = []
    for component in bom["components"]:
        if component.get("type") == "operating-system":
            continue
        pair = (component.get("name"), component.get("version"))
        require(component.get("type") == "library" and pair in expected, "SBOM_PACKAGE")
        row = expected[pair]
        require(
            component.get("purl") == row["purl"] and component.get("bom-ref") == row["purl"],
            "SBOM_PURL",
        )
        properties = component.get("properties")
        require(
            type(properties) is list
            and len(properties) == 3
            and all(type(prop) is dict and set(prop) == {"name", "value"} for prop in properties),
            "SBOM_SOURCE_SCHEMA",
        )
        wanted = {
            "aquasecurity:trivy:SrcName": row["source_name"],
            "aquasecurity:trivy:SrcVersion": row["source_version"],
            "aquasecurity:trivy:Arch": row["architecture"],
        }
        require({prop["name"]: prop["value"] for prop in properties} == wanted, "SBOM_SOURCE")
        observed.append(pair)
    require(Counter(observed) == Counter(expected.keys()), "SBOM_COVERAGE")
    return [expected[pair] for pair in sorted(expected)]


def capture_publication(reader):
    directory_chain(PUBLICATION_ROOT)
    info = PUBLICATION_ROOT.lstat()
    require(
        {"device": info.st_dev, "inode": info.st_ino} == PUBLICATION_IDENTITY,
        "PUBLICATION_DIRECTORY",
    )
    raw, records = {}, {}
    for name, pin in PUBLICATION_FILES.items():
        path = PUBLICATION_ROOT / name
        raw[name] = reader.read_exact(path, 1024**2, pin)
        records[name] = reader.hash_file(path, 1024**2)
        require(
            records[name]["sha256"] == pin and digest(raw[name]) == pin,
            "PUBLICATION_BYTES",
        )
    marker, plan, receipt = (
        json_object(raw[name]) for name in (".owner.json", "plan.json", "receipt.json")
    )
    require(
        marker.get("owner") == receipt.get("owner") == plan.get("owner") == PUBLICATION_OWNER
        and marker.get("directory") == receipt.get("directory") == PUBLICATION_IDENTITY
        and marker.get("path") == plan.get("run_root") == str(PUBLICATION_ROOT)
        and marker.get("plan_sha256") == receipt.get("plan_sha256") == PUBLICATION_PLAN_SHA
        and digest(canonical(plan)) == PUBLICATION_PLAN_SHA
        and plan.get("sources") == PUBLICATION_SOURCE_PINS
        and receipt.get("artifacts_published") is True
        and all(
            receipt.get(key) is False
            for key in ("binary_accepted", "sca_executed", "runtime_executed")
        ),
        "PUBLICATION_CHAIN",
    )
    outputs = {
        name: {"sha256": digest(value), "bytes": len(value)}
        for name, value in raw.items()
        if name not in {".owner.json", "plan.json", "receipt.json"}
    }
    require(plan.get("outputs") == receipt.get("outputs") == outputs, "PUBLICATION_OUTPUTS")
    inventory, bom = json_object(raw["inventory.json"]), json_object(raw["os-packages.cdx.json"])
    packages = validate_inputs(inventory, bom)
    after = PUBLICATION_ROOT.lstat()
    require((info.st_dev, info.st_ino) == (after.st_dev, after.st_ino), "PUBLICATION_CHANGED")
    return {
        "publication": {
            "path": str(PUBLICATION_ROOT),
            "directory": PUBLICATION_IDENTITY,
            "files": records,
        },
        "packages": packages,
        "sbom_raw": raw["os-packages.cdx.json"],
    }


def validate_report(report, packages):
    require(
        type(report) is dict
        and report.get("SchemaVersion") == 2
        and type(report.get("Metadata")) is dict
        and type(report["Metadata"].get("OS")) is dict
        and report["Metadata"]["OS"].get("Family") == "alpine"
        and report["Metadata"]["OS"].get("Name") == "3.24.3",
        "REPORT_OS",
    )
    expected = {(row["name"], row["version"]): row for row in packages}
    require(len(expected) == len(packages) == 23, "REPORT_EXPECTED_SCOPE")
    require(type(report.get("Results")) is list and len(report["Results"]) == 1, "REPORT_RESULTS")
    result = report["Results"][0]
    require(
        type(result) is dict
        and result.get("Class") == "os-pkgs"
        and result.get("Type") == "alpine",
        "REPORT_ORIGIN",
    )
    require(
        not result.get("ModifiedFindings") and not result.get("ExperimentalModifiedFindings"),
        "SUPPRESSED_FINDINGS",
    )
    require(type(result.get("Packages")) is list, "REPORT_PACKAGES")
    observed, identifiers = [], {}
    for package in result["Packages"]:
        require(
            type(package) is dict and type(package.get("Identifier")) is dict,
            "REPORT_PACKAGE_SCHEMA",
        )
        pair = (package.get("Name"), package.get("Version"))
        require(pair in expected, "REPORT_PACKAGE")
        row = expected[pair]
        require(
            package.get("Arch") == row["architecture"]
            and package.get("SrcName") == row["source_name"]
            and package.get("SrcVersion") == row["source_version"]
            and package.get("Identifier", {}).get("PURL") == row["purl"],
            "REPORT_PACKAGE_SOURCE_PURL",
        )
        package_id = package.get("ID")
        require(
            type(package_id) is str and package_id and package_id not in identifiers,
            "REPORT_PACKAGE_ID",
        )
        identifiers[package_id] = pair
        observed.append(pair)
    require(Counter(observed) == Counter(expected.keys()), "REPORT_COVERAGE")
    findings = result.get("Vulnerabilities", [])
    require(type(findings) is list, "FINDINGS_SCHEMA")
    for finding in findings:
        require(type(finding) is dict, "FINDING_SCHEMA")
        pair = (finding.get("PkgName"), finding.get("InstalledVersion"))
        require(
            pair in expected
            and identifiers.get(finding.get("PkgID")) == pair
            and type(finding.get("PkgIdentifier")) is dict
            and finding["PkgIdentifier"].get("PURL") == expected[pair]["purl"]
            and type(finding.get("DataSource")) is dict
            and finding["DataSource"].get("ID") == "alpine"
            and type(finding.get("VulnerabilityID")) is str
            and finding["VulnerabilityID"]
            and finding.get("Severity") in SEVERITIES,
            "FINDING_ORIGIN_OR_SEVERITY",
        )
    counts = Counter(row["Severity"] for row in findings)
    return {
        "packages": len(observed),
        "findings": len(findings),
        "by_severity": {level: counts[level] for level in SEVERITIES},
        "os_policy_passed": counts["HIGH"] + counts["CRITICAL"] + counts["UNKNOWN"] == 0,
    }


def verify_sources(reader, plan):
    reader.read_exact(Path(__file__), 128 * 1024, plan["source_sha256"])
    reader.read_exact(ROOT / JOB_PATH, 65536, JOB_SHA)
    reader.read_exact(ROOT / READER_PATH, 128 * 1024, READER_SHA)


def prepare():
    reader = load_reader()
    model = capture_publication(reader)
    source = reader.read_exact(Path(__file__), 128 * 1024)
    reader.read_exact(ROOT / JOB_PATH, 65536, JOB_SHA)
    plan = {
        "schema_version": 1,
        "kind": "valkey-os-23-sca-plan",
        "source_sha256": digest(source),
        "reader_sha256": READER_SHA,
        "job_sha256": JOB_SHA,
        "owner": OWNER,
        "run_root": str(RUN_ROOT),
        "publication": model["publication"],
        "sbom_sha256": digest(model["sbom_raw"]),
        "expected_os": {"family": "alpine", "version": "3.24.3"},
        "packages": model["packages"],
        "snapshot": capture_snapshot(reader),
        "trivy_version": "0.76.2",
        "bounds": {
            "seconds": 180,
            "processes": 8,
            "memory_bytes": 2 * 1024**3,
            "combined_output_bytes": MAX_OUTPUT,
        },
        "policy_blocking_severities": ["HIGH", "CRITICAL", "UNKNOWN"],
        "binary_accepted": False,
        "runtime_accepted": False,
        "gaps": [
            "Esta unidad sólo cubre los 23 APK declarados del sistema base.",
            "Los binarios Valkey y los materiales embebidos requieren cobertura separada.",
            "No acredita UID efectivo, TLS, instrucciones de CPU ni funcionamiento del servicio.",
        ],
    }
    return plan, model


def scanner_arguments(run_root):
    return [
        "sbom",
        str(run_root / "os-packages.cdx.json"),
        "--cache-dir",
        str(CACHE),
        "--cache-backend",
        "memory",
        "--config",
        "",
        "--ignorefile",
        "",
        "--offline-scan",
        "--skip-db-update",
        "--skip-java-db-update",
        "--disable-telemetry",
        "--no-progress",
        "--scanners",
        "vuln",
        "--pkg-types",
        "os",
        "--list-all-pkgs",
        "--severity",
        ",".join(SEVERITIES),
        "--format",
        "json",
    ]


def run_scan(reader, job, plan, model, run_root):
    verify_sources(reader, plan)
    require(capture_snapshot(reader) == plan["snapshot"], "SNAPSHOT_PRECHECK")
    require(capture_publication(reader) == model, "PUBLICATION_PRECHECK")
    require(
        plan["publication"] == model["publication"]
        and plan["sbom_sha256"] == digest(model["sbom_raw"]),
        "INPUT_PLAN_PIN",
    )
    directory_chain(run_root.parent)
    os.mkdir(run_root, 0o700)
    info = run_root.lstat()
    identity = {"device": info.st_dev, "inode": info.st_ino}
    attempt = {}
    try:
        write_new(
            run_root / ".owner.json",
            canonical(
                {
                    "owner": OWNER,
                    "path": str(run_root),
                    "directory": identity,
                    "plan_sha256": digest(canonical(plan)),
                }
            ),
        )
        write_new(run_root / "plan.json", canonical(plan))
        write_new(run_root / "os-packages.cdx.json", model["sbom_raw"])
        require(capture_snapshot(reader) == plan["snapshot"], "SNAPSHOT_BEFORE_SCAN")
        try:
            result = job.run_job(
                TRIVY,
                scanner_arguments(run_root),
                cwd=run_root,
                env=scanner_environment(),
                timeout=180,
                limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
                output_limit=MAX_OUTPUT,
            )
            attempt = {
                "status": result.status,
                "exit_code": result.exit_code,
                "cleanup_complete": result.cleanup_complete,
                "elapsed_seconds": result.elapsed_seconds,
                "captured_output_complete": result.status == "completed"
                and result.cleanup_complete,
            }
            report_file = write_new(run_root / "os-packages.trivy.json", result.stdout)
            log_file = write_new(run_root / "os-packages.stderr.log", result.stderr)
        finally:
            require(capture_snapshot(reader) == plan["snapshot"], "SNAPSHOT_AFTER_SCAN")
            require(capture_publication(reader) == model, "PUBLICATION_AFTER_SCAN")
            verify_sources(reader, plan)
        reader.validate_process(result)
        require(len(result.stdout) + len(result.stderr) <= MAX_OUTPUT, "COMBINED_OUTPUT_LIMIT")
        coverage = validate_report(json_object(result.stdout), model["packages"])
        # Un WARN conocido sólo se clasifica después de acreditar origen/cobertura/PURL.
        warnings = validate_stderr(result.stderr)
        require(
            (run_root.lstat().st_dev, run_root.lstat().st_ino) == (info.st_dev, info.st_ino),
            "RUN_DIRECTORY_CHANGED",
        )
        receipt = {
            "schema_version": 1,
            "owner": OWNER,
            "directory": identity,
            "source_sha256": plan["source_sha256"],
            "plan_sha256": digest(canonical(plan)),
            "publication": plan["publication"],
            "sbom_sha256": plan["sbom_sha256"],
            "analysis_complete": True,
            "os_coverage_complete": True,
            "os_policy_passed": coverage["os_policy_passed"],
            "coverage": coverage,
            "report": report_file,
            "stderr": log_file,
            "warnings": warnings,
            "process": attempt,
            "snapshot": plan["snapshot"],
            "snapshot_verified_before_after": True,
            "publication_verified_before_after": True,
            "binary_accepted": False,
            "runtime_accepted": False,
            "binary_sca_verified": False,
            "embedded_libraries_complete": False,
            "whole_image_sca_approved": False,
            "cache_permissions_changed": False,
        }
        write_new(run_root / "receipt.json", canonical(receipt))
        return receipt
    except BaseException:
        write_new(
            run_root / "failure.json",
            canonical(
                {
                    "analysis_complete": False,
                    "os_coverage_complete": False,
                    "os_policy_passed": False,
                    "binary_accepted": False,
                    "runtime_accepted": False,
                    "error": "SCA_INCOMPLETE",
                    "attempt": attempt,
                    "source_sha256": plan["source_sha256"],
                    "plan_sha256": digest(canonical(plan)),
                }
            ),
        )
        raise


def load_job(reader):
    return reader.load_exact("valkey_os_native_job_fixed", ROOT / JOB_PATH, JOB_SHA)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    if args.scan:
        require(
            os.name == "nt"
            and all(
                re.fullmatch(r"[0-9a-f]{64}", value or "")
                for value in (args.expected_source_sha256, args.expected_plan_sha256)
            ),
            "EXPLICIT_SCAN_PINS",
        )
    plan, model = prepare()
    if not args.scan:
        print(
            json.dumps(
                {"plan": plan, "plan_sha256": digest(canonical(plan)), "scan_executed": False},
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    require(
        plan["source_sha256"] == args.expected_source_sha256
        and digest(canonical(plan)) == args.expected_plan_sha256,
        "EXTERNAL_SCAN_PINS",
    )
    reader = load_reader()
    receipt = run_scan(reader, load_job(reader), plan, model, RUN_ROOT)
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0 if receipt["os_policy_passed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "analysis_complete": False,
                    "binary_accepted": False,
                    "runtime_accepted": False,
                    "error": "SCA_REJECTED",
                }
            )
        )
        raise SystemExit(1) from None
