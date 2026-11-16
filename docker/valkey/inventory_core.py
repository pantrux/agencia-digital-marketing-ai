"""Inventario OCI como datos; no ejecuta ni materializa el contenido de imagen."""

import gzip
import hashlib
import io
import json
import posixpath
import re
import tarfile
from urllib.parse import quote

MAX_ARCHIVE = 256 * 1024**2
MAX_LAYER = 64 * 1024**2
MAX_EXPANDED = 128 * 1024**2
MAX_MEMBER = 32 * 1024**2
MAX_NODES = 20000


def require(value, code):
    if not value:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def tar_members(raw, parser, *, outer=False, maximum_nodes=MAX_NODES):
    require(len(raw) >= 1024 and len(raw) % 512 == 0, "TAR_SIZE")
    rows, seen, end = [], set(), 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            end = max(end, member.offset_data + (member.size + 511) // 512 * 512)
            if member.name.rstrip("/") in ("", ".", "./") and member.isdir():
                continue
            name = parser.safe_name(member.name)
            require(name not in seen and len(seen) < maximum_nodes, "TAR_DUPLICATE_OR_LIMIT")
            seen.add(name)
            require(
                member.sparse is None
                and 0 <= member.size <= (MAX_ARCHIVE if outer else MAX_MEMBER),
                "TAR_MEMBER_LIMIT",
            )
            if outer:
                require(
                    member.isfile() or member.isdir() and name in {"blobs", "blobs/sha256"},
                    "OUTER_MEMBER_TYPE",
                )
            else:
                require(
                    member.isfile()
                    or member.isdir()
                    or member.issym()
                    or member.islnk()
                    or member.ischr()
                    or member.isblk()
                    or member.isfifo(),
                    "LAYER_MEMBER_TYPE",
                )
            body = None
            if member.isfile():
                stream = archive.extractfile(member)
                require(stream is not None, "TAR_BODY")
                with stream:
                    body = stream.read(member.size + 1)
                require(len(body) == member.size, "TAR_TRUNCATED")
            rows.append((name, member, body))
    require(len(raw) - end >= 1024 and not any(raw[end:]), "TAR_TRAILER")
    return rows


def parse_oci(raw, parser, expected, *, max_layer=MAX_LAYER, max_nodes=MAX_NODES):
    require(type(raw) is bytes and len(raw) <= MAX_ARCHIVE, "ARCHIVE_LIMIT")
    require(
        type(max_layer) is int
        and 0 < max_layer <= MAX_LAYER
        and type(max_nodes) is int
        and 0 < max_nodes <= MAX_NODES,
        "PARSER_BOUNDS",
    )
    blobs, special = {}, {}
    for name, member, body in tar_members(raw, parser, outer=True, maximum_nodes=max_nodes):
        if member.isdir():
            continue
        if re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name):
            require(digest(body) == name.rsplit("/", 1)[1], "OCI_BLOB_SHA")
            blobs["sha256:" + digest(body)] = body
        else:
            require(
                name in {"oci-layout", "index.json", "manifest.json"} and len(body) <= 1024**2,
                "OCI_OUTER_NAME",
            )
            special[name] = body
    require(
        parser.json_object(special.get("oci-layout", b"{}")) == {"imageLayoutVersion": "1.0.0"},
        "OCI_LAYOUT",
    )
    index = parser.json_object(special["index.json"])
    require(index.get("schemaVersion") == 2 and len(index.get("manifests", [])) == 1, "OCI_INDEX")

    def document(row):
        require(re.fullmatch(r"sha256:[a-f0-9]{64}", row.get("digest", "")), "OCI_DESCRIPTOR")
        body = blobs[row["digest"]]
        require(type(row["size"]) is int and len(body) == row["size"], "OCI_DESCRIPTOR_SIZE")
        return parser.json_object(body)

    descriptor = index["manifests"][0]
    require(descriptor["digest"] == expected["platform_digest"], "PLATFORM_DIGEST")
    manifest = document(descriptor)
    require(
        manifest == expected["manifest"] and manifest.get("schemaVersion") == 2,
        "CANDIDATE_MANIFEST",
    )
    config = document(manifest["config"])
    require(
        config.get("architecture") == "amd64"
        and config.get("os") == "linux"
        and config.get("rootfs", {}).get("type") == "layers",
        "OCI_PLATFORM",
    )
    layers, diffids = manifest["layers"], config["rootfs"]["diff_ids"]
    require(1 <= len(layers) <= 32 and len(layers) == len(diffids), "OCI_LAYER_COUNT")
    reachable = {descriptor["digest"], manifest["config"]["digest"]} | {
        row["digest"] for row in layers
    }
    require(reachable == set(blobs), "OCI_UNREACHABLE_BLOBS")
    files, payloads, origins, evidence = {}, {}, {}, []
    expanded, member_count = 0, 0
    for number, (layer, diffid) in enumerate(zip(layers, diffids, strict=True)):
        blob = blobs[layer["digest"]]
        require(len(blob) == layer["size"], "LAYER_SIZE")
        kind = layer["mediaType"]
        if kind in {
            "application/vnd.oci.image.layer.v1.tar+gzip",
            "application/vnd.docker.image.rootfs.diff.tar.gzip",
        }:
            with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
                plain = stream.read(max_layer + 1)
        else:
            require(kind == "application/vnd.oci.image.layer.v1.tar", "LAYER_COMPRESSION")
            plain = blob
        expanded += len(plain)
        require(
            len(plain) <= max_layer
            and expanded <= MAX_EXPANDED
            and diffid == "sha256:" + digest(plain),
            "EXPANSION_OR_DIFFID",
        )
        rows = tar_members(plain, parser, maximum_nodes=max_nodes)
        member_count += len(rows)
        require(member_count <= max_nodes, "LAYER_NODE_LIMIT")
        before = set(files)
        # Reutiliza exclusivamente el parser puro congelado; no modifica sus globals.
        parser.apply_layer(files, plain)
        require(len(files) <= max_nodes, "FILESYSTEM_NODE_LIMIT")
        for name in list(payloads):
            if name not in files or files[name]["kind"] != "file":
                del payloads[name]
        for name in list(origins):
            if name not in files:
                del origins[name]
        whiteouts = []
        for name, member, body in rows:
            if posixpath.basename(name).startswith(".wh."):
                whiteouts.append(name)
                continue
            if name not in files:
                continue
            origins[name] = {
                "layer": number,
                "layer_digest": layer["digest"],
                "diff_id": diffid,
                "header_name": member.name,
                "header_offset": member.offset,
                "pax_headers": dict(member.pax_headers),
            }
            if member.isfile():
                require(files[name].get("sha256") == digest(body), "PAYLOAD_ORIGIN")
                payloads[name] = body
        for name in files:
            parent = posixpath.dirname(name)
            while parent:
                require(
                    parent not in files or files[parent]["kind"] in {"directory", "symlink"},
                    "FILE_ANCESTOR",
                )
                parent = posixpath.dirname(parent)
        require(
            set(payloads) == {name for name, row in files.items() if row["kind"] == "file"},
            "PAYLOAD_SET",
        )
        evidence.append(
            {
                "layer": number,
                **layer,
                "diff_id": diffid,
                "expanded_bytes": len(plain),
                "members": len(rows),
                "whiteouts": whiteouts,
                "removed_nodes": sorted(before - set(files)),
            }
        )
    return {
        "manifest_digest": descriptor["digest"],
        "config_digest": manifest["config"]["digest"],
        "manifest": manifest,
        "config": config,
        "files": files,
        "payloads": payloads,
        "origins": origins,
        "layers": evidence,
        "expanded_bytes": expanded,
        "version_expected": expected["version"],
        "declared_index_digest": expected["index_digest"],
        "index_link_verified": False,
    }


