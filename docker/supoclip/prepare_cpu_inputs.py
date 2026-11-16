"""Cierre CPU reproducible de SupoClip como datos; no instala ni ejecuta fuentes."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import os
import re
import stat
import tarfile
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import packaging
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

COMMIT = "f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38"
REMOVED = frozenset({"openai-whisper", "whisper", "setuptools-rust"})
FORBIDDEN = REMOVED | {"torch", "torchaudio", "torchvision", "triton", "numba", "llvmlite"}
TARGET = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.16",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "platform_python_implementation": "CPython",
    "python_full_version": "3.12.16",
    "python_version": "3.12",
    "sys_platform": "linux",
}


class InputError(ValueError):
    pass


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _require(condition, code):
    if not condition:
        raise InputError(code)


def cpu_profile(lock):
    """Propuesta explícita del metapaquete; no modifica lock ni código upstream."""
    value = copy.deepcopy(lock)
    roots = [item for item in value["package"] if item["name"] == "supoclip-backend"]
    _require(len(roots) == 1, "ROOT_IDENTITY")
    edges = roots[0]["dependencies"]
    _require(
        sum(edge["name"] == "pydantic-ai" for edge in edges) == 1
        and not any(edge["name"] in {"pydantic-ai-slim", "python-multipart"} for edge in edges),
        "CPU_PROFILE_UPSTREAM_CHANGED",
    )
    roots[0]["dependencies"] = [
        {"name": "pydantic-ai-slim", "extra": ["anthropic", "google", "openai"]}
        if edge["name"] == "pydantic-ai"
        else edge
        for edge in edges
    ] + [{"name": "python-multipart"}]
    return value


def select_graph(lock, overrides=None):
    """Evalúa el lock ya resuelto, incluidos extras transitivos, para un target fijo."""
    _require(
        SpecifierSet(lock["requires-python"]).contains(TARGET["python_full_version"]),
        "PYTHON_TARGET",
    )
    index = defaultdict(list)
    for item in lock["package"]:
        index[canonicalize_name(item["name"])].append(item)
    roots = index["supoclip-backend"]
    _require(len(roots) == 1 and roots[0]["source"] == {"editable": "."}, "ROOT_IDENTITY")
    pending = deque(
        (edge, "", "supoclip-backend")
        for edge in roots[0]["dependencies"]
        if canonicalize_name(edge["name"]) not in REMOVED
    )
    selected, expanded, edges = {}, set(), []
    while pending:
        edge, parent_extra, parent = pending.popleft()
        if edge.get("marker") and not Marker(edge["marker"]).evaluate(
            {**TARGET, "extra": parent_extra}
        ):
            continue
        name = canonicalize_name(edge["name"])
        _require(
            name not in FORBIDDEN and not name.startswith(("nvidia-", "cuda-")), "GPU_DEPENDENCY"
        )
        choices = [
            item
            for item in index[name]
            if ("version" not in edge or item["version"] == edge["version"])
            and ("source" not in edge or item["source"] == edge["source"])
        ]
        _require(len(choices) == 1, "MISSING_OR_AMBIGUOUS_DEPENDENCY")
        item = choices[0]
        _require(item["source"] == {"registry": "https://pypi.org/simple"}, "NON_PYPI_SOURCE")
        if name in selected:
            _require(item["version"] == selected[name]["version"], "VERSION_CONFLICT")
        selected[name] = item
        edges.append({"from": parent, "to": name, "extra": parent_extra})
        for extra in ["", *edge.get("extra", [])]:
            if (name, extra) in expanded:
                continue
            expanded.add((name, extra))
            if extra:
                _require(extra in item.get("optional-dependencies", {}), "UNKNOWN_EXTRA")
                children = item["optional-dependencies"][extra]
            else:
                children = item.get("dependencies", [])
            pending.extend((child, extra, name) for child in children)
    for name, replacement in (overrides or {}).items():
        _require(name in selected and name == canonicalize_name(name), "OVERRIDE_IDENTITY")
        original = selected[name]
        selected[name] = {**original, **replacement}
        _require(
            selected[name]["name"] == original["name"]
            and selected[name]["source"] == original["source"],
            "OVERRIDE_IDENTITY",
        )
    return {
        "packages": dict(sorted(selected.items())),
        "extras": {
            name: sorted(extra for package, extra in expanded if package == name)
            for name in sorted(selected)
        },
        "edges": edges,
    }


def validate_metadata(graph, rows):
    """Bijección de METADATA y todas sus aristas activas, sin resolver paquetes nuevos."""
    by_name = {canonicalize_name(row["name"]): row for row in rows}
    _require(
        len(by_name) == len(rows) and set(by_name) == set(graph["packages"]), "METADATA_COVERAGE"
    )
    edges = []
    for name, row in by_name.items():
        item = graph["packages"][name]
        _require(
            canonicalize_name(row["metadata_name"]) == name
            and Version(row["metadata_version"]) == Version(item["version"]),
            "METADATA_IDENTITY",
        )
        _require(
            not row.get("requires_python")
            or SpecifierSet(row["requires_python"]).contains(TARGET["python_full_version"]),
            "METADATA_PYTHON",
        )
        for raw in row.get("requires_dist", []):
            requirement = Requirement(raw)
            if requirement.marker and not any(
                requirement.marker.evaluate({**TARGET, "extra": extra})
                for extra in graph["extras"][name]
            ):
                continue
            child = canonicalize_name(requirement.name)
            _require(not requirement.url and child in by_name, "METADATA_DEPENDENCY")
            _require(
                requirement.specifier.contains(by_name[child]["metadata_version"]),
                "METADATA_VERSION",
            )
            _require(set(requirement.extras) <= set(graph["extras"][child]), "METADATA_EXTRA")
            edges.append({"from": name, "to": child, "requirement": raw})
    return {"complete": True, "nodes": len(rows), "edges": edges}


def _artifact(value):
    url = urlsplit(value["url"])
    digest = value.get("hash", "")
    _require(
        url.scheme == "https"
        and url.netloc == "files.pythonhosted.org"
        and not url.query
        and not url.fragment
        and url.path.startswith("/packages/")
        and re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
        and type(value.get("size")) is int
        and 0 < value["size"] <= 512 * 1024**2,
        "ARTIFACT_PIN",
    )
    return {"url": value["url"], "sha256": digest[7:], "size": value["size"]}


def choose_artifact(package):
    platforms = [f"manylinux_2_{minor}_x86_64" for minor in range(44, 4, -1)]
    platforms += [
        "manylinux2014_x86_64",
        "manylinux2010_x86_64",
        "manylinux1_x86_64",
        "linux_x86_64",
    ]
    supported = list(cpython_tags((3, 12), platforms=platforms))
    supported += list(compatible_tags((3, 12), interpreter="cp312", platforms=platforms))
    rank = {tag: position for position, tag in enumerate(supported)}
    matches = []
    for wheel in package.get("wheels", []):
        artifact = _artifact(wheel)
        filename = urlsplit(artifact["url"]).path.rsplit("/", 1)[-1]
        name, version, _build, tags = parse_wheel_filename(filename)
        _require(
            name == canonicalize_name(package["name"]) and version == Version(package["version"]),
            "WHEEL_IDENTITY",
        )
        common = tags & rank.keys()
        if common:
            matches.append((min(rank[tag] for tag in common), filename, artifact))
    if matches:
        return {"kind": "wheel", "artifact": min(matches, key=lambda x: (x[0], x[1]))[2]}
    _require(
        package["name"] == "srt" and package["version"] == "3.5.3" and "sdist" in package,
        "NO_AUTHORIZED_WHEEL_OR_SOURCE",
    )
    return {"kind": "sdist", "artifact": _artifact(package["sdist"])}


def node_inventory(lock):
    """Catálogo npm por SRI; no ejecuta pnpm ni resuelve versiones nuevas."""
    _require(str(lock["lockfileVersion"]) == "9.0", "NODE_LOCK_VERSION")
    result = []
    for key, value in lock["packages"].items():
        name, separator, version = key.rpartition("@")
        _require(
            separator
            and re.fullmatch(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", name)
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version),
            "NODE_PACKAGE_IDENTITY",
        )
        resolution = value["resolution"]
        _require(set(resolution) == {"integrity"}, "NODE_REMOTE_OR_VCS")
        integrity = resolution["integrity"]
        _require(type(integrity) is str and integrity.startswith("sha512-"), "NODE_SRI")
        try:
            raw = base64.b64decode(integrity[7:], validate=True)
        except ValueError:
            raise InputError("NODE_SRI") from None
        _require(len(raw) == 64, "NODE_SRI")
        basename = name.rsplit("/", 1)[-1]
        result.append(
            {
                "name": name,
                "version": version,
                "integrity": integrity,
                "url": f"https://registry.npmjs.org/{name}/-/{basename}-{version}.tgz",
                "os": value.get("os", []),
                "cpu": value.get("cpu", []),
            }
        )
    return sorted(result, key=lambda row: (row["name"], row["version"]))


def audit_source(raw, expected_sha256, tree):
    """Lee TAR completo y verifica cada blob contra el árbol API fijado; nunca extrae."""
    _require(
        type(raw) is bytes and len(raw) <= 64 * 1024**2 and sha256(raw) == expected_sha256,
        "SOURCE_ARCHIVE_PIN",
    )
    _require(tree.get("sha") == COMMIT and tree.get("truncated") is False, "SOURCE_TREE")
    expected = {item["path"]: item for item in tree["tree"] if item["type"] == "blob"}
    _require(
        len(expected) == sum(x["type"] == "blob" for x in tree["tree"]), "SOURCE_TREE_DUPLICATE"
    )
    prefix = "supoclip-" + COMMIT + "/"
    seen, folded, total = set(), set(), 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for index, member in enumerate(archive):
            _require(index < 20000 and (member.isdir() or member.isfile()), "SOURCE_ENTRY_TYPE")
            _require(
                member.name.startswith(prefix) or member.name.rstrip("/") == prefix.rstrip("/"),
                "SOURCE_ROOT",
            )
            name = member.name[len(prefix) :].rstrip("/")
            if not name and member.isdir():
                continue
            path = PurePosixPath(name)
            _require(
                name
                and not path.is_absolute()
                and ".." not in path.parts
                and "\\" not in name
                and ":" not in name,
                "SOURCE_PATH",
            )
            if member.isdir():
                continue
            _require(
                name not in seen and name.casefold() not in folded and name in expected,
                "SOURCE_DUPLICATE_OR_EXTRA",
            )
            seen.add(name)
            folded.add(name.casefold())
            total += member.size
            _require(0 <= member.size <= 64 * 1024**2 and total <= 128 * 1024**2, "SOURCE_SIZE")
            body = archive.extractfile(member).read(member.size + 1)
            blob = hashlib.sha1(
                b"blob " + str(len(body)).encode() + b"\0" + body, usedforsecurity=False
            ).hexdigest()
            _require(
                len(body) == expected[name]["size"] == member.size
                and blob == expected[name]["sha"],
                "SOURCE_BLOB_MISMATCH",
            )
    _require(seen == set(expected), "SOURCE_INCOMPLETE")
    return {
        "archive_sha256": expected_sha256,
        "files_verified": len(seen),
        "uncompressed_bytes": total,
        "extracted": False,
        "executed": False,
    }


def _read_regular(path, maximum):
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        _require(
            not stat.S_ISLNK(info.st_mode)
            and not getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0),
            "INPUT_LINK",
        )
    info = path.stat()
    _require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum, "INPUT_FILE"
    )
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _require(
            (opened.st_dev, opened.st_ino, opened.st_size)
            == (info.st_dev, info.st_ino, info.st_size),
            "INPUT_CHANGED",
        )
        raw = stream.read(maximum + 1)
    _require(len(raw) == info.st_size, "INPUT_CHANGED")
    return raw


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "JSON_DUPLICATE")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise InputError("JSON_NONFINITE")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def validate_requirements(requirements, packages):
    """Lock de ruedas de registro: ni el sdist SRT ni OpenCV upstream son fallback."""
    parsed = []
    for line in requirements.decode("utf-8", errors="strict").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+) --hash=sha256:([a-f0-9]{64})", line)
        _require(match is not None, "REQUIREMENTS_FORMAT")
        parsed.append((canonicalize_name(match[1]), match[2], match[3]))
    names = [row[0] for row in parsed]
    expected_rows = [row for row in packages if row["role"] == "registry-wheel"]
    expected = {row["name"]: row for row in expected_rows}
    _require(len(expected) == len(expected_rows), "REQUIREMENTS_DUPLICATE_PACKAGE")
    _require(len(names) == len(set(names)) and set(names) == set(expected), "REQUIREMENTS_COVERAGE")
    for name, version, pin in parsed:
        _require(
            version == expected[name]["version"] and pin == expected[name]["artifact"]["sha256"],
            "REQUIREMENTS_VERSION_OR_HASH",
        )
    _require(
        not (set(names) & FORBIDDEN) and not any(name.startswith("nvidia-") for name in names),
        "GPU_DEPENDENCY",
    )
    return len(expected)


def validate_inputs(value, requirements):
    _require(value["schema"] == 1 and value["source"]["commit"] == COMMIT, "MANIFEST_IDENTITY")
    _require(
        value["build_enabled"] is False
        and value["deployment_enabled"] is False
        and value["python"]["requirements_complete_install"] is False
        and value["native"]["payload_materialization_and_native_solver_complete"] is False,
        "UNPROVEN_EXECUTION_CLAIM",
    )
    python = value["python"]
    _require(sha256(requirements) == python["requirements_sha256"], "REQUIREMENTS_PIN")
    rows = python["packages"]
    names = [row["name"] for row in rows]
    _require(
        len(rows) == 84
        and len(set(names)) == len(names)
        and names == sorted(names)
        and all(name == canonicalize_name(name) for name in names),
        "PYTHON_COVERAGE",
    )
    _require(
        not (set(names) & (FORBIDDEN | {"pydantic-ai"}))
        and not any(name.startswith(("nvidia-", "cuda-")) for name in names),
        "GPU_DEPENDENCY",
    )
    by_name = {row["name"]: row for row in rows}
    _require(
        by_name["pydantic-ai-slim"]["version"] == "1.107.10"
        and by_name["pydantic-ai-slim"]["extras"] == ["", "anthropic", "google", "openai"]
        and by_name["python-multipart"]["version"] == "0.0.33"
        and by_name["mako"]["version"] == "1.4.3",
        "CPU_PROFILE_IDENTITY",
    )
    for row in rows:
        role = {"srt": "source-wheel-build", "opencv-python": "native-opencv-build"}.get(
            row["name"], "registry-wheel"
        )
        _require(
            row["role"] == role
            and row["artifact_installable"] is (role == "registry-wheel")
            and re.fullmatch(r"[a-f0-9]{64}", row["metadata_sha256"])
            and "" in row["extras"],
            "PACKAGE_ROLE_OR_METADATA",
        )
        pinned = {
            "url": row["artifact"]["url"],
            "hash": "sha256:" + row["artifact"]["sha256"],
            "size": row["artifact"]["size"],
        }
        if role == "source-wheel-build":
            _require(
                row["version"] == "3.5.3" and row["artifact"]["url"].endswith("/srt-3.5.3.tar.gz"),
                "SRT_SOURCE",
            )
            _artifact(pinned)
        else:
            choose_artifact({"name": row["name"], "version": row["version"], "wheels": [pinned]})
    graph = {"packages": by_name, "extras": {name: row["extras"] for name, row in by_name.items()}}
    metadata = validate_metadata(graph, rows)
    _require(
        metadata["edges"] == python["metadata_edges"]
        and metadata["nodes"] == python["closure"]["nodes"]
        and len(metadata["edges"]) == python["closure"]["active_metadata_edges"],
        "METADATA_EDGES",
    )
    wheel_count = validate_requirements(requirements, rows)
    _require(
        wheel_count == 82
        and python["closure"]["registry_wheels"] == 82
        and python["closure"]["external_wheel_outputs_pending"] == 2,
        "WHEEL_COVERAGE",
    )
    node = value["frontend"]["packages"]
    keyed = {
        row["name"] + "@" + row["version"]: {
            "resolution": {"integrity": row["integrity"]},
            "os": row["os"],
            "cpu": row["cpu"],
        }
        for row in node
    }
    _require(
        len(keyed) == len(node) == value["frontend"]["package_count"] == 920
        and node_inventory({"lockfileVersion": "9.0", "packages": keyed}) == node,
        "NODE_COVERAGE_OR_URL",
    )
    native = value["native"]
    for row in native["packages"]:
        record = row["record"]
        for field in ("D", "p"):
            if field in record:
                _require(
                    type(record[field]) is list
                    and all(
                        type(token) is str and token and not any(c.isspace() for c in token)
                        for token in record[field]
                    ),
                    "NATIVE_CATALOGUE_TOKENS",
                )
        original = {
            key: " ".join(item) if key in {"D", "p"} else item for key, item in record.items()
        }
        _require(
            sha256(json.dumps(original, sort_keys=True, separators=(",", ":")).encode())
            == row["catalogue_record_sha256"],
            "NATIVE_CATALOGUE_PIN",
        )
    native_names = [(row["record"]["P"], row["record"]["V"]) for row in native["packages"]]
    _require(
        len(set(native_names))
        == len(native_names)
        == native["sca"]["coverage"]["expected"]
        == native["sca"]["coverage"]["reported"]
        and native["sca"]["coverage"]["complete"] is True
        and all(
            row["payload_verified"] is False and row["payload_sha256"] is None
            for row in native["packages"]
        ),
        "NATIVE_DECLARED_COVERAGE",
    )
    return {
        "python_nodes": metadata["nodes"],
        "python_metadata_edges": len(metadata["edges"]),
        "registry_wheels": wheel_count,
        "external_wheel_outputs_pending": 2,
        "npm_packages": len(node),
        "native_declared_packages": len(native_names),
    }


def plan(manifest_path=None):
    _require(packaging.__version__ == "26.2", "METADATA_TOOL_VERSION")
    directory = Path(__file__).absolute().parent
    path = Path(manifest_path) if manifest_path else directory / "cpu-build-inputs.json"
    raw = _read_regular(path, 2 * 1024**2)
    value = strict_json(raw)
    counts = validate_inputs(value, _read_regular(directory / "requirements-cpu.lock", 256 * 1024))
    return {
        "status": "plan",
        "manifest_sha256": sha256(raw),
        "coverage": counts,
        "source_commit": COMMIT,
        "source_archive_sha256": value["source"]["archive_sha256"],
        "pending_barriers": value["build_plan"]["pending_barriers"],
        "build_enabled": False,
        "deployment_enabled": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest")
    parser.add_argument("--source-archive")
    parser.add_argument("--source-tree")
    args = parser.parse_args(argv)
    try:
        result = plan(args.manifest)
        if args.source_archive or args.source_tree:
            _require(bool(args.source_archive and args.source_tree), "SOURCE_INPUTS_REQUIRED")
            result["source_verification"] = audit_source(
                _read_regular(args.source_archive, 64 * 1024**2),
                result["source_archive_sha256"],
                strict_json(_read_regular(args.source_tree, 1024**2)),
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (InputError, OSError, ValueError, KeyError, TypeError, tarfile.TarError):
        print(json.dumps({"status": "error", "error": "CPU_INPUTS_INVALID"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
