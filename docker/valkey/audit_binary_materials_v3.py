"""Auditoría fresca Valkey; plan sin red y namespace V3 exclusivo."""

import argparse
import ast
import copy
import hashlib
import http.client
import json
import os
import queue
import re
import socket
import ssl
import stat
import threading
import time
import types
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
OWNER = "agencia-digital-marketing-ai:valkey-binary-sca:v3"
RUN_ROOT = ROOT / ".tools/evidence/phase4-valkey-binary-sca-v3"
V2_PATH = ROOT / "docker/valkey/audit_binary_materials_v2.py"
V2_SHA = "dff05453d15919a401cec24acd86f56d161bf564999337ecea600972149534b1"
READER_PATH = "docker/kitsu-zou/recover_opencv_sdk_scan_v2.py"
READER_SHA = "b15f8ed20e19b0b1b19dfdd5ffd81ea60667175a014ab63409b29012fa6f253c"
CORE_SHA = "2ef9c1735fa908a0099815f6c0e36446e7188271a3f4b5dbde00c18ebd25c442"
MANIFEST_SHA = "f06af3fd6e4e12299bebeb9caceb9f17516e7fafa341647aaefc1d3123ec5632"
TLS_CA_PATH = ".tools/evidence/phase4-valkey-binary-sca-tls-ca-v1.pem"
TLS_CA_SHA = "ace948cba698b9fa4b428ce170aa0f2ba35782c408acfd6be1b7d24f8439572d"
SCOPE_PATH = "docker/valkey/scan_os_packages.py"
SCOPE_SHA = "f0577bf6e174c27872d5fb5bf63e9eabad59771f2eb2af686a3749a2f8544617"
DECLARATION_OWNER = "agencia-digital-marketing-ai:valkey-binary-sca:v1"
DECLARED_BOUNDS = {
    "total_seconds": 300,
    "request_seconds": 15,
    "responses": 40,
    "pages_per_query": 4,
    "response_bytes": 2097152,
    "total_response_bytes": 33554432,
    "nvd_spacing_seconds": 6.2,
}
BOUNDS = {**DECLARED_BOUNDS, "pages_per_query": 5}
EXPORTS = {
    "require",
    "digest",
    "canonical",
    "json_object",
    "directory_chain",
    "load_reader",
    "write_new",
    "check_request",
    "https_transport",
    "pagination_url",
    "collect",
    "nvd_pages",
}
OUTPUT_NAMES = {"materials.json", "source-files.json", "license-catalog.json", "query-plan.json"}


def require(value, code):
    if not value:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def load_helpers():
    for path in (*reversed(V2_PATH.parents), V2_PATH.parent):
        info = path.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "SOURCE_PARENT_LINK",
        )
    before = V2_PATH.lstat()
    require(
        stat.S_ISREG(before.st_mode)
        and before.st_nlink == 1
        and not getattr(before, "st_file_attributes", 0) & 0x400,
        "SOURCE_FILE",
    )
    with V2_PATH.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(128 * 1024 + 1)
        after = V2_PATH.lstat()

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    require(
        identity(before) == identity(opened) == identity(after)
        and before.st_ctime_ns == after.st_ctime_ns
        and digest(raw) == V2_SHA,
        "V2_SOURCE_PIN",
    )
    tree = ast.parse(raw)
    nodes = [
        n
        for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in EXPORTS)
        or (isinstance(n, ast.ClassDef) and n.name == "TransportFailure")
    ]
    require({n.name for n in nodes} == EXPORTS | {"TransportFailure"}, "AST_EXPORTS")
    value = types.ModuleType("valkey_v3_fixed_helpers")
    value.__dict__.update(
        {
            "copy": copy,
            "hashlib": hashlib,
            "http": http,
            "json": json,
            "os": os,
            "queue": queue,
            "re": re,
            "socket": socket,
            "ssl": ssl,
            "stat": stat,
            "threading": threading,
            "time": time,
            "types": types,
            "Path": Path,
            "parse_qs": parse_qs,
            "parse_qsl": parse_qsl,
            "urlencode": urlencode,
            "urlsplit": urlsplit,
            "urlunsplit": urlunsplit,
            "ROOT": ROOT,
            "READER_PATH": READER_PATH,
            "READER_SHA": READER_SHA,
            "CORE_SHA": CORE_SHA,
        }
    )
    value.BOUNDS = dict(BOUNDS)
    assignments = {
        n.targets[0].id: n.value
        for n in tree.body
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
    }
    require(ast.literal_eval(assignments["BOUNDS"]) == DECLARED_BOUNDS, "DECLARATION_BOUNDS")
    value.INPUTS = ast.literal_eval(assignments["INPUTS"])
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), "<colector-V2-fijado>", "exec"),
        vars(value),
    )
    value.C = value.load_reader().load_exact(
        "valkey_v3_fixed_core", ROOT / "docker/valkey/binary_materials.py", CORE_SHA
    )
    return value


