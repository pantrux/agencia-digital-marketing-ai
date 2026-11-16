"""Gate físico de imagen Python como datos; no ejecuta archivos del runtime."""

import ast
import copy
import gzip
import hashlib
import io
import json
import posixpath
import re
import struct
import tarfile

PARSER_SHA = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"
BASE_MANIFEST = "sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72"
BASE_CONFIG = "sha256:b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7"
DOCKERFILE_SHA = "79f19aad430c5e02e820ee89001a5999ffadb4bf3f6f2d49f71824836e2d0448"
MAX_ARCHIVE = 256 * 1024**2
MAX_LAYER = 128 * 1024**2
MAX_EXPANDED = 512 * 1024**2
MAX_MEMBER = 32 * 1024**2
MAX_NODES = 30000
HEX = re.compile(r"[a-f0-9]{64}")


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def decode(raw):
    def pairs(items):
        result = {}
        for name, value in items:
            require(name not in result, "JSON duplicado.")
            result[name] = value
        return result

    return json.loads(
        raw.decode("utf-8", "strict"),
        object_pairs_hook=pairs,
        parse_constant=lambda _: require(False, "JSON no finito."),
    )


def load_parser(raw):
    """Selecciona bytes AST exactos y todos sus globals; no importa operaciones PG."""
    require(digest(raw) == PARSER_SHA, "Parser OCI/ELF fuera del pin.")
    functions = {
        "digest",
        "part",
        "elf_info",
        "file_entry",
        "json_object",
        "safe_name",
        "apply_layer",
        "layer_bytes",
        "read_oci",
    }
    constants = {"MAX_FILE", "MAX_ARCHIVE"}
    tree = ast.parse(raw.decode("utf-8", "strict"))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in functions
        or isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in constants
    ]
    require(
        {node.name for node in nodes if isinstance(node, ast.FunctionDef)} == functions
        and len(nodes) == len(functions) + len(constants),
        "AST incompleto/ambiguo.",
    )
    scope = {"hashlib": hashlib, "json": json, "posixpath": posixpath, "struct": struct}
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), "<parser-generico-fijado>", "exec"), scope
    )
    require(
        scope["MAX_FILE"] == 512 * 1024**2 and scope["MAX_ARCHIVE"] == 768 * 1024**2,
        "Límites originales diferentes.",
    )
    return scope


def safe_path(name):
    require(
        isinstance(name, str)
        and 0 < len(name.encode("utf-8", "strict")) <= 4096
        and not name.startswith("/")
        and "\\" not in name
        and ":" not in name
        and "\0" not in name
        and all(part not in ("", ".", "..") for part in name.split("/")),
        "Ruta no canónica.",
    )
    return name


