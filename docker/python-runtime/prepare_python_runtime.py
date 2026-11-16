"""Prepara datos de runtime Python; nunca construye imágenes ni ejecuta payloads."""

import argparse
import hashlib
import io
import json
import math
import os
import re
import struct
import sys
import tarfile
import types
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
OWNER = "agencia-digital-marketing-ai:python-runtime:v1"
OUTPUT_RELATIVE = ".tools/backup-source-build/python-runtime-v1"
SPEC_PATH = "docker/python-runtime/python-runtime-inputs.json"
DOCKERFILE_PATH = "docker/python-runtime/Dockerfile.runtime-python"
RELOCATOR_SHA = "b7b14e650a0eb900f585591077680fa91a335c0224057ad99cb24e68afd771f6"
PACKAGER_SHA = "831a28043da1de203bae7845244e90efd548da4cd75f707d1358d33288b2b9d9"
VERIFIER_SHA = "b00b1a1a1822b13ed145128209664a15f3efa781b39cd850d0d63bcc0666192b"
ELF_SHA = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"
BASE_MANIFEST = "sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72"
BASE_CONFIG = "sha256:b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7"
INDEX_SHA = "9b27ab69add838665af44c9aa304a0dd910af90f2cd9e191e52e0b585a3ce2fd"
PUBLIC_SIGNING_SHA256 = "f0031424cf46f7db780ce63a45f0fd6aa6f85f601e6bb3b7a91fe3d4d5b7d2cc"
MANIFEST_SHA = "1207f5840f3d663775ee54fd377688ef00081681a1a50952df61c5d1ee01cd3a"
BASE_REPORT_SHA = "419f999d5268db32a90b201ec9ec3a8dfba03fefab737f9cc4ac711c704da5fe"
PYTHON_SHA = "54bb87946dd0d8231507c0cef7a5745443a97411c406bf6f010f3ec75780a75b"
LIBPYTHON_SHA = "ad918e7753f89b9c256a6c40a17e48d080583cd25577d8634d737ab9a1e960b8"
PACKAGE_NAMES = {
    "gdbm",
    "libbz2-1",
    "libexpat1",
    "libffi",
    "libstdc++",
    "libuuid",
    "mpdecimal",
    "ncurses",
    "ncurses-terminfo-base",
    "py3-pip-wheel",
    "python-3.12-base",
    "readline",
    "sqlite-libs",
    "xz",
}
KIT_RELATIVE = ".tools/kitsu-zou-opencv-sdk-v1/kit-755ea032b52a767f/data"
REFERENCE_PATHS = {
    "manifest": ".tools/backup-source-build/opencv-relocation-inputs-v1/manifest-approved.json",
    "index": KIT_RELATIVE + "/wolfi-APKINDEX.tar.gz",
    "key": KIT_RELATIVE + "/wolfi-signing.rsa.pub",
    "base_report": ".tools/evidence/phase4-wolfi-base-trivy.json",
    "packager": "docker/kitsu-zou/package_opencv_minimal.py",
    "apk_verifier": "docker/postgres-immich/native_inputs.py",
    "elf_parser": "docker/postgres-immich/final_image_remote.py",
    "relocator": "docker/kitsu-zou/prepare_opencv_relocated_wheel.py",
}
MAX_CONTEXT = 128 * 1024**2


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def json_object(raw):
    def unique(items):
        result = {}
        for key, value in items:
            require(key not in result, "JSON duplicado.")
            result[key] = value
        return result

    value = json.loads(
        raw.decode("utf-8", "strict"),
        object_pairs_hook=unique,
        parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("JSON no finito.")),
    )
    require(type(value) is dict, "Se exige objeto JSON.")
    return value


def safe_path(value):
    require(
        isinstance(value, str)
        and 0 < len(value) <= 4096
        and not value.startswith("/")
        and "\\" not in value
        and ":" not in value
        and "\0" not in value,
        "Ruta relativa inválida.",
    )
    require(
        all(
            part not in ("", ".", "..") and not part.startswith(".wh.") for part in value.split("/")
        ),
        "Ruta ambigua/traversal/whiteout.",
    )
    value.encode("utf-8", "strict")
    return value


def normalized_target(parent, target):
    require(
        isinstance(target, str)
        and 0 < len(target) <= 4096
        and "\\" not in target
        and "\0" not in target
        and ":" not in target,
        "Enlace virtual inválido.",
    )
    target.encode("utf-8", "strict")
    parts = [] if target.startswith("/") else parent.split("/") if parent else []
    for item in target.split("/"):
        if item in ("", "."):
            continue
        if item == "..":
            require(parts, "Enlace fuera de raíz.")
            parts.pop()
        else:
            parts.append(item)
    require(parts, "Enlace sin destino.")
    return safe_path("/".join(parts))