def diagnostic(error):
    cause = error.__context__
    if not isinstance(cause, (OSError, ValueError, http.client.HTTPException, queue.Empty)):
        cause = error
    operation = error.args[0] if error.args else ""
    transport_error = hasattr(error, "status") and hasattr(error, "partial")
    if operation in {"DNS_FAILED", "DNS_DEADLINE"}:
        phase = "dns"
    elif isinstance(cause, ssl.SSLError):
        phase = "tls"
    elif not transport_error:
        phase = "unknown"
    elif error.status is not None:
        phase = "http_body"
    else:
        phase = "pre_headers"
    value = {"error_type": type(cause).__name__, "phase": phase}
    if isinstance(cause, ssl.SSLCertVerificationError):
        code = getattr(cause, "verify_code", None)
        if type(code) is int:
            value["verify_code"] = code
    return value


def retryable(request, error):
    # Sólo cortes transitorios anteriores a cualquier respuesta; nunca se reintenta POST.
    if request["method"] != "GET" or error.status is not None or error.partial:
        return False
    cause = error.__context__
    if isinstance(cause, ssl.SSLCertVerificationError):
        return False
    return error.args == ("DNS_DEADLINE",) or isinstance(
        cause,
        (
            TimeoutError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            ssl.SSLEOFError,
        ),
    )


class AttemptTransport:
    """Presupuesto físico: también cuentan los intentos fallidos y sus parciales."""

    def __init__(self, helpers, transport, store, *, clock=time.monotonic, sleep=time.sleep):
        self.helpers, self.transport, self.store = helpers, transport, store
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.attempts, self.bytes, self.last_nvd = 0, 0, None

    def __call__(self, request, *, timeout, maximum):
        self.helpers.check_request(request)
        require(0 < timeout <= 15 and 0 < maximum <= BOUNDS["response_bytes"], "ATTEMPT_LIMITS")
        for index in range(2):
            require(self.attempts < BOUNDS["responses"], "HTTP_COUNT_LIMIT")
            require(self.bytes < BOUNDS["total_response_bytes"], "HTTP_BYTES_LIMIT")
            now = self.clock()
            require(now - self.started < BOUNDS["total_seconds"], "AUDIT_DEADLINE")
            if request["kind"].startswith("nvd-") and self.last_nvd is not None:
                wait = max(0, self.last_nvd + BOUNDS["nvd_spacing_seconds"] - now)
                require(now + wait - self.started < BOUNDS["total_seconds"], "AUDIT_DEADLINE")
                if wait:
                    self.sleep(wait)
            now = self.clock()
            remaining = BOUNDS["total_seconds"] - (now - self.started)
            require(remaining > 0, "AUDIT_DEADLINE")
            if request["kind"].startswith("nvd-"):
                self.last_nvd = now
            self.attempts += 1
            error = None
            try:
                status, raw = self.transport(
                    request, timeout=min(timeout, remaining), maximum=maximum
                )
            except self.helpers.TransportFailure as failure:
                status, raw, error = failure.status, failure.partial, failure
            except BaseException as failure:
                status, raw, error = None, b"", failure
            require(type(raw) is bytes and len(raw) <= maximum, "ATTEMPT_RESPONSE_LIMIT")
            self.bytes += len(raw)
            self.store(
                {
                    "number": self.attempts,
                    "logical_attempt": index + 1,
                    "request": copy.deepcopy(request),
                    "status": status,
                    "raw": raw,
                    "captured_complete": error is None,
                    "observed_at_utc": datetime.now(UTC).isoformat(),
                    "diagnostic": diagnostic(error) if error else None,
                }
            )
            require(self.bytes <= BOUNDS["total_response_bytes"], "HTTP_BYTES_LIMIT")
            require(self.clock() - self.started < BOUNDS["total_seconds"], "AUDIT_DEADLINE")
            if error is None:
                return status, raw
            if (
                index
                or not isinstance(error, self.helpers.TransportFailure)
                or not retryable(request, error)
            ):
                raise error
        raise AssertionError("Intentos agotados sin resultado")


