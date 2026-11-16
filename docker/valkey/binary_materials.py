"""Núcleo puro del inventario y avisos Valkey; ningún archivo o programa se ejecuta."""

import base64
import gzip
import hashlib
import io
import json
import re
import tarfile
from collections import Counter
from datetime import datetime
from urllib.parse import urlencode, urlsplit

MAX_SOURCE_ARCHIVE = 8 * 1024**2
MAX_SOURCE_EXPANDED = 32 * 1024**2
MAX_SOURCE_FILE = 1024**2
MAX_SOURCE_MEMBERS = 4096
SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")


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

    def nonfinite(_value):
        raise ValueError("JSON_NONFINITE")

    result = json.loads(
        raw.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=nonfinite
    )
    require(type(result) is dict, "JSON_OBJECT")
    return result


def safe_name(name):
    require(
        type(name) is str
        and name
        and not name.startswith("/")
        and "\\" not in name
        and ":" not in name,
        "SOURCE_PATH",
    )
    require(
        all(part not in {"", ".", ".."} for part in name.split("/"))
        and all(ord(char) >= 32 for char in name),
        "SOURCE_PATH",
    )
    return name


def catalogue_source(raw, expected):
    require(
        type(raw) is bytes
        and len(raw) == expected["bytes"] <= MAX_SOURCE_ARCHIVE
        and digest(raw) == expected["sha256"],
        "SOURCE_PIN",
    )
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        plain = stream.read(MAX_SOURCE_EXPANDED + 1)
    require(len(plain) <= MAX_SOURCE_EXPANDED and len(plain) % 512 == 0, "SOURCE_EXPANDED")
    root = safe_name(expected["root"])
    files, directories, names = {}, set(), set()
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as archive:
        for member in archive:
            require(len(names) < MAX_SOURCE_MEMBERS, "SOURCE_MEMBERS")
            name = safe_name(member.name.rstrip("/"))
            require(
                name not in names and (name == root or name.startswith(root + "/")),
                "SOURCE_DUPLICATE_OR_ROOT",
            )
            names.add(name)
            require(member.isdir() or member.isfile(), "SOURCE_LINK_OR_SPECIAL")
            require(not member.sparse and 0 <= member.size <= MAX_SOURCE_FILE, "SOURCE_MEMBER_SIZE")
            if name == root:
                require(member.isdir(), "SOURCE_ROOT_TYPE")
                continue
            relative = name[len(root) + 1 :]
            if member.isdir():
                directories.add(relative)
                continue
            with archive.extractfile(member) as stream:
                payload = stream.read(MAX_SOURCE_FILE + 1)
            require(len(payload) == member.size, "SOURCE_TRUNCATED")
            files[relative] = {
                "sha256": digest(payload),
                "bytes": len(payload),
                "mode": member.mode,
                "data": payload,
            }
        tail = plain[archive.offset :]
        require(len(tail) >= 1024 and not any(tail), "SOURCE_TRAILER")
        commit = archive.pax_headers.get("comment")
    require(
        commit == expected["commit_declared"] and re.fullmatch(r"[a-f0-9]{40}", commit or ""),
        "SOURCE_COMMIT",
    )
    for name in files.keys() | directories:
        parts = name.split("/")
        require(
            all("/".join(parts[:length]) not in files for length in range(1, len(parts))),
            "SOURCE_FILE_ANCESTOR",
        )
    listing = {
        name: {key: value for key, value in row.items() if key != "data"}
        for name, row in sorted(files.items())
    }
    return {
        "files": files,
        "directories": sorted(directories),
        "members": len(names),
        "expanded_bytes": sum(row["bytes"] for row in files.values()),
        "commit_declared": commit,
        "tree_sha256": digest(canonical(listing)),
    }


def verify_provenance(provenance, source):
    documents = provenance["documents"]
    require(type(documents) is list and len(documents) == 5, "PROVENANCE_DOCUMENTS")
    checked = []
    for document in documents:
        require(
            document.get("encoding") == "utf-8" and type(document.get("content")) is str,
            "PROVENANCE_ENCODING",
        )
        raw = document["content"].encode()
        git_blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        require(git_blob == document["sha"], "PROVENANCE_BLOB")
        url = urlsplit(document["display_url"])
        require(
            url.scheme == "https"
            and url.netloc == "github.com"
            and not url.query
            and not url.fragment,
            "PROVENANCE_URL",
        )
        marker = "/valkey/blob/9.1.3/"
        if marker in url.path:
            relative = safe_name(url.path.split(marker, 1)[1])
            require(source["files"][relative]["data"] == raw, "PROVENANCE_TAR_BYTES")
        checked.append(
            {
                "url": document["display_url"],
                "blob_sha1": git_blob,
                "sha256": digest(raw),
                "bytes": len(raw),
            }
        )
    return checked


