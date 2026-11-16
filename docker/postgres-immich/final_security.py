"""Auditoría como datos: OCI, inventario Cargo y cobertura; nunca aprueba un release."""

import ast
import base64
import hashlib
import json
import re
import tomllib
import types
from collections import Counter

PURE_SHA = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"
PROFILE_SHA = "af3aee69455287b71f6d7387c1d95456f6d046455bdb39d199d41677f05c324c"
WORK = "/work/.security-rebuild-v7"
ANNEXES = {"postgresql", "pgvector", "vectorchord", "sqlite", "mimalloc"}
LEVELS = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
KNOWN_WARNINGS = {
    "Third-party SBOM may lead to inaccurate vulnerability detection",
    "Recommend using Trivy to generate SBOMs",
    "Using severities from other vendors for some vulnerabilities. Read https://trivy.dev/docs/v0.76/guide/scanner/vulnerability#severity-selection for details.",
}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_object(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "JSON_DUPLICATE")
            value[key] = item
        return value

    def invalid(_value):
        raise ValueError("JSON_NONFINITE")

    value = json.loads(
        raw.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=invalid
    )
    require(type(value) is dict, "JSON_OBJECT")
    return value


def load_oci_reader(raw):
    require(digest(raw) == PURE_SHA, "OCI_READER_PIN")
    functions = {
        "canonical",
        "digest",
        "part",
        "elf_info",
        "file_entry",
        "virtual_path",
        "follow",
        "abi_closure",
        "json_object",
        "safe_name",
        "apply_layer",
        "layer_bytes",
        "apk_inventory",
        "read_oci",
        "project",
        "validate_base",
        "parents",
    }
    constants = {"BASE_MANIFEST", "BASE_CONFIG", "LICENSES", "LIBRARIES", "MAX_ARCHIVE", "MAX_FILE"}
    selected = []
    for node in ast.parse(raw).body:
        if isinstance(node, ast.Import) and all(
            n.name in {"hashlib", "json", "posixpath", "struct"} for n in node.names
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            selected.append(node)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in constants
        ):
            selected.append(node)
    require(
        {n.name for n in selected if isinstance(n, ast.FunctionDef)} == functions,
        "OCI_FUNCTION_SET",
    )
    require(
        {n.targets[0].id for n in selected if isinstance(n, ast.Assign)} == constants,
        "OCI_CONSTANT_SET",
    )
    module = types.ModuleType("pg_final_oci_data_only")
    exec(
        compile(ast.Module(body=selected, type_ignores=[]), "<oci-9e0-cerrado>", "exec"),
        module.__dict__,
    )
    return module


def positive(value, stage, owner, profile=None):
    row = value.get("receipt", {})
    require(
        set(value) == {"receipt", "receipt_sha256"}
        and digest(canonical(row)) == value["receipt_sha256"],
        "RECEIPT_HASH",
    )
    require(
        row.get("success") is True and row.get("stage") == stage and row.get("owner") == owner,
        "RECEIPT_ROLE",
    )
    if profile is not None:
        require(row.get("profile_sha256") == profile, "RECEIPT_PROFILE")
    return row