def apk_database(raw):
    require(len(raw) <= 4 * 1024**2, "APK_DATABASE_LIMIT")
    packages, seen, owners = [], set(), {}
    for block in raw.decode("utf-8", "strict").strip().split("\n\n"):
        fields, directory, package_files = {}, "", []
        for line in block.splitlines():
            require(len(line) >= 2 and line[1] == ":", "APK_FIELD")
            key, value = line[0], line[2:]
            if key in "PVAoLc":
                require(key not in fields and value, "APK_DUPLICATE_FIELD")
                fields[key] = value
            elif key == "F":
                require(
                    not value.startswith("/")
                    and "\\" not in value
                    and all(part not in {".", "..", ""} for part in value.split("/")),
                    "APK_DIRECTORY",
                )
                directory = value
            elif key == "R":
                require(
                    value not in {"", ".", ".."} and "/" not in value and "\\" not in value,
                    "APK_FILENAME",
                )
                package_files.append(posixpath.join(directory, value))
        require(
            set("PVAoL") <= set(fields)
            and fields["P"] not in seen
            and fields["A"] in {"x86_64", "noarch"},
            "APK_IDENTITY",
        )
        seen.add(fields["P"])
        for name in package_files:
            owners.setdefault(name, []).append(fields["P"])
        packages.append(
            {
                "name": fields["P"],
                "version": fields["V"],
                "architecture": fields["A"],
                "source_name": fields["o"],
                "source_version": fields["V"],
                "license_declared": fields["L"],
                "source_commit_declared": fields.get("c"),
                "files_declared": package_files,
            }
        )
    return packages


