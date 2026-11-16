"""Plan y SCA condicional del JavaScript embebido; sin aprobar el runtime."""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import stat
import types
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ".tools/evidence/phase4-kitsu-swagger-embedded-js-inventory-v1.json"
INVENTORY_SHA = "6a09feefa77e8e252307a3fe3e75f65ca4ebe96c9b8a58ed754a15eb5371fda6"
READER_PATH = "docker/kitsu-zou/recover_opencv_sdk_scan_v2.py"
READER_SHA = "b15f8ed20e19b0b1b19dfdd5ffd81ea60667175a014ab63409b29012fa6f253c"
JOB_PATH = "sre/windows_build_job.py"
JOB_SHA = "dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44"
TRIVY = ROOT / ".tools/bin/trivy.exe"
TRIVY_SHA = "dd07adf881a72b54d1a922191d62fa939938df2155be85a727da93b67b24e617"
CACHE = ROOT / ".tools/trivy-cache-current-20261106"
CACHE_OWNER = "agencia-digital-marketing-ai:trivy-current-db:v2"
DB_SHA = "7520aa80bfb8db4a230514baa510ec6b1d7de9099b74f7879be65450407a6f1f"
METADATA_SHA = "475ea2061a7f343953b880fd0eb2ce56253cca495d48f12661f3bd3e5e95c125"
RUN_ROOT = ROOT / ".tools/evidence/phase4-kitsu-swagger-embedded-js-sca-v1"
OWNER = "agencia-digital-marketing-ai:kitsu-swagger-embedded-js-sca:v1"
VCS_REFERENCE = (
    "git+ssh://git@github.com/cgwire/fabricjs-psbrush.git#17a0cf4e853ae21809176236f5db1428667887b7"
)
MAX_OUTPUT = 8 * 1024**2
LANES = ("kitsu_candidates", "flasgger_self_declared")
SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
KNOWN_WARNINGS = {
    "Third-party SBOM may lead to inaccurate vulnerability detection": "sbom-externo-identidades-cotejadas",
    "Recommend using Trivy to generate SBOMs": "sbom-externo-cobertura-cotejada",
    "Using severities from other vendors for some vulnerabilities. Read https://trivy.dev/docs/v0.76/guide/scanner/vulnerability#severity-selection for details.": "severidad-proveedor-conservada",
}
INPUTS = {
    "flasgger": (
        ".tools/kitsu-zou-build-flasgger-v2/wheels/flasgger-0.9.7.1-py2.py3-none-any.whl",
        "ee71b3e30ed15c59274d72a27277ae258378a7f28dc50ac17a4526d343c4618f",
    ),
    "flasgger_sdist": (
        ".tools/cache/phase4-sources/flasgger-0.9.7.1.tar.gz",
        "ca098e10bfbb12f047acc6299cc70a33851943a746e550d86e65e60d4df245fb",
    ),
    "kitsu": (
        ".tools/cache/phase4-sources/kitsu-1.0.77.tgz",
        "f030dc59ae28155d3686c09ce35a2855381582ab42fcc983158c727ec5f1204e",
    ),
    "kitsu_lock": (
        ".tools/cache/phase4-sources/kitsu-c1edc91a3e2bfcef-package-lock.json",
        "5e8e258946c3944cd0768afafeb8a5ea3dd7dec6024591ed7334f12e55076583",
    ),
    "kitsu_package": (
        ".tools/cache/phase4-sources/kitsu-c1edc91a3e2bfcef-package.json",
        "88d73c7be2350ae1cf3f32db37aefbfe3180e96cea2551a71542546b891478b1",
    ),
}


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


def npm_purl(name, version):
    token = r"[a-z0-9~][a-z0-9._~-]*"
    require(type(name) is str and re.fullmatch(r"(?:@" + token + r"/)?" + token, name), "NPM_NAME")
    require(
        type(version) is str
        and re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
            version,
        ),
        "NPM_VERSION",
    )
    return "pkg:npm/" + quote(name, safe="/") + "@" + quote(version, safe=".-")