def provides(record):
    result = {record["P"]: record["V"]}
    for token in record.get("p", "").split():
        name, separator, version = token.partition("=")
        require(name and not any(x in name for x in "<>~!"), "Provide no cubierto.")
        require(name not in result or result[name] == version, "Provide ambiguo.")
        result[name] = version if separator else ""
    return result


def dependency(token):
    require(
        not token.startswith("!") and not any(x in token for x in "<>~"),
        "Constraint/conflicto no cubierto en payload manual.",
    )
    name, operator, version = token.partition("=")
    require(name and not (operator and not version), "Dependencia inválida.")
    return name, version if operator else None


def close_dependencies(base, managed, seed):
    selected = set()
    used = set()
    edges = []
    pending = [seed]
    base_provides = {name: provides(row) for name, row in base.items()}
    manual_provides = {name: provides(row) for name, row in managed.items()}
    while pending:
        name = pending.pop()
        require(name in managed, "Payload requerido ausente.")
        if name in selected:
            continue
        selected.add(name)
        for token in managed[name].get("D", "").split():
            target, version = dependency(token)
            choices = [
                key
                for key, values in base_provides.items()
                if target in values and (version is None or values[target] == version)
            ]
            scope = "base-installed"
            if not choices:
                choices = [
                    key
                    for key, values in manual_provides.items()
                    if target in values and (version is None or values[target] == version)
                ]
                scope = "manual-copy"
            require(len(choices) == 1, "Proveedor ausente o ambiguo para: " + token)
            resolved = choices[0]
            edges.append({"from": name, "requirement": token, "scope": scope, "to": resolved})
            if scope == "base-installed":
                used.add(resolved)
            else:
                pending.append(resolved)
    return {
        "selected": sorted(selected),
        "base_used": sorted(used),
        "edges": sorted(edges, key=lambda r: (r["from"], r["requirement"])),
    }


def payload_nodes(raw, package):
    require(
        0 < len(raw) <= 96 * 1024**2 and len(raw) % 512 == 0, "Payload TAR excesivo/desalineado."
    )
    nodes = {}
    folded = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            safe_path(name)
            require(
                name.casefold() not in folded and len(nodes) < 20000,
                "Payload duplicado/case-collision/excesivo.",
            )
            folded.add(name.casefold())
            require(
                member.uid == member.gid == 0 and math.isfinite(member.mtime) and member.mtime >= 0,
                "Propiedad o tiempo de payload no admitidos.",
            )
            require(
                not member.mode & 0o6000
                and not any(
                    "xattr" in key.lower() or "sparse" in key.lower() for key in member.pax_headers
                ),
                "Privilegios/xattr/sparse no admitidos.",
            )
            require(
                set(member.pax_headers)
                <= {"path", "linkpath", "mtime", "atime", "ctime", "APK-TOOLS.checksum.SHA1"},
                "Metadatos PAX no revisados.",
            )
            if member.isfile():
                kind = "file"
            elif member.isdir():
                kind = "dir"
            elif member.issym():
                kind = "symlink"
            elif member.islnk():
                kind = "hardlink"
            else:
                raise ValueError("Tipo especial de payload no admitido.")
            if kind not in ("symlink", "hardlink"):
                require(not member.mode & 0o022, "Payload modificable por grupo/otros.")
            row = {
                "kind": kind,
                "mode": member.mode & 0o777,
                "uid": member.uid,
                "gid": member.gid,
                "mtime": member.mtime,
                "packages": [package],
            }
            if "APK-TOOLS.checksum.SHA1" in member.pax_headers:
                checksum = member.pax_headers["APK-TOOLS.checksum.SHA1"]
                require(re.fullmatch(r"[a-f0-9]{40}", checksum), "Checksum PAX declarado inválido.")
                row["apk_tools_checksum_sha1_declared"] = checksum
            if kind == "file":
                require(0 <= member.size <= 16 * 1024**2, "Archivo de payload excesivo.")
                total += member.size
                require(total <= 96 * 1024**2, "Payload expandido excesivo.")
                with archive.extractfile(member) as stream:
                    data = stream.read(member.size + 1)
                require(len(data) == member.size, "Payload truncado.")
                row.update(data=data, sha256=digest(data), bytes=len(data))
            elif kind in ("symlink", "hardlink"):
                normalized_target(
                    name.rsplit("/", 1)[0] if "/" in name and kind == "symlink" else "",
                    member.linkname,
                )
                row["target"] = member.linkname
            nodes[name] = row
        end = archive.offset
        require(
            end <= len(raw) - 1024 and not any(raw[end:]),
            "Payload sin terminador o con trailer oculto.",
        )
    require(nodes, "Payload vacío.")
    return nodes