def release_identity(raw):
    result = {}
    for line in raw.decode("utf-8", "strict").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        require(
            separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key) and key not in result, "OS_RELEASE"
        )
        if value.startswith('"'):
            require(value.endswith('"'), "OS_RELEASE_QUOTE")
            value = value[1:-1]
        result[key] = value
    require(
        result.get("ID") == "alpine"
        and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", result.get("VERSION_ID", "")),
        "OS_IDENTITY",
    )
    return result


def accounts(passwd, group):
    users, groups, uids, gids = {}, {}, set(), set()
    for line in passwd.decode("utf-8", "strict").splitlines():
        fields = line.split(":")
        require(
            len(fields) == 7
            and fields[0] not in users
            and all(re.fullmatch(r"0|[1-9][0-9]*", fields[i]) for i in (2, 3)),
            "PASSWD_RECORD",
        )
        uid, gid = int(fields[2]), int(fields[3])
        require(uid not in uids, "PASSWD_UID")
        uids.add(uid)
        users[fields[0]] = {"uid": uid, "gid": gid, "home": fields[5], "shell": fields[6]}
    for line in group.decode("utf-8", "strict").splitlines():
        fields = line.split(":")
        require(
            len(fields) == 4
            and fields[0] not in groups
            and re.fullmatch(r"0|[1-9][0-9]*", fields[2]),
            "GROUP_RECORD",
        )
        gid = int(fields[2])
        require(gid not in gids, "GROUP_GID")
        gids.add(gid)
        groups[fields[0]] = {"gid": gid, "members": fields[3].split(",") if fields[3] else []}
    require(
        "valkey" in users
        and "valkey" in groups
        and users["valkey"]["gid"] == groups["valkey"]["gid"],
        "VALKEY_ACCOUNT",
    )
    return {
        "valkey": users["valkey"],
        "valkey_group": groups["valkey"],
        "effective_identity_verified": False,
    }


