"""Consulta offline de 19 hipótesis pip/vendor; nunca autoriza runtime."""

import argparse
import hashlib
import json
import os
import re
import sys
import types
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = "agencia-digital-marketing-ai:python-pip-vendor-sca:v1"
DESTINATION = ROOT / ".tools/evidence/phase4-python-pip-vendor-sca-v1"
INPUT_ROOT = ROOT / ".tools/backup-source-build/python-pip-vendor-inventory-v1"
CACHE = ROOT / ".tools/trivy-cache-current-20261106"
PINS = dict(
    [
        (
            "docker/kitsu-zou/recover_opencv_sdk_scan_v2.py",
            "b15f8ed20e19b0b1b19dfdd5ffd81ea60667175a014ab63409b29012fa6f253c",
        ),
        (
            "docker/kitsu-zou/audit_opencv_sdk.py",
            "1455c74e1dd547d1fb0068b681f67c0de1208a46bdd6d1a9acd6f060c511c01e",
        ),
        (
            "sre/windows_build_job.py",
            "dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44",
        ),
        (
            "sre/backup_tools_access.py",
            "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b",
        ),
    ]
)
DATA_SHA = "7520aa80bfb8db4a230514baa510ec6b1d7de9099b74f7879be65450407a6f1f"
META_SHA = "475ea2061a7f343953b880fd0eb2ce56253cca495d48f12661f3bd3e5e95c125"
TOOL_SHA = "dd07adf881a72b54d1a922191d62fa939938df2155be85a727da93b67b24e617"
BOM_SHA = "ef383b18fe3fc707961569d0a313f39678d664ed59aedd351a5c08e9653518ac"
RECEIPT_SHA = "4263e0753a5c0d8660ffd7a3caf2754d02da1a6646aa681428f8b9ff70b4fea0"


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def module(relative):
    path = ROOT / relative
    raw = path.read_bytes()
    require(digest(raw) == PINS[relative], "Fuente auxiliar fuera del pin.")
    result = types.ModuleType("pip_sca_" + path.stem)
    result.__file__ = str(path)
    sys.modules[result.__name__] = result
    exec(compile(raw, str(path), "exec"), vars(result))
    return result