def resolve_link(nodes, name):
    pending = safe_path(name).split("/")
    resolved = []
    seen = set()
    while pending:
        part = pending.pop(0)
        path = "/".join([*resolved, part])
        node = nodes.get(path)
        if node is not None and node["kind"] in ("symlink", "hardlink"):
            require(path not in seen and len(seen) < 40, "Ciclo de enlaces.")
            seen.add(path)
            parent = "/".join(resolved) if node["kind"] == "symlink" else ""
            pending = normalized_target(parent, node["target"]).split("/") + pending
            resolved = []
        else:
            require(
                node is None or not pending or node["kind"] == "dir", "Archivo usado como padre."
            )
            resolved.append(part)
    path = "/".join(resolved)
    require(path in nodes and nodes[path]["kind"] == "file", "Enlace sin archivo regular conocido.")
    return path


def merge_payloads(payloads, *, pending_aliases=None):
    pending_aliases = pending_aliases or {}
    merged = {}
    folded = {}
    for nodes in payloads:
        for name, source in nodes.items():
            require(
                name.casefold() not in folded or folded[name.casefold()] == name,
                "Colisión de mayúsculas entre payloads.",
            )
            folded[name.casefold()] = name
            if name not in merged:
                merged[name] = {**source, "packages": list(source["packages"])}
                continue
            previous = merged[name]
            keys = ("kind", "mode", "uid", "gid")
            require(
                all(previous[k] == source[k] for k in keys),
                "Tipo/metadata de entrada compartida incompatible.",
            )
            if source["kind"] == "file":
                require(
                    previous["sha256"] == source["sha256"], "Dos archivos compiten por una ruta."
                )
            elif source["kind"] in ("symlink", "hardlink"):
                require(
                    previous["target"] == source["target"], "Dos enlaces compiten por una ruta."
                )
            previous.setdefault("source_mtimes", [previous["mtime"]]).append(source["mtime"])
            previous["mtime"] = (
                0 if source["kind"] == "dir" else min(previous["mtime"], source["mtime"])
            )
            previous["packages"] = sorted(set(previous["packages"] + source["packages"]))
    for name, row in merged.items():
        for index in range(1, len(name.split("/"))):
            parent = "/".join(name.split("/")[:index])
            require(
                parent not in merged or merged[parent]["kind"] == "dir",
                "Un padre del overlay es archivo/enlace.",
            )
        if name in pending_aliases:
            require(
                row["kind"] == "symlink"
                and row["target"] == pending_aliases[name]
                and normalized_target(name.rsplit("/", 1)[0], row["target"]) not in merged,
                "Alias pendiente distinto del dato auténtico.",
            )
            row["unresolved_base_alias"] = True
        elif row["kind"] in ("symlink", "hardlink"):
            resolve_link(merged, name)
    require(set(pending_aliases) <= set(merged), "Alias declarado ausente del payload.")
    return dict(sorted(merged.items()))


def validate_dockerfile(raw):
    lines = [
        line
        for line in raw.decode("utf-8", "strict").splitlines()
        if line and not line.startswith("#")
    ]
    require(
        lines
        == [
            "FROM cgr.dev/chainguard/wolfi-base@" + BASE_MANIFEST,
            "COPY --chown=0:0 rootfs/ /",
            "ENV LD_LIBRARY_PATH=/usr/lib64:/usr/lib",
            "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1",
            "USER 1000:1000",
            "WORKDIR /",
            'CMD ["/usr/bin/python3.12", "-I", "-S", "-B", "-c", "raise SystemExit(\'Runtime base: falta una operación explícita\')"]',
        ],
        "Dockerfile fuera del contrato sin RUN.",
    )


def validate_overlay_scope(nodes):
    for name in nodes:
        require(
            not any(
                name == prefix or name.startswith(prefix + "/")
                for prefix in ("lib/apk", "etc/apk", "usr/lib/apk")
            ),
            "El overlay intenta tocar el gestor/DB APK de base.",
        )
        if name.startswith(("usr/bin/", "bin/", "usr/sbin/", "sbin/")):
            leaf = name.rsplit("/", 1)[-1]
            require(
                not re.fullmatch(
                    r"(?:gcc|g\+\+|cc|c\+\+|cmake|ninja|make|ld|as|rustc|cargo|go)(?:-[0-9.]+)?",
                    leaf,
                ),
                "Toolchain en payload de runtime.",
            )