def publish(
    plan,
    model,
    helpers,
    transport=None,
    *,
    root=None,
    current_plan=None,
    clock=time.monotonic,
    sleep=time.sleep,
):
    root = RUN_ROOT if root is None else root
    helpers.directory_chain(root.parent)
    os.mkdir(root, 0o700)
    identity = (root.lstat().st_dev, root.lstat().st_ino)
    attempts, responses, cursor = [], [], 0

    def write(name, raw):
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", name) and name not in {".", ".."},
            "OUTPUT_NAME",
        )
        return helpers.write_new(root / name, raw)

    def store_attempt(row):
        row = dict(row)
        body = write(f"attempt-{row['number']:02d}.body", row.pop("raw"))
        row["response"] = body
        write(f"attempt-{row['number']:02d}.json", canonical(row))
        attempts.append(row)

    run = AttemptTransport(
        helpers, transport or helpers.https_transport, store_attempt, clock=clock, sleep=sleep
    )

    def store_response(number, request, status, raw, complete):
        nonlocal cursor
        body = write(f"response-{number:02d}.json", raw)
        row = {
            "request": request,
            "status": status,
            "response": body,
            "captured_complete": complete,
            "fresh_network_attempts": [r["number"] for r in attempts[cursor:]],
            "reused_response": False,
        }
        cursor = len(attempts)
        write(f"response-{number:02d}-request.json", canonical(row))
        responses.append(row)

    try:
        write(
            "owner.json",
            canonical(
                {
                    "owner": OWNER,
                    "path": str(root),
                    "directory": {"device": identity[0], "inode": identity[1]},
                    "plan_sha256": digest(canonical(plan)),
                }
            ),
        )
        write("plan.json", canonical(plan))
        require(set(model["outputs"]) == set(plan["outputs"]) == OUTPUT_NAMES, "OUTPUT_NAMES")
        for name, raw in model["outputs"].items():
            require(
                plan["outputs"][name] == {"sha256": digest(raw), "bytes": len(raw)}, "OUTPUT_PLAN"
            )
            write(name, raw)
        result = helpers.collect(model, run, store_response, clock=clock, sleep=sleep)
        require(run.clock() - run.started < BOUNDS["total_seconds"], "AUDIT_DEADLINE")
        if current_plan is None:

            def current_plan():
                return prepare_data()[0]

        require(current_plan() == plan, "INPUTS_CHANGED_DURING_AUDIT")
        require((root.lstat().st_dev, root.lstat().st_ino) == identity, "RUN_DIRECTORY_CHANGED")
        findings = write("findings.json", canonical(result))
        summary = {
            key: value
            for key, value in result["assessment"].items()
            if key not in {"gaps", "advisory_groups"}
        }
        summary["coverage_gaps"] = len(result["assessment"]["gaps"])
        receipt = {
            "schema_version": 1,
            "owner": OWNER,
            "source_sha256": plan["source_sha256"],
            "plan_sha256": digest(canonical(plan)),
            "audit_complete": True,
            "fresh_collection": True,
            "reused_responses": 0,
            "http_attempts": run.attempts,
            "response_bytes_including_partials": run.bytes,
            "logical_responses": len(responses),
            "attempts": attempts,
            "responses": responses,
            "findings": findings,
            "assessment": summary,
            "binary_accepted": False,
            "runtime_accepted": False,
            "release_accepted": False,
        }
        write("receipt.json", canonical(receipt))
        return receipt
    except BaseException as error:
        write(
            "failure.json",
            canonical(
                {
                    "owner": OWNER,
                    "plan_sha256": digest(canonical(plan)),
                    "audit_complete": False,
                    "diagnostic": diagnostic(error)
                    if isinstance(error, helpers.TransportFailure)
                    else {"error_type": type(error).__name__, "phase": "collection"},
                    "attempts": attempts,
                    "responses": responses,
                    "fresh_collection": True,
                    "reused_responses": 0,
                    "binary_accepted": False,
                    "runtime_accepted": False,
                    "release_accepted": False,
                }
            ),
        )
        raise