def inventory(parsed, parser):
    files, payloads = parsed["files"], parsed["payloads"]

    def role(path):
        target = parser.follow(files, path)
        body = payloads[target]
        require(digest(body) == files[target]["sha256"], "ROLE_PAYLOAD")
        return target, body

    os_path, os_raw = role("etc/os-release")
    db_path, db_raw = role("lib/apk/db/installed")
    packages = apk_database(db_raw)
    identities = accounts(role("etc/passwd")[1], role("etc/group")[1])
    release = release_identity(os_raw)
    spdx_path, spdx_raw = role("usr/local/valkey.spdx.json")
    require(len(spdx_raw) <= 1024**2, "SHIPPED_SPDX_LIMIT")
    spdx = parser.json_object(spdx_raw)
    declared = [row for row in spdx.get("packages", []) if row.get("name") == "valkey-server"]
    config = parsed["config"].get("config", {})
    env = config.get("Env", [])
    versions = [row.split("=", 1)[1] for row in env if row.startswith("VALKEY_VERSION=")]
    require(
        len(declared) == len(versions) == 1
        and declared[0].get("versionInfo") == versions[0] == parsed["version_expected"],
        "VERSION_CLAIMS",
    )
    aliases, binaries, candidates = {}, [], []
    package_owners = {}
    for package in packages:
        for name in package["files_declared"]:
            package_owners.setdefault(name, []).append(package["name"])
    linked = {}

    def dependencies(path, depth=0):
        require(depth <= 128, "DYNAMIC_DEPTH")
        if path in linked:
            return
        info = files[path].get("elf")
        require(info is not None, "DYNAMIC_PROVIDER_ELF")
        linked[path] = {
            "sha256": files[path]["sha256"],
            "apk_owners_declared": package_owners.get(path, []),
            "needed": {},
        }
        search = []
        for item in (info["runpath"] or info["rpath"] or "").split(":"):
            if not item:
                continue
            item = item.replace("${ORIGIN}", "/" + posixpath.dirname(path)).replace(
                "$ORIGIN", "/" + posixpath.dirname(path)
            )
            require(item.startswith("/") and "$" not in item, "DYNAMIC_SEARCH_PATH")
            search.append(item.lstrip("/"))
        for name in info["needed"]:
            require("/" not in name and name not in {"", ".", ".."}, "DYNAMIC_SONAME")
            provider = None
            for parent in [*search, "lib", "usr/local/lib", "usr/lib"]:
                try:
                    provider = parser.follow(files, posixpath.join(parent, name))
                    break
                except ValueError:
                    continue
            require(
                provider is not None and files[provider].get("elf") is not None,
                "DYNAMIC_PROVIDER_MISSING",
            )
            require(
                set(info["version_requirements"].get(name, []))
                <= set(files[provider]["elf"]["version_definitions"]),
                "DYNAMIC_ABI_VERSION",
            )
            linked[path]["needed"][name] = provider
            dependencies(provider, depth + 1)

    for name, row in sorted(files.items()):
        if not name.startswith("usr/local/bin/"):
            continue
        if row["kind"] in {"symlink", "hardlink"}:
            resolved = parser.follow(files, name)
            aliases[name] = {
                "target": row["target"],
                "resolved": resolved,
                "sha256": files[resolved]["sha256"],
                "origin": parsed["origins"][name],
            }
        elif row["kind"] == "file" and row.get("elf"):
            elf = row["elf"]
            dependencies(name)
            binaries.append(
                {
                    "path": name,
                    "sha256": row["sha256"],
                    "bytes": row["size"],
                    "mode": row["mode"],
                    "uid": row["uid"],
                    "gid": row["gid"],
                    "origin": parsed["origins"][name],
                    "needed": elf["needed"],
                    "runpath": elf["runpath"],
                    "rpath": elf["rpath"],
                    "version_requirements": elf["version_requirements"],
                    "defined_symbols": len(elf["defined"]),
                    "strong_imports": len(elf["undefined"]),
                    "apk_owners_declared": package_owners.get(name, []),
                }
            )
            body = payloads[name]
            for library, needle, version in [
                ("lua", b"Lua 5.1.5", "5.1.5"),
                ("jemalloc", b"jemalloc-5.3.0", "5.3.0"),
            ]:
                offset = body.find(needle)
                if offset >= 0:
                    candidates.append(
                        {
                            "name": library,
                            "version_marker": version,
                            "binary": name,
                            "binary_sha256": row["sha256"],
                            "offset": offset,
                            "needle": needle.decode(),
                            "modified_upstream_candidate": True,
                            "exact_component_identity_verified": False,
                        }
                    )
    require(
        {row["path"] for row in binaries}
        >= {"usr/local/bin/valkey-" + name for name in ("server", "cli", "benchmark")},
        "VALKEY_BINARIES",
    )
    text_files = [
        {"path": name, "sha256": row.get("sha256"), "kind": row["kind"]}
        for name, row in sorted(files.items())
        if re.search(r"(?i)(license|copying|copyright|notice)", name)
    ]
    source_refs = {
        "declared_repository": config.get("Labels", {}).get("org.opencontainers.image.source"),
        "declared_download": declared[0].get("downloadLocation"),
        "binary_to_source_binding_verified": False,
        "reference_only": [
            {
                "url": "https://github.com/valkey-io/valkey/blob/9.1.3/deps/Makefile",
                "git_blob_sha1": "94d938552d3b19cf598646c8c5d11b01586c5154",
            },
            {
                "url": "https://github.com/valkey-io/valkey/blob/9.1.3/deps/README.md",
                "git_blob_sha1": "cc6a4a8cd802cdfcf9cede5e1369666340d9ab49",
            },
            {
                "url": "https://github.com/valkey-io/valkey/blob/9.1.3/COPYING",
                "git_blob_sha1": "2254cb05c4434c5d2ec74282eca5b0d00323e228",
            },
        ],
    }
    return {
        "schema_version": 1,
        "manifest_digest": parsed["manifest_digest"],
        "config_digest": parsed["config_digest"],
        "os": {
            "identity": release,
            "resolved_os_release": os_path,
            "os_release_sha256": digest(os_raw),
            "database_path": db_path,
            "database_sha256": digest(db_raw),
            "packages": packages,
            "apk_signatures_independently_verified": False,
        },
        "accounts": identities,
        "configuration": {
            "user_field_present": "User" in config,
            "user": config.get("User"),
            "effective_uid_verified": False,
            "entrypoint": config.get("Entrypoint"),
            "cmd": config.get("Cmd"),
            "working_directory": config.get("WorkingDir"),
            "labels": config.get("Labels", {}),
        },
        "source": source_refs,
        "history": parsed["config"].get("history", []),
        "layers": parsed["layers"],
        "binaries": binaries,
        "aliases": aliases,
        "embedded_candidates": candidates,
        "dynamic_link_structure": {"resolved": linked, "loader_executed": False},
        "shipped_spdx": spdx,
        "shipped_spdx_path": spdx_path,
        "shipped_spdx_sha256": digest(spdx_raw),
        "licenses": {
            "text_files": text_files,
            "declared": declared[0].get("licenseDeclared"),
            "complete": False,
        },
        "binary_version_declared": versions[0],
        "binary_accepted": False,
        "binary_sca_verified": False,
        "os_sca_verified": False,
        "embedded_libraries_complete": False,
        "gaps": [
            "El SPDX incluido con filesAnalyzed=false no acredita cobertura del binario ni de sus dependencias",
            "El PURL genérico de Valkey no corresponde a un APK ni a una dependencia de lenguaje compatible",
            "Los indicadores de Lua/jemalloc son candidatos derivados de fuentes originales modificadas",
            "Están pendientes la acreditación de fuente y construcción y las identidades completas de las dependencias embebidas",
            "Las declaraciones de licencia no sustituyen los avisos completos ausentes",
            "La estructura ELF estática no demuestra el UID efectivo, TLS ni la ejecución de comandos",
        ],
    }