def context_bytes(dockerfile, nodes):
    require(len(nodes) <= 20000, "Contexto excesivo.")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:", format=tarfile.PAX_FORMAT) as archive:
        root = tarfile.TarInfo("rootfs")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        order = {"dir": 0, "file": 1, "symlink": 2, "hardlink": 3}
        for name, row in sorted(nodes.items(), key=lambda item: (order[item[1]["kind"]], item[0])):
            safe_path(name)
            item = tarfile.TarInfo("rootfs/" + name)
            item.mode = row["mode"]
            item.uid = row["uid"]
            item.gid = row["gid"]
            item.mtime = row["mtime"]
            if "apk_tools_checksum_sha1_declared" in row:
                item.pax_headers = {
                    "APK-TOOLS.checksum.SHA1": row["apk_tools_checksum_sha1_declared"]
                }
            if row["kind"] == "file":
                item.size = len(row["data"])
                archive.addfile(item, io.BytesIO(row["data"]))
            elif row["kind"] == "dir":
                item.type = tarfile.DIRTYPE
                archive.addfile(item)
            else:
                item.type = tarfile.SYMTYPE if row["kind"] == "symlink" else tarfile.LNKTYPE
                item.linkname = (
                    row["target"]
                    if row["kind"] == "symlink"
                    else "rootfs/" + resolve_link(nodes, name)
                )
                archive.addfile(item)
        item = tarfile.TarInfo("Dockerfile")
        item.mode = 0o644
        item.size = len(dockerfile)
        archive.addfile(item, io.BytesIO(dockerfile))
    raw = output.getvalue()
    require(len(raw) <= MAX_CONTEXT, "Contexto excede el límite total.")
    return raw


def component(record, manual=False, archive_sha=None):
    name, version = record["P"], record["V"]
    purl = (
        "pkg:apk/wolfi/"
        + quote(name, safe="")
        + "@"
        + quote(version, safe=".-")
        + "?arch="
        + record["A"]
        + "&distro=20230201"
    )
    result = {
        "type": "library",
        "bom-ref": purl,
        "name": name,
        "version": version,
        "purl": purl,
        "licenses": [{"license": {"name": record.get("L", "NOASSERTION")}}],
        "properties": [
            {
                "name": "agency:provenance",
                "value": "manual-authenticated-apk-payload-not-installed"
                if manual
                else "base-package-observed-in-physical-report",
            },
            {"name": "aquasecurity:trivy:SrcName", "value": record.get("o", name)},
            {"name": "aquasecurity:trivy:SrcVersion", "value": version},
            {"name": "aquasecurity:trivy:Arch", "value": record["A"]},
        ],
    }
    if archive_sha:
        result["hashes"] = [{"alg": "SHA-256", "content": archive_sha}]
    return result


def sbom_documents(base, manual):
    def document(rows, scope):
        return {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "version": 1,
            "metadata": {
                "properties": [
                    {"name": "agency:installation-state", "value": scope},
                    {"name": "agency:security-approved", "value": "false"},
                ]
            },
            "components": [
                {
                    "type": "operating-system",
                    "bom-ref": "os:wolfi",
                    "name": "wolfi",
                    "version": "20230201",
                },
                *rows,
            ],
        }

    return {
        "base": document(
            [component(row) for _, row in sorted(base.items())],
            "base-installed-observed-not-new-install",
        ),
        "manual": document(
            [component(row["record"], True, row["sha256"]) for row in manual],
            "manual-copy-not-apk-db-installed",
        ),
    }


def load_fixed(path, pin, name, reader=None):
    raw = reader(path, 256 * 1024) if reader else Path(path).read_bytes()
    require(len(raw) <= 256 * 1024 and digest(raw) == pin, "Fuente auxiliar distinta del pin.")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec"), vars(module))
    return module