def build_analysis(inventory, lock, members):
    artifacts = {row["artifact"]: row for row in inventory["inventories"]}
    require(
        len(artifacts) == len(inventory["inventories"]) == 2
        and set(artifacts) == {"kitsu", "flasgger"},
        "ARTIFACT_SET",
    )
    require(inventory["sca_approved"] is False, "INVENTORY_SCOPE")
    lanes = {name: {"name": name, "components": [], "witnesses": []} for name in LANES}
    components, ambiguous, vcs = {}, [], []
    for package in artifacts["kitsu"]["packages"]:
        name = package["name"]
        require(
            package["version"] is None
            and package["version_status"] == "pending"
            and package["sources"],
            "CANDIDATE_STATUS",
        )
        require(package["lock_candidates"], "CANDIDATE_MISSING")
        versions = set()
        for row in package["lock_candidates"]:
            version = row["version"]
            purl = npm_purl(name, version)
            require(row["bundle_bytes_verified"] is False, "CANDIDATE_EQUIVALENCE")
            path = row["lock_path"]
            require(
                type(path) is str
                and "node_modules/" in path
                and path.rsplit("node_modules/", 1)[1] == name,
                "LOCK_PACKAGE_NAME",
            )
            actual = lock["packages"].get(path)
            require(
                type(actual) is dict
                and all(actual.get(key) == row[key] for key in ("version", "resolved", "integrity"))
                and actual.get("license") == row["license_declared_in_lock"],
                "LOCK_ORIGIN",
            )
            evidence_kind = "lock-candidate"
            if row["resolved"] == VCS_REFERENCE:
                require(
                    name == "fabricjs-psbrush"
                    and version == "0.0.22"
                    and path == "node_modules/fabricjs-psbrush"
                    and row["integrity"] is None,
                    "VCS_REFERENCE",
                )
                # Sólo datos del lock: jamás resolver VCS ni afirmar equivalencia npm.
                evidence_kind = "lock-vcs-candidate"
                vcs.append(
                    {
                        "name": name,
                        "version": version,
                        "resolved": VCS_REFERENCE,
                        "npm_identity_confirmed": False,
                        "git_execution_allowed": False,
                    }
                )
            else:
                url = urlsplit(row["resolved"])
                require(
                    url.scheme == "https"
                    and url.netloc == "registry.npmjs.org"
                    and not url.query
                    and not url.fragment
                    and "\\" not in url.path
                    and "/../" not in url.path
                    and url.path.endswith(".tgz"),
                    "LOCK_REGISTRY",
                )
                require(
                    type(row["integrity"]) is str and row["integrity"].startswith("sha512-"),
                    "LOCK_INTEGRITY",
                )
                require(
                    len(base64.b64decode(row["integrity"][7:], validate=True)) == 64,
                    "LOCK_INTEGRITY",
                )
            components[(name, version)] = {
                "name": name,
                "version": version,
                "purl": purl,
                "evidence_kind": evidence_kind,
            }
            lanes[LANES[0]]["witnesses"].append({"name": name, **row})
            versions.add(version)
        if len(versions) > 1:
            ambiguous.append(name)
    lanes[LANES[0]]["components"] = [components[key] for key in sorted(components)]
    declared = {}
    for row in artifacts["flasgger"]["version_evidence"]:
        raw = members.get(row["member"])
        require(type(raw) is bytes and digest(raw) == row["sha256"], "DECLARED_MEMBER_SHA")
        needle, offset = row["needle"].encode("utf-8"), row["offset"]
        require(
            type(offset) is int and offset >= 0 and raw[offset : offset + len(needle)] == needle,
            "DECLARED_OFFSET",
        )
        require(
            row["classification"] == "self-declared; not registry archive equivalence",
            "DECLARED_CLASSIFICATION",
        )
        name, version = row["name"], row["version"]
        key = (name, version)
        require(key not in declared, "DECLARED_DUPLICATE")
        declared[key] = {
            "name": name,
            "version": version,
            "purl": npm_purl(name, version),
            "evidence_kind": "self-declared",
        }
        lanes[LANES[1]]["witnesses"].append(row)
    require(
        set(declared) == {("jquery", "1.8.0"), ("jquery", "2.2.4"), ("backbone", "1.1.2")},
        "DECLARED_SET",
    )
    lanes[LANES[1]]["components"] = [declared[key] for key in sorted(declared)]
    unresolved = {}
    for name, artifact in artifacts.items():
        require(
            artifact["coverage"]["component_version_coverage_complete"] is False, "VERSION_SCOPE"
        )
        unresolved[name] = [
            row["name"] for row in artifact["packages"] if not row["lock_candidates"]
        ]
    return {
        "lanes": lanes,
        "ambiguous_packages": sorted(ambiguous),
        "vcs_candidates": vcs,
        "unresolved_versions": unresolved,
        "gaps": {name: row["coverage"] for name, row in artifacts.items()},
        "limitations": inventory["limitations"],
        "component_version_coverage_complete": False,
        "runtime_sca_approved": False,
    }


