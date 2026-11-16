"""Lector de export SDK propio como datos; no ejecuta ni cambia el contenedor."""

import base64
import hashlib
import json
import os
import struct
import tarfile
import types

CONTROLLER_SHA = "77f442612a9c14df8f0685474ce5e3f47c8c0da16e828888ee8250dc1d7e002b"
PROFILE_SHA = "aa4d28b1ac14c23ceae011fbf22e36a8060ec8e086af0da64aaa4d3ffe785640"
MAX_ARCHIVE = 3 * 1024**3


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def tar_metadata(stream, size, *, database_limit=4 * 1024**2):
    if not 1024 <= size <= MAX_ARCHIVE or size % 512:
        raise ValueError("TAR de exportación con tamaño no válido.")
    targets = {
        "lib",
        "lib/apk/db/installed",
        "usr/lib/apk/db/installed",
        "etc/os-release",
        "usr/lib/os-release",
    }
    found, members = {}, 0
    try:
        with tarfile.open(fileobj=stream, mode="r:") as package:
            for member in package:
                members += 1
                if (
                    members > 200000
                    or member.size < 0
                    or member.offset_data + ((member.size + 511) // 512) * 512 > size
                ):
                    raise ValueError("Inventario TAR excesivo o truncado.")
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                if name.lstrip("/") in targets and name.startswith("/"):
                    raise ValueError("Miembro objetivo absoluto.")
                if name not in targets:
                    continue
                if name in found:
                    raise ValueError("Miembro objetivo repetido.")
                if name == "lib":
                    if member.issym():
                        if member.linkname != "usr/lib":
                            raise ValueError("El alias lib no apunta exactamente a usr/lib.")
                        found[name] = {"link": "usr/lib"}
                    elif member.isdir():
                        found[name] = {"directory": True}
                    else:
                        raise ValueError("El alias lib tiene un tipo contradictorio.")
                elif name == "etc/os-release" and member.issym():
                    if member.linkname != "../usr/lib/os-release":
                        raise ValueError("os-release enlaza fuera del único destino permitido.")
                    found[name] = {"link": member.linkname}
                elif member.isfile():
                    limit = database_limit if name.endswith("/apk/db/installed") else 16384
                    if member.size > limit:
                        raise ValueError("Metadato objetivo excesivo.")
                    content = package.extractfile(member).read(limit + 1)
                    if len(content) != member.size:
                        raise ValueError("Metadato TAR truncado.")
                    found[name] = content
                else:
                    raise ValueError("Tipo no permitido en un metadato objetivo.")
            end = package.offset
        if end > size - 1024:
            raise ValueError("Falta el terminador TAR completo.")
        stream.seek(end)
        remaining = size - end
        while remaining:
            data = stream.read(min(1024**2, remaining))
            if not data or any(data):
                raise ValueError("Contenido no nulo después del final TAR.")
            remaining -= len(data)
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ValueError("La estructura completa del TAR no es válida.") from error
    legacy, merged = "lib/apk/db/installed", "usr/lib/apk/db/installed"
    if legacy in found and merged in found:
        raise ValueError("La DB APK está duplicada mediante dos alias.")
    database_path = merged if merged in found else legacy
    if database_path == merged:
        if found.get("lib") != {"link": "usr/lib"}:
            raise ValueError("La DB física usr/lib no tiene el alias lib acreditado.")
    elif found.get("lib") == {"link": "usr/lib"}:
        raise ValueError("La DB física lib contradice el alias usrmerge.")
    database = found.get(database_path)
    operating = found.get("etc/os-release")
    path = "etc/os-release"
    if isinstance(operating, dict):
        operating, path = found.get("usr/lib/os-release"), "usr/lib/os-release"
    if not isinstance(database, bytes) or not isinstance(operating, bytes):
        raise ValueError("Falta DB APK u os-release real.")
    return {
        "database": database,
        "database_path": database_path,
        "database_resolution": found.get("lib"),
        "os_release": operating,
        "os_release_path": path,
        "members": members,
    }


def fingerprint(info):
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def capture(request):
    audit = request["audit"]
    if digest(canonical(audit)) != request["audit_sha256"] or audit["sources"][
        "remote"
    ] != globals().get("_AUDIT_SOURCE_SHA256"):
        raise ValueError("El plan auditor o la fuente difieren del respaldo.")
    source = base64.b64decode(request["controller_source_b64"], validate=True)
    if digest(source) != CONTROLLER_SHA:
        raise ValueError("El controlador SDK cambió.")
    module = types.ModuleType("opencv_audit_fixed_controller")
    module.__dict__["_SOURCE_SHA256"] = CONTROLLER_SHA
    exec(compile(source, "<opencv-control-fijado>", "exec"), module.__dict__)
    context = {
        "plan": audit["sdk_plan"],
        "backed": audit["backed"],
        "layout_source_b64": request["layout_source_b64"],
    }
    module.check_request(context)
    module.verify_host(context)
    if audit["sdk_plan"]["profile_sha256"] != PROFILE_SHA:
        raise ValueError("Perfil SDK distinto del revisado.")
    store = module.Store(context)
    try:
        actual = store.receipts()
        for stage in ("prepare", "create", "start", "sdk", "export"):
            module.require_backed_receipt(stage, actual, audit["backed"])
        store.verify()
        docker = store.docker()
        cid = actual["create"]["receipt"]["cid"]
        before = module.check_container(docker.inspect(cid), cid, audit["sdk_plan"])
        exported = actual["export"]["receipt"]
        path = audit["sdk_plan"]["paths"]["control"] + "/sdk-rootfs.tar"
        if (
            exported["path"] != path
            or exported["cid"] != cid
            or not 1024 <= exported["bytes"] <= MAX_ARCHIVE
        ):
            raise ValueError("Exportación ajena o excesiva.")
        control = store.descriptors["control"]
        fd = os.open("sdk-rootfs.tar", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=control)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            module.checked_node(info, 0)
            if info.st_size != exported["bytes"]:
                raise ValueError("La exportación cambió de tamaño.")
            checksum = hashlib.sha256()
            while block := stream.read(1024**2):
                checksum.update(block)
            if checksum.hexdigest() != exported["sha256"]:
                raise ValueError("El SHA completo de la exportación no coincide.")
            stream.seek(0)
            metadata = tar_metadata(stream, info.st_size)
            if fingerprint(os.fstat(stream.fileno())) != fingerprint(info) or fingerprint(
                os.stat("sdk-rootfs.tar", dir_fd=control, follow_symlinks=False)
            ) != fingerprint(info):
                raise ValueError("La identidad o metadata del TAR cambió durante la lectura.")
        inputs = store.descriptors["inputs"]
        parent, name = module.open_parent(inputs, "data/manifest-sdk.json")
        try:
            manifest = module.read_file(parent, name, maximum=2 * 1024**2)
        finally:
            os.close(parent)
        if digest(manifest) != audit["sdk_plan"]["sdk_manifest_sha256"]:
            raise ValueError("El manifiesto suplementario difiere de su pin.")
        declaration = module.json_object(manifest)
        inputs_path = audit["sdk_plan"]["paths"]["inputs"]
        packager = module.load_code(
            os.path.join(inputs_path, "code/docker/kitsu-zou/package_opencv_minimal.py"),
            module.PACKAGER_SHA,
            "opencv_audit_pinned_packager",
        )
        index_info = declaration["wolfi_index"]

        def input_bytes(name, maximum):
            parent, leaf = module.open_parent(inputs, "data/" + name)
            try:
                return module.read_file(parent, leaf, maximum=maximum)
            finally:
                os.close(parent)

        index = packager.authenticate_index(
            input_bytes(index_info["path"], 16 * 1024**2),
            input_bytes(index_info["key_path"], 16384),
            index_sha256=index_info["sha256"],
            key_sha256=index_info["public_signing_digest"],
        )
        header_records = [
            dict(index.records[(row["name"], row["version"])])
            for row in declaration["direct_headers"]
        ]
        # Revalidar árboles RO y sus hashes de prepare; no ejecutar código del SDK.
        store.verify()
        after = module.check_container(docker.inspect(cid), cid, audit["sdk_plan"])
        if after != before:
            raise ValueError("El contenedor cambió durante la lectura.")
        return {
            "schema_version": 1,
            "audit_sha256": request["audit_sha256"],
            "source_sha256": audit["sources"]["remote"],
            "cid": cid,
            "container": after,
            "export_receipt_sha256": actual["export"]["receipt_sha256"],
            "archive_sha256": exported["sha256"],
            "archive": fingerprint(info),
            "members": metadata["members"],
            "database_b64": base64.b64encode(metadata["database"]).decode(),
            "database_path": metadata["database_path"],
            "database_resolution": metadata["database_resolution"],
            "database_sha256": digest(metadata["database"]),
            "os_release_b64": base64.b64encode(metadata["os_release"]).decode(),
            "os_release_sha256": digest(metadata["os_release"]),
            "os_release_path": metadata["os_release_path"],
            "manifest_b64": base64.b64encode(manifest).decode(),
            "manifest_sha256": digest(manifest),
            "material_proofs": actual["prepare"]["receipt"]["material_proofs"],
            "header_records": header_records,
            "sdk_executed": False,
            "container_mutated": False,
            "full_archive_transferred": False,
        }
    finally:
        store.close()


def main():
    import signal
    import sys

    def timeout(_signal, _frame):
        raise TimeoutError("Lectura de datos agotó el presupuesto de 180 segundos.")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(180)
    try:
        stream = sys.stdin.buffer.raw

        def exact(size):
            data = bytearray()
            while len(data) < size:
                block = stream.read(size - len(data))
                if not block:
                    raise ValueError("Petición de datos truncada.")
                data.extend(block)
            return bytes(data)

        size = struct.unpack(">I", exact(4))[0]
        if not 0 < size <= 2 * 1024**2:
            raise ValueError("Petición de datos excesiva.")
        request = json.loads(exact(size))
        if stream.read(1):
            raise ValueError("Bytes adicionales en petición.")
        result = capture(request)
        print(json.dumps({"success": True, "result": result}), flush=True)
        return 0
    except BaseException as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "detail": str(error)[:240],
                    "values_hidden": True,
                }
            ),
            flush=True,
        )
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