def validate_spec(spec):
    require(
        set(spec)
        == {
            "schema_version",
            "owner",
            "output_relative",
            "base",
            "references",
            "manual_apks",
            "python",
            "load_paths",
            "scripts_executed",
            "sdk_executed",
            "image_built",
            "runtime_approved",
            "pending_aliases",
        },
        "Spec con campos distintos.",
    )
    require(
        spec["schema_version"] == 1
        and spec["owner"] == OWNER
        and spec["output_relative"] == OUTPUT_RELATIVE
        and all(
            spec[k] is False
            for k in ("scripts_executed", "sdk_executed", "image_built", "runtime_approved")
        ),
        "Identidad/alcance incorrectos.",
    )
    require(
        spec["base"]["manifest"] == BASE_MANIFEST
        and spec["base"]["config"] == BASE_CONFIG
        and len(spec["base"]["packages"]) == 19
        and spec["base"]["raw_apk_database_sha256"] is None
        and spec["base"]["physical_file_catalog_verified"] is False,
        "Base fuera del contrato.",
    )
    require(
        len(spec["manual_apks"]) == 14
        and {r["name"] for r in spec["manual_apks"]} == PACKAGE_NAMES
        and spec["load_paths"] == ["/usr/lib64", "/usr/lib"],
        "Clausura/rutas de carga incorrectas.",
    )
    require(
        spec["pending_aliases"] == {"usr/lib/terminfo": "../share/terminfo"},
        "Tabla de alias pendientes diferente.",
    )
    require(
        spec["python"]
        == {
            "package": "python-3.12-base",
            "version": "3.12.16",
            "package_version": "3.12.16-r0",
            "executable": "usr/bin/python3.12",
            "executable_sha256": PYTHON_SHA,
            "libpython": "usr/lib/libpython3.12.so.1.0",
            "libpython_sha256": LIBPYTHON_SHA,
        },
        "Identidad Python diferente.",
    )
    pins = {
        "manifest": MANIFEST_SHA,
        "index": INDEX_SHA,
        "key": PUBLIC_SIGNING_SHA256,
        "base_report": BASE_REPORT_SHA,
        "packager": PACKAGER_SHA,
        "apk_verifier": VERIFIER_SHA,
        "elf_parser": ELF_SHA,
        "relocator": RELOCATOR_SHA,
    }
    require(set(spec["references"]) == set(pins), "Referencias distintas.")
    for key, pin in pins.items():
        require(
            spec["references"][key]["sha256"] == pin
            and spec["references"][key]["path"] == REFERENCE_PATHS[key],
            "Referencia/ruta no fijada.",
        )
    for row in spec["manual_apks"]:
        require(
            row["record"]["P"] == row["name"]
            and row["record"]["V"] == row["version"]
            and row["archive"]["path"]
            == KIT_RELATIVE + "/apks/" + row["name"] + "-" + row["version"] + ".apk"
            and row["authentication_receipt"]["path"]
            == ".tools/zou-native-research-v1/opencv-inputs-v1/receipts/"
            + row["name"]
            + "-"
            + row["version"]
            + ".json",
            "Ruta/identidad APK fuera del cierre.",
        )
    for reference in [
        *spec["references"].values(),
        *[r[k] for r in spec["manual_apks"] for k in ("archive", "authentication_receipt")],
    ]:
        safe_path(reference["path"])
        require(
            set(reference) == {"path", "sha256", "bytes"}
            and type(reference["bytes"]) is int
            and 0 < reference["bytes"] <= 32 * 1024**2
            and re.fullmatch(r"[a-f0-9]{64}", reference["sha256"]),
            "Referencia de archivo inválida.",
        )


def node_catalog(nodes):
    result = {name: {k: v for k, v in row.items() if k != "data"} for name, row in nodes.items()}
    for name, row in nodes.items():
        if row["kind"] == "hardlink":
            result[name]["context_hardlink_target"] = "rootfs/" + resolve_link(nodes, name)
    return result


def interpreter(raw):
    require(raw[:7] == b"\x7fELF\x02\x01\x01" and len(raw) >= 64, "ELF64 requerido.")
    offset = struct.unpack_from("<Q", raw, 32)[0]
    width, count = struct.unpack_from("<HH", raw, 54)
    require(
        width == 56 and count <= 256 and offset + width * count <= len(raw),
        "Program headers excesivos/truncados.",
    )
    values = []
    for at in range(count):
        kind, _flags, start, _va, _pa, size, _mem, _align = struct.unpack_from(
            "<IIQQQQQQ", raw, offset + at * width
        )
        if kind == 3:
            require(1 < size <= 4096 and start + size <= len(raw), "PT_INTERP inválido.")
            value = raw[start : start + size]
            require(value.endswith(b"\0") and b"\0" not in value[:-1], "PT_INTERP ambiguo.")
            values.append(value[:-1].decode("utf-8", "strict"))
    require(len(values) <= 1, "PT_INTERP duplicado.")
    return values[0] if values else None


