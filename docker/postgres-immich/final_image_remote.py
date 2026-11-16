"""Promoción propia y cierre ABI como datos; importar no ejecuta acciones."""

import hashlib
import json
import posixpath
import struct

BASE_MANIFEST = "c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a"
BASE_CONFIG = "22fdd47fd045fefc8c744ff00f801c6efe0e99cab9eb67e1751712760974316c"
COMPILE_SOURCE = "e0e2bc46a65563e122a1469ac9ed5040836697748846e0408938e208e7049a3d"
COMPILE_PROFILE = "bffefb81179fd767d640faf106965ba7d465e90c3a789b4e7800b988a0152153"
SDK_SOURCE = "a13ab96098b5f739ed88cbacaffc5bed521825aeb033d428bddbf93be57137ed"
SDK_PLAN = "ba4a6f71026bf2af93bdd2b530f5233ff887936b02e61ef60983dc6565a4c15e"
CID = "f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d"
OWNER = "agencia-digital-marketing-ai:postgres-immich-runtime:v4.1"
BUILD_ID = "fase4-20261106-01"
CONTEXT = "/opt/media-platform/build/contexts/postgres-immich-runtime/" + BUILD_ID
RECORD = "/var/lib/media-platform/build/postgres-immich-runtime/" + BUILD_ID
MAX_FILE = 512 * 1024**2
POSTVERIFY_SOURCE = "fa397b9b4dd99300b5eb74a327fc7aa40194b91cbea923af2079f546635e6a32"
POSTVERIFY_PROFILE = "f46df24f7e36c655aa7199e3a2add76995ec8558ebe22cd803d7df05edaf4f0f"
FAILED_COMPILE_SHA = "b460fc786cceadbfa92e84093acfeb15f1d7013d7dedb65a0b68690931051d0e"
STOP_SHA = "7c2a9fa9223d1d9b745fe648637273470fe351a17b1a6c26a637672c214cf95e"
PRIOR_POSTVERIFY_FAILED = "407e6eedb2d0b3a8242972f7b96e7c3e5334374677c01fdc8e1cd57027147d36"
POSTVERIFY_STAGE = "postverify-v3-recover"
PRIOR_ACCEPTANCE_FAILED = "0cab178b0f3e4d255f4d6f5e0513fd2f7adc140977d8eea586ed543489950b57"
POSTVERIFY_RECEIPT_SHA = "fc3341c72a1d2c9dfba1f1b25a23116ff457f75fc985a4f6fe5a68c1d3ee8d51"
POSTVERIFY_ARTIFACT_SHA = "79b873a93c6cdb7ab6f6628a18a0f9424fe36a4737a72a39465726960117c034"
FILES_MANIFEST_SHA = "bb3bad780869657b4c97ba7065e6cc3d4764671105d238b47b64f647a05820e4"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def envelope(receipt):
    return {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}


def require_gates(failed, stopped, verified):
    f, stop, proof = (
        failed.get("receipt", {}),
        stopped.get("receipt", {}),
        verified.get("receipt", {}),
    )
    if (
        failed != envelope(f)
        or stopped != envelope(stop)
        or verified != envelope(proof)
        or verified["receipt_sha256"] != POSTVERIFY_RECEIPT_SHA
        or failed["receipt_sha256"] != FAILED_COMPILE_SHA
        or stopped["receipt_sha256"] != STOP_SHA
        or f.get("success") is not False
        or f.get("stage") != "compile"
        or f.get("cid") != CID
        or f.get("source_sha256") != COMPILE_SOURCE
        or f.get("profile_sha256") != COMPILE_PROFILE
        or stop.get("success") is not True
        or stop.get("stage") != "stop"
        or stop.get("source_sha256") != SDK_SOURCE
        or stop.get("plan_sha256") != SDK_PLAN
        or stop.get("cid") != CID
        or stop.get("stopped") is not True
        or type(stop.get("after_pid")) is not int
        or stop["after_pid"] != 0
        or proof.get("success") is not True
        or proof.get("stage") != POSTVERIFY_STAGE
        or proof.get("cid") != CID
        or proof.get("source_sha256") != POSTVERIFY_SOURCE
        or proof.get("profile_sha256") != POSTVERIFY_PROFILE
        or proof.get("failed_compile_receipt_sha256") != FAILED_COMPILE_SHA
        or proof.get("stop_receipt_sha256") != STOP_SHA
        or proof.get("prior_postverify_failed_sha256") != PRIOR_POSTVERIFY_FAILED
        or proof.get("prior_acceptance_failed_sha256") != PRIOR_ACCEPTANCE_FAILED
        or proof.get("files_manifest_sha256") != FILES_MANIFEST_SHA
        or proof.get("artifact_sha256") != POSTVERIFY_ARTIFACT_SHA
        or proof.get("sdk_executed") is not False
        or proof.get("compiler_executed") is not False
        or proof.get("artifact", {}).get("verified") is not True
        or digest(canonical(proof["artifact"])) != proof.get("artifact_sha256")
    ):
        raise ValueError(
            "Se requieren fallo original, postverify positivo y STOP propio respaldados."
        )


def part(data, offset, size):
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise ValueError("Rango ELF fuera de archivo.")
    return data[offset : offset + size]


