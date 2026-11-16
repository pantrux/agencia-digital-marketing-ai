"""Proyección cerrada de libffi como datos; sin archivos, red, procesos o CLI."""

import copy
import hashlib
import io
import tarfile
import types

CORE_SHA256 = "cd320c4280fdd7b78ffa9ecc3c1b8cf9e8450af657ee8fb109e521b56c7a63b7"
CONTEXT_SHA256 = "f41fbd541699f2476db70ab3ae2d65c6cb369c3ce39b9070d86f9ed99b7c9d23"
V3_CONTEXT_SHA256 = "e956a64f6c68b102b08fddd9b56747be1f4ad7240216272c07b64b6617fde5e1"
INVENTORY_SHA256 = "4ccf6511e482a6656afde4e5310e380eaed505f3760c48cbe774e7125951a346"
LIBFFI_SHA256 = "362c9592846776a36d5bb4b6deb85d5b031bbaa178699f7b73ed0d6f7f1a529b"
CLEAR_SHA256 = "47b7dca0f5b24e262cfae8016edfb5a7abf2c7f43d95ecd6a309f3072d55a7bb"
OMITTED = {
    "usr/bin/clear": None,
    "usr/bin/reset": "tset",
    "usr/bin/lzcat": "xz",
    "usr/bin/lzma": "xz",
    "usr/bin/unlzma": "xz",
    "usr/bin/unxz": "xz",
    "usr/bin/xzcat": "xz",
}
MAPPING = {
    "usr/lib64/libffi.so.8.5.0": "usr/lib/libffi.so.8.5.0",
    "usr/lib64/libffi.so": "usr/lib/libffi.so",
    "usr/lib64/libffi.so.8": "usr/lib/libffi.so.8",
}
REMOVED = "usr/lib64"
ALIASES = {"usr/lib64": "lib", "lib": "usr/lib", "lib64": "usr/lib"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def pinned(raw, checksum, size):
    require(
        type(raw) is bytes and len(raw) == size and digest(raw) == checksum,
        "Se requieren los bytes originales exactos y su tamaño fijado.",
    )


def load_core(raw):
    require(
        type(raw) is bytes and len(raw) <= 65536 and digest(raw) == CORE_SHA256,
        "El núcleo debe coincidir con cd320 antes de ejecutar sus definiciones.",
    )
    module = types.ModuleType("python_runtime_core_frozen_cd320")
    module.__file__ = "<runtime_image_inventory_v3_cd320>"
    exec(compile(raw, module.__file__, "exec"), vars(module))
    return module


def _source_set(names, prefix=""):
    branch = {n for n in names if n == prefix + REMOVED or n.startswith(prefix + REMOVED + "/")}
    require(
        branch == {prefix + REMOVED, *(prefix + n for n in MAPPING)},
        "La rama lib64 no contiene exactamente el directorio y los tres nodos revisados.",
    )
    require(
        not any(prefix + n in names for n in MAPPING.values()),
        "Un destino físico libffi ya está ocupado; no se adopta ni sobrescribe.",
    )
    require(
        all(prefix + name in names for name in OMITTED),
        "Falta una de las siete contribuciones CLI cuya omisión es explícita.",
    )


def _check_cli_identity(name, row):
    target = OMITTED[name]
    require(row.get("uid") == row.get("gid") == 0, "Propietario de CLI distinto.")
    if target is None:
        require(
            row.get("kind") == "file" and row.get("mode") == 0o755 and row.get("bytes") == 17208,
            "La identidad de clear no es la revisada.",
        )
    else:
        require(
            row.get("kind") == "symlink"
            and row.get("mode") == 0o777
            and row.get("target") == target,
            "Destino/tipo/modo de CLI distinto.",
        )


def _cli_rows(rows):
    values = {}
    for name, node, body in rows:
        source = name.removeprefix("rootfs/")
        if source not in OMITTED:
            continue
        descriptor = {
            "kind": "file" if node.isfile() else "symlink" if node.issym() else "otro",
            "mode": node.mode,
            "uid": node.uid,
            "gid": node.gid,
            "bytes": node.size,
            "target": node.linkname,
        }
        _check_cli_identity(source, descriptor)
        values[source] = (descriptor, body)
    return values


def _project_rows(rows):
    names = [name for name, _, _ in rows]
    require(len(names) == len(set(names)), "Miembros de contexto duplicados.")
    _source_set(names, "rootfs/")
    _cli_rows(rows)
    original_directory = next(node for n, node, _ in rows if n == "rootfs/" + REMOVED)
    require(
        original_directory.isdir()
        and original_directory.mode == 0o755
        and original_directory.uid == original_directory.gid == 0,
        "El directorio original lib64 no tiene el tipo/modo/propietario revisado.",
    )
    result = []
    for name, node, body in rows:
        if name == "rootfs/" + REMOVED or name.removeprefix("rootfs/") in OMITTED:
            continue
        target = "rootfs/" + MAPPING[name[7:]] if name[7:] in MAPPING else name
        item = copy.copy(node)
        item.pax_headers = dict(node.pax_headers)
        if target != name:
            require(
                "path" not in item.pax_headers or item.pax_headers["path"] == name,
                "PAX path de origen ambiguo.",
            )
            if "path" in item.pax_headers:
                item.pax_headers["path"] = target
            item.name = target
        result.append((target, item, body))
    return result


def _check_payload(rows):
    by_name = {name: (node, body) for name, node, body in rows}
    for old in MAPPING:
        node, body = by_name["rootfs/" + old]
        require(node.uid == node.gid == 0, "Propietario libffi distinto.")
        if old.endswith(".8.5.0"):
            require(
                node.isfile()
                and node.mode == 0o755
                and node.size == 76584
                and type(body) is bytes
                and len(body) == 76584
                and digest(body) == LIBFFI_SHA256,
                "Los bytes o metadatos de libffi difieren de la biblioteca revisada.",
            )
        else:
            require(
                node.issym()
                and node.mode == 0o777
                and node.size == 0
                and node.linkname == "libffi.so.8.5.0"
                and body is None,
                "Un alias libffi no conserva el destino relativo exacto.",
            )
    for name, (descriptor, body) in _cli_rows(rows).items():
        if name == "usr/bin/clear":
            require(
                type(body) is bytes and len(body) == 17208 and digest(body) == CLEAR_SHA256,
                "Los bytes de clear no son los revisados para la omisión.",
            )
        else:
            require(
                body is None and descriptor["bytes"] == 0,
                "El alias CLI no puede incorporar otro cuerpo.",
            )


def derive_context(raw, core_raw):
    """Partir de f41; conservar la derivación FROM V3 y proyectar sólo libffi."""
    pinned(raw, CONTEXT_SHA256, 52152320)
    core = load_core(core_raw)
    v3, from_proof = core.derive_context(raw)
    require(digest(v3) == V3_CONTEXT_SHA256, "La derivación FROM congelada cambió.")
    rows = core.tar_rows(v3)
    _source_set([name for name, _, _ in rows], "rootfs/")
    _check_payload(rows)
    projected = _project_rows(rows)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:", format=tarfile.PAX_FORMAT) as archive:
        for _name, node, body in projected:
            archive.addfile(node, io.BytesIO(body) if body is not None else None)
    output = stream.getvalue()
    # Relectura por el mismo núcleo: nombres/tipos/trailer y tamaño permanecen acotados.
    reread = core.tar_rows(output)
    require(
        [n for n, _, _ in reread] == [n for n, _, _ in projected],
        "El TAR resultante no conserva sus nombres físicos.",
    )
    return output, {
        "schema_version": 1,
        "core_sha256": CORE_SHA256,
        "original_context_sha256": CONTEXT_SHA256,
        "v3_from_derivation": from_proof,
        "v3_context_sha256": V3_CONTEXT_SHA256,
        "projected_context_sha256": digest(output),
        "projected_bytes": len(output),
        "removed_directory": REMOVED,
        "mapping": dict(MAPPING),
        "library_sha256": LIBFFI_SHA256,
        "required_base_aliases": dict(ALIASES),
        "not_projected_manual_contributions": {
            name: {
                "source": descriptor,
                "sha256": digest(body) if body is not None else None,
                "projected": False,
                "base_target_preserved": "/bin/busybox",
            }
            for name, (descriptor, body) in _cli_rows(rows).items()
        },
        "complete_manual_payload_copied": False,
        "retained_payload_bytes_modes_targets_unchanged": True,
        "base_modified": False,
        "image_built": False,
        "runtime_approved": False,
    }


def _project_inventory(value):
    original, files, elf = value, value["files"], value["elf"]
    _source_set(files)
    for name in OMITTED:
        _check_cli_identity(name, files[name])
    require(
        files["usr/bin/clear"].get("sha256") == CLEAR_SHA256
        and elf.get("usr/bin/clear", {}).get("sha256") == CLEAR_SHA256
        and elf["usr/bin/clear"].get("bytes") == 17208,
        "El ELF/hash de clear no coincide con la omisión autorizada.",
    )
    directory = files[REMOVED]
    require(
        directory["kind"] == "dir"
        and directory["mode"] == 0o755
        and directory["uid"] == directory["gid"] == 0,
        "Directorio inventariado distinto.",
    )
    for old in MAPPING:
        row = files[old]
        require(row["uid"] == row["gid"] == 0, "Propietario inventariado distinto.")
        if old.endswith(".8.5.0"):
            require(
                row["kind"] == "file"
                and row["mode"] == 0o755
                and row["bytes"] == 76584
                and row["sha256"] == LIBFFI_SHA256,
                "Biblioteca inventariada distinta.",
            )
        else:
            require(
                row["kind"] == "symlink"
                and row["mode"] == 0o777
                and row["target"] == "libffi.so.8.5.0",
                "Alias inventariado distinto.",
            )
    source, target = "usr/lib64/libffi.so.8.5.0", "usr/lib/libffi.so.8.5.0"
    require(
        {n for n in elf if n == REMOVED or n.startswith(REMOVED + "/")} == {source}
        and target not in elf
        and elf[source]["sha256"] == LIBFFI_SHA256
        and elf[source]["bytes"] == 76584
        and elf[source]["soname"] == "libffi.so.8",
        "Inventario ELF de la biblioteca diferente o destino ocupado.",
    )
    require("physical_projection" not in original, "El inventario ya fue proyectado.")
    result = copy.deepcopy(original)
    del result["files"][REMOVED]
    for old, new in MAPPING.items():
        result["files"][new] = result["files"].pop(old)
    result["elf"][target] = result["elf"].pop(source)
    for name in OMITTED:
        del result["files"][name]
    del result["elf"]["usr/bin/clear"]
    result["physical_projection"] = {
        "schema_version": 1,
        "source_inventory_sha256": INVENTORY_SHA256,
        "source_context_sha256": CONTEXT_SHA256,
        "mapping": dict(MAPPING),
        "removed_directory": {"path": REMOVED, "source_metadata": copy.deepcopy(directory)},
        "required_base_aliases": dict(ALIASES),
        "source_package_records_unchanged": True,
        "not_projected_manual_contributions": {
            name: {
                "source_metadata": copy.deepcopy(original["files"][name]),
                "projected": False,
                "base_target_preserved": "/bin/busybox",
            }
            for name in OMITTED
        },
        "complete_manual_payload_copied": False,
        "base_modified": False,
        "runtime_approved": False,
    }
    return result


def derive_physical_inventory(raw, core_raw):
    """Nuevo catálogo físico; registros APK/SPDX/licencias de origen quedan intactos."""
    pinned(raw, INVENTORY_SHA256, 690216)
    core = load_core(core_raw)
    return _project_inventory(core.decode(raw))


def check_base(raw, core_raw, parser_raw):
    """Retornar la base OCI auténtica leída por cd320/9e0, sin extraer ni modificar."""
    core = load_core(core_raw)
    parser = core.load_parser(parser_raw)
    base = core.read_oci(raw, core.BASE_MANIFEST, parser)
    for name, target in ALIASES.items():
        row = base["files"].get(name, {})
        require(
            row.get("kind") == "symlink"
            and row.get("target") == target
            and row.get("mode") == 0o777
            and row.get("uid") == row.get("gid") == 0,
            "El alias base no coincide con los bytes/identidad revisados.",
        )
    for name in OMITTED:
        row = base["files"].get(name, {})
        require(
            row.get("kind") == "symlink"
            and row.get("target") == "/bin/busybox"
            and row.get("mode") == 0o777
            and row.get("uid") == row.get("gid") == 0,
            "Una de las siete CLI base no conserva el alias root0777 a busybox.",
        )
    require(
        not any(name in base["files"] for name in MAPPING.values()),
        "La base ya contiene un destino libffi; no se sobrescribe.",
    )
    return base