def cargo_inventory(vendor, lock_raw, old_profile, graph):
    packages = tomllib.loads(lock_raw.decode("utf-8"))["package"]
    available, local = {}, set()
    for row in packages:
        key = row["name"], row["version"]
        require(key not in available and key not in local, "LOCK_DUPLICATE")
        if "source" in row:
            require(
                row["source"] == "registry+https://github.com/rust-lang/crates.io-index",
                "NON_REGISTRY_SOURCE",
            )
            available[key] = row["checksum"]
        else:
            local.add(key)
    crates = vendor["crates"]
    entries = {(row["name"], row["version"]): row["archive_sha256"] for row in crates}
    require(
        len(crates) == len(entries) == len(available) == 244 and available == entries,
        "VENDOR_LOCK_BIJECTION",
    )
    require(vendor["cargo_lock_sha256"] == digest(lock_raw), "LOCK_HASH")
    require(
        ("anyhow", "1.0.103") in entries
        and ("crossbeam-epoch", "0.9.20") in entries
        and ("anyhow", "1.0.101") not in entries
        and ("crossbeam-epoch", "0.9.18") not in entries,
        "SECURITY_VERSIONS",
    )
    text = json_object(base64.b64decode(old_profile["feature_wrapper_b64"], validate=True))[
        "features"
    ]
    text = text.replace("(/work/source)", "(" + WORK + "/source)").replace(
        "(/work/source/", "(" + WORK + "/source/"
    )
    text = text.replace("anyhow v1.0.101|", "anyhow v1.0.103|").replace(
        "crossbeam-epoch v0.9.18|", "crossbeam-epoch v0.9.20|"
    )
    features = {}
    for line in text.rstrip("\n").splitlines():
        match = re.fullmatch(
            r"([A-Za-z0-9_-]+) v([^ |]+)((?: \([^()\r\n]+\))*)\|([A-Za-z0-9_,+.-]*)(?: \(\*\))?",
            line,
        )
        require(match is not None and match.group(1, 2) in entries.keys() | local, "GRAPH_PACKAGE")
        for annotation in re.findall(r"\(([^()]+)\)", match[3]):
            require(
                annotation in {"proc-macro", "*"}
                or (annotation.startswith(WORK + "/source") and ".." not in annotation.split("/")),
                "GRAPH_SOURCE",
            )
        features.setdefault(match.group(1, 2), set()).update(filter(None, match[4].split(",")))
    encoded = {name + "@" + version: sorted(value) for (name, version), value in features.items()}
    receipt = positive(
        graph,
        "graph",
        "agencia-digital-marketing-ai:postgres-immich-security-rebuild:v7",
        PROFILE_SHA,
    )
    require(
        receipt["artifact"]["expected_graph_match"] is True
        and receipt["artifact"]["features_sha256"] == digest(canonical(encoded)),
        "GRAPH_EFFECTIVE_HASH",
    )
    selected = set(features) & entries.keys()
    local_selected = set(features) - entries.keys()
    require(
        len(selected) == receipt["artifact"]["selected_registry"] == 165
        and len(local_selected) == 13
        and len(entries.keys() - selected) == 79,
        "GRAPH_COUNTS",
    )
    for key, mandatory in [
        (("vchord", "0.0.0"), {"default", "pg16"}),
        (("simd", "0.0.0"), {"init"}),
        (("pgrx", "0.17.0"), {"cshim", "pg16"}),
    ]:
        require(mandatory <= features.get(key, set()), "GRAPH_REQUIRED_FEATURES")
    return {
        "counts": {
            "registry": 244,
            "selected_registry": 165,
            "unselected_registry": 79,
            "selected_local": 13,
        },
        "registry": [
            {
                "name": n,
                "version": v,
                "archive_sha256": entries[n, v],
                "selected": (n, v) in selected,
                "features": sorted(features.get((n, v), set())),
            }
            for n, v in sorted(entries)
        ],
        "local": [
            {"name": n, "version": v, "features": sorted(features[n, v])}
            for n, v in sorted(local_selected)
        ],
        "features_sha256": digest(canonical(encoded)),
    }


def cargo_sbom(inventory):
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "components": [
            {
                "type": "library",
                "name": row["name"],
                "version": row["version"],
                "bom-ref": "pkg:cargo/" + row["name"] + "@" + row["version"],
                "purl": "pkg:cargo/" + row["name"] + "@" + row["version"],
                "hashes": [{"alg": "SHA-256", "content": row["archive_sha256"]}],
            }
            for row in inventory["registry"]
        ],
    }


def sdk_gates(gates):
    owner = "agencia-digital-marketing-ai:postgres-immich-security-rebuild:v7"
    restart = positive(gates["v7_restart"], "restart", owner, PROFILE_SHA)
    catalog = positive(gates["v7_catalog"], "catalog", owner, PROFILE_SHA)
    stopped = positive(gates["v7_stop"], "stop", owner, PROFILE_SHA)
    require(
        stopped["cleanup"]["stopped"] is True
        and stopped["container"]["running"] is False
        and stopped["container"]["pid"] == 0,
        "SDK_STOP_PROOF",
    )
    identity = ("cid", "created", "started_at", "image_id")
    require(
        all(row["container"].get("no_foreign") is True for row in (restart, catalog, stopped)),
        "SDK_NO_FOREIGN",
    )
    require(
        all(
            all(row["container"][key] == restart["container"][key] for key in identity)
            for row in (catalog, stopped)
        ),
        "SDK_EPOCH",
    )
    require(
        restart["container"]["running"] is True
        and catalog["container"]["running"] is True
        and restart["container"]["pid"] == catalog["container"]["pid"] > 0,
        "SDK_ACTIVE_EPOCH",
    )
    return catalog