def make_bom(lane):
    components = []
    for row in lane["components"]:
        group, name = (
            row["name"].split("/", 1) if row["name"].startswith("@") else ("", row["name"])
        )
        components.append(
            {
                "type": "library",
                "name": name,
                "group": group,
                "version": row["version"],
                "purl": row["purl"],
                "bom-ref": row["purl"],
                "properties": [
                    {"name": "agency:evidence-kind", "value": row["evidence_kind"]},
                    {"name": "agency:inventory-sha256", "value": INVENTORY_SHA},
                    {"name": "agency:runtime-identity-confirmed", "value": "false"},
                ],
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "tools": {
                "components": [
                    {"type": "application", "name": "agency-embedded-js-hypotheses", "version": "1"}
                ]
            }
        },
        "components": components,
    }


def validate_report(report, components):
    require(type(report) is dict and type(report.get("Metadata", {})) is dict, "REPORT_SCHEMA")
    require(
        report.get("SchemaVersion") == 2 and not report.get("Metadata", {}).get("OS"),
        "REPORT_SCHEMA_OR_OS",
    )
    expected = {(row["name"], row["version"]): row["purl"] for row in components}
    require(len(expected) == len(components), "EXPECTED_DUPLICATE")
    require(type(report.get("Results")) is list, "RESULTS_REQUIRED")
    observed, findings = [], []
    for result in report["Results"]:
        require(
            result.get("Class") == "lang-pkgs" and result.get("Type") == "node-pkg", "NPM_ORIGIN"
        )
        require(
            not result.get("ModifiedFindings") and not result.get("ExperimentalModifiedFindings"),
            "SUPPRESSED_FINDINGS",
        )
        require(type(result.get("Packages")) is list, "PACKAGES_REQUIRED")
        for row in result["Packages"]:
            pair = (row["Name"], row["Version"])
            require(
                pair in expected and row.get("Identifier", {}).get("PURL") == expected[pair],
                "PACKAGE_PURL",
            )
            observed.append(pair)
        vulnerabilities = result.get("Vulnerabilities", [])
        require(type(vulnerabilities) is list, "FINDINGS_SCHEMA")
        for row in vulnerabilities:
            require(type(row) is dict, "FINDING_SCHEMA")
            require(
                (row.get("PkgName"), row.get("InstalledVersion")) in expected
                and type(row.get("VulnerabilityID")) is str
                and row["VulnerabilityID"]
                and row.get("Severity") in SEVERITIES,
                "FINDING_ORIGIN_OR_SEVERITY",
            )
            findings.append(row)
    require(Counter(observed) == Counter(expected.keys()), "PACKAGE_COVERAGE")
    counts = Counter(row["Severity"] for row in findings)
    return {
        "packages": len(observed),
        "findings": len(findings),
        "by_severity": {level: counts[level] for level in SEVERITIES},
        "high_critical": counts["HIGH"] + counts["CRITICAL"],
        "unknown_severity": counts["UNKNOWN"],
    }


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