def license_catalogue(source):
    notices = []
    pattern = re.compile(
        rb"copyright|SPDX-License-Identifier|permission is hereby|redistribution and use|Apache License|GNU General Public License|Boost Software License",
        re.I,
    )
    for name, row in sorted(source["files"].items()):
        if re.search(
            r"(?:^|/)(?:COPYING|LICENSE|NOTICE|COPYRIGHT)(?:[.\-][^/]*)?$", name, re.I
        ) or pattern.search(row["data"]):
            notices.append(
                {
                    "path": name,
                    "sha256": row["sha256"],
                    "bytes": row["bytes"],
                    "content_base64": base64.b64encode(row["data"]).decode(),
                    "scope_assignment_requires_component_matrix": True,
                }
            )
    return notices


def materials(source, inventory, components):
    result, ids = [], set()
    binary_names = {row["path"].rsplit("/", 1)[-1] for row in inventory["binaries"]}
    for component in components:
        identifier = component["id"]
        require(
            re.fullmatch(r"[a-z][a-z0-9-]{0,47}", identifier) and identifier not in ids,
            "COMPONENT_ID",
        )
        ids.add(identifier)
        require(
            component["scope"] in {"runtime", "build-test-only"}
            and set(component["supplier_link_candidates"]).issubset(binary_names),
            "COMPONENT_SCOPE",
        )
        require(
            component["scope"] != "build-test-only" or not component["supplier_link_candidates"],
            "TEST_COMPONENT_LINK",
        )
        selected = {}
        for selector in component["paths"]:
            safe_name(selector.rstrip("/"))
            for name, row in source["files"].items():
                if name.startswith(selector) if selector.endswith("/") else name == selector:
                    selected[name] = {key: value for key, value in row.items() if key != "data"}
        require(not component["paths"] or selected, "COMPONENT_SOURCE_MISSING")
        witnesses = []
        for witness in component.get("version_witnesses", []):
            path = safe_name(witness["path"])
            raw, needle = source["files"][path]["data"], witness["needle"].encode()
            require(needle and needle in raw, "VERSION_WITNESS")
            witnesses.append(
                {**witness, "source_file_sha256": digest(raw), "offset": raw.index(needle)}
            )
        markers = [row for row in inventory["embedded_candidates"] if row["name"] == identifier]
        result.append(
            {
                **component,
                "source_files": selected,
                "source_tree_sha256": digest(canonical(selected)) if selected else None,
                "source_bytes_external_pending": not bool(selected),
                "binary_markers": markers,
                "version_evidence": witnesses,
                "binary_equal_to_upstream_proven": False,
                "identity_basis": "Declaración del proveedor y bytes verificados por hash",
                "link_candidates_are_not_proof_of_linker_retention": True,
            }
        )
    return {
        "components": result,
        "binaries": inventory["binaries"],
        "aliases": inventory["aliases"],
        "source_tree_sha256": source["tree_sha256"],
        "license_overlay_pending": not bool(inventory["licenses"]["text_files"]),
        "reproducible_build_required_for_inventory": False,
        "binary_accepted": False,
        "runtime_accepted": False,
    }


def query_plan(components):
    queries = []
    for component in components:
        if component["scope"] != "runtime":
            continue
        identifier, repository, commit = (
            component["id"],
            component["repository"].rstrip("/"),
            component["commit_declared"],
        )
        if commit:
            require(
                re.fullmatch(r"[a-f0-9]{40}", commit)
                and re.fullmatch(
                    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
                ),
                "COMMIT_OR_REPOSITORY",
            )
            slug = repository.split("github.com/", 1)[1]
            queries.extend(
                [
                    {
                        "component": identifier,
                        "kind": "github-commit",
                        "method": "GET",
                        "url": "https://api.github.com/repos/" + slug + "/commits/" + commit,
                    },
                    {
                        "component": identifier,
                        "kind": "osv",
                        "method": "POST",
                        "url": "https://api.osv.dev/v1/query",
                        "body": {"commit": commit},
                        "requires_authenticated_commit": True,
                    },
                ]
            )
        require(
            type(component["nvd_keyword"]) is str
            and re.fullmatch(r"[A-Za-z0-9_. -]{1,48}", component["nvd_keyword"]),
            "NVD_KEYWORD",
        )
        queries.append(
            {
                "component": identifier,
                "kind": "nvd-dictionary",
                "method": "GET",
                "url": "https://services.nvd.nist.gov/rest/json/cpes/2.0?"
                + urlencode(
                    {
                        "keywordSearch": component["nvd_keyword"],
                        "resultsPerPage": 100,
                        "startIndex": 0,
                    }
                )
                + "&keywordExactMatch",
            }
        )
    return queries


def osv_page(body):
    require(type(body) is dict and set(body).issubset({"vulns", "next_page_token"}), "OSV_SCHEMA")
    rows = body.get("vulns", [])
    require(type(rows) is list, "OSV_VULNS")
    for row in rows:
        require(
            type(row) is dict
            and type(row.get("id")) is str
            and row["id"]
            and type(row.get("affected")) is list
            and row["affected"],
            "OSV_AFFECTED",
        )
        require(
            "schema_version" not in row
            or re.fullmatch(r"1\.[0-9]+\.[0-9]+", row["schema_version"]),
            "OSV_SCHEMA_VERSION",
        )
    token = body.get("next_page_token")
    require(
        token is None
        or type(token) is str
        and 0 < len(token) <= 4096
        and token.isascii()
        and all(32 < ord(char) < 127 for char in token),
        "OSV_TOKEN",
    )
    return rows, token