def elf_info(data):
    if len(data) < 64 or len(data) > MAX_FILE or data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError("Se requiere ELF64 little endian acotado.")
    kind, machine = struct.unpack_from("<HH", data, 16)
    if kind not in (2, 3) or machine != 62:
        raise ValueError("ELF ajeno a ejecutable/biblioteca amd64.")
    start = struct.unpack_from("<Q", data, 40)[0]
    width, count = struct.unpack_from("<HH", data, 58)
    if width != 64 or not 1 <= count <= 16384:
        raise ValueError("Secciones ELF no admitidas.")
    raw = part(data, start, count * width)
    sections = [struct.unpack_from("<IIQQQQIIQQ", raw, i * width) for i in range(count)]
    result = {
        "needed": [],
        "soname": None,
        "runpath": None,
        "rpath": None,
        "defined": [],
        "undefined": [],
        "weak": [],
        "version_requirements": {},
        "version_definitions": [],
    }
    exports, imports, weak, provided = set(), set(), set(), set()

    def strings(index):
        if not 0 <= index < len(sections) or sections[index][1] != 3:
            raise ValueError("Tabla de cadenas inválida.")
        row = sections[index]
        return part(data, row[4], row[5])

    def label(table, offset):
        end = table.find(b"\0", offset)
        if offset < 0 or offset >= len(table) or end < 0 or end - offset > 65536:
            raise ValueError("Cadena ELF truncada.")
        return table[offset:end].decode("utf-8")

    for row in sections:
        tag = row[1]
        if tag not in (6, 11, 0x6FFFFFFE, 0x6FFFFFFD):
            continue
        table = strings(row[6])
        payload = part(data, row[4], row[5])
        if tag == 6:
            if len(payload) % 16:
                raise ValueError("Tabla dinámica truncada.")
            for at in range(0, len(payload), 16):
                key, value = struct.unpack_from("<qQ", payload, at)
                if key == 0:
                    break
                if key == 1:
                    result["needed"].append(label(table, value))
                elif key in (14, 15, 29):
                    name = {14: "soname", 15: "rpath", 29: "runpath"}[key]
                    if result[name] is not None:
                        raise ValueError("Etiqueta ELF repetida.")
                    result[name] = label(table, value)
        elif tag == 11:
            if row[9] != 24 or len(payload) % 24:
                raise ValueError("Símbolos dinámicos truncados.")
            for at in range(0, len(payload), 24):
                name, info, other, shndx, _address, _size = struct.unpack_from(
                    "<IBBHQQ", payload, at
                )
                name = label(table, name)
                if not name or info >> 4 not in (1, 2) or other & 3 not in (0, 3):
                    continue
                if shndx:
                    exports.add(name)
                elif info >> 4 == 2:
                    weak.add(name)
                else:
                    imports.add(name)
        else:
            at, seen = 0, set()
            while at < len(payload):
                if at in seen or len(seen) > 10000:
                    raise ValueError("Cadena de versiones ELF cíclica.")
                seen.add(at)
                if tag == 0x6FFFFFFE:
                    version, number, filename, auxiliary, following = struct.unpack(
                        "<HHIII", part(payload, at, 16)
                    )
                    filename = label(table, filename)
                    names, cursor = [], at + auxiliary
                    for _ in range(number):
                        _hash, _flags, _other, name, next_aux = struct.unpack(
                            "<IHHII", part(payload, cursor, 16)
                        )
                        names.append(label(table, name))
                        cursor += next_aux
                    result["version_requirements"].setdefault(filename, []).extend(names)
                else:
                    version, _flags, _index, number, _hash, auxiliary, following = struct.unpack(
                        "<HHHHIII", part(payload, at, 20)
                    )
                    if not number:
                        raise ValueError("Versión ELF sin definición.")
                    name, _next = struct.unpack("<II", part(payload, at + auxiliary, 8))
                    provided.add(label(table, name))
                if version != 1:
                    raise ValueError("Versión de estructura ELF no admitida.")
                if not following:
                    break
                at += following
    result.update(
        defined=sorted(exports),
        undefined=sorted(imports),
        weak=sorted(weak),
        version_definitions=sorted(provided),
    )
    return result


def file_entry(data):
    return {
        "kind": "file",
        "sha256": digest(data),
        "size": len(data),
        "elf": elf_info(data) if data.startswith(b"\x7fELF") else None,
    }


def virtual_path(path):
    if "\0" in path or "\\" in path:
        raise ValueError("Ruta virtual no admitida.")
    return posixpath.normpath("/" + path.lstrip("/")).lstrip("/")


def follow(files, path):
    path = virtual_path(path)
    seen = set()
    for _ in range(40):
        if path in seen:
            raise ValueError("Ciclo de enlaces en imagen virtual.")
        seen.add(path)
        parts = path.split("/")
        for index in range(len(parts)):
            prefix = "/".join(parts[: index + 1])
            item = files.get(prefix)
            if item and item["kind"] in ("symlink", "hardlink"):
                target = item["target"]
                if not target.startswith("/"):
                    target = posixpath.join(posixpath.dirname(prefix), target)
                path = virtual_path(posixpath.join(target, *parts[index + 1 :]))
                break
        else:
            if path not in files or files[path]["kind"] != "file":
                raise ValueError("Biblioteca virtual ausente: " + path)
            return path
    raise ValueError("Demasiados enlaces virtuales.")


def abi_closure(base, additions):
    if set(base) & set(additions):
        raise ValueError("El overlay intenta sustituir un archivo de la base.")
    files = {**base, **additions}
    pending = ["usr/local/bin/postgres", *sorted(additions)]
    resolved, edges, loaded = {}, {}, {}

    def resolve(name, origin, info):
        if "/" in name:
            choices = [virtual_path(name)] if name.startswith("/") else []
        else:
            paths = (info["runpath"] or info["rpath"] or "").split(":")
            search = []
            for value in paths:
                if not value:
                    continue
                value = value.replace("${ORIGIN}", "/" + origin).replace("$ORIGIN", "/" + origin)
                if "$" in value or not value.startswith("/"):
                    raise ValueError("RUNPATH relativo o variable desconocida.")
                search.append(virtual_path(value))
            choices = [
                posixpath.join(directory, name)
                for directory in [*search, "lib", "usr/local/lib", "usr/lib"]
            ]
        for candidate in choices:
            try:
                found = follow(files, candidate)
            except ValueError:
                continue
            if not files[found].get("elf"):
                raise ValueError("DT_NEEDED apunta a datos no ELF: " + name)
            previous = loaded.setdefault(name, found)
            if previous != found and files[previous]["sha256"] != files[found]["sha256"]:
                raise ValueError("SONAME resuelto a proveedores diferentes: " + name)
            return found
        raise ValueError("DT_NEEDED no se resuelve contra la base: " + name)

    while pending:
        path = follow(files, pending.pop())
        if path in resolved:
            continue
        info = files[path].get("elf")
        if not info:
            continue
        resolved[path] = info
        origin = posixpath.dirname(path)
        edges[path] = {name: resolve(name, origin, info) for name in info["needed"]}
        pending.extend(edges[path].values())
    exports = set().union(*(set(info["defined"]) for info in resolved.values()))
    for path, info in resolved.items():
        missing = sorted(set(info["undefined"]) - exports)
        if missing:
            raise ValueError(
                "Símbolos fuertes sin proveedor en " + path + ": " + ", ".join(missing[:12])
            )
        for name, versions in info["version_requirements"].items():
            provider = edges[path].get(name)
            if not provider:
                raise ValueError("Versión sin DT_NEEDED: " + name)
            missing = sorted(set(versions) - set(resolved[provider]["version_definitions"]))
            if missing:
                raise ValueError("Versiones ABI ausentes: " + ", ".join(missing[:12]))
    return {
        "verified": True,
        "resolved": {
            path: {
                "sha256": files[path]["sha256"],
                "needed": edges[path],
                "exports": len(info["defined"]),
                "strong_imports": len(info["undefined"]),
                "version_requirements": info["version_requirements"],
            }
            for path, info in sorted(resolved.items())
        },
        "sdk_libraries_copied": False,
    }


