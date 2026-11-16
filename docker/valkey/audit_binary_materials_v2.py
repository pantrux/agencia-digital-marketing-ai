"""Plan fuente/binario Valkey; consultas HTTPS sólo mediante acción explícita Root."""

import argparse
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
import tarfile
import threading
import time
import types
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = ROOT / ".tools/evidence/phase4-valkey-binary-sca-v2"
OWNER = "agencia-digital-marketing-ai:valkey-binary-sca:v2"
DECLARATION_OWNER = "agencia-digital-marketing-ai:valkey-binary-sca:v1"
MANIFEST_SHA = "f06af3fd6e4e12299bebeb9caceb9f17516e7fafa341647aaefc1d3123ec5632"
READER_PATH = "docker/kitsu-zou/recover_opencv_sdk_scan_v2.py"
READER_SHA = "b15f8ed20e19b0b1b19dfdd5ffd81ea60667175a014ab63409b29012fa6f253c"
CORE_SHA = "2ef9c1735fa908a0099815f6c0e36446e7188271a3f4b5dbde00c18ebd25c442"
SCOPE_PATH = "docker/valkey/scan_os_packages.py"
SCOPE_SHA = "f0577bf6e174c27872d5fb5bf63e9eabad59771f2eb2af686a3749a2f8544617"
INPUTS = {
    "archive": (
        ".tools/cache/phase4-sources/valkey-9.1.3.tar.gz",
        "184e34ed2d9b51879b682b5b15205e2b9d677c60280e3cb453be8f520d04e293",
    ),
    "provenance": (
        ".tools/evidence/phase4-valkey-source-provenance-v1.json",
        "3338a16479d25c905c02f8a0fd84429f8b39b6950c064ff0738d01e421a255ac",
    ),
    "inventory": (
        ".tools/evidence/phase4-valkey-inventory-v2/inventory.json",
        "7ebf54457de5fa7c6413d6d5f7c64fed94517f81894cd3e5da5596efc911ccfd",
    ),
    "os_receipt": (
        ".tools/evidence/phase4-valkey-os-sca-v1/receipt.json",
        "b24e765de94369944f11aa48b02b38c00606e329678d91ce7db9a0e1bdc3304b",
    ),
    "os_report": (
        ".tools/evidence/phase4-valkey-os-sca-v1/os-packages.trivy.json",
        "37ee66d9fe4b73f5f389ea05f6d3a67cb1046ed9555ae98a079108576932d9b1",
    ),
}
BOUNDS = {
    "total_seconds": 300,
    "request_seconds": 15,
    "responses": 40,
    "pages_per_query": 4,
    "response_bytes": 2097152,
    "total_response_bytes": 33554432,
    "nvd_spacing_seconds": 6.2,
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


C = load_reader().load_exact(
    "valkey_binary_core_fixed", Path(__file__).with_name("binary_materials.py"), CORE_SHA
)


def check_request(request):
    parsed = urlsplit(request["url"])
    require(
        parsed.scheme == "https"
        and parsed.port in {None, 443}
        and not parsed.username
        and not parsed.password
        and not parsed.fragment,
        "HTTPS_ENDPOINT",
    )
    kind, path, host = request["kind"], parsed.path, parsed.hostname
    if kind == "github-commit":
        require(
            request["method"] == "GET"
            and host == "api.github.com"
            and not parsed.query
            and re.fullmatch(r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/commits/[a-f0-9]{40}", path),
            "GITHUB_ENDPOINT",
        )
    elif kind == "osv":
        body = request.get("body")
        require(
            request["method"] == "POST"
            and host == "api.osv.dev"
            and path == "/v1/query"
            and not parsed.query
            and type(body) is dict
            and set(body).issubset({"commit", "page_token"})
            and re.fullmatch(r"[a-f0-9]{40}", body.get("commit", "")),
            "OSV_COMMIT_ENDPOINT",
        )
    else:
        require(
            kind in {"nvd-dictionary", "nvd-cves"}
            and request["method"] == "GET"
            and host == "services.nvd.nist.gov"
            and path
            == ("/rest/json/cpes/2.0" if kind == "nvd-dictionary" else "/rest/json/cves/2.0"),
            "NVD_ENDPOINT",
        )
        query = parse_qs(parsed.query, keep_blank_values=True)
        wanted = (
            {"keywordSearch", "keywordExactMatch", "resultsPerPage", "startIndex"}
            if kind == "nvd-dictionary"
            else {"cpeName", "resultsPerPage", "startIndex"}
        )
        require(
            set(query) == wanted
            and all(len(value) == 1 for value in query.values())
            and query["resultsPerPage"] == ["100"]
            and re.fullmatch(r"0|[1-9][0-9]*", query["startIndex"][0]),
            "NVD_PARAMETERS",
        )
        if kind == "nvd-cves":
            require(C.cpe_fields(query["cpeName"][0])[2] == "a", "NVD_APPLICATION_CPE")
    return parsed


class TransportFailure(ValueError):
    def __init__(self, operation, status=None, partial=b""):
        super().__init__(operation)
        self.status = status
        self.partial = partial


def https_transport(request, *, timeout, maximum):
    """DNS sólo resuelve; el hilo nunca puede iniciar una petición tras vencer."""
    parsed = check_request(request)
    deadline = time.monotonic() + timeout
    answer = queue.Queue(maxsize=1)

    def resolve():
        try:
            answer.put((True, socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)))
        except BaseException:
            answer.put((False, None))

    resolver = threading.Thread(target=resolve, daemon=True)
    resolver.start()
    try:
        ok, addresses = answer.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        raise TransportFailure("DNS_DEADLINE") from None
    if not ok or not addresses or time.monotonic() >= deadline:
        raise TransportFailure("DNS_FAILED")
    owned = {"socket": None}

    def close_owned():
        current = owned["socket"]
        if current is not None:
            try:
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            current.close()

    watchdog = threading.Timer(max(0, deadline - time.monotonic()), close_owned)
    watchdog.daemon = True
    watchdog.start()
    connection = None
    chunks, total, status = [], 0, None
    try:
        family, kind, protocol, _, address = addresses[0]
        transport = socket.socket(family, kind, protocol)
        owned["socket"] = transport
        transport.settimeout(max(0.001, deadline - time.monotonic()))
        transport.connect(address)
        secured = ssl.create_default_context().wrap_socket(
            transport, server_hostname=parsed.hostname, do_handshake_on_connect=False
        )
        owned["socket"] = secured
        secured.settimeout(max(0.001, deadline - time.monotonic()))
        secured.do_handshake()
        require(time.monotonic() < deadline, "TLS_DEADLINE")
        connection = http.client.HTTPSConnection(
            parsed.hostname, 443, timeout=max(0.001, deadline - time.monotonic())
        )
        connection.sock = secured
        body = canonical(request["body"]) if "body" in request else None
        headers = {
            "Accept": "application/json",
            "User-Agent": "agency-valkey-source-audit/1",
            "Connection": "close",
        }
        if body is not None:
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection.request(request["method"], target, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        try:
            lengths = response.headers.get_all("Content-Length", [])
            encodings = response.headers.get_all("Transfer-Encoding", [])
            require(
                len(lengths) <= 1 and len(encodings) <= 1 and not (lengths and encodings),
                "HTTP_FRAMING",
            )
            require(
                not lengths or re.fullmatch(r"0|[1-9][0-9]*", lengths[0]), "HTTP_CONTENT_LENGTH"
            )
            require(not encodings or encodings[0].lower() == "chunked", "HTTP_TRANSFER_ENCODING")
            while True:
                require(time.monotonic() < deadline, "HTTP_DEADLINE")
                secured.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = response.read1(min(65536, maximum + 1 - total))
                if not chunk:
                    require(
                        not lengths or total == int(lengths[0]), "HTTP_TRUNCATED_CONTENT_LENGTH"
                    )
                    break
                chunks.append(chunk)
                total += len(chunk)
                require(total <= maximum, "HTTP_RESPONSE_LIMIT")
            require(time.monotonic() < deadline, "HTTP_DEADLINE")
            return response.status, b"".join(chunks)
        finally:
            response.close()
    except http.client.IncompleteRead as error:
        raise TransportFailure(
            "HTTPS_INCOMPLETE", status, (b"".join(chunks) + error.partial)[:maximum]
        ) from None
    except (OSError, ValueError, http.client.HTTPException):
        raise TransportFailure("HTTPS_INCOMPLETE", status, b"".join(chunks)[:maximum]) from None
    finally:
        watchdog.cancel()
        watchdog.join()
        close_owned()
        if connection is not None:
            connection.close()


def pagination_url(url, start):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["startIndex"] = str(start)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def collect(model, transport, store, *, clock=time.monotonic, sleep=time.sleep):
    require(model["queries"] == C.query_plan(model["components"]), "QUERY_PLAN_COVERAGE")
    started, count, size, last_nvd = clock(), 0, 0, None
    findings, gaps, completed, metadata = [], [], [], []
    components = {row["id"]: row for row in model["components"]}
    require(len(components) == len(model["components"]), "COMPONENT_DUPLICATE")

    def ask(request):
        nonlocal count, size, last_nvd
        check_request(request)
        require(count < BOUNDS["responses"], "HTTP_COUNT_LIMIT")
        now = clock()
        require(now - started < BOUNDS["total_seconds"], "AUDIT_DEADLINE")
        if request["kind"].startswith("nvd-") and last_nvd is not None:
            wait = max(0, last_nvd + BOUNDS["nvd_spacing_seconds"] - now)
            require(now + wait - started < BOUNDS["total_seconds"], "AUDIT_DEADLINE")
            if wait:
                sleep(wait)
        now = clock()
        require(now - started < BOUNDS["total_seconds"], "AUDIT_DEADLINE")
        if request["kind"].startswith("nvd-"):
            last_nvd = now
        count += 1
        try:
            status, raw = transport(
                request,
                timeout=min(BOUNDS["request_seconds"], BOUNDS["total_seconds"] - (now - started)),
                maximum=BOUNDS["response_bytes"],
            )
        except TransportFailure as error:
            store(count, request, error.status, error.partial[: BOUNDS["response_bytes"]], False)
            raise
        require(type(raw) is bytes, "HTTP_RESPONSE_TYPE")
        complete = len(raw) <= BOUNDS["response_bytes"]
        store(count, request, status, raw[: BOUNDS["response_bytes"]], complete)
        size += len(raw)
        require(complete and size <= BOUNDS["total_response_bytes"], "HTTP_BYTES_LIMIT")
        require(
            clock() - started < BOUNDS["total_seconds"] and status == 200, "HTTP_STATUS_OR_DEADLINE"
        )
        return C.json_object(raw)

    for component in model["components"]:
        if component["scope"] != "runtime":
            continue
        rows = [row for row in model["queries"] if row["component"] == component["id"]]
        commit_ok = False
        for request in rows:
            if request["kind"] == "github-commit":
                body = ask(request)
                expected = (
                    component["repository"].rstrip("/") + "/commit/" + component["commit_declared"]
                )
                require(
                    body.get("sha") == component["commit_declared"]
                    and body.get("html_url") == expected,
                    "COMMIT_AUTHENTICATION",
                )
                metadata.append(
                    {
                        "component": component["id"],
                        "commit": body["sha"],
                        "endpoint": request["url"],
                    }
                )
                commit_ok = True
                completed.append(request)
            elif request["kind"] == "osv":
                require(commit_ok, "COMMIT_REQUIRED_BEFORE_OSV")
                current, seen, exhausted = copy.deepcopy(request), set(), False
                for _ in range(BOUNDS["pages_per_query"]):
                    body = ask(current)
                    records, token = C.osv_page(body)
                    findings.extend(C.osv_finding(row, component) for row in records)
                    if token is None:
                        exhausted = True
                        break
                    require(token not in seen, "OSV_REPEATED_TOKEN")
                    seen.add(token)
                    current["body"]["page_token"] = token
                require(exhausted, "OSV_PAGINATION_LIMIT")
                completed.append(request)
            else:
                products = nvd_pages(request, "NVD_CPE", "products", ask)
                selected, pending = C.select_cpes(products, component)
                if not products:
                    gaps.append(
                        {
                            "component": component["id"],
                            "reason": "nvd_dictionary_query_returned_no_products",
                        }
                    )
                elif not selected:
                    gaps.append(
                        {
                            "component": component["id"],
                            "reason": "nvd_identity_or_version_unresolved",
                            "candidates": pending,
                        }
                    )
                metadata.append(
                    {
                        "component": component["id"],
                        "nvd_dictionary_products": products,
                        "selected_cpes": selected,
                        "pending_candidates": pending,
                    }
                )
                for cpe in selected:
                    cve_request = {
                        "component": component["id"],
                        "kind": "nvd-cves",
                        "method": "GET",
                        "url": "https://services.nvd.nist.gov/rest/json/cves/2.0?"
                        + urlencode({"cpeName": cpe, "resultsPerPage": 100, "startIndex": 0}),
                    }
                    records = nvd_pages(cve_request, "NVD_CVE", "vulnerabilities", ask)
                    findings.extend(C.nvd_finding(row["cve"], component) for row in records)
                completed.append(request)
        if not component["commit_declared"]:
            gaps.append(
                {
                    "component": component["id"],
                    "reason": "osv_commit_not_authenticated_no_tag_inferred",
                }
            )
    require(set(components) == {row["id"] for row in model["components"]}, "COMPONENT_DUPLICATE")
    return {
        "findings": findings,
        "assessment": C.assess(findings, gaps),
        "completed_queries": completed,
        "metadata": metadata,
        "http_responses": count,
        "response_bytes": size,
        "elapsed_seconds": clock() - started,
        "runtime_accepted": False,
        "binary_accepted": False,
    }


def nvd_pages(request, format_name, key, ask):
    rows, start, total = [], 0, None
    for _ in range(BOUNDS["pages_per_query"]):
        body = ask({**request, "url": pagination_url(request["url"], start)})
        require(body.get("format") == format_name and body.get("version") == "2.0", "NVD_SCHEMA")
        page = body.get(key)
        require(
            type(page) is list
            and all(
                type(body.get(field)) is int and body[field] >= 0
                for field in ("startIndex", "resultsPerPage", "totalResults")
            ),
            "NVD_PAGINATION_SCHEMA",
        )
        require(
            body["startIndex"] == start
            and body["resultsPerPage"] == len(page)
            and start + len(page) <= body["totalResults"],
            "NVD_PAGINATION_RANGE",
        )
        total = body["totalResults"] if total is None else total
        require(total == body["totalResults"], "NVD_TOTAL_CHANGED")
        rows.extend(page)
        start += len(page)
        if start == total:
            return rows
        require(page, "NVD_EMPTY_PAGE")
    raise ValueError("NVD_PAGINATION_LIMIT")


def prepare():
    reader = load_reader()
    scope = reader.load_exact("valkey_binary_publication_scope_fixed", ROOT / SCOPE_PATH, SCOPE_SHA)
    publication = scope.capture_publication(reader)
    inputs, records = {}, {}
    for key, (relative, pin) in INPUTS.items():
        inputs[key] = reader.read_exact(ROOT / relative, 8 * 1024**2, pin)
        records[key] = {"path": relative, "sha256": pin, "bytes": len(inputs[key])}
    manifest_path = Path(__file__).with_name("binary-sca-inputs.json")
    manifest_raw = reader.read_exact(manifest_path, 128 * 1024, MANIFEST_SHA)
    manifest = C.json_object(manifest_raw)
    require(
        manifest.get("schema_version") == 1
        and manifest.get("owner") == DECLARATION_OWNER
        and manifest.get("bounds") == BOUNDS
        and manifest.get("exceptions") == [],
        "MANIFEST_POLICY",
    )
    require(
        (manifest["source_archive"]["path"], manifest["source_archive"]["sha256"])
        == INPUTS["archive"]
        and (manifest["provenance"]["path"], manifest["provenance"]["sha256"])
        == INPUTS["provenance"],
        "MANIFEST_INPUTS",
    )
    os_receipt, os_report = C.json_object(inputs["os_receipt"]), C.json_object(inputs["os_report"])
    coverage = scope.validate_report(os_report, publication["packages"])
    require(
        os_receipt.get("analysis_complete") is True
        and os_receipt.get("os_coverage_complete") is True
        and os_receipt.get("os_policy_passed") is True
        and coverage["packages"] == 23
        and coverage["findings"] == 0,
        "OS_SCOPE_PROOF",
    )
    source = C.catalogue_source(inputs["archive"], manifest["source_archive"])
    provenance = C.json_object(inputs["provenance"])
    provenance_rows = C.verify_provenance(provenance, source)
    inventory = C.json_object(inputs["inventory"])
    material = C.materials(source, inventory, manifest["components"])
    require(
        len(material["components"]) == 14
        and len({row["id"] for row in material["components"]}) == 14,
        "COMPONENT_COUNTS",
    )
    for row in material["components"]:
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
                    document["display_title"] == "Dockerfile"
                    and row["commit_declared"] in document["content"]
                    and "USE_LIBBACKTRACE=yes" in document["content"]
                    for document in provenance["documents"]
                ),
                "BACKTRACE_COMMIT_BINDING",
            )
        else:
            require(row["commit_declared"] is None, "UNREVIEWED_COMMIT")
    source_listing = {
        name: {key: value for key, value in row.items() if key != "data"}
        for name, row in source["files"].items()
    }
    notices = C.license_catalogue(source)
    for notice in notices:
        notice["component_scopes"] = [
            {"component": row["id"], "scope": row["scope"]}
            for row in material["components"]
            if notice["path"] in row["source_files"]
        ]
        if notice["path"] == "COPYING":
            notice["component_scopes"].append({"component": "valkey", "scope": "project-notice"})
        notice["source_only_unassigned"] = not bool(notice["component_scopes"])
    queries = C.query_plan(material["components"])
    outputs = {
        "materials.json": canonical(
            {
                **material,
                "supplier_documents": provenance_rows,
                "os_scope": {
                    "receipt_sha256": INPUTS["os_receipt"][1],
                    "report_sha256": INPUTS["os_report"][1],
                },
            }
        ),
        "source-files.json": canonical(source_listing),
        "license-catalog.json": canonical(notices),
        "query-plan.json": canonical(queries),
    }
    core_path = Path(__file__).with_name("binary_materials.py")
    reader.read_exact(core_path, 128 * 1024, CORE_SHA)
    plan = {
        "schema_version": 1,
        "kind": "valkey-source-binary-audit-plan",
        "owner": OWNER,
        "run_root": str(RUN_ROOT),
        "source_sha256": digest(reader.read_exact(Path(__file__), 128 * 1024)),
        "core_sha256": CORE_SHA,
        "reader_sha256": READER_SHA,
        "publication_scope_sha256": SCOPE_SHA,
        "manifest_sha256": digest(manifest_raw),
        "declaration_owner": DECLARATION_OWNER,
        "inputs": records,
        "publication": publication["publication"],
        "source_tree_sha256": source["tree_sha256"],
        "counts": {
            "members": source["members"],
            "source_files": len(source["files"]),
            "expanded_bytes": source["expanded_bytes"],
            "components": len(material["components"]),
            "runtime_components": sum(row["scope"] == "runtime" for row in material["components"]),
            "notice_files": len(notices),
            "initial_queries": len(queries),
        },
        "outputs": {
            name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in outputs.items()
        },
        "bounds": BOUNDS,
        "queries": queries,
        "binary_accepted": False,
        "runtime_accepted": False,
        "audit_executed": False,
    }
    model = {"components": material["components"], "queries": queries, "outputs": outputs}
    return plan, model