def prepare():
    reader = load_reader()
    raw = reader.read_exact(ROOT / INVENTORY, 3 * 1024**2, INVENTORY_SHA)
    inventory = json_object(raw)
    inputs, records = {}, {}
    for key, (relative, pin) in INPUTS.items():
        row = inventory["inputs"][key] if key in inventory["inputs"] else inventory[key]
        require(row["path"] == relative and row["sha256"] == pin, "INPUT_REFERENCE")
        inputs[key] = reader.read_exact(ROOT / relative, 32 * 1024**2, pin)
        records[key] = {"path": relative, "sha256": pin, "bytes": len(inputs[key])}
    members = {}
    with zipfile.ZipFile(io.BytesIO(inputs["flasgger"])) as archive:
        declarations = next(
            row for row in inventory["inventories"] if row["artifact"] == "flasgger"
        )["version_evidence"]
        for row in declarations:
            require(archive.namelist().count(row["member"]) == 1, "DECLARED_ARCHIVE_DUPLICATE")
            info = archive.getinfo(row["member"])
            require(
                not info.is_dir() and info.file_size <= 2 * 1024**2 and not info.flag_bits & 1,
                "DECLARED_ARCHIVE_MEMBER",
            )
            members[row["member"]] = archive.read(info)
    model = build_analysis(inventory, json_object(inputs["kitsu_lock"]), members)
    require(
        len(model["lanes"][LANES[0]]["components"]) == 136
        and len(model["lanes"][LANES[0]]["witnesses"]) == 137
        and len(model["unresolved_versions"]["flasgger"]) == 93,
        "FROZEN_SCOPE",
    )
    reader.read_exact(ROOT / JOB_PATH, 65536, JOB_SHA)
    source = reader.read_exact(Path(__file__), 128 * 1024)
    plan = {
        "schema_version": 1,
        "source_sha256": digest(source),
        "inventory_sha256": INVENTORY_SHA,
        "reader_sha256": READER_SHA,
        "job_sha256": JOB_SHA,
        "trivy_version": "0.76.2",
        "run_root": str(RUN_ROOT),
        "owner": OWNER,
        "inputs": records,
        "snapshot": capture_snapshot(reader),
        "boms": {
            name: {
                "sha256": digest(canonical(make_bom(model["lanes"][name]))),
                "components": len(model["lanes"][name]["components"]),
            }
            for name in LANES
        },
        "bounds_each_scan": {
            "seconds": 180,
            "processes": 8,
            "memory_bytes": 2 * 1024**3,
            "combined_output_bytes": MAX_OUTPUT,
        },
        "gaps": model["gaps"],
        "unresolved_versions": model["unresolved_versions"],
        "vcs_candidates": model["vcs_candidates"],
        "component_version_coverage_complete": False,
        "runtime_sca_approved": False,
    }
    return plan, model


def scanner_arguments(lane, run_root):
    require(lane in LANES, "SCAN_LANE")
    return [
        "sbom",
        str(run_root / (lane + ".cdx.json")),
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
        "library",
        "--list-all-pkgs",
        "--severity",
        ",".join(SEVERITIES),
        "--format",
        "json",
    ]


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