# El parser mantiene un sistema de archivos virtual: nunca extrae capas al host.
LICENSES = {
    "pgvector-LICENSE",
    "VectorChord-LICENSE",
    "VectorChord-AGPLv3",
    "VectorChord-ELv2",
    "SQLite-NOTICE",
}
LIBRARIES = {
    "usr/local/lib/postgresql/" + name for name in ("vector.so", "vchord.so", "libsqlite3.so.0")
}
MAX_ARCHIVE = 768 * 1024**2


def json_object(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Clave JSON duplicada.")
            result[key] = value
        return result

    return json.loads(
        data,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("JSON no finito.")),
    )


def safe_name(name):
    value = name.rstrip("/")
    while value.startswith("./"):
        value = value[2:]
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\0" in value
        or any(p in ("", ".", "..") for p in value.split("/"))
    ):
        raise ValueError("Nombre TAR no canónico.")
    return value


def apply_layer(files, raw):
    import io
    import tarfile

    if len(raw) > MAX_ARCHIVE:
        raise ValueError("Capa OCI excesiva.")
    additions, whiteouts, opaque = {}, [], []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        seen = set()
        for member in archive:
            if member.name.rstrip("/") in ("", ".", "./") and member.isdir():
                continue
            name = safe_name(member.name)
            if name in seen or len(seen) >= 100000:
                raise ValueError("Miembro TAR repetido o inventario excesivo.")
            seen.add(name)
            parent, leaf = posixpath.split(name)
            if leaf.startswith(".wh."):
                if not member.isfile() or member.size:
                    raise ValueError("Whiteout OCI no ordinario.")
                if leaf == ".wh..wh..opq":
                    opaque.append(parent)
                else:
                    whiteouts.append(posixpath.join(parent, leaf[4:]))
                continue
            common = {"mode": member.mode & 0o7777, "uid": member.uid, "gid": member.gid}
            if member.isdir():
                item = {"kind": "directory"}
            elif member.issym() or member.islnk():
                if not member.linkname or "\0" in member.linkname or "\\" in member.linkname:
                    raise ValueError("Enlace OCI ambiguo.")
                item = {
                    "kind": "symlink" if member.issym() else "hardlink",
                    "target": member.linkname
                    if member.issym()
                    else "/" + safe_name(member.linkname),
                }
            elif member.isfile() and 0 <= member.size <= MAX_FILE:
                source = archive.extractfile(member)
                data = source.read(MAX_FILE + 1)
                if len(data) != member.size:
                    raise ValueError("Miembro OCI truncado.")
                item = file_entry(data)
                if name in {"lib/apk/db/installed", "etc/os-release", "etc/alpine-release"}:
                    item["data"] = data
            else:
                # No se materializan dispositivos; su identidad permanece visible.
                if member.ischr() or member.isblk() or member.isfifo():
                    item = {
                        "kind": "special",
                        "type": member.type.decode("ascii"),
                        "major": member.devmajor,
                        "minor": member.devminor,
                    }
                else:
                    raise ValueError("Tipo TAR no soportado.")
            additions[name] = {**item, **common}
    for name in list(files):
        if any(name == target or name.startswith(target + "/") for target in whiteouts) or any(
            name.startswith(target + "/") for target in opaque
        ):
            del files[name]
    for name, item in additions.items():
        # Un archivo nuevo sustituye también el subárbol previo de esa ruta.
        if item["kind"] != "directory":
            for old in list(files):
                if old.startswith(name + "/"):
                    del files[old]
        files[name] = item


def layer_bytes(blob, row, diffid):
    import gzip
    import io

    if row.get("digest") != "sha256:" + digest(blob) or row.get("size") != len(blob):
        raise ValueError("Blob OCI no coincide con digest/tamaño.")
    kind = row.get("mediaType")
    if kind in (
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
    ):
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
            raw = stream.read(MAX_ARCHIVE + 1)
    elif kind == "application/vnd.oci.image.layer.v1.tar":
        raw = blob
    else:
        raise ValueError("Compresión de capa no admitida.")
    if len(raw) > MAX_ARCHIVE or diffid != "sha256:" + digest(raw):
        raise ValueError("DiffID descomprimido no coincide.")
    return raw


def apk_inventory(data):
    result = {}
    for record in data.decode("utf-8").strip().split("\n\n"):
        fields = {}
        for line in record.splitlines():
            if len(line) >= 2 and line[1] == ":" and line[0] in "PVA":
                if line[0] in fields:
                    raise ValueError("Campo APK ambiguo.")
                fields[line[0]] = line[2:]
        if (
            set(fields) != set("PVA")
            or not all(fields.values())
            or fields["P"] in result
            or fields["A"] not in ("x86_64", "noarch")
        ):
            raise ValueError("Inventario APK incompleto/duplicado o arquitectura ajena.")
        result[fields["P"]] = {"version": fields["V"], "architecture": fields["A"]}
    if len(result) != 45:
        raise ValueError("La base debe conservar los 45 APK auditados.")
    return result