def cpe_fields(value):
    require(
        type(value) is str
        and len(value) <= 1024
        and value.isascii()
        and all(32 < ord(char) < 127 for char in value),
        "CPE_STRING",
    )
    fields, buffer, escaped = [], "", False
    for char in value:
        if escaped:
            buffer += "\\" + char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(buffer)
            buffer = ""
        else:
            buffer += char
    fields.append(buffer)
    require(
        not escaped
        and len(fields) == 13
        and fields[:2] == ["cpe", "2.3"]
        and fields[2] in {"a", "o", "h"}
        and all(fields),
        "CPE_FORMAT",
    )
    return fields


def primary_reference(reference, prefixes):
    parsed = urlsplit(reference)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
    ):
        return False
    for prefix in prefixes:
        expected = urlsplit(prefix)
        host = (parsed.hostname or "").removeprefix("www.")
        other = (expected.hostname or "").removeprefix("www.")
        base = expected.path.rstrip("/").removesuffix(".git")
        path = parsed.path.rstrip("/").removesuffix(".git")
        if host == other and (path == base or path.startswith(base + "/")):
            return True
    return False


def select_cpes(products, component):
    require(type(products) is list, "NVD_PRODUCTS")
    selected, pending = set(), []
    for product in products:
        cpe = product["cpe"]
        fields = cpe_fields(cpe["cpeName"])
        require(type(cpe.get("deprecated")) is bool, "CPE_DEPRECATED")
        target = component["nvd_version"]
        trusted = cpe["cpeName"] == component["supplier_cpe"] or any(
            primary_reference(row["ref"], component["reference_prefixes"])
            for row in cpe.get("refs", [])
        )
        if (
            target
            and fields[2] == "a"
            and fields[5] == target
            and trusted
            and not cpe["deprecated"]
        ):
            selected.add(cpe["cpeName"])
        else:
            pending.append({"cpe": cpe, "reason": "identity_or_version_not_resolved"})
    return sorted(selected), pending


def named_severity(values):
    names = [value.upper() for value in values if type(value) is str and value]
    if not names or any(value not in SEVERITIES for value in names):
        return "UNKNOWN"
    return max(names, key=SEVERITIES.index)


def osv_finding(row, component):
    names = [row.get("database_specific", {}).get("severity")]
    names.extend(
        affected.get("ecosystem_specific", {}).get("severity") for affected in row["affected"]
    )
    withdrawn = row.get("withdrawn")
    if withdrawn is not None:
        require(type(withdrawn) is str and withdrawn.endswith("Z"), "OSV_WITHDRAWN")
        datetime.fromisoformat(withdrawn.replace("Z", "+00:00"))
    return {
        "component": component["id"],
        "id": row["id"],
        "origin": "osv",
        "severity": named_severity(names),
        "applicability": "withdrawn_by_source" if withdrawn else "unresolved",
        "active": not bool(withdrawn),
        "raw": row,
    }


def nvd_finding(row, component):
    require(
        type(row.get("id")) is str and re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", row["id"]),
        "NVD_CVE_ID",
    )
    metrics = row.get("metrics", {})
    require(type(metrics) is dict, "NVD_METRICS")
    names = []
    for values in metrics.values():
        require(type(values) is list, "NVD_METRICS_SCHEMA")
        for value in values:
            names.append(value.get("cvssData", {}).get("baseSeverity") or value.get("baseSeverity"))
    rejected = row.get("vulnStatus") == "Rejected"
    return {
        "component": component["id"],
        "id": row["id"],
        "origin": "nvd",
        "severity": named_severity(names),
        "applicability": "rejected_by_source" if rejected else "unresolved",
        "active": not rejected,
        "raw": row,
    }


def assess(findings, gaps=None):
    gaps = gaps or []
    counts = Counter(row["severity"] for row in findings)
    blocked = [
        row
        for row in findings
        if row["active"] and row["severity"] in {"HIGH", "CRITICAL", "UNKNOWN"}
    ]
    groups = []
    for number, row in enumerate(findings):
        aliases = row["raw"].get("aliases", [])
        require(
            type(aliases) is list and all(type(alias) is str and alias for alias in aliases),
            "ADVISORY_ALIASES",
        )
        names = {row["id"], *aliases}
        overlaps = [
            group
            for group in groups
            if group["component"] == row["component"] and names.intersection(group["ids"])
        ]
        indices = {number}
        for group in overlaps:
            names.update(group["ids"])
            indices.update(group["record_indices"])
            groups.remove(group)
        groups.append(
            {"component": row["component"], "ids": sorted(names), "record_indices": sorted(indices)}
        )
    return {
        "findings": len(findings),
        "by_severity": {level: counts[level] for level in SEVERITIES},
        "known_findings_policy_passed": not blocked,
        "blocking_records": len(blocked),
        "advisory_groups": groups,
        "all_component_feeds_available": not gaps,
        "gaps": gaps,
        "binary_accepted": False,
        "runtime_accepted": False,
        "release_accepted": False,
    }