def publish_audit(plan, model, transport=https_transport):
    directory_chain(RUN_ROOT.parent)
    os.mkdir(RUN_ROOT, 0o700)
    info = RUN_ROOT.lstat()
    identity = {"device": info.st_dev, "inode": info.st_ino}
    responses = []
    try:
        require(
            set(model["outputs"])
            == set(plan["outputs"])
            == {"materials.json", "source-files.json", "license-catalog.json", "query-plan.json"},
            "OUTPUT_NAMES",
        )
        write_new(
            RUN_ROOT / ".owner.json",
            canonical(
                {
                    "owner": OWNER,
                    "path": str(RUN_ROOT),
                    "directory": identity,
                    "plan_sha256": digest(canonical(plan)),
                }
            ),
        )
        write_new(RUN_ROOT / "plan.json", canonical(plan))
        for name, raw in model["outputs"].items():
            require(
                name
                in {
                    "materials.json",
                    "source-files.json",
                    "license-catalog.json",
                    "query-plan.json",
                }
                and plan["outputs"][name] == {"sha256": digest(raw), "bytes": len(raw)},
                "OUTPUT_PLAN",
            )
            write_new(RUN_ROOT / name, raw)

        def store(number, request, status, raw, complete):
            prefix = f"response-{number:02d}"
            body = write_new(RUN_ROOT / (prefix + ".json"), raw)
            record = {
                "request": request,
                "status": status,
                "response": body,
                "captured_complete": complete,
            }
            write_new(RUN_ROOT / (prefix + "-request.json"), canonical(record))
            responses.append(record)

        result = collect(model, transport, store)
        again, _ = prepare()
        require(again == plan, "INPUTS_CHANGED_DURING_AUDIT")
        require(
            (RUN_ROOT.lstat().st_dev, RUN_ROOT.lstat().st_ino) == (info.st_dev, info.st_ino),
            "RUN_DIRECTORY_CHANGED",
        )
        write_new(RUN_ROOT / "findings.json", canonical(result))
        receipt = {
            "schema_version": 1,
            "owner": OWNER,
            "directory": identity,
            "source_sha256": plan["source_sha256"],
            "plan_sha256": digest(canonical(plan)),
            "assessment": result["assessment"],
            "responses": responses,
            "http_responses": result["http_responses"],
            "response_bytes": result["response_bytes"],
            "elapsed_seconds": result["elapsed_seconds"],
            "audit_complete": True,
            "binary_accepted": False,
            "runtime_accepted": False,
            "release_accepted": False,
        }
        write_new(RUN_ROOT / "receipt.json", canonical(receipt))
        return receipt
    except BaseException:
        write_new(
            RUN_ROOT / "failure.json",
            canonical(
                {
                    "audit_complete": False,
                    "binary_accepted": False,
                    "runtime_accepted": False,
                    "error": "BINARY_AUDIT_INCOMPLETE",
                    "responses": responses,
                    "plan_sha256": digest(canonical(plan)),
                }
            ),
        )
        raise


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
    plan, model = prepare()
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
    receipt = publish_audit(plan, model)
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0 if receipt["assessment"]["known_findings_policy_passed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, EOFError, tarfile.TarError):
        print(
            json.dumps(
                {
                    "audit_complete": False,
                    "binary_accepted": False,
                    "runtime_accepted": False,
                    "error": "BINARY_AUDIT_REJECTED",
                }
            )
        )
        raise SystemExit(1) from None