def image_inventory(reader, archive, subject, prepared, built, gates):
    owner = subject["runtime_owner"]
    before = positive(prepared, "prepare", owner)
    final = positive(built, "build", owner, before["profile_sha256"])
    require(
        final["prepare_receipt_sha256"] == prepared["receipt_sha256"]
        and final["image_id"] == subject["image_id"]
        and final["config_digest"] == subject["config_digest"]
        and final["archive"]
        == {"sha256": subject["archive"]["sha256"], "size": subject["archive"]["size"]},
        "IMAGE_RECEIPT_BINDING",
    )
    catalog = sdk_gates(gates)
    require(
        before["catalog_artifact_sha256"] == digest(canonical(catalog["artifact"])),
        "CATALOG_BINDING",
    )
    image = reader.read_oci(archive, subject["image_id"][7:])
    require(image["config_digest"] == subject["config_digest"], "OCI_CONFIG_SUBJECT")
    config, files = image["config"]["config"], image["files"]
    require(
        config["User"] == "70:70"
        and config["Entrypoint"] == ["docker-entrypoint.sh"]
        and config["Cmd"] == ["postgres"]
        and config["Labels"]["cl.e-scale.agencia.owner"] == owner
        and "PG_VERSION=16.15" in config["Env"],
        "OCI_RUNTIME_IDENTITY",
    )
    require(len(before["projected"]) == 66, "RUNTIME_FILE_COUNT")
    for name, expected in before["projected"].items():
        row = files[name]
        require(
            {key: row[key] for key in ("sha256", "size")} == expected
            and row["uid"] == row["gid"] == 0
            and row["mode"] == 0o444,
            "RUNTIME_FILE_BINDING",
        )
    packages = reader.apk_inventory(files["lib/apk/db/installed"]["data"])
    require(
        len(packages) == 45 and packages == before["base"]["packages"] == final["packages"],
        "OS_PACKAGE_BIJECTION",
    )
    native_paths = {
        "postgresql": "usr/local/bin/postgres",
        "pgvector": "usr/local/lib/postgresql/vector.so",
        "vectorchord": "usr/local/lib/postgresql/vchord.so",
        "sqlite": "usr/local/lib/postgresql/libsqlite3.so.0",
    }
    native = {}
    for name, path in native_paths.items():
        row = files[path]
        require(row["kind"] == "file" and row["elf"] is not None, "NATIVE_ELF_ABSENT")
        native[name] = {
            "path": path,
            "sha256": row["sha256"],
            "size": row["size"],
            "elf": row["elf"],
            "assessment": "pending",
        }
    require(
        native["sqlite"]["sha256"]
        == "78711a51904d2ce47d4c16cb53365e85d73bc15cd4aa6d10c4d2ef3754d81c53"
        and native["pgvector"]["sha256"]
        == "050bd2625e0efcf8f1b5bebd32135b1958a6f9b98e2c73d13137152488451d3b",
        "REUSED_LIBRARIES",
    )
    native["mimalloc"] = {
        "embedded_subject": native["vectorchord"]["sha256"],
        "assessment": "pending",
        "retention_or_version_not_inferred_from_rust_wrapper": True,
    }
    return {
        "subject": subject,
        "os_packages": packages,
        "os_version": files["etc/alpine-release"]["data"].decode().strip(),
        "projected": before["projected"],
        "native_components": native,
        "sqlite_cli_paths": sorted(
            n for n in files if n.rsplit("/", 1)[-1] in {"sqlite", "sqlite3"}
        ),
        "runtime_files": 66,
        "image_executed": False,
        "sdk_executed": False,
    }


def report_coverage(report, kind, expected):
    require(type(expected) is set and expected, "EMPTY_EXPECTED_COVERAGE")
    packages, findings = [], []
    for result in report.get("Results", []):
        if result.get("Type") != kind:
            require(
                not result.get("Packages") and not result.get("Vulnerabilities"),
                "UNACCOUNTED_RESULT",
            )
            continue
        packages.extend((row["Name"], row["Version"]) for row in result.get("Packages", []))
        findings.extend(result.get("Vulnerabilities", []))
    require(
        len(packages) == len(set(packages)) == len(expected) and set(packages) == expected,
        "REPORT_BIJECTION",
    )
    require(
        all(
            type(row.get("VulnerabilityID")) is str
            and row["VulnerabilityID"]
            and (row.get("PkgName"), row.get("InstalledVersion")) in expected
            for row in findings
        ),
        "FINDING_PACKAGE_SCOPE",
    )
    require(
        all(type(row.get("Severity", "UNKNOWN")) is str for row in findings),
        "FINDING_SEVERITY_TYPE",
    )
    levels = Counter(row.get("Severity", "UNKNOWN").upper() for row in findings)
    require(set(levels) <= set(LEVELS), "UNKNOWN_SEVERITY_VALUE")
    return {
        "packages": len(packages),
        "findings": findings,
        "high_critical": levels["HIGH"] + levels["CRITICAL"],
        "unknown": levels["UNKNOWN"],
        "counts": dict(levels),
    }