def read_oci(path, expected_manifest=None, *, publish=None):
    import io
    import re
    import tarfile
    from pathlib import Path

    blobs, special = {}, {}
    with tarfile.open(
        fileobj=io.BytesIO(path) if isinstance(path, bytes) else None,
        name=None if isinstance(path, bytes) else str(path),
        mode="r:",
    ) as archive:
        seen, total = set(), 0
        for member in archive:
            name = safe_name(member.name)
            if name in seen or len(seen) > 10000:
                raise ValueError("TAR OCI repetido o excesivo.")
            seen.add(name)
            if member.isdir() and name in ("blobs", "blobs/sha256"):
                continue
            if not member.isfile() or member.size > MAX_ARCHIVE:
                raise ValueError("Archivo OCI no ordinario o excesivo.")
            total += member.size
            if total > 2 * MAX_ARCHIVE:
                raise ValueError("Archivo OCI excede el presupuesto de datos.")
            data = archive.extractfile(member).read(MAX_ARCHIVE + 1)
            if len(data) != member.size:
                raise ValueError("Archivo OCI truncado.")
            if re.fullmatch(r"blobs/sha256/[a-f0-9]{64}", name):
                if digest(data) != name.rsplit("/", 1)[1]:
                    raise ValueError("Blob del TAR alterado.")
                blobs["sha256:" + digest(data)] = data
            elif name in ("index.json", "oci-layout", "manifest.json"):
                special[name] = data
            else:
                raise ValueError("Miembro ajeno al OCI local.")
            if publish is not None and name != "manifest.json":
                target = Path(publish) / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(data)
                target.chmod(0o600)
    if json_object(special.get("oci-layout", b"{}")) != {"imageLayoutVersion": "1.0.0"}:
        raise ValueError("No es un layout OCI 1.0.")
    index = json_object(special["index.json"])
    manifests = index.get("manifests", [])
    if index.get("schemaVersion") != 2 or len(manifests) != 1:
        raise ValueError("Se exige un solo manifiesto OCI.")
    descriptor = manifests[0]
    manifest_digest = descriptor["digest"]
    if expected_manifest is not None and manifest_digest != "sha256:" + expected_manifest:
        raise ValueError("La exportación no corresponde al manifiesto fijado.")

    def document(row):
        data = blobs[row["digest"]]
        if len(data) != row["size"]:
            raise ValueError("Descriptor OCI con tamaño incorrecto.")
        return json_object(data)

    manifest = document(descriptor)
    config = document(manifest["config"])
    if (
        config.get("architecture") != "amd64"
        or config.get("os") != "linux"
        or config.get("rootfs", {}).get("type") != "layers"
    ):
        raise ValueError("Plataforma OCI o rootfs ajenos.")
    layers, diffids = manifest["layers"], config["rootfs"]["diff_ids"]
    if not 1 <= len(layers) <= 32 or len(layers) != len(diffids):
        raise ValueError("Cadena de capas inconsistente.")
    reachable = {manifest_digest, manifest["config"]["digest"]} | {row["digest"] for row in layers}
    if reachable != set(blobs):
        raise ValueError("Blobs OCI extra o ausentes.")
    files = {}
    for row, diffid in zip(layers, diffids, strict=True):
        apply_layer(files, layer_bytes(blobs[row["digest"]], row, diffid))
    return {
        "manifest_digest": manifest_digest,
        "manifest": manifest,
        "config": config,
        "config_digest": manifest["config"]["digest"],
        "files": files,
    }


def project(candidates):
    import re

    selected, excluded = {}, []
    for name, row in candidates.items():
        if name.startswith(
            ("usr/local/include/postgresql/server/extension/", "usr/local/lib/postgresql/bitcode/")
        ):
            excluded.append(name)
        elif (
            name in LIBRARIES
            or re.fullmatch(
                r"usr/local/share/postgresql/extension/(vector|vchord)(?:\.control|--[0-9][0-9A-Za-z_.+-]*\.sql)",
                name,
            )
            or name in {"usr/local/share/licenses/agency-postgres-immich/" + n for n in LICENSES}
        ):
            selected[name] = {k: row[k] for k in ("sha256", "size")}
        else:
            raise ValueError("Artefacto fuera de la proyección aprobada: " + name)
    required = (
        LIBRARIES
        | {
            "usr/local/share/postgresql/extension/" + n
            for n in ("vector.control", "vchord.control", "vector--0.8.5.sql", "vchord--1.1.1.sql")
        }
        | {"usr/local/share/licenses/agency-postgres-immich/" + n for n in LICENSES}
    )
    if not required <= set(selected) or len(selected) > 256:
        raise ValueError("Faltan bibliotecas, controles, SQL o licencias requeridas.")
    return selected, sorted(excluded)


def validate_prepared(value, profile_sha, source_sha):
    receipt = value.get("receipt", {})
    if (
        value != envelope(receipt)
        or receipt.get("stage") != "prepare"
        or receipt.get("success") is not True
        or receipt.get("profile_sha256") != profile_sha
        or receipt.get("source_sha256") != source_sha
    ):
        raise ValueError("Prepare no está respaldado para esta fuente y perfil.")
    return receipt


COMPILE_LOCAL = "eeced2b0f9f5572832a8e2cf7e4d18349818280985da9a7990fd4d6859e755cb"
COMPILE_RECIPE = "851cedce1107d751e50a598ef825e585ed82148755b8f83aed66a9e996b1cd19"
SDK_LOCAL = "685bb8a54fc7d65fd25710b5ea317f8eda239b57aa2978a0ff5e33b8aabba608"
MARKER = ".runtime-image-owner.json"
LABELS = {
    "org.opencontainers.image.title": "PostgreSQL Immich de la agencia",
    "org.opencontainers.image.version": "16.15-vchord1.1.1-vector0.8.5-sqlite3.54.1-agencia.1",
    "cl.e-scale.agencia.owner": OWNER,
}


def proposal():
    return {
        "schema_version": 1,
        "owner": OWNER,
        "context": CONTEXT,
        "record": RECORD,
        "base_manifest": BASE_MANIFEST,
        "base_config": BASE_CONFIG,
        "compile_profile_sha256": COMPILE_PROFILE,
        "cid": CID,
        "platform": "linux/amd64",
        "builder": "default",
        "driver": "docker",
        "network": "none",
        "budget_seconds": 600,
        "capture_limit": 4 * 1024**2,
        "sql_verified": False,
        "final_sca_verified": False,
    }


def postverify_history(profile):
    wrapper = profile["postverify_profile"]
    if digest(canonical(wrapper)) != POSTVERIFY_PROFILE:
        raise ValueError("El perfil de recuperación no coincide con el freeze.")
    base = wrapper["base_profile"]
    if (
        base["sdk_plan"] != profile["sdk_plan"]
        or base["compile_profile"] != profile["compile_profile"]
    ):
        raise ValueError("La recuperación no conserva SDK y compile históricos del runtime.")
    return base


def validate_profile(profile):
    import re

    for key, value in proposal().items():
        if canonical(profile.get(key)) != canonical(value):
            raise ValueError("El perfil difiere del contrato runtime fijo: " + key)
    if set(profile["sources"]) != {"local", "remote", "dockerfile"} or any(
        not re.fullmatch("[a-f0-9]{64}", v) for v in profile["sources"].values()
    ):
        raise ValueError("Pins runtime incompletos.")
    compiled = profile["compile_profile"]
    if digest(canonical(compiled)) != COMPILE_PROFILE or compiled["sources"] != {
        "local": COMPILE_LOCAL,
        "remote": COMPILE_SOURCE,
        "recipe": COMPILE_RECIPE,
    }:
        raise ValueError("El perfil de compile V3 no coincide con el congelado.")
    if compiled["sdk_plan"] != profile["sdk_plan"]:
        raise ValueError("El plan SDK no corresponde al compile histórico.")
    old, backed = compiled["backed"], profile["sdk_backed"]
    if set(backed) != set(old) | {"stop"} or any(backed[k] != value for k, value in old.items()):
        raise ValueError("Sólo se permite añadir STOP al histórico de etapas SDK.")
    postverify_history(profile)
    require_gates(profile["failed"], backed["stop"], profile["postverified"])