def prepare_data():
    """Reconstruye materiales estáticos; no llama prepare/main/publicación históricos."""
    h = load_helpers()
    reader, core = h.load_reader(), h.C
    tls_ca = reader.read_exact(ROOT / TLS_CA_PATH, 8192, TLS_CA_SHA)
    scope = reader.load_exact("valkey_v3_fixed_scope", ROOT / SCOPE_PATH, SCOPE_SHA)
    publication = scope.capture_publication(reader)
    records, raw_inputs = {}, {}
    for key, (relative, pin) in h.INPUTS.items():
        raw = reader.read_exact(ROOT / relative, 8 * 1024**2, pin)
        raw_inputs[key] = raw
        records[key] = {"path": relative, "sha256": pin, "bytes": len(raw)}
    manifest_raw = reader.read_exact(
        Path(__file__).with_name("binary-sca-inputs.json"), 128 * 1024, MANIFEST_SHA
    )
    declaration = core.json_object(manifest_raw)
    require(
        declaration.get("schema_version") == 1
        and declaration.get("owner") == DECLARATION_OWNER
        and declaration.get("bounds") == DECLARED_BOUNDS
        and declaration.get("exceptions") == [],
        "MANIFEST_POLICY",
    )
    require(
        (declaration["source_archive"]["path"], declaration["source_archive"]["sha256"])
        == h.INPUTS["archive"]
        and (declaration["provenance"]["path"], declaration["provenance"]["sha256"])
        == h.INPUTS["provenance"],
        "MANIFEST_INPUTS",
    )
    receipt = core.json_object(raw_inputs["os_receipt"])
    coverage = scope.validate_report(
        core.json_object(raw_inputs["os_report"]), publication["packages"]
    )
    require(
        all(
            receipt.get(k) is True
            for k in ("analysis_complete", "os_coverage_complete", "os_policy_passed")
        )
        and coverage["packages"] == 23
        and coverage["findings"] == 0,
        "OS_SCOPE_PROOF",
    )
    source = core.catalogue_source(raw_inputs["archive"], declaration["source_archive"])
    provenance = core.json_object(raw_inputs["provenance"])
    supplier = core.verify_provenance(provenance, source)
    material = core.materials(
        source, core.json_object(raw_inputs["inventory"]), declaration["components"]
    )
    components = material["components"]
    require(len(components) == len({r["id"] for r in components}) == 14, "COMPONENT_COUNTS")
    for row in components:
        require(
            row["version_declared"] is None or row["version_evidence"],
            "DECLARED_VERSION_UNWITNESSED",
        )
        if row["id"] == "valkey":
            require(row["commit_declared"] == source["commit_declared"], "VALKEY_COMMIT_BINDING")
        elif row["id"] == "hdr-histogram":
            require(
                row["commit_declared"].encode() in source["files"]["deps/README.md"]["data"],
                "HDR_COMMIT_BINDING",
            )
        elif row["id"] == "libbacktrace":
            require(
                any(
                    d["display_title"] == "Dockerfile"
                    and row["commit_declared"] in d["content"]
                    and "USE_LIBBACKTRACE=yes" in d["content"]
                    for d in provenance["documents"]
                ),
                "BACKTRACE_COMMIT_BINDING",
            )
        else:
            require(row["commit_declared"] is None, "UNREVIEWED_COMMIT")
    notices = core.license_catalogue(source)
    for notice in notices:
        notice["component_scopes"] = [
            {"component": r["id"], "scope": r["scope"]}
            for r in components
            if notice["path"] in r["source_files"]
        ]
        if notice["path"] == "COPYING":
            notice["component_scopes"].append({"component": "valkey", "scope": "project-notice"})
        notice["source_only_unassigned"] = not bool(notice["component_scopes"])
    queries = core.query_plan(components)
    outputs = {
        "materials.json": canonical(
            {
                **material,
                "supplier_documents": supplier,
                "os_scope": {
                    "receipt_sha256": h.INPUTS["os_receipt"][1],
                    "report_sha256": h.INPUTS["os_report"][1],
                },
            }
        ),
        "source-files.json": canonical(
            {
                name: {k: v for k, v in row.items() if k != "data"}
                for name, row in source["files"].items()
            }
        ),
        "license-catalog.json": canonical(notices),
        "query-plan.json": canonical(queries),
    }
    plan = {
        "schema_version": 1,
        "owner": OWNER,
        "run_root": str(RUN_ROOT),
        "source_sha256": digest(reader.read_exact(Path(__file__), 128 * 1024)),
        "v2_source_sha256": V2_SHA,
        "core_sha256": CORE_SHA,
        "reader_sha256": READER_SHA,
        "publication_scope_sha256": SCOPE_SHA,
        "manifest_sha256": MANIFEST_SHA,
        "declaration_owner": DECLARATION_OWNER,
        "declared_bounds": DECLARED_BOUNDS,
        "bounds": BOUNDS,
        "retry": {
            "additional_get_attempts": 1,
            "post": False,
            "certificate_trust": False,
            "framing": False,
            "http_status": False,
        },
        "external_supervisor": {
            "seconds": 390,
            "processes": 8,
            "memory_bytes": 2147483648,
            "output_limit": 1048576,
            "job_sha256": "dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44",
            "tls_ca": {"path": TLS_CA_PATH, "sha256": TLS_CA_SHA, "bytes": len(tls_ca)},
            "cert_required": True,
            "check_hostname": True,
            "partial_chain": False,
        },
        "fresh_collection": True,
        "historical_responses_read": False,
        "reused_responses": 0,
        "helper_ast_exports": sorted(EXPORTS | {"TransportFailure"}),
        "inputs": records,
        "publication": publication["publication"],
        "source_tree_sha256": source["tree_sha256"],
        "counts": {
            "source_files": len(source["files"]),
            "components": len(components),
            "runtime_components": sum(r["scope"] == "runtime" for r in components),
            "notice_files": len(notices),
            "initial_queries": len(queries),
        },
        "outputs": {
            name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in outputs.items()
        },
        "queries": queries,
        "audit_executed": False,
        "binary_accepted": False,
        "runtime_accepted": False,
        "release_accepted": False,
    }
    return plan, {"components": components, "queries": queries, "outputs": outputs}, h


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    if args.audit:
        require(
            all(
                re.fullmatch(r"[a-f0-9]{64}", value or "")
                for value in (args.expected_source_sha256, args.expected_plan_sha256)
            ),
            "ROOT_AUDIT_PINS",
        )
    plan, model, helpers = prepare_data()
    if not args.audit:
        print(
            json.dumps(
                {"plan": plan, "plan_sha256": digest(canonical(plan)), "audit_executed": False},
                sort_keys=True,
                indent=2,
            )
        )
        return 0
    require(
        plan["source_sha256"] == args.expected_source_sha256
        and digest(canonical(plan)) == args.expected_plan_sha256,
        "EXTERNAL_AUDIT_PINS",
    )
    result = publish(plan, model, helpers)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["assessment"]["known_findings_policy_passed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            json.dumps(
                {
                    "audit_complete": False,
                    "binary_accepted": False,
                    "runtime_accepted": False,
                    "release_accepted": False,
                    "error": "BINARY_AUDIT_REJECTED",
                    "error_type": type(error).__name__,
                }
            )
        )
        raise SystemExit(1) from None