def osv_coverage(bundle, expected):
    require(type(expected) is set and expected, "OSV_EXPECTED_PACKAGES")
    require(
        bundle.get("ecosystem") == "crates.io"
        and bundle.get("queries") == len(expected)
        and bundle.get("omitted") == 0
        and bundle.get("pagination_exhausted") is True,
        "OSV_COVERAGE",
    )
    rows = bundle.get("results", [])
    require(
        len(rows) == len(expected) and {(r["name"], r["version"]) for r in rows} == expected,
        "OSV_PACKAGE_BIJECTION",
    )
    findings = []
    for row in rows:
        pages, seen = row.get("pages", []), set()
        require(row.get("pagination_exhausted") is True and 1 <= len(pages) <= 8, "OSV_PAGES")
        for index, page in enumerate(pages):
            require(
                type(page) is dict and set(page) <= {"vulns", "next_page_token"}, "OSV_PAGE_SCHEMA"
            )
            token = page.get("next_page_token")
            require((token is None) == (index == len(pages) - 1), "OSV_PAGE_EXHAUSTION")
            if token is not None:
                require(
                    type(token) is str and token and token not in seen and len(token) <= 4096,
                    "OSV_PAGE_TOKEN",
                )
                seen.add(token)
            require(type(page.get("vulns", [])) is list, "OSV_VULNERABILITIES_ARRAY")
            for advisory in page.get("vulns", []):
                require(
                    type(advisory) is dict and type(advisory.get("id")) is str and advisory["id"],
                    "OSV_ADVISORY_SCHEMA",
                )
                affected = advisory.get("affected")
                require(
                    type(affected) is list
                    and affected
                    and all(type(row) is dict for row in affected),
                    "OSV_AFFECTED_ARRAY",
                )
                require(
                    any(
                        item.get("package", {}).get("ecosystem") == "crates.io"
                        and item["package"].get("name") == row["name"]
                        for item in affected
                    ),
                    "OSV_ADVISORY_PACKAGE",
                )
                severity = advisory.get("database_specific", {}).get("severity", "UNKNOWN")
                severity = severity.upper() if type(severity) is str else "UNKNOWN"
                if severity not in LEVELS:
                    severity = "UNKNOWN"
                findings.append(
                    {
                        "name": row["name"],
                        "version": row["version"],
                        "id": advisory["id"],
                        "severity": severity,
                        "raw": advisory,
                        "assessment": "pending_root_review",
                    }
                )
    levels = Counter(row["severity"] for row in findings)
    return {
        "queries": len(rows),
        "findings": findings,
        "high_critical": levels["HIGH"] + levels["CRITICAL"],
        "unknown": levels["UNKNOWN"],
        "coverage_scope": "Conjunto exacto suministrado; se conservan todos los alias y avisos originales.",
    }


def scan_warnings(raw):
    warnings = []
    for line in raw.decode("utf-8", "strict").splitlines():
        require("ERROR" not in line and "FATAL" not in line, "SCANNER_ERROR")
        if "\tWARN\t" in line:
            message = line.split("\tWARN\t", 1)[1]
            require(message in KNOWN_WARNINGS, "UNRECOGNIZED_WARNING")
            warnings.append(message)
        elif re.search(r"\bWARN\b", line):
            raise ValueError("UNRECOGNIZED_WARNING_FORMAT")
    return warnings


def assessment(os_report, cargo_report, osv, annexes):
    require(set(annexes) <= ANNEXES, "ANNEX_SET")
    native_findings = []
    for row in annexes.values():
        require(
            type(row.get("raw_findings")) is list
            and type(row.get("unresolved_advisories")) is list
            and type(row.get("unresolved_high_critical")) is int
            and row["unresolved_high_critical"] >= 0,
            "ANNEX_ASSESSMENT_SCHEMA",
        )
        require(
            all(
                type(finding) is dict
                and type(finding.get("id")) is str
                and finding["id"]
                and finding.get("severity", "UNKNOWN") in LEVELS
                for finding in row["raw_findings"]
            ),
            "ANNEX_FINDING_SCHEMA",
        )
        native_findings.extend(row["raw_findings"])
    levels = Counter(row.get("severity", "UNKNOWN") for row in native_findings)
    pending = sorted(
        ANNEXES
        - {key for key, row in annexes.items() if row.get("assessment") == "evaluated_by_root"}
    )
    return {
        "approved": False,
        "release_approved": False,
        "assessment": "pending_root_review",
        "coverage_complete": not pending and osv is not None,
        "pending_annexes": pending,
        "osv_pending": osv is None,
        "raw_high_critical": sum(
            row.get("high_critical", 0) for row in (os_report, cargo_report, osv or {})
        )
        + levels["HIGH"]
        + levels["CRITICAL"],
        "raw_unknown": sum(row.get("unknown", 0) for row in (os_report, cargo_report, osv or {}))
        + levels["UNKNOWN"],
        "classifications_required": True,
        "limit": "Cero hallazgos en OS/Cargo no acredita los componentes nativos ni aprueba la publicación.",
    }