def validate_base(image):
    if (
        image["manifest_digest"] != "sha256:" + BASE_MANIFEST
        or image["config_digest"] != "sha256:" + BASE_CONFIG
        or len(image["manifest"]["layers"]) != 12
    ):
        raise ValueError("La base local no es postgres-apps c908/config22fdd.")
    config, files = image["config"]["config"], image["files"]
    if (
        config.get("User") != "70:70"
        or config.get("Entrypoint") != ["docker-entrypoint.sh"]
        or config.get("Cmd") != ["postgres"]
        or config.get("StopSignal") != "SIGINT"
    ):
        raise ValueError("La base perdió UID70 o entrypoint oficial.")
    for name, expected in {
        "usr/local/bin/docker-entrypoint.sh": "9c440299ae04a0a79d55b8bf03307036d890a40979d2fb698073c9050d4b20a5",
        "usr/local/bin/docker-ensure-initdb.sh": "922ade6b23a312e65023349b36e873752b15ff38ae4c0ae461d74451e104f312",
    }.items():
        if files[follow(files, name)]["sha256"] != expected:
            raise ValueError("Script oficial de PostgreSQL alterado.")
    if any(name in files for name in ("usr/local/bin/gosu", "usr/local/bin/su-exec")):
        raise ValueError("La base aún contiene gosu/su-exec.")
    packages = apk_inventory(files["lib/apk/db/installed"]["data"])
    return {"packages": packages, "apk_database_sha256": files["lib/apk/db/installed"]["sha256"]}


def verify_final(base, final, projected):
    validate_base(base)
    config, wanted = final["config"]["config"], dict(base["config"]["config"])
    wanted["Labels"] = {**wanted.get("Labels", {}), **LABELS}
    if config != wanted:
        raise ValueError("El config final cambió parámetros heredados ajenos al overlay.")
    layers = base["manifest"]["layers"]
    if (
        final["manifest"]["layers"][: len(layers)] != layers
        or final["config"]["rootfs"]["diff_ids"][: len(layers)]
        != base["config"]["rootfs"]["diff_ids"]
    ):
        raise ValueError("La imagen final no conserva las capas de la base.")
    for name, entry in projected.items():
        value = final["files"].get(name, {})
        if (
            any(value.get(k) != entry[k] for k in ("sha256", "size"))
            or value.get("uid") != 0
            or value.get("gid") != 0
            or value.get("mode") != 0o444
        ):
            raise ValueError("Artefacto final cambiado o escribible: " + name)
    for name, entry in base["files"].items():
        if entry != final["files"].get(name):
            raise ValueError("La imagen final alteró un nodo heredado: " + name)
    allowed_dirs = {posixpath.dirname(name) for name in projected}
    allowed_dirs |= {parent for name in list(allowed_dirs) for parent in parents(name)}
    for name in set(final["files"]) - set(base["files"]) - set(projected):
        entry = final["files"][name]
        if name not in allowed_dirs or entry != {
            "kind": "directory",
            "mode": 0o755,
            "uid": 0,
            "gid": 0,
        }:
            raise ValueError("Nodo adicional no permitido en imagen final: " + name)
    if (
        final["files"]["lib/apk/db/installed"]["sha256"]
        != base["files"]["lib/apk/db/installed"]["sha256"]
    ):
        raise ValueError("Cambió la base de 45 APK.")
    return abi_closure(base["files"], {n: final["files"][n] for n in projected})


def parents(path):
    result = []
    while path and path != ".":
        result.append(path)
        path = posixpath.dirname(path)
    return result


def load_module(raw, expected, name, flag=None):
    import types

    if digest(raw) != expected:
        raise ValueError("Un helper no coincide con la fuente fijada.")
    module = types.ModuleType(name)
    if flag:
        module.__dict__[flag] = expected
    exec(compile(raw, "<" + name + "-fijado>", "exec"), module.__dict__)
    return module


def modules(request):
    import base64

    normal = load_module(
        base64.b64decode(request["sdk_source_b64"], validate=True),
        SDK_SOURCE,
        "runtime_sdk",
        "_SOURCE_SHA256",
    )
    compiled = load_module(
        base64.b64decode(request["compile_source_b64"], validate=True),
        COMPILE_SOURCE,
        "runtime_compile",
        "_COMPILE_SOURCE_SHA256",
    )
    compiled.validate_profile(request["profile"]["compile_profile"])
    verifier = load_module(
        base64.b64decode(request["postverify_source_b64"], validate=True),
        POSTVERIFY_SOURCE,
        "runtime_postverify",
        "_POSTVERIFY_RECOVER_SOURCE_SHA256",
    )
    verifier.validate_profile(request["profile"]["postverify_profile"])
    return normal, compiled, verifier


def runtime_directory(normal, path, *, create=False):
    import os

    parent = normal.open_directory(posixpath.dirname(path))
    try:
        name = posixpath.basename(path)
        if create:
            os.mkdir(name, 0o700, dir_fd=parent)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            normal.checked_node(os.fstat(fd), 0, directory=True)
            marker = {"owner": OWNER, "path": path, "directory": normal.identity(os.fstat(fd))}
            if create:
                normal.write_file(fd, MARKER, canonical(marker))
            elif normal.json_object(normal.read_file(fd, MARKER)) != marker:
                raise ValueError("Marcador runtime no acredita el inodo propio.")
            if (
                normal.identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
                != marker["directory"]
            ):
                raise ValueError("La ruta runtime cambió de inodo.")
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(parent)


def open_runtime(normal, *, create=False):
    import os

    result = {}
    try:
        for shared in (
            "/opt/media-platform/build",
            "/opt/media-platform/build/contexts",
            "/var/lib/media-platform/build",
        ):
            fd = normal.marked_directory(shared, shared=True)
            os.close(fd)
        for name, path in (("context", CONTEXT), ("record", RECORD)):
            parent = posixpath.dirname(path)
            try:
                os.stat(parent, follow_symlinks=False)
                exists = True
            except FileNotFoundError:
                exists = False
            fd = runtime_directory(normal, parent, create=create and not exists)
            os.close(fd)
            result[name] = runtime_directory(normal, path, create=create)
        return result
    except BaseException:
        for fd in result.values():
            os.close(fd)
        raise