def tar_rows(raw, *, outer=False):
    require(
        type(raw) is bytes and 1024 <= len(raw) <= MAX_ARCHIVE and len(raw) % 512 == 0,
        "TAR fuera del límite.",
    )
    rows, seen, folded, end = [], set(), set(), 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            end = max(end, member.offset_data + (member.size + 511) // 512 * 512)
            if member.isdir() and member.name.rstrip("/") in ("", "."):
                continue
            name = member.name.rstrip("/")
            while name.startswith("./"):
                name = name[2:]
            safe_path(name)
            require(
                name not in seen and name.casefold() not in folded and len(seen) < MAX_NODES,
                "TAR duplicado/case collision.",
            )
            seen.add(name)
            folded.add(name.casefold())
            require(
                member.sparse is None
                and 0 <= member.size <= (MAX_ARCHIVE if outer else MAX_MEMBER),
                "Miembro TAR excesivo/sparse.",
            )
            require(
                member.isfile()
                or member.isdir()
                or not outer
                and (member.issym() or member.islnk()),
                "Tipo TAR no admitido.",
            )
            body = None
            if member.isfile():
                stream = archive.extractfile(member)
                require(stream is not None, "TAR sin cuerpo.")
                with stream:
                    body = stream.read(member.size + 1)
                require(len(body) == member.size, "TAR truncado.")
            rows.append((name, member, body))
    require(
        len(raw) - end >= 1024 and not any(raw[end:]), "TAR sin terminador o con bytes ocultos."
    )
    return rows


def read_oci(raw, expected_manifest, parser):
    require(type(raw) is bytes and len(raw) <= MAX_ARCHIVE, "OCI excesivo.")
    blobs, documents = {}, {}
    for name, member, body in tar_rows(raw, outer=True):
        if member.isdir():
            require(name in {"blobs", "blobs/sha256"}, "Directorio OCI ajeno.")
        elif re.fullmatch(r"blobs/sha256/[a-f0-9]{64}", name):
            require(digest(body) == name.rsplit("/", 1)[1], "SHA blob OCI distinto.")
            blobs["sha256:" + digest(body)] = body
        else:
            require(
                name in {"oci-layout", "index.json", "manifest.json"} and len(body) <= 1024**2,
                "Documento OCI ajeno.",
            )
            documents[name] = decode(body)
    require(documents.get("oci-layout") == {"imageLayoutVersion": "1.0.0"}, "Layout OCI distinto.")
    descriptors = documents.get("index.json", {}).get("manifests", [])
    require(
        len(descriptors) == 1 and descriptors[0]["digest"] == expected_manifest,
        "Manifiesto OCI distinto.",
    )
    manifest = decode(blobs[expected_manifest])
    require(
        type(manifest.get("schemaVersion")) is int and manifest["schemaVersion"] == 2,
        "Schema manifiesto OCI inválido.",
    )
    config = decode(blobs[manifest["config"]["digest"]])
    layers, diffids = manifest["layers"], config["rootfs"]["diff_ids"]
    require(1 <= len(layers) <= 32 and len(layers) == len(diffids), "Capas inconsistentes.")
    expanded, nodes, payloads = 0, 0, {}
    for row, diffid in zip(layers, diffids, strict=True):
        blob = blobs[row["digest"]]
        require(len(blob) == row["size"], "Tamaño capa distinto.")
        if row["mediaType"] in {
            "application/vnd.oci.image.layer.v1.tar+gzip",
            "application/vnd.docker.image.rootfs.diff.tar.gzip",
        }:
            with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
                plain = stream.read(MAX_LAYER + 1)
        else:
            require(
                row["mediaType"] == "application/vnd.oci.image.layer.v1.tar",
                "Compresión no admitida.",
            )
            plain = blob
        expanded += len(plain)
        require(
            len(plain) <= MAX_LAYER
            and expanded <= MAX_EXPANDED
            and diffid == "sha256:" + digest(plain),
            "DiffID/límite expandido distinto.",
        )
        rows = tar_rows(plain)
        nodes += len(rows)
        require(nodes <= MAX_NODES, "Demasiados nodos OCI.")
        for name, member, body in rows:
            if member.isfile() and body.startswith(b"\x7fELF"):
                payloads[name] = body
    # El parser original revalida descriptores, alcance completo y plataforma.
    result = parser["read_oci"](raw, expected_manifest[7:])
    for name, row in result["files"].items():
        if row.get("elf"):
            require(
                name in payloads and digest(payloads[name]) == row["sha256"],
                "ELF sin cuerpo físico correlacionado.",
            )
            row["elf"]["interpreter"] = program_interpreter(payloads[name])
    return result


def program_interpreter(raw):
    require(len(raw) >= 64 and raw[:7] == b"\x7fELF\x02\x01\x01", "ELF64 inválido.")
    offset = struct.unpack_from("<Q", raw, 32)[0]
    width, count = struct.unpack_from("<HH", raw, 54)
    require(
        count <= 256 and (not count or width == 56 and offset + width * count <= len(raw)),
        "Program headers inválidos.",
    )
    values = []
    for index in range(count):
        kind, _flags, start, _va, _pa, size, _mem, _align = struct.unpack_from(
            "<IIQQQQQQ", raw, offset + index * width
        )
        if kind == 3:
            require(1 < size <= 4096 and start + size <= len(raw), "PT_INTERP fuera de rango.")
            value = raw[start : start + size]
            require(value.endswith(b"\0") and b"\0" not in value[:-1], "PT_INTERP ambiguo.")
            values.append(value[:-1].decode("utf-8", "strict"))
    require(len(values) <= 1, "PT_INTERP duplicado.")
    return values[0] if values else None


def derive_context(raw):
    rows = tar_rows(raw)
    original = next((body for name, _node, body in rows if name == "Dockerfile"), None)
    require(
        original is not None and digest(original) == DOCKERFILE_SHA,
        "Dockerfile original fuera del pin.",
    )
    source = ("FROM cgr.dev/chainguard/wolfi-base@" + BASE_MANIFEST + "\n").encode()
    require(original.count(source) == 1, "FROM ambiguo.")
    modified = original.replace(source, b"FROM pythonbase\n")
    output, payloads = io.BytesIO(), {}
    with tarfile.open(fileobj=output, mode="w:", format=tarfile.PAX_FORMAT) as archive:
        for name, node, body in rows:
            require(
                name == "Dockerfile" or name == "rootfs" or name.startswith("rootfs/"),
                "Miembro contexto ajeno.",
            )
            require(
                not posixpath.basename(name).startswith(".wh."), "Whiteout no permitido en COPY."
            )
            item = copy.copy(node)
            item.name = name
            if name == "Dockerfile":
                require(node.isfile(), "Dockerfile no regular.")
                body = modified
                item.size = len(body)
            else:
                payloads[name] = {
                    "kind": "file"
                    if node.isfile()
                    else "dir"
                    if node.isdir()
                    else "symlink"
                    if node.issym()
                    else "hardlink",
                    "sha256": digest(body) if body is not None else None,
                    "target": node.linkname,
                    "mode": node.mode,
                    "uid": node.uid,
                    "gid": node.gid,
                }
            archive.addfile(item, io.BytesIO(body) if body is not None else None)
    derived = output.getvalue()
    return derived, {
        "original_sha256": digest(raw),
        "derived_sha256": digest(derived),
        "dockerfile_original_sha256": digest(original),
        "dockerfile_derived_sha256": digest(modified),
        "rootfs_catalog_sha256": digest(canonical(payloads)),
        "only_from_changed": True,
    }


def observed_build_ids(metadata, iid, log):
    values = {"iidfile": iid}
    if isinstance(metadata, dict):
        values.update(
            manifest=metadata.get("containerimage.digest"),
            config=metadata.get("containerimage.config.digest"),
        )
    result = {
        key: value
        for key, value in values.items()
        if isinstance(value, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value)
    }
    configs = (
        re.findall(r"exporting config (sha256:[a-f0-9]{64}) done", log)
        if isinstance(log, str)
        else []
    )
    if len(configs) == 1:
        if "config" not in result:
            result["config"] = configs[0]
        elif result["config"] != configs[0]:
            result["log_config"] = configs[0]
    elif configs:
        result["log_config_candidates"] = sorted(set(configs))
    return result


def build_identity(metadata, iid, log, *, inspection=None, final=None):
    """Dos contratos explícitos BuildKit; el config ausente procede sólo de log único."""
    known = observed_build_ids(metadata, iid, log)
    try:
        require(isinstance(metadata, dict) and isinstance(log, str), "Metadata/log inválidos.")
        manifest = metadata.get("containerimage.digest")
        require(
            isinstance(manifest, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", manifest),
            "Manifiesto metadata inválido.",
        )
        explicit = "containerimage.config.digest" in metadata
        configs = re.findall(r"exporting config (sha256:[a-f0-9]{64}) done", log)
        manifests = re.findall(r"exporting manifest (sha256:[a-f0-9]{64}) done", log)
        require(
            len(configs) == 1 and manifests == [manifest],
            "Log no acredita un único config/manifiesto.",
        )
        config = configs[0]
        require(
            (metadata["containerimage.config.digest"] == config and iid == config)
            if explicit
            else iid == manifest,
            "IID no corresponde a la rama explícita de metadata.",
        )
        require((inspection is None) == (final is None), "Inspect/OCI deben verificarse juntos.")
        if final is not None:
            require(
                final.get("manifest_digest") == manifest
                and final.get("config_digest") == config
                and inspection.get("Id") == manifest,
                "OCI/inspect final difieren del build.",
            )
            descriptor = inspection.get("Descriptor") or {}
            annotations = descriptor.get("annotations") or {}
            require(
                descriptor.get("digest", manifest) == manifest
                and annotations.get("config.digest", config) == config,
                "Descriptor inspect no correlaciona config/manifiesto.",
            )
            require(
                inspection.get("Architecture") == "amd64"
                and inspection.get("Os") == "linux"
                and inspection.get("Config", {}).get("User") == "1000:1000",
                "Inspect final plataforma/UID ajenos.",
            )
            for key in ("User", "WorkingDir", "Entrypoint", "Cmd", "Env"):
                require(
                    inspection["Config"].get(key) == final["config"]["config"].get(key),
                    "Inspect/OCI config difiere: " + key,
                )
        return {
            "manifest": manifest,
            "config": config,
            "iidfile": iid,
            "config_metadata_present": explicit,
            "iid_interpretation": "config" if explicit else "manifest-fallback",
            "final_oci_inspect_verified": final is not None,
        }
    except (ValueError, TypeError, AttributeError, KeyError) as failure:
        error = ValueError("BuildKit no correlaciona IID/config/manifiesto: " + str(failure))
        error.identities = known
        raise error from failure


def apk_records(raw):
    result = {}
    for record in raw.decode("utf-8", "strict").strip().split("\n\n"):
        fields = {}
        for line in record.splitlines():
            if line[:2] in {"P:", "V:", "A:"}:
                require(line[0] not in fields, "Campo APK repetido.")
                fields[line[0]] = line[2:]
        require(
            set(fields) == {"P", "V", "A"}
            and fields["P"] not in result
            and fields["A"] in {"x86_64", "noarch"},
            "DB APK ambigua.",
        )
        result[fields["P"]] = {"P": fields["P"], "V": fields["V"], "A": fields["A"]}
    require(len(result) == 19, "DB base debe conservar exactamente19 APK.")
    return result


def expected_config():
    return {
        "User": "1000:1000",
        "WorkingDir": "/",
        "Env": [
            "LD_LIBRARY_PATH=/usr/lib64:/usr/lib",
            "PYTHONDONTWRITEBYTECODE=1",
            "PYTHONNOUSERSITE=1",
        ],
        "Cmd": [
            "/usr/bin/python3.12",
            "-I",
            "-S",
            "-B",
            "-c",
            "raise SystemExit('Runtime base: falta una operación explícita')",
        ],
    }


def validate_config(final, base_config=None):
    require(
        final.get("architecture") == "amd64" and final.get("os") == "linux",
        "Config plataforma distinta.",
    )
    actual = final["config"]
    expected = expected_config()
    for key in ("User", "WorkingDir", "Cmd"):
        require(actual.get(key) == expected[key], "Config final distinta: " + key)
    require(
        not any(
            actual.get(key)
            for key in ("Entrypoint", "OnBuild", "Volumes", "Healthcheck", "ExposedPorts")
        ),
        "Config activa/heredada no prevista.",
    )
    env = actual.get("Env", [])
    require(
        type(env) is list and all(isinstance(value, str) and "=" in value for value in env),
        "Env OCI inválida.",
    )
    values = dict(value.split("=", 1) for value in env)
    require(len(values) == len(env), "Env duplicada.")
    inherited = dict(
        value.split("=", 1) for value in (base_config or {}).get("config", {}).get("Env", [])
    )
    inherited.update(value.split("=", 1) for value in expected["Env"])
    require(
        values == inherited
        and not any(name in values for name in ("LD_PRELOAD", "PYTHONPATH", "PYTHONHOME")),
        "Entorno final fuera de clausura.",
    )


def follow(files, name):
    parts, seen = safe_path(name).split("/"), set()
    for _ in range(40):
        full = "/".join(parts)
        require(full not in seen, "Ciclo de alias.")
        seen.add(full)
        for index in range(len(parts)):
            path = "/".join(parts[: index + 1])
            node = files.get(path)
            if node and node["kind"] in ("symlink", "hardlink"):
                target = node["target"]
                require(
                    isinstance(target, str) and "\\" not in target and "\0" not in target,
                    "Alias inválido.",
                )
                start = [] if target.startswith("/") else parts[:index]
                for part in [*target.split("/"), *parts[index + 1 :]]:
                    if part in ("", "."):
                        continue
                    if part == "..":
                        require(start, "Alias escapa raíz.")
                        start.pop()
                    else:
                        start.append(part)
                parts = start
                break
        else:
            require(
                full in files and files[full]["kind"] == "file", "Proveedor/alias ausente: " + full
            )
            return full
    raise ValueError("Demasiados alias.")


def abi_contexts(files, roots, parser):
    del parser  # Los ELF ya están analizados por el parser fijado.
    python = "usr/bin/python3.12"
    libpython = "usr/lib/libpython3.12.so.1.0"
    evidence = {}
    for root in roots:
        pending, loaded, edges = [root, python, libpython], {}, {}
        interpreter = files[follow(files, python)]["elf"].get("interpreter")
        if interpreter:
            require(
                interpreter == "/lib64/ld-linux-x86-64.so.2",
                "PT_INTERP no es el cargador glibc previsto.",
            )
            pending.append(interpreter.lstrip("/"))
        while pending:
            path = follow(files, pending.pop())
            if path in loaded:
                continue
            info = files[path].get("elf")
            require(info is not None, "Proveedor requerido no es ELF.")
            loaded[path] = info
            resolved = {}
            for name in info["needed"]:
                require("/" not in name and "\0" not in name, "DT_NEEDED no es SONAME simple.")
                paths = []
                for value in (info.get("rpath") or "").split(":"):
                    if value:
                        paths.append(value)
                paths.extend(("/usr/lib64", "/usr/lib"))
                paths.extend(value for value in (info.get("runpath") or "").split(":") if value)
                paths.extend(("/lib64", "/lib"))
                choices = []
                for directory in paths:
                    directory = directory.replace(
                        "${ORIGIN}", "/" + posixpath.dirname(path)
                    ).replace("$ORIGIN", "/" + posixpath.dirname(path))
                    require(
                        directory.startswith("/")
                        and "$" not in directory
                        and ".." not in directory.split("/"),
                        "Ruta cargador desconocida.",
                    )
                    try:
                        candidate = follow(files, directory.lstrip("/") + "/" + name)
                    except ValueError:
                        continue
                    choices.append(candidate)
                require(choices, "DT_NEEDED sin proveedor: " + name)
                provider = choices[0]
                require(files[provider].get("elf") is not None, "Proveedor no ELF.")
                require(
                    files[provider]["elf"].get("soname") in (None, name),
                    "SONAME proveedor distinto.",
                )
                resolved[name] = provider
                pending.append(provider)
            edges[path] = resolved
        exports = set().union(*(set(row["defined"]) for row in loaded.values()))
        for path, info in loaded.items():
            require(
                not set(info["undefined"]) - exports,
                "Símbolos fuertes sin proveedor en contexto: " + path,
            )
            for soname, versions in info["version_requirements"].items():
                provider = edges[path].get(soname)
                require(
                    provider is not None
                    and set(versions) <= set(loaded[provider]["version_definitions"]),
                    "Versión ABI sin proveedor físico.",
                )
        evidence[root] = {
            "loaded": {path: files[path]["sha256"] for path in sorted(loaded)},
            "needed": edges,
        }
    return {
        "verified": True,
        "contexts": evidence,
        "scope": "DT_NEEDED/símbolos fuertes/nombres de versión por contexto; no ejecuta dlopen ni prueba bindings de símbolos versionados individualmente",
    }


def catalog_identity(row):
    result = {key: row[key] for key in ("kind", "mode", "uid", "gid")}
    if row["kind"] == "file":
        result.update(sha256=row["sha256"], bytes=row.get("bytes", row.get("size")))
    elif row["kind"] in ("symlink", "hardlink"):
        result["target"] = row["target"]
    return result


def verify_image(base, final, expected, parser):
    require(
        base["manifest_digest"] == BASE_MANIFEST and base["config_digest"] == BASE_CONFIG,
        "Base OCI no autorizada.",
    )
    basefiles, files = base["files"], final["files"]
    catalog = expected["files"]
    require(
        len(expected["manual_materials"]) == 14
        and len({row["name"] for row in expected["manual_materials"]}) == 14,
        "Clausura manual no biunívoca14.",
    )
    db = basefiles["lib/apk/db/installed"]["data"]
    declared = {
        name: {key: row[key] for key in ("P", "V", "A")}
        for name, row in expected["base"]["packages"].items()
    }
    require(apk_records(db) == declared, "DB base no corresponde a los19 declarados.")
    for name, old in basefiles.items():
        require(
            name in files and catalog_identity(files[name]) == catalog_identity(old),
            "La imagen cambia archivos base: " + name,
        )
        if name in catalog:
            require(
                old["kind"] == "directory" and catalog[name]["kind"] == "dir",
                "COPY colisiona con archivo base: " + name,
            )
    # Los directorios padres implícitos pueden ser introducidos por COPY.
    implicit = set()
    for name in catalog:
        parent = posixpath.dirname(name)
        while parent:
            implicit.add(parent)
            parent = posixpath.dirname(parent)
    require(
        not set(files) - set(basefiles) - set(catalog) - implicit,
        "Archivos finales fuera del contexto.",
    )
    for name, wanted in catalog.items():
        require(name in files, "Falta payload físico: " + name)
        actual = catalog_identity(files[name])
        target = catalog_identity(
            {**wanted, "kind": "directory" if wanted["kind"] == "dir" else wanted["kind"]}
        )
        if wanted["kind"] == "hardlink":
            # OCI puede representar el hardlink como archivo regular; cotejar destino y bytes.
            resolved = follow(files, name)
            source = wanted.get("context_hardlink_target", "").removeprefix("rootfs/")
            require(
                source
                and files[resolved]["sha256"] == files[follow(files, source)]["sha256"]
                and (
                    files[name]["kind"] == "file"
                    or files[name]["kind"] == "hardlink"
                    and resolved == follow(files, source)
                )
                and all(files[name][key] == wanted[key] for key in ("uid", "gid", "mode")),
                "Hardlink final ajeno.",
            )
        else:
            require(actual == target, "Payload físico distinto: " + name)
    alias_observations = {}
    for name, target in expected.get("unresolved_aliases", {}).items():
        require(
            files.get(name, {}).get("kind") == "symlink" and files[name]["target"] == target,
            "Alias pendiente cambió.",
        )
        try:
            alias_observations[name] = {"target": target, "resolved": follow(files, name)}
        except ValueError:
            # El alias auténtico pendiente no se repara ni se presenta como resuelto.
            require(
                name == "usr/lib/terminfo" and target == "../share/terminfo",
                "Alias no clasificado.",
            )
            alias_observations[name] = {
                "target": target,
                "resolved": None,
                "root_classification_required": True,
            }
    python = expected["python"]
    require(
        files[follow(files, python["executable"])]["sha256"] == python["executable_sha256"]
        and files[follow(files, python["libpython"])]["sha256"] == python["libpython_sha256"],
        "Python/libpython difieren del pin.",
    )
    roots = [name for name in catalog if files[name].get("elf")]
    if "elf" in expected:
        require(set(roots) == set(expected["elf"]), "Conteo/paths ELF físicos distintos.")
        for name in roots:
            require(
                files[name]["elf"].get("interpreter") == expected["elf"][name].get("pt_interp"),
                "PT_INTERP difiere del inventario preparado.",
            )
    abi = abi_contexts(files, roots, parser)
    validate_config(final["config"], base.get("config"))
    return {
        "schema_version": 1,
        "physical_files_verified": True,
        "installed_apk_count": 19,
        "manual_component_count": 14,
        "coverage_components": 33,
        "base_manifest": base["manifest_digest"],
        "base_config": base["config_digest"],
        "final_manifest": final["manifest_digest"],
        "final_config": final["config_digest"],
        "base_db_sha256": digest(db),
        "database_unchanged": True,
        "catalog_sha256": digest(canonical(catalog)),
        "abi": abi,
        "alias_observations": alias_observations,
        "root_release_blockers": [
            "Root debe clasificar el alias físico pendiente: " + name
            for name, row in alias_observations.items()
            if row.get("root_classification_required")
        ],
        "runtime_approved": False,
        "sca_approved": False,
        "binary_executed": False,
        "gaps": [
            "No prueba importaciones ni resolución dinámica posterior a dlopen.",
            "SCA de base/manuales/vendors y release específico de Root son posteriores.",
        ],
    }