def run_scans(reader, job, plan, analysis, run_root):
    require(capture_snapshot(reader) == plan["snapshot"], "SNAPSHOT_PRECHECK")
    require(
        set(plan["boms"]) == set(LANES)
        and all(
            plan["boms"][lane]
            == {
                "sha256": digest(canonical(make_bom(analysis["lanes"][lane]))),
                "components": len(analysis["lanes"][lane]["components"]),
            }
            for lane in LANES
        ),
        "BOM_PLAN_PIN",
    )
    directory_chain(run_root.parent)
    os.mkdir(run_root, 0o700)  # EXCL: no adopción, sobrescritura ni migración.
    info = run_root.lstat()
    identity = {"device": info.st_dev, "inode": info.st_ino}
    completed, attempts = {}, {}
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
        write_new(run_root / "analysis.json", canonical(analysis))
        for lane in LANES:
            components = analysis["lanes"][lane]["components"]
            bom_raw = canonical(make_bom(analysis["lanes"][lane]))
            require(digest(bom_raw) == plan["boms"][lane]["sha256"], "BOM_PLAN_PIN")
            write_new(run_root / (lane + ".cdx.json"), bom_raw)
            require(capture_snapshot(reader) == plan["snapshot"], "SNAPSHOT_BEFORE_SCAN")
            try:
                result = job.run_job(
                    TRIVY,
                    scanner_arguments(lane, run_root),
                    cwd=run_root,
                    env=scanner_environment(),
                    timeout=180,
                    limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
                    output_limit=MAX_OUTPUT,
                )
                attempts[lane] = {
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "cleanup_complete": result.cleanup_complete,
                    "elapsed_seconds": result.elapsed_seconds,
                }
                report_file = write_new(run_root / (lane + ".trivy.json"), result.stdout)
                log_file = write_new(run_root / (lane + ".stderr.log"), result.stderr)
            finally:
                require(capture_snapshot(reader) == plan["snapshot"], "SNAPSHOT_AFTER_SCAN")
            reader.validate_process(result)
            require(len(result.stdout) + len(result.stderr) <= MAX_OUTPUT, "COMBINED_OUTPUT_LIMIT")
            coverage = validate_report(json_object(result.stdout), components)
            # Sólo después de cotejar cobertura/origen/PURL se clasifican WARN.
            warnings = validate_stderr(result.stderr)
            completed[lane] = {
                "coverage": coverage,
                "warnings": warnings,
                "report": report_file,
                "stderr": log_file,
                "sbom_sha256": digest(bom_raw),
                "status": result.status,
                "cleanup_complete": result.cleanup_complete,
                "elapsed_seconds": result.elapsed_seconds,
                "exit_code": result.exit_code,
                "bounds": {
                    "seconds": 180,
                    "processes": 8,
                    "memory_bytes": 2 * 1024**3,
                    "combined_output_bytes": MAX_OUTPUT,
                },
            }
        receipt = {
            "schema_version": 1,
            "analysis_complete": True,
            "runtime_sca_approved": False,
            "component_version_coverage_complete": False,
            "owner": OWNER,
            "directory": identity,
            "source_sha256": plan["source_sha256"],
            "plan_sha256": digest(canonical(plan)),
            "inventory_sha256": plan["inventory_sha256"],
            "scans": completed,
            "snapshot": plan["snapshot"],
            "snapshot_verified_before_after_each_scan": True,
            "reader_sha256": READER_SHA,
            "job_sha256": JOB_SHA,
            "known_hypotheses_high_critical_or_unknown": sum(
                row["coverage"]["high_critical"] + row["coverage"]["unknown_severity"]
                for row in completed.values()
            ),
            "gaps": analysis["gaps"],
            "unresolved_versions": analysis["unresolved_versions"],
            "vcs_candidates": analysis["vcs_candidates"],
            "sdk_executed": False,
            "cache_permissions_changed": False,
        }
        write_new(run_root / "receipt.json", canonical(receipt))
        return receipt
    except BaseException:
        write_new(
            run_root / "failure.json",
            canonical(
                {
                    "schema_version": 1,
                    "analysis_complete": False,
                    "runtime_sca_approved": False,
                    "error": "SCA_INCOMPLETE",
                    "completed_scans": completed,
                    "attempts": attempts,
                    "source_sha256": plan["source_sha256"],
                    "plan_sha256": digest(canonical(plan)),
                }
            ),
        )
        raise


def load_job(reader):
    return reader.load_exact("embedded_js_native_job_fixed", ROOT / JOB_PATH, JOB_SHA)


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
    plan, analysis = prepare()
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
    receipt = run_scans(reader, load_job(reader), plan, analysis, RUN_ROOT)
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 2 if receipt["known_hypotheses_high_critical_or_unknown"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError):
        print(
            json.dumps(
                {"analysis_complete": False, "runtime_sca_approved": False, "error": "SCA_REJECTED"}
            )
        )
        raise SystemExit(1) from None