def prepare_data():
    relocator = load_fixed(
        ROOT / "docker/kitsu-zou/prepare_opencv_relocated_wheel.py",
        RELOCATOR_SHA,
        "python_runtime_fixed_reader",
    )
    read = relocator.read_regular
    spec_raw = read(ROOT / SPEC_PATH, 256 * 1024)
    spec = json_object(spec_raw)
    validate_spec(spec)

    def ref(row):
        raw = read(ROOT / row["path"], row["bytes"])
        require(
            len(raw) == row["bytes"] and digest(raw) == row["sha256"],
            "Entrada diferente del pin: " + row["path"],
        )
        return raw

    refs = {key: ref(row) for key, row in spec["references"].items()}
    packager = load_fixed(
        ROOT / spec["references"]["packager"]["path"],
        PACKAGER_SHA,
        "python_runtime_fixed_packager",
        read,
    )
    parser = load_fixed(
        ROOT / spec["references"]["elf_parser"]["path"], ELF_SHA, "python_runtime_fixed_elf", read
    )
    signed = packager.authenticate_index(
        refs["index"], refs["key"], index_sha256=INDEX_SHA, key_sha256=PUBLIC_SIGNING_SHA256
    )
    manifest = json_object(refs["manifest"])
    require(
        manifest["base"]["packages"] == spec["base"]["packages"],
        "Base no ligada al manifiesto aprobado.",
    )
    report = json_object(refs["base_report"])
    require(
        report["ArtifactName"] == spec["base"]["reference"]
        and report["Metadata"]["ImageID"] == BASE_CONFIG
        and report["Metadata"]["OS"] == {"Family": "wolfi", "Name": "20230201"},
        "Informe físico de otra base.",
    )
    rows = [
        r
        for result in report["Results"]
        if result["Class"] == "os-pkgs" and result["Type"] == "wolfi"
        for r in result.get("Packages", [])
    ]
    require(
        len(rows) == 19
        and {(r["Name"], r["Version"], r["Arch"]) for r in rows}
        == {(r["P"], r["V"], r["A"]) for r in spec["base"]["packages"].values()},
        "Informe base sin biyección19.",
    )
    declared = {r["name"]: r for r in manifest["packages"]}
    managed = {r["name"]: r["record"] for r in spec["manual_apks"]}
    closure = close_dependencies(spec["base"]["packages"], managed, "python-3.12-base")
    require(set(closure["selected"]) == PACKAGE_NAMES, "La clausura seleccionada cambió.")
    payloads = []
    materials = []
    for row in spec["manual_apks"]:
        name, version = row["name"], row["version"]
        require(
            name in declared
            and declared[name]["record"] == row["record"]
            and declared[name]["sha256"] == row["archive"]["sha256"]
            and dict(signed.records[(name, version)]) == row["record"]
            and not name.endswith("-dev"),
            "Paquete fuera del lock/índice.",
        )
        archive = ref(row["archive"])
        previous = json_object(ref(row["authentication_receipt"]))
        require(
            previous["sha256"] == digest(archive)
            and previous["record"] == row["record"]
            and previous["scripts_executed"] is False,
            "Recibo APK previo incoherente.",
        )
        verified = packager.verify_wolfi_apk(
            archive, signed, name, version, row["archive"]["sha256"]
        )
        verifier = packager._verifier()
        _control_compressed, control_tar, _payload_compressed = verifier.gzip_part(archive)
        control_files = verifier.tar_files(control_tar)
        license_lines = [
            line.removeprefix("license = ")
            for line in control_files[".PKGINFO"].decode("utf-8", "strict").splitlines()
            if line.startswith("license = ")
        ]
        require(
            license_lines == [row["record"]["L"]], "Licencia PKGINFO distinta del índice firmado."
        )
        require(
            digest(verified.payload) == previous["payload_tar_sha256"],
            "Payload difiere del recibo autenticado.",
        )
        payload = payload_nodes(verified.payload, name)
        payloads.append(payload)
        materials.append(
            {
                "name": name,
                "version": version,
                "record": row["record"],
                "sha256": digest(archive),
                "archive": row["archive"],
                "prior_receipt": row["authentication_receipt"],
                "payload_tar_sha256": digest(verified.payload),
                "control_metadata_members": list(verified.scripts),
                "scripts_executed": False,
                "individual_apk_signature": False,
                "authentication": "signed-index-rsa256/control-q1/payload-sha256",
                "payload_complete": True,
                "apk_db_installed": False,
                "pkginfo_license": license_lines[0],
                "control_metadata": {
                    key: {"bytes": len(value), "sha256": digest(value)}
                    for key, value in control_files.items()
                },
            }
        )
    nodes = merge_payloads(payloads, pending_aliases=spec["pending_aliases"])
    validate_overlay_scope(nodes)
    require(
        digest(nodes[spec["python"]["executable"]]["data"]) == PYTHON_SHA
        and digest(nodes[spec["python"]["libpython"]]["data"]) == LIBPYTHON_SHA,
        "Bytes Python/libpython diferentes.",
    )
    elf = {}
    for name, row in nodes.items():
        if row["kind"] == "file" and row["data"].startswith(b"\x7fELF"):
            info = parser.elf_info(row["data"])
            elf[name] = {
                "sha256": row["sha256"],
                "bytes": row["bytes"],
                "packages": row["packages"],
                "pt_interp": interpreter(row["data"]),
                **{
                    k: info[k]
                    for k in (
                        "soname",
                        "needed",
                        "runpath",
                        "rpath",
                        "version_requirements",
                        "version_definitions",
                    )
                },
                "symbols": {
                    k: {"count": len(info[k]), "sha256": digest(canonical(info[k]))}
                    for k in ("defined", "undefined", "weak")
                },
            }
    require(
        elf[spec["python"]["libpython"]]["soname"] == "libpython3.12.so.1.0",
        "SONAME libpython incorrecto.",
    )
    dockerfile = read(ROOT / DOCKERFILE_PATH, 8192)
    validate_dockerfile(dockerfile)
    context = context_bytes(dockerfile, nodes)
    catalog = node_catalog(nodes)
    stdlib = {name: row for name, row in catalog.items() if name.startswith("usr/lib/python3.12/")}
    bom = sbom_documents(spec["base"]["packages"], materials)
    inventory = {
        "schema_version": 1,
        "owner": OWNER,
        "base": spec["base"],
        "base_observation": spec["references"]["base_report"],
        "manual_materials": materials,
        "dependency_closure": closure,
        "files": catalog,
        "elf": elf,
        "python": spec["python"],
        "stdlib": {"entries": len(stdlib), "tree_sha256": digest(canonical(stdlib))},
        "load_paths": spec["load_paths"],
        "unresolved_aliases": spec["pending_aliases"],
        "license_evidence": {
            "declared_by_authenticated_index_and_pkginfo": {
                row["name"]: row["record"]["L"] for row in materials
            },
            "physical_payload_documents": {
                name: {
                    "sha256": row["sha256"],
                    "bytes": row["bytes"],
                    "packages": row["packages"],
                    "classification": "upstream-spdx-not-license-text"
                    if name.endswith(".spdx.json")
                    else "license-text-candidate-by-path",
                }
                for name, row in catalog.items()
                if row["kind"] == "file"
                and (
                    name.endswith(".spdx.json")
                    or any(
                        part.lower().startswith(("license", "copying", "copyright", "notice"))
                        for part in name.split("/")
                    )
                )
            },
            "legal_approval": False,
        },
        "database_modified": False,
        "base_file_collision_verified": False,
        "physical_base_final_abi_verified": False,
        "image_built": False,
        "runtime_approved": False,
        "scripts_executed": False,
        "sdk_executed": False,
        "gaps": [
            "Faltan catálogo físico y SHA DB base/final; no se acredita ausencia de colisiones ni ABI completa con base sólo por registros.",
            "py3-pip-wheel conserva vendors embebidos como datos; OS-SCA no acredita automáticamente su clausura interna.",
            "L/SPDX declarados y archivos de licencia físicos se conservan por separado; no se inventan textos ausentes ni aprobación jurídica universal.",
        ],
    }
    docs = {
        "inventory.json": canonical(inventory),
        "base-os19.cdx.json": canonical(bom["base"]),
        "manual-runtime14.cdx.json": canonical(bom["manual"]),
        "context.tar": context,
    }
    image_gate = {
        "schema_version": 1,
        "owner": OWNER,
        "base_manifest": BASE_MANIFEST,
        "base_config": BASE_CONFIG,
        "context_sha256": digest(context),
        "inventory_sha256": digest(docs["inventory.json"]),
        "expected_manual_catalog_sha256": digest(canonical(catalog)),
        "expected_python": spec["python"],
        "expected_stdlib": inventory["stdlib"],
        "expected_load_paths": spec["load_paths"],
        "base_raw_db_sha256": None,
        "required_before_any_execution": [
            "Capturar OCI base/final físicos y anclar manifest/config/archive/layers.",
            "Cotejar DB real19 base/final idéntica; no añadir los14manuales.",
            "Cotejar cada copia física, incluidos intérprete54bb, libpythonad918, stdlib y links/mode/owner.",
            "Resolver proveedores/símbolos/versiones ELF112 contra bytes base/final; clasificar el alias terminfo pendiente.",
            "Auditar19base/14manuales y vendors embebidos con cobertura exacta; conservar avisos reales.",
            "Verificar config imagen y aislamiento externo UID1000/RO/tmpfs/networknone/1CPU512MiB32pids.",
            "Root emite release específico físico ligado a inventario/ABI/SCA/manifest/config/archive antes de fixture.",
        ],
        "flasgger_consumer_release_contract": {
            "kind": "flasgger-isolated-runtime-root-release-v1",
            "required_root_fields": [
                "authorized_isolated_fixture",
                "runtime_closure_verified",
                "runtime_image_manifest",
                "runtime_security_approval_sha256",
                "python",
            ],
            "python": {"version": "3.12.16", "path": "/usr/bin/python3.12", "sha256": PYTHON_SHA},
            "proof_anchor": "linux-proof.json.runtime_approval_sha256 -> runtime-approval.json; la comprobación libpython/stdlib/base debe preceder al inicio del intérprete",
        },
        "image_built": False,
        "physical_abi_verified": False,
        "runtime_approved": False,
    }
    docs["runtime-image-gate.json"] = canonical(image_gate)
    access = relocator.access_reader()
    parent = ROOT / ".tools/backup-source-build"
    access.check_access(parent, private=True)
    parent_marker = read(parent / ".agency-backup-build-owner", 4096)
    require(
        parent_marker == b"agencia-digital-marketing-ai:backup-source-build:v1",
        "Padre no acreditado.",
    )
    info = parent.lstat()
    plan = {
        "schema_version": 1,
        "owner": OWNER,
        "root": str(ROOT / OUTPUT_RELATIVE),
        "source_sha256": digest(read(Path(__file__), 256 * 1024)),
        "spec_sha256": digest(spec_raw),
        "dockerfile_sha256": digest(dockerfile),
        "parent": {
            "device": info.st_dev,
            "inode": info.st_ino,
            "marker_sha256": digest(parent_marker),
        },
        "references": spec["references"],
        "base_installed_packages": 19,
        "manual_payload_packages": 14,
        "selected_packages": closure["selected"],
        "python": spec["python"],
        "native_elf_files": len(elf),
        "stdlib": inventory["stdlib"],
        "load_paths": spec["load_paths"],
        "outputs": {name: {"bytes": len(raw), "sha256": digest(raw)} for name, raw in docs.items()},
        "scripts_executed": False,
        "sdk_executed": False,
        "image_built": False,
        "runtime_approved": False,
        "image_gate": {
            "base_manifest": BASE_MANIFEST,
            "base_config": BASE_CONFIG,
            "requires_physical_base_final_db19_and_collision_check": True,
            "requires_physical_abi_and_payload_sca": True,
            "consumer": "accept_flasgger_ui.py runtime-approval.json; Root release sólo tras bytes/config/ABI/SCA",
            "acceptance": {
                "cpus": 1,
                "memory_bytes": 512 * 1024**2,
                "pids": 32,
                "uid": 1000,
                "gid": 1000,
                "read_only": True,
                "network": "none",
                "tmpfs": "/tmp",
            },
        },
    }
    return plan, docs, relocator, access


