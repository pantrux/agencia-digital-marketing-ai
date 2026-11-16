"""Finaliza evidencia de imagen existente; importar no ejecuta infraestructura."""

import base64
import hashlib
import json
import os
import re
import stat
import struct
from datetime import UTC, datetime

OBSERVATIONS_SHA = "c37deca7e96d13ac3025189803ff7e2c7d1354cda7d8b1d54c6222da8d71af2f"
RUNTIME_SOURCE = "9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49"
RUNTIME_LOCAL = "4369a61b3c82f56967d337c20ebe4ace144c9ec66660a63fcc76aa48465763f9"
STAGE = "finish-image-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def envelope(receipt):
    return {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}


def proposal(observations):
    return {
        "schema_version": 1,
        "stage": STAGE,
        "owner": observations["owner"],
        "observations_sha256": OBSERVATIONS_SHA,
        "historical_profile": observations["historical_profile"],
        "prepared_receipt_sha256": observations["prepared_receipt_sha256"],
        "failed_build_receipt_sha256": observations["failed_build_receipt_sha256"],
        "image_id": observations["image_id"],
        "config_digest": observations["config_digest"],
        "finish_directory": observations["finish_directory"],
        "budget_seconds": 600,
        "capture_limit_bytes": 4 * 1024**2,
        "sql_verified": False,
        "final_sca_verified": False,
    }


def validate_profile(profile, observations):
    expected = proposal(observations)
    if (
        digest(canonical(observations)) != OBSERVATIONS_SHA
        or set(profile) != set(expected) | {"sources"}
        or any(canonical(profile.get(k)) != canonical(v) for k, v in expected.items())
    ):
        raise ValueError("Perfil de finalización distinto de las referencias autorizadas.")
    if (
        set(profile["sources"]) != {"local", "remote", "observations"}
        or profile["sources"]["observations"] != OBSERVATIONS_SHA
        or any(re.fullmatch("[a-f0-9]{64}", v) is None for v in profile["sources"].values())
    ):
        raise ValueError("Pins de finalización incompletos.")


def read_exact_file(fd, name, observations):
    if name not in observations["files"]:
        raise ValueError("Archivo fuera de las tres observaciones exactas.")
    expected = observations["files"][name]
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)

    def metadata(info):
        return {
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode),
            "nlink": info.st_nlink,
            "size": info.st_size,
            "device": info.st_dev,
            "inode": info.st_ino,
        }

    with os.fdopen(source, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or metadata(before) != {k: expected[k] for k in metadata(before)}
            or before.st_size > 4 * 1024**2
        ):
            raise ValueError("Metadata exacta cambió: " + name)
        data = stream.read(4 * 1024**2 + 1)
        after = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (
            metadata(after) != metadata(before)
            or len(data) != before.st_size
            or digest(data) != expected["sha256"]
        ):
            raise ValueError("Bytes o identidad exactos cambiaron: " + name)
        return data


def resolve_identity(metadata, iidfile, inspection, log, observations):
    image, config = observations["image_id"], observations["config_digest"]
    if (
        metadata.get("containerimage.digest") != image
        or inspection.get("id") != image
        or inspection.get("descriptor", {}).get("digest") != image
        or inspection.get("descriptor", {}).get("annotations", {}).get("config.digest") != config
    ):
        raise ValueError("Manifiesto/config no coinciden entre metadata e inspect.")
    if (
        inspection.get("user") != "70:70"
        or inspection.get("entrypoint") != ["docker-entrypoint.sh"]
        or inspection.get("cmd") != ["postgres"]
        or inspection.get("os") != "linux"
        or inspection.get("architecture") != "amd64"
        or inspection.get("labels", {}).get("cl.e-scale.agencia.owner") != observations["owner"]
    ):
        raise ValueError("La imagen observada no acredita ownership, plataforma y UID70.")
    if "containerimage.config.digest" in metadata:
        if metadata["containerimage.config.digest"] != config or iidfile != config:
            raise ValueError("IID no corresponde al config explícito.")
        interpretation = "config"
    else:
        if iidfile != image:
            raise ValueError("IID no corresponde al fallback de manifiesto.")
        interpretation = "manifest-fallback"
    if re.findall(r"exporting manifest (sha256:[a-f0-9]{64}) done", log) != [image] or re.findall(
        r"exporting config (sha256:[a-f0-9]{64}) done", log
    ) != [config]:
        raise ValueError("Log no acredita exactamente los mismos IDs.")
    return {
        "image_id": image,
        "config_digest": config,
        "iidfile": iidfile,
        "iid_interpretation": interpretation,
        "config_metadata_present": "containerimage.config.digest" in metadata,
    }


def inspect_image(docker, image):
    template = '{"id":{{json .Id}},"descriptor":{{json .Descriptor}},"user":{{json .Config.User}},"entrypoint":{{json .Config.Entrypoint}},"cmd":{{json .Config.Cmd}},"labels":{{json .Config.Labels}},"os":{{json .Os}},"architecture":{{json .Architecture}}}'
    return json.loads(docker.capture(["image", "inspect", "--format", template, image], timeout=5))