def sdk_gates(request, normal, compiled, verifier, store, docker):
    profile = request["profile"]
    base = postverify_history(profile)
    store.verify()
    actual = store.receipts()
    if actual != {k: v for k, v in profile["sdk_backed"].items() if k != "transfer"}:
        raise ValueError("Los recibos físicos SDK difieren de la bóveda, incluido STOP.")
    require_gates(profile["failed"], actual["stop"], profile["postverified"])
    control = store.descriptors["control"]
    if normal.read_file(control, "compile-v3-receipt.json", maximum=512 * 1024) != canonical(
        profile["failed"]["receipt"]
    ):
        raise ValueError("El fallo físico V3 no coincide con el original respaldado.")
    if normal.read_file(control, POSTVERIFY_STAGE + ".json", maximum=512 * 1024) != canonical(
        profile["postverified"]["receipt"]
    ) or normal.read_file(
        control, POSTVERIFY_STAGE + "-profile.json", maximum=256 * 1024
    ) != canonical(profile["postverify_profile"]):
        raise ValueError("La postverificación física difiere del respaldo.")
    if normal.read_file(control, verifier.PRIOR_STAGE + ".json", maximum=512 * 1024) != canonical(
        base["prior_postverify_failed"]["receipt"]
    ) or normal.read_file(
        control, verifier.PRIOR_STAGE + "-profile.json", maximum=256 * 1024
    ) != canonical(base["prior_postverify_profile"]):
        raise ValueError("El primer postverify fallido o su perfil físico cambió.")
    if normal.read_file(control, verifier.ACCEPT_STAGE + ".json", maximum=512 * 1024) != canonical(
        profile["postverify_profile"]["failed_acceptance"]["receipt"]
    ) or normal.read_file(
        control, verifier.ACCEPT_STAGE + "-profile.json", maximum=256 * 1024
    ) != canonical(base):
        raise ValueError("La aceptación fallida o su perfil físico cambió.")
    observed = normal.check_container(docker.inspect(CID), CID)
    if (
        observed["running"]
        or observed["pid"] != 0
        or observed != profile["postverified"]["receipt"]["container"]
    ):
        raise ValueError(
            "No se promueve mientras el SDK pueda escribir salidas o cambie de instancia."
        )
    raw_archive = normal.read_file(control, POSTVERIFY_STAGE + "-archive.tar", maximum=4 * 1024**2)
    archive = verifier.extract_build_output(raw_archive)
    if {k: v for k, v in archive.items() if k != "output"} != profile["postverified"]["receipt"][
        "archive"
    ] or normal.read_file(
        control, POSTVERIFY_STAGE + "-build-output.txt", maximum=65536
    ) != archive["output"]:
        raise ValueError("El archivo/build output conservado cambió tras postverify.")
    artifact = verifier.catalog(
        normal, compiled, archive["output"], verifier.source_texts(normal, store)
    )
    if artifact != profile["postverified"]["receipt"]["artifact"] or normal.read_file(
        control, POSTVERIFY_STAGE + "-artifact.json", maximum=512 * 1024
    ) != canonical(artifact):
        raise ValueError("El catálogo real de artefactos cambió tras postverify.")
    if normal.read_file(control, "cid.txt", maximum=128).decode() != CID:
        raise ValueError("El CID guardado es distinto del detenido.")
    return artifact


def read_overlay_source(normal, verifier, parent, leaf, name, expected):
    data = verifier.read_owned(normal, parent, leaf, maximum=MAX_FILE, logical_path=name)[0]
    if len(data) != expected["size"] or digest(data) != expected["sha256"]:
        raise ValueError("El artefacto cambió durante la promoción FD.")
    return data


def write_overlay(normal, verifier, context_fd, source_fd, projected):
    import os
    import stat

    os.mkdir("runtime-root", 0o755, dir_fd=context_fd)
    target = os.open(
        "runtime-root", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=context_fd
    )
    try:
        os.fchmod(target, 0o755)
        for name, expected in sorted(projected.items()):
            src_parent = os.dup(source_fd)
            leaf = name.split("/")[-1]
            try:
                for part in name.split("/")[:-1]:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_parent
                    )
                    info = os.fstat(child)
                    if (
                        info.st_uid != 0
                        or info.st_gid != 0
                        or stat.S_IMODE(info.st_mode) not in (0o700, 0o755)
                    ):
                        os.close(child)
                        raise ValueError("Ancestro de salida SDK ajeno o escribible.")
                    os.close(src_parent)
                    src_parent = child
            except BaseException:
                os.close(src_parent)
                raise
            current = os.dup(target)
            try:
                for part in name.split("/")[:-1]:
                    try:
                        os.mkdir(part, 0o755, dir_fd=current)
                        created = True
                    except FileExistsError:
                        created = False
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                    )
                    if created:
                        os.fchmod(child, 0o755)
                    info = os.fstat(child)
                    if (
                        not stat.S_ISDIR(info.st_mode)
                        or info.st_uid != 0
                        or info.st_gid != 0
                        or stat.S_IMODE(info.st_mode) != 0o755
                    ):
                        os.close(child)
                        raise ValueError("Directorio de overlay no tiene 0755 root.")
                    os.close(current)
                    current = child
                data = read_overlay_source(normal, verifier, src_parent, leaf, name, expected)
                out = os.open(
                    leaf,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o444,
                    dir_fd=current,
                )
                with os.fdopen(out, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o444)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(src_parent)
                os.close(current)
    finally:
        os.close(target)


def snapshot_overlay(normal, context_fd, projected):
    import os
    import stat

    root = os.open("runtime-root", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=context_fd)
    actual, contents = {}, {}

    def walk(fd, prefix=""):
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o755:
            raise ValueError("Directorio de overlay escribible o ajeno.")
        for entry in os.scandir(fd):
            name = prefix + entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    walk(child, name + "/")
                finally:
                    os.close(child)
            else:
                if (
                    name not in projected
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != 0
                    or info.st_gid != 0
                    or stat.S_IMODE(info.st_mode) != 0o444
                ):
                    raise ValueError("Archivo de overlay ajeno, enlazado o escribible.")
                source = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(source, "rb") as stream:
                    data = stream.read(MAX_FILE + 1)
                    if (
                        normal.identity(os.fstat(stream.fileno())) != normal.identity(info)
                        or len(data) != info.st_size
                    ):
                        raise ValueError("Overlay cambiado durante lectura.")
                actual[name] = {"sha256": digest(data), "size": len(data)}
                contents[name] = file_entry(data)

    try:
        walk(root)
    finally:
        os.close(root)
    if actual != projected:
        raise ValueError("El overlay difiere de su catálogo sellado.")
    return contents