def make_plan():
    plan, _docs, _reader, _access = prepare_data()
    require(not os.path.lexists(plan["root"]), "No se adopta un namespace existente.")
    return plan


def prepare(expected):
    plan, docs, reader, access = prepare_data()
    require(digest(canonical(plan)) == expected, "Plan diferente del pin externo.")
    output = Path(plan["root"])
    info = output.parent.lstat()
    require(
        (info.st_dev, info.st_ino) == (plan["parent"]["device"], plan["parent"]["inode"]),
        "El padre cambió.",
    )
    os.mkdir(output, 0o700)
    access.check_access(output, private=True)
    node = output.lstat()
    proof = {
        "owner": OWNER,
        "path": str(output),
        "directory": {"device": node.st_dev, "inode": node.st_ino},
        "plan_sha256": expected,
    }
    all_outputs = {".owner.json": canonical(proof), "plan.json": canonical(plan), **docs}
    for name, raw in all_outputs.items():
        fd = os.open(
            output / name,
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
        access.check_access(output / name, private=True)
        require(
            reader.read_regular(output / name, MAX_CONTEXT) == raw,
            "Salida cambió durante publicación.",
        )
    receipt = {
        "success": True,
        "owner": OWNER,
        "directory": proof,
        "plan_sha256": expected,
        "outputs": {
            name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in all_outputs.items()
        },
        "scripts_executed": False,
        "sdk_executed": False,
        "image_built": False,
        "runtime_approved": False,
    }
    raw = canonical(receipt)
    fd = os.open(
        output / "receipt.json",
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
    access.check_access(output / "receipt.json", private=True)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.prepare:
            require(
                isinstance(args.expected_plan_sha256, str)
                and re.fullmatch(r"[a-f0-9]{64}", args.expected_plan_sha256),
                "Falta pin de plan externo.",
            )
            result = prepare(args.expected_plan_sha256)
        else:
            plan = make_plan()
            result = {"plan": plan, "plan_sha256": digest(canonical(plan))}
        print(canonical(result).decode())
        return 0
    except Exception as error:
        print(
            canonical(
                {
                    "success": False,
                    "error": "PYTHON_RUNTIME_PREPARATION_REJECTED",
                    "error_type": type(error).__name__,
                    "detail": str(error)[:200],
                    "sdk_executed": False,
                    "image_built": False,
                    "runtime_approved": False,
                }
            ).decode()
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