def file_catalog(parsed):
    result = {}
    for name, row in sorted(parsed["files"].items()):
        result[name] = {key: value for key, value in row.items() if key not in {"data", "elf"}}
        result[name]["origin"] = parsed["origins"][name]
    return result


def os_sbom(value):
    version = value["os"]["identity"]["VERSION_ID"]
    components = [
        {"type": "operating-system", "name": "alpine", "version": version, "bom-ref": "os:alpine"}
    ]
    for row in value["os"]["packages"]:
        purl = (
            "pkg:apk/alpine/"
            + quote(row["name"], safe="")
            + "@"
            + quote(row["version"], safe=".-")
            + "?arch="
            + row["architecture"]
            + "&distro="
            + version
        )
        components.append(
            {
                "type": "library",
                "name": row["name"],
                "version": row["version"],
                "purl": purl,
                "bom-ref": purl,
                "licenses": [{"license": {"name": row["license_declared"]}}],
                "properties": [
                    {"name": "aquasecurity:trivy:SrcName", "value": row["source_name"]},
                    {"name": "aquasecurity:trivy:SrcVersion", "value": row["source_version"]},
                    {"name": "aquasecurity:trivy:Arch", "value": row["architecture"]},
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
                    {
                        "type": "application",
                        "name": "agency-valkey-static-inventory",
                        "version": "1",
                    }
                ]
            }
        },
        "components": components,
    }