def build_identity(metadata, iidfile):
    import re

    manifest = metadata.get("containerimage.digest")
    config = metadata.get("containerimage.config.digest")
    if (
        any(
            not isinstance(v, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", v) is None
            for v in (manifest, config)
        )
        or iidfile != config
    ):
        raise ValueError("BuildKit no correlaciona iidfile config y manifiesto completos.")
    return {"manifest": manifest, "config": config, "iidfile": iidfile}


def build_arguments():
    return [
        "buildx",
        "build",
        "--builder",
        "default",
        "--platform",
        "linux/amd64",
        "--network",
        "none",
        "--pull=false",
        "--provenance=false",
        "--sbom=false",
        "--build-context",
        "pgbase=oci-layout://" + RECORD + "/base-oci@sha256:" + BASE_MANIFEST,
        "--iidfile",
        RECORD + "/image-id.txt",
        "--metadata-file",
        RECORD + "/build-metadata.json",
        "--file",
        CONTEXT + "/Dockerfile",
        "--load",
        CONTEXT,
    ]


def tree_identity(normal, descriptors):
    import os

    result = {}
    for name, path in (("context", CONTEXT), ("record", RECORD)):
        current = runtime_directory(normal, path)
        try:
            info = normal.identity(os.fstat(descriptors[name]))
            if normal.identity(os.fstat(current)) != info:
                raise ValueError("El namespace runtime cambió de inodo.")
            result[name] = {
                "path": path,
                "directory": info,
                "marker_sha256": digest(normal.read_file(current, MARKER)),
            }
        finally:
            os.close(current)
    return result


def check_docker(docker):
    if (
        docker.capture(["version", "--format", "{{.Server.Version}}"], timeout=5).decode().strip()
        != "29.9.3"
    ):
        raise ValueError("Se exige el daemon Docker29.9.3 revisado.")
    version = docker.capture(["buildx", "version"], timeout=5).decode().strip()
    if not version.startswith("github.com/docker/buildx v0.38.1 "):
        raise ValueError("Se exige Buildx0.38.1 revisado.")
    inspection = docker.capture(["buildx", "inspect", "default"], timeout=5).decode()
    drivers = [
        line.split(":", 1)[1].strip()
        for line in inspection.splitlines()
        if line.startswith("Driver:")
    ]
    if drivers != ["docker"]:
        raise ValueError("Sólo se admite el builder default con driver docker.")
    return {"daemon": "29.9.3", "buildx": version, "driver": "docker", "builder": "default"}


def hash_archive(normal, record_fd, name):
    import os

    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=record_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        normal.checked_node(info, 0)
        if info.st_size > MAX_ARCHIVE:
            raise ValueError("Exportación OCI excesiva.")
        sha = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
        if normal.identity(
            os.stat(name, dir_fd=record_fd, follow_symlinks=False)
        ) != normal.identity(info):
            raise ValueError("La exportación cambió mientras se leía.")
        return {"size": info.st_size, "sha256": sha.hexdigest()}


def export_image(normal, docker, record_fd, image_id, name):
    import os

    try:
        os.stat(name, dir_fd=record_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("Una exportación anterior no se sobrescribe.")
    result = docker.command(
        ["image", "save", "--output", RECORD + "/" + name, image_id], timeout=120
    )
    if result["returncode"] != 0:
        raise ValueError("La exportación local Docker falló.")
    # umask077 y padre root0700; Docker crea el archivo regular privado.
    evidence = hash_archive(normal, record_fd, name)
    return evidence


def verify_oci_tree(normal, record_fd, expected):
    import os

    root = normal.open_directory(RECORD + "/base-oci")
    try:
        actual = {}

        def walk(fd, prefix=""):
            normal.checked_node(os.fstat(fd), 0, directory=True)
            for entry in os.scandir(fd):
                import stat

                name = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    try:
                        walk(child, name + "/")
                    finally:
                        os.close(child)
                else:
                    normal.checked_node(info, 0)
                    data = normal.read_file(fd, entry.name, maximum=MAX_ARCHIVE)
                    actual[name] = {"sha256": digest(data), "size": len(data)}

        walk(root)
        if expected is not None and actual != expected:
            raise ValueError("Los blobs OCI publicados cambiaron desde prepare.")
        return actual
    finally:
        os.close(root)


def apply(request):
    import base64
    import os
    import re
    import signal
    from datetime import UTC, datetime

    profile = request["profile"]
    validate_profile(profile)
    profile_sha = digest(canonical(profile))
    if (
        profile_sha != request["profile_sha256"]
        or globals().get("_RUNTIME_SOURCE_SHA256") != profile["sources"]["remote"]
    ):
        raise ValueError("Perfil o fuente runtime distintos del SHA externo.")
    normal, compiled, verifier = modules(request)
    context = {"plan": profile["sdk_plan"], "layout_source_b64": request["layout_source_b64"]}
    normal.check_request(context)
    normal.verify_host(context)
    store = normal.Store(context)
    descriptors, armed, log_fd = {}, False, None
    mode = request["mode"]
    if mode not in ("prepare", "build"):
        raise ValueError("Acción runtime no admitida.")
    try:
        os.umask(0o077)
        store.open()
        docker = store.docker()
        sdk_gates(request, normal, compiled, verifier, store, docker)
        versions = check_docker(docker)
        descriptors = open_runtime(normal, create=mode == "prepare")
        record, context_fd = descriptors["record"], descriptors["context"]
        os.mkdir("." + mode + "-started", 0o700, dir_fd=record)
        armed = True
        dockerfile = base64.b64decode(request["dockerfile_b64"], validate=True)
        if digest(dockerfile) != profile["sources"]["dockerfile"]:
            raise ValueError("Dockerfile distinto del pin respaldado.")
        artifact = profile["postverified"]["receipt"]["artifact"]
        projected, excluded = project(artifact["runtime_files"])
        if mode == "prepare":
            normal.write_file(record, "profile.json", canonical(profile))
            normal.write_file(context_fd, "Dockerfile", dockerfile)
            normal.write_file(
                context_fd, ".dockerignore", b"*\n!Dockerfile\n!runtime-root/\n!runtime-root/**\n"
            )
            source = normal.open_directory(normal.OUTPUTS + "/compile-v3/runtime-root")
            try:
                write_overlay(normal, verifier, context_fd, source, projected)
            finally:
                os.close(source)
            overlay = snapshot_overlay(normal, context_fd, projected)
            archive = export_image(normal, docker, record, "sha256:" + BASE_MANIFEST, "base.tar")
            os.mkdir("base-oci", 0o700, dir_fd=record)
            base = read_oci(RECORD + "/base.tar", BASE_MANIFEST, publish=RECORD + "/base-oci")
            base_evidence = validate_base(base)
            closure = abi_closure(base["files"], overlay)
            fields = {
                "directories": tree_identity(normal, descriptors),
                "archive": archive,
                "base": base_evidence,
                "oci_files": verify_oci_tree(normal, record, None),
                "projected": projected,
                "excluded": excluded,
                "abi": closure,
                "input_artifact_sha256": profile["postverified"]["receipt"]["artifact_sha256"],
            }
        else:
            prepared = validate_prepared(
                request["prepared"], profile_sha, profile["sources"]["remote"]
            )
            if normal.read_file(record, "prepare.json", maximum=2 * 1024**2) != canonical(
                prepared
            ) or normal.read_file(record, "profile.json", maximum=512 * 1024) != canonical(profile):
                raise ValueError("Prepare/perfil físicos no coinciden con el respaldo.")
            if (
                prepared["directories"] != tree_identity(normal, descriptors)
                or prepared["projected"] != projected
                or prepared["excluded"] != excluded
            ):
                raise ValueError("El contexto o catálogo cambió desde prepare.")
            if (
                set(os.listdir(context_fd))
                != {MARKER, "Dockerfile", ".dockerignore", "runtime-root"}
                or normal.read_file(context_fd, "Dockerfile") != dockerfile
                or normal.read_file(context_fd, ".dockerignore")
                != b"*\n!Dockerfile\n!runtime-root/\n!runtime-root/**\n"
            ):
                raise ValueError("El contexto contiene archivos ajenos o fuente distinta.")
            overlay = snapshot_overlay(normal, context_fd, projected)
            if hash_archive(normal, record, "base.tar") != prepared["archive"]:
                raise ValueError("El TAR local de la base cambió.")
            verify_oci_tree(normal, record, prepared["oci_files"])
            base = read_oci(RECORD + "/base.tar", BASE_MANIFEST)
            if (
                validate_base(base) != prepared["base"]
                or abi_closure(base["files"], overlay) != prepared["abi"]
            ):
                raise ValueError("El cierre ABI difiere del prepare respaldado.")
            log_fd = os.open(
                "build.log",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=record,
            )
            with os.fdopen(log_fd, "wb", buffering=0) as sink:
                log_fd = None
                process = docker.command(build_arguments(), timeout=300, sink=sink)
                sink.flush()
                os.fsync(sink.fileno())
            if process["returncode"] != 0:
                raise ValueError("Build final terminó con error; se conserva su log.")
            iid = normal.read_file(record, "image-id.txt", maximum=128).decode().strip()
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", iid):
                raise ValueError("Build no devolvió ID de imagen completo.")
            metadata = normal.json_object(
                normal.read_file(record, "build-metadata.json", maximum=65536)
            )
            ids = build_identity(metadata, iid)
            final_archive = export_image(normal, docker, record, ids["manifest"], "image.tar")
            final = read_oci(RECORD + "/image.tar", ids["manifest"][7:])
            if final["config_digest"] != ids["config"]:
                raise ValueError("El config OCI exportado difiere de metadata/iidfile.")
            final_abi = verify_final(base, final, projected)
            if snapshot_overlay(normal, context_fd, projected) != overlay:
                raise ValueError("El overlay cambió durante build.")
            sdk_gates(request, normal, compiled, verifier, store, docker)
            fields = {
                "image_id": ids["manifest"],
                "config_digest": ids["config"],
                "iidfile": iid,
                "archive": final_archive,
                "metadata_sha256": digest(canonical(metadata)),
                "prepare_receipt_sha256": request["prepared"]["receipt_sha256"],
                "abi": final_abi,
                "packages": prepared["base"]["packages"],
                "process": process["process"],
                "log_sha256": digest(
                    normal.read_file(record, "build.log", maximum=normal.MAX_OUTPUT)
                ),
            }
        sdk_gates(request, normal, compiled, verifier, store, docker)
        receipt = {
            "stage": mode,
            "success": True,
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "owner": OWNER,
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "docker_versions": versions,
            "sql_verified": False,
            "final_sca_verified": False,
            **fields,
        }
        normal.write_file(record, mode + ".json", canonical(receipt))
        return envelope(receipt)
    except BaseException as error:
        receipt = {
            "stage": mode,
            "success": False,
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "owner": OWNER,
            "error_type": type(error).__name__,
            "detail": str(error)[:400],
            "process": getattr(error, "process", None),
            "partial_evidence_preserved": True,
            "sql_verified": False,
            "final_sca_verified": False,
        }
        if armed:
            try:
                normal.write_file(descriptors["record"], mode + ".json", canonical(receipt))
            except BaseException:
                receipt["receipt_written"] = False
        return envelope(receipt)
    finally:
        if log_fd is not None:
            os.close(log_fd)
        for fd in descriptors.values():
            os.close(fd)
        store.close()
        if hasattr(signal, "alarm"):
            signal.alarm(0)


def main():
    import signal
    import sys

    def timeout(_signum, _frame):
        raise TimeoutError("El helper runtime agotó el presupuesto global de 600 s.")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(600)
    try:
        header = sys.stdin.buffer.read(4)
        if len(header) != 4:
            raise ValueError("Petición runtime truncada.")
        size = struct.unpack(">I", header)[0]
        if size > 768 * 1024:
            raise ValueError("Petición runtime excesiva.")
        data = sys.stdin.buffer.read(size)
        if len(data) != size:
            raise ValueError("Petición runtime truncada.")
        result = apply(json_object(data))
        print(json.dumps({"success": result["receipt"]["success"], "result": result}), flush=True)
    except BaseException as error:
        print(
            json.dumps(
                {"success": False, "error_type": type(error).__name__, "values_hidden": True}
            ),
            flush=True,
        )
        return 1
    return 0 if result["receipt"]["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