def apply(request):
    observations = request["observations"]
    profile = request["profile"]
    validate_profile(profile, observations)
    profile_sha = digest(canonical(profile))
    if profile_sha != request["profile_sha256"] or profile["sources"]["remote"] != globals().get(
        "_FINISH_SOURCE_SHA256"
    ):
        raise ValueError("Helper/perfil distintos de los pins externos.")
    # Sólo se cargan fuentes públicas congeladas. Ninguna función apply/build del runtime se invoca.
    import types

    raw = base64.b64decode(request["runtime_source_b64"], validate=True)
    if digest(raw) != RUNTIME_SOURCE:
        raise ValueError("Runtime histórico distinto del congelado.")
    runtime = types.ModuleType("finish_fixed_runtime")
    runtime.__dict__["_RUNTIME_SOURCE_SHA256"] = RUNTIME_SOURCE
    exec(compile(raw, "<runtime-historico-fijado>", "exec"), runtime.__dict__)
    normal = runtime.load_module(
        base64.b64decode(request["sdk_source_b64"], validate=True),
        observations["sdk_remote_sha256"],
        "finish_fixed_sdk",
        "_SOURCE_SHA256",
    )
    layout = runtime.load_module(
        base64.b64decode(request["layout_source_b64"], validate=True),
        observations["layout_helper_sha256"],
        "finish_fixed_layout",
    )
    layout.host_contract(layout.inspect_host(), apply=True)
    descriptors, store, finish_fd = {}, None, None
    step = "preflight"
    export_process = None
    try:
        os.umask(0o077)
        descriptors = runtime.open_runtime(normal, create=False)
        record, context_fd = descriptors["record"], descriptors["context"]
        if runtime.tree_identity(normal, descriptors) != observations["directories"]:
            raise ValueError("Los namespaces propios cambiaron de identidad.")
        historic_raw = normal.read_file(record, "profile.json", maximum=512 * 1024)
        if digest(historic_raw) != observations["historical_profile"]["sha256"]:
            raise ValueError("El perfil histórico físico cambió de SHA.")
        historic = runtime.json_object(historic_raw)
        if canonical(historic) != historic_raw:
            raise ValueError("El perfil histórico no es canónico.")
        runtime.validate_profile(historic)
        old_request = {**request, "profile": historic}
        normal, compiled, verifier = runtime.modules(old_request)
        sdk_context = {
            "plan": historic["sdk_plan"],
            "layout_source_b64": request["layout_source_b64"],
        }
        normal.check_request(sdk_context)
        normal.verify_host(sdk_context)
        store = normal.Store(sdk_context)
        store.open()
        docker = store.docker()
        runtime.sdk_gates(old_request, normal, compiled, verifier, store, docker)
        prepared_raw = normal.read_file(record, "prepare.json", maximum=2 * 1024**2)
        prepared = normal.json_object(prepared_raw)
        prepared_envelope = envelope(prepared)
        runtime.validate_prepared(
            prepared_envelope, observations["historical_profile"]["sha256"], RUNTIME_SOURCE
        )
        if (
            prepared_envelope["receipt_sha256"] != observations["prepared_receipt_sha256"]
            or canonical(prepared) != prepared_raw
            or prepared["directories"] != observations["directories"]
        ):
            raise ValueError("Prepare físico distinto del respaldado.")
        failure_raw = normal.read_file(record, "build.json", maximum=512 * 1024)
        failure = normal.json_object(failure_raw)
        if (
            digest(failure_raw) != observations["failed_build_receipt_sha256"]
            or canonical(failure) != failure_raw
            or failure.get("success") is not False
            or failure.get("source_sha256") != RUNTIME_SOURCE
            or failure.get("profile_sha256") != observations["historical_profile"]["sha256"]
        ):
            raise ValueError("El fallo físico original no coincide con la historia.")
        sources = {
            name: read_exact_file(record, name, observations) for name in observations["files"]
        }
        metadata = runtime.json_object(sources["build-metadata.json"])
        iid = sources["image-id.txt"].decode("ascii").strip()
        log = sources["build.log"].decode("utf-8")
        initial_image = inspect_image(docker, observations["image_id"])
        identity = resolve_identity(metadata, iid, initial_image, log, observations)
        artifact = historic["postverified"]["receipt"]["artifact"]
        projected, excluded = runtime.project(artifact["runtime_files"])
        if projected != prepared["projected"] or excluded != prepared["excluded"]:
            raise ValueError("La proyección difiere de prepare.")
        if (
            set(os.listdir(context_fd))
            != {runtime.MARKER, "Dockerfile", ".dockerignore", "runtime-root"}
            or digest(normal.read_file(context_fd, "Dockerfile"))
            != historic["sources"]["dockerfile"]
            or normal.read_file(context_fd, ".dockerignore")
            != b"*\n!Dockerfile\n!runtime-root/\n!runtime-root/**\n"
        ):
            raise ValueError("El contexto histórico cambió.")
        overlay = runtime.snapshot_overlay(normal, context_fd, projected)
        if runtime.hash_archive(normal, record, "base.tar") != prepared["archive"]:
            raise ValueError("El TAR base histórico cambió.")
        runtime.verify_oci_tree(normal, record, prepared["oci_files"])
        base = runtime.read_oci(observations["record"] + "/base.tar", runtime.BASE_MANIFEST)
        if (
            runtime.validate_base(base) != prepared["base"]
            or runtime.abi_closure(base["files"], overlay) != prepared["abi"]
        ):
            raise ValueError("El cierre base/overlay difiere de prepare.")
        # Directorio nuevo EXCL, dentro del record ya respaldado; los originales no se reescriben.
        finish_fd = runtime.runtime_directory(normal, observations["finish_directory"], create=True)
        normal.write_file(finish_fd, "profile.json", canonical(profile))
        normal.write_file(finish_fd, "observations.json", canonical(observations))
        step = "image-save"
        process = docker.command(
            [
                "image",
                "save",
                "--output",
                observations["finish_directory"] + "/image.tar",
                observations["image_id"],
            ],
            timeout=120,
        )
        export_process = process["process"]
        if process["returncode"] != 0:
            failure = ValueError("La exportación de la imagen existente falló.")
            failure.process = export_process
            raise failure
        archive = runtime.hash_archive(normal, finish_fd, "image.tar")
        step = "oci-abi-verification"
        final = runtime.read_oci(
            observations["finish_directory"] + "/image.tar", observations["image_id"][7:]
        )
        if final["config_digest"] != identity["config_digest"]:
            raise ValueError("El config del OCI real no coincide con inspect/log.")
        abi = runtime.verify_final(base, final, projected)
        if (
            runtime.snapshot_overlay(normal, context_fd, projected) != overlay
            or inspect_image(docker, observations["image_id"]) != initial_image
        ):
            raise ValueError("La imagen/contexto cambió durante la finalización.")
        runtime.sdk_gates(old_request, normal, compiled, verifier, store, docker)
        if (
            runtime.tree_identity(normal, descriptors) != observations["directories"]
            or normal.read_file(record, "build.json", maximum=512 * 1024) != failure_raw
            or normal.read_file(record, "prepare.json", maximum=2 * 1024**2) != prepared_raw
            or normal.read_file(record, "profile.json", maximum=512 * 1024) != historic_raw
        ):
            raise ValueError("La historia original cambió durante la verificación.")
        for name, data in sources.items():
            if read_exact_file(record, name, observations) != data:
                raise ValueError("Cambió un archivo original de BuildKit.")
        receipt = {
            "stage": STAGE,
            "success": True,
            "owner": observations["owner"],
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "identity": identity,
            "image_id": identity["image_id"],
            "config_digest": identity["config_digest"],
            "archive": archive,
            "archive_path": observations["finish_directory"] + "/image.tar",
            "historical_profile_sha256": observations["historical_profile"]["sha256"],
            "prepared_receipt_sha256": observations["prepared_receipt_sha256"],
            "failed_build_receipt_sha256": observations["failed_build_receipt_sha256"],
            "observations_sha256": OBSERVATIONS_SHA,
            "metadata_original_sha256": digest(sources["build-metadata.json"]),
            "abi": abi,
            "packages": prepared["base"]["packages"],
            "export_process": process["process"],
            "original_build_process": None,
            "build_reexecuted": False,
            "sdk_executed": False,
            "sql_verified": False,
            "final_sca_verified": False,
        }
        normal.write_file(finish_fd, "receipt.json", canonical(receipt))
        return envelope(receipt)
    except BaseException as error:
        receipt = {
            "stage": STAGE,
            "success": False,
            "owner": observations["owner"],
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "step": step,
            "error_type": type(error).__name__,
            "detail": str(error)[:400],
            "process": getattr(error, "process", export_process),
            "original_failure_preserved": True,
            "build_reexecuted": False,
            "sdk_executed": False,
            "sql_verified": False,
            "final_sca_verified": False,
        }
        if finish_fd is not None:
            try:
                normal.write_file(finish_fd, "receipt.json", canonical(receipt))
            except BaseException:
                receipt["receipt_written"] = False
        return envelope(receipt)
    finally:
        if finish_fd is not None:
            os.close(finish_fd)
        for fd in descriptors.values():
            os.close(fd)
        if store is not None:
            store.close()


def main():
    import signal
    import sys

    def timeout(_signum, _frame):
        raise TimeoutError("Finalización de datos agotó600s.")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(600)
    try:
        header = sys.stdin.buffer.read(4)
        if len(header) != 4:
            raise ValueError("Petición truncada.")
        size = struct.unpack(">I", header)[0]
        if not 0 < size <= 768 * 1024:
            raise ValueError("Petición excesiva.")
        raw = sys.stdin.buffer.read(size)
        if len(raw) != size:
            raise ValueError("Petición truncada.")
        result = apply(json.loads(raw))
        print(json.dumps({"success": result["receipt"]["success"], "result": result}), flush=True)
        return 0 if result["receipt"]["success"] else 1
    except BaseException as error:
        print(
            json.dumps(
                {"success": False, "error_type": type(error).__name__, "values_hidden": True}
            ),
            flush=True,
        )
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