def validate_coverage(report, components):
    expected = {(row["name"], row["version"]): row["purl"] for row in components}
    require(len(expected) == len(components), "Hipótesis duplicadas.")
    observed, findings = [], []
    for result in report.get("Results", []):
        require(result.get("Type") == "python-pkg", "Tipo de resultado distinto de Python.")
        for row in result.get("Packages", []):
            pair = row["Name"], row["Version"]
            require(pair in expected, "Cobertura contiene nombre/versión ajenos.")
            require(row.get("Identifier", {}).get("PURL") == expected[pair], "PURL distinto.")
            observed.append(pair)
        for row in result.get("Vulnerabilities", []):
            require(
                (row.get("PkgName"), row.get("InstalledVersion")) in expected
                and isinstance(row.get("VulnerabilityID"), str)
                and bool(row["VulnerabilityID"])
                and row.get("Severity") in {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"},
                "Hallazgo ajeno o sin severidad válida.",
            )
            findings.append(row)
    require(Counter(observed) == Counter(expected.keys()), "Cobertura incompleta o duplicada.")
    counts = Counter(row["Severity"] for row in findings)
    return {
        "hypotheses": len(expected),
        "findings": len(findings),
        "by_severity": dict(counts),
        "high_critical": counts["HIGH"] + counts["CRITICAL"],
        "unknown": counts["UNKNOWN"],
    }


def inputs():
    helper = module("docker/kitsu-zou/recover_opencv_sdk_scan_v2.py")
    access = module("sre/backup_tools_access.py")
    access.check_access(INPUT_ROOT, private=True)
    receipt_raw = helper.read_exact(INPUT_ROOT / "receipt.json", 65536, RECEIPT_SHA)
    receipt = json.loads(receipt_raw)
    marker = json.loads(helper.read_exact(INPUT_ROOT / ".owner.json", 65536))
    require(marker == receipt["directory"], "Marcador no coincide.")
    st = INPUT_ROOT.lstat()
    require(
        marker["directory"] == {"device": st.st_dev, "inode": st.st_ino},
        "Identidad de input cambió.",
    )
    raw = helper.read_exact(INPUT_ROOT / "pip-and-vendors.cdx.json", 65536, BOM_SHA)
    require(
        receipt["outputs"]["pip-and-vendors.cdx.json"]
        == {"bytes": len(raw), "sha256": digest(raw)},
        "Recibo no liga SBOM.",
    )
    bom = json.loads(raw)
    require(
        len(bom["components"]) == 19
        and all(row["purl"].startswith("pkg:pypi/") for row in bom["components"]),
        "Se requieren19 hipótesis PyPI.",
    )
    require(
        helper.read_exact(CACHE / ".owner", 4096)
        == b"agencia-digital-marketing-ai:trivy-current-db:v2",
        "Caché ajena.",
    )
    meta_raw = helper.read_exact(CACHE / "db/metadata.json", 16384, META_SHA)
    module("docker/kitsu-zou/audit_opencv_sdk.py").validate_db(json.loads(meta_raw))
    tool = helper.hash_file(ROOT / ".tools/bin/trivy.exe", 256 * 1024**2)
    database = helper.hash_file(CACHE / "db/trivy.db", 2 * 1024**3)
    require(tool["sha256"] == TOOL_SHA and database["sha256"] == DATA_SHA, "Scanner/DB distintos.")
    plan = {
        "owner": OWNER,
        "root": str(DESTINATION),
        "source_sha256": digest(Path(__file__).read_bytes()),
        "helpers": PINS,
        "receipt_sha256": RECEIPT_SHA,
        "sbom_sha256": BOM_SHA,
        "tool_sha256": TOOL_SHA,
        "db_sha256": DATA_SHA,
        "metadata_sha256": META_SHA,
        "hypotheses": 19,
        "timeout_seconds": 180,
        "processes": 8,
        "memory_bytes": 2 * 1024**3,
        "output_bytes": 1024**2,
        "offline": True,
        "runtime_approved": False,
    }
    return plan, helper, bom, raw, tool, database, meta_raw


def write_new(name, raw):
    fd = os.open(
        DESTINATION / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def execute(expected):
    plan, helper, bom, raw, tool, database, meta_raw = inputs()
    require(digest(canonical(plan)) == expected, "Plan distinto del pin externo.")
    os.mkdir(DESTINATION, 0o700)
    write_new("plan.json", canonical(plan))
    write_new("input.cdx.json", raw)
    job = module("sre/windows_build_job.py")
    environment = {
        key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ
    }
    environment["PATH"] = str(Path(environment["SystemRoot"]) / "System32")
    try:
        result = job.run_job(
            ROOT / ".tools/bin/trivy.exe",
            [
                "sbom",
                str(DESTINATION / "input.cdx.json"),
                "--cache-dir",
                str(CACHE),
                "--skip-db-update",
                "--offline-scan",
                "--list-all-pkgs",
                "--format",
                "json",
                "--output",
                str(DESTINATION / "report.json"),
            ],
            cwd=DESTINATION,
            env=environment,
            timeout=180,
            limits=job.Limits(processes=8, memory_bytes=2 * 1024**3),
            output_limit=1024**2,
        )
        write_new("stdout.log", result.stdout)
        write_new("stderr.log", result.stderr)
        write_new(
            "process.json",
            canonical(
                {
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "cleanup_complete": result.cleanup_complete,
                    "elapsed_seconds": result.elapsed_seconds,
                }
            ),
        )
        helper.validate_process(result)
        after = inputs()
        require(
            after[0] == plan and after[3:] == (raw, tool, database, meta_raw),
            "Inputs, herramienta o DB cambiaron.",
        )
        warnings = []
        for line in (result.stdout + result.stderr).decode("utf-8", "strict").splitlines():
            require(not re.search(r"\b(ERROR|FATAL)\b", line, re.I), "Error del scanner.")
            if re.search(r"\bWARN\b", line, re.I):
                fields = line.split("\t", 2)
                require(
                    len(fields) == 3 and fields[1] == "WARN" and fields[2] in helper.KNOWN_WARNINGS,
                    "Advertencia nueva sin clasificar.",
                )
                warnings.append(fields[2])
        report_raw = helper.read_exact(DESTINATION / "report.json", 16 * 1024**2)
        coverage = validate_coverage(json.loads(report_raw), bom["components"])
        receipt = {
            "success": True,
            "owner": OWNER,
            "plan_sha256": expected,
            "coverage": coverage,
            "report_sha256": digest(report_raw),
            "warnings": warnings,
            "hypothesis_scan_complete": True,
            "identity_complete": False,
            "runtime_approved": False,
            "requires_applicability_review": True,
        }
        write_new("receipt.json", canonical(receipt))
        return receipt
    except Exception as error:
        write_new(
            "failure.json",
            canonical(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "runtime_approved": False,
                    "evidence_preserved": True,
                }
            ),
        )
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    if args.execute:
        result = execute(args.expected_plan_sha256)
    else:
        plan = inputs()[0]
        result = {"plan": plan, "plan_sha256": digest(canonical(plan))}
    print(canonical(result).decode())


if __name__ == "__main__":
    main()
