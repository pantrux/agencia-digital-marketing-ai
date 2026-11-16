"""Reanuda únicamente el SDK detenido acreditado; importar no ejecuta acciones."""

import base64
import hashlib
import json
import os
import signal
import stat
import struct
import sys
import tarfile
import types
from pathlib import Path, PurePosixPath

CID = "f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d"
COMPILE_SHA = "31bcbb5f235d77b2890128fa7a525e06e677b940333036af0080505804618bb2"
PROFILE_V1_SHA = "81dddfe2adac7fb852ebb4a2167b4affb0eed898619a6c2f3890ace4b3162b0a"
FAILED_SHA = "5878733d8f7d09e3f1e7a2ff65df6f8a6c7d1318b74442da77672492b0a99d6d"
FAILED_LOG_SHA = "224e13a408ee7773965da41f208fa72f1317acda574f0e2f52cc8bbb27b9fe40"
WORK = "/work/.resume-agency-v1"
PROOF = "resume-v1-sdk-proof.json"
CONFIG = b"""[source.crates-io]
replace-with = "vendored-sources"
[source.vendored-sources]
directory = "/work/vendor"
[net]
offline = true
[unstable]
host-config = true
target-applies-to-host = true
[host]
rustflags = ["-Dwarnings", "-Ctarget-feature=-crt-static", "-Cpanic=unwind"]
[build]
rustflags = ["-Dwarnings", "-Ctarget-feature=-crt-static", "-Cpanic=unwind"]
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require_stopped(value, previous):
    state = value.get("State", {})
    if (
        value.get("Id") != CID
        or value.get("Created") != previous["created"]
        or state.get("Status") != "exited"
        or state.get("Running") is not False
        or state.get("Pid") != 0
        or state.get("StartedAt") != previous["started_at"]
        or state.get("ExitCode") != 0
        or any(
            state.get(name) is not False for name in ("OOMKilled", "Dead", "Paused", "Restarting")
        )
        or not state.get("FinishedAt")
        or state["FinishedAt"].startswith("0001-")
    ):
        raise ValueError("La instancia no acredita parada limpia del CID anterior.")


def require_restarted(value, previous):
    if (
        value.get("cid") != CID
        or value.get("created") != previous["created"]
        or value.get("running") is not True
        or type(value.get("pid")) is not int
        or value["pid"] <= 0
        or not value.get("started_at")
        or value["started_at"] == previous["started_at"]
        or value["started_at"].startswith("0001-")
    ):
        raise ValueError("Falta una instancia reiniciada nueva y acreditada.")


def safe_relative(name):
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(p in ("", ".", "..") for p in name.split("/"))
    ):
        raise ValueError("Ruta relativa no admitida.")
    return str(PurePosixPath(name))


def read_regular(path, maximum=512 * 1024**2):
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("Archivo no regular, enlazado o excesivo.")
        data = stream.read(maximum + 1)
        if len(data) != info.st_size:
            raise ValueError("El archivo cambió durante su lectura.")
        return data


def tree(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Raíz ausente o enlazada.")
    result = {}
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(base) / name).is_symlink():
                raise ValueError("Directorio enlazado.")
        for name in files:
            path = Path(base) / name
            label = str(path.relative_to(root)).replace("\\", "/")
            if path.is_symlink() or len(result) >= 25000:
                raise ValueError("Árbol enlazado o excesivo.")
            result[label] = digest(read_regular(path))
    return result


def verify_tree(root, expected):
    observed = tree(root)
    if observed != expected:
        raise ValueError("Los bytes o el inventario no coinciden con sus fuentes.")
    return {"files": len(observed), "tree_sha256": digest(canonical(observed))}


def tar_files(archive, prefix):
    result, total = {}, 0
    for member in archive:
        name = member.name.rstrip("/")
        safe_relative(name)
        if not member.isdir() and not member.isfile():
            raise ValueError("Tipo especial en archivo de fuentes.")
        if not member.isfile() or not name.startswith(prefix):
            continue
        relative = safe_relative(name[len(prefix) :])
        total += member.size
        if (
            relative in result
            or len(result) >= 25000
            or member.size > 512 * 1024**2
            or total > 2 * 1024**3
        ):
            raise ValueError("Archivo de fuentes ambiguo o excesivo.")
        result[relative] = digest(archive.extractfile(member).read())
    return result


def inside_verify():
    # Sólo lee los inputs y los bytes ya instalados; no invoca SDK/compiladores.
    lock = json.loads(read_regular("/inputs/native-sdk-lock.json"))
    blocks = read_regular("/lib/apk/db/installed").decode().split("\n\n")
    installed = {}
    for block in blocks:
        fields = dict(
            line.split(":", 1) for line in block.splitlines() if len(line) > 1 and line[1] == ":"
        )
        if "P" in fields:
            if fields["P"] in installed:
                raise ValueError("Paquete APK duplicado.")
            installed[fields["P"]] = fields["V"]
    expected = {k: v["V"] for k, v in lock["base_reused"].items()}
    # base_reused contiene sólo paquetes referenciados por la clausura; la copia
    # completa de 45 paquetes original se conserva en base-installed.before.
    before = {}
    before_raw = read_regular("/work/base-installed.before")
    if digest(before_raw) != lock["base_installed_sha256"]:
        raise ValueError("La copia de la base APK no conserva su SHA fijado.")
    for block in before_raw.decode().split("\n\n"):
        fields = dict(
            line.split(":", 1) for line in block.splitlines() if len(line) > 1 and line[1] == ":"
        )
        if "P" in fields:
            before[fields["P"]] = fields["V"]
    if len(before) != 45 or any(before.get(k) != v for k, v in expected.items()):
        raise ValueError("La base APK difiere de sus 45 paquetes originales.")
    expected = {**before, **{p["name"]: p["version"] for p in lock["packages"]}}
    if len(expected) != 110 or installed != expected:
        raise ValueError("Los 110 APK difieren de la clausura aprobada.")
    manifest = json.loads(read_regular("/inputs/musl-build-inputs.json"))
    if digest(read_regular("/work/source/Cargo.lock")) != manifest["cargo"]["lock_sha256"]:
        raise ValueError("Cargo.lock cambió.")
    with tarfile.open("/inputs/vectorchord-source.tar.gz", "r:gz") as archive:
        prefix = archive.getnames()[0].split("/")[0] + "/"
        source = tar_files(archive, prefix)
    source["crates/xtask/src/main.rs"] = (
        "9055ec9b313bcc827c52705f37bca6ceba4468de2073cb5de319e703684969b1"
    )
    source[".cargo/config.toml"] = digest(CONFIG)
    source_proof = verify_tree("/work/source", source)
    with tarfile.open("/inputs/vendor.tar.gz", "r:gz") as archive:
        vendor = tar_files(archive, "")
    vendor_proof = verify_tree("/work/vendor", vendor)
    sdk = {}
    for item in lock["rust_sdk"]:
        path = "/inputs/rust/" + item["component"] + ".tar.xz"
        if digest(read_regular(path)) != item["archive_sha256"]:
            raise ValueError("Un archivo SDK difiere del pin.")
        with tarfile.open(path, "r:xz") as archive:
            names = archive.getnames()
            root = names[0].split("/")[0] + "/"
            components = archive.extractfile(root + "components").read().decode().splitlines()
            if len(components) != 1:
                raise ValueError("SDK con componentes ambiguos.")
            prefix = root + safe_relative(components[0]) + "/"
            listed = archive.extractfile(prefix + "manifest.in").read().decode().splitlines()
            selected = []
            for line in listed:
                kind, name = line.split(":", 1)
                safe_relative(name)
                if kind != "file":
                    raise ValueError("El manifiesto SDK incorpora una entrada no revisada.")
                selected.append(name)
            payload = tar_files(archive, prefix)
            for name in selected:
                expected_sha = payload[name]
                if name in sdk and sdk[name] != expected_sha:
                    raise ValueError("SDK con archivos superpuestos distintos.")
                sdk[name] = expected_sha
                if digest(read_regular(Path("/opt/agency-rust") / name)) != expected_sha:
                    raise ValueError("Los bytes instalados del SDK cambiaron.")
    partial = {}
    for root in (
        "/work/.compile-agency-v1",
        "/outputs/compile-v1",
        "/opt/agency-sqlite",
        "/work/target",
    ):
        if not Path(root).exists():
            partial[root] = {"exists": False}
            continue
        observed = {}
        for base, directories, files in os.walk(root, followlinks=False):
            for name in directories + files:
                path = Path(base) / name
                relative = str(path.relative_to(root))
                if path.is_symlink():
                    if (
                        str(path) != "/opt/agency-sqlite/lib/libsqlite3.so"
                        or os.readlink(path) != "libsqlite3.so.0"
                    ):
                        raise ValueError("Enlace no admitido en los parciales V1.")
                    observed[relative] = {"link": "libsqlite3.so.0"}
                elif path.is_file():
                    observed[relative] = digest(read_regular(path))
                elif not path.is_dir():
                    raise ValueError("Archivo especial en los parciales V1.")
                if len(observed) > 25000:
                    raise ValueError("Parciales excesivos.")
        partial[root] = {
            "exists": True,
            "files": len(observed),
            "tree_sha256": digest(canonical(observed)),
        }
    probe = json.loads(read_regular("/outputs/compile-v1/sqlite-probe.json"))
    if probe != {
        "version": "3.54.1",
        "number": 3054001,
        "source_id": "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76",
        "fts5": True,
        "threadsafe": True,
    }:
        raise ValueError("La sonda SQLite V1 no corresponde al fallo conservado.")
    proof = {
        "verified": True,
        "sdk_executed": False,
        "compiler_executed": False,
        "apk_count": len(installed),
        "apk_versions_sha256": digest(canonical(installed)),
        "apk_database_sha256": digest(read_regular("/lib/apk/db/installed")),
        "source": source_proof,
        "vendor": vendor_proof,
        "rust_payload_files": len(sdk),
        "rust_payload_sha256": digest(canonical(sdk)),
        "partials": partial,
        "sqlite_v1_probe_sha256": digest(canonical(probe)),
    }
    descriptor = os.open(
        "/outputs/" + PROOF, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(proof))
        stream.flush()
        os.fsync(stream.fileno())


def module_from_bytes(name, raw, expected):
    if digest(raw) != expected:
        raise ValueError("Fuente auxiliar distinta del pin congelado.")
    result = types.ModuleType(name)
    exec(compile(raw, "<fuente-fijada>", "exec"), result.__dict__)
    return result


def validate_profile(profile):
    previous = profile["compile_profile"]
    failure = profile["failure"]
    if (
        digest(canonical(previous)) != PROFILE_V1_SHA
        or failure != {"receipt": failure["receipt"], "receipt_sha256": FAILED_SHA}
        or digest(canonical(failure["receipt"])) != FAILED_SHA
        or failure["receipt"].get("log_sha256") != FAILED_LOG_SHA
        or failure["receipt"].get("success") is not False
        or profile.get("cid") != CID
        or profile.get("schema_version") != 1
    ):
        raise ValueError("La reanudación no corresponde al único fallo aprobado.")


def envelope(receipt):
    return {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}


def apply(request):
    profile = request["profile"]
    validate_profile(profile)
    profile_sha = digest(canonical(profile))
    if (
        profile_sha != request["profile_sha256"]
        or globals().get("_RESUME_SOURCE_SHA256") != profile["sources"]["remote"]
    ):
        raise ValueError("Perfil y fuente resume diferentes del respaldo.")
    compiler = module_from_bytes(
        "resume_compile_v1",
        base64.b64decode(request["compile_source_b64"], validate=True),
        COMPILE_SHA,
    )
    normal = compiler.load_sdk(request)
    context = {
        "plan": profile["compile_profile"]["sdk_plan"],
        "layout_source_b64": request["layout_source_b64"],
    }
    normal.check_request(context)
    normal.verify_host(context)
    store = normal.Store(context)
    docker, armed, step = None, False, "preflight"
    try:
        if request["action"] == "stop":
            store.descriptors["control"] = normal.marked_directory(normal.CONTROL)
            previous = profile["compile_profile"]["backed"]["prepare"]["receipt"]["directories"][
                "control"
            ]["directory"]
            if normal.identity(os.fstat(store.descriptors["control"])) != previous:
                raise ValueError("Control distinto antes de la parada propia.")
            return {"success": True, "cleanup": normal.stop_owned(store.docker(), CID)}
        store.open()
        compiler.verify_sdk_store(store, normal, profile["compile_profile"])
        control = store.descriptors["control"]
        if normal.read_file(control, "compile-v1-receipt.json") != canonical(
            profile["failure"]["receipt"]
        ):
            raise ValueError("El fallo remoto no coincide con el respaldo.")
        if (
            digest(normal.read_file(control, "compile-v1.log", maximum=normal.MAX_OUTPUT))
            != FAILED_LOG_SHA
        ):
            raise ValueError("El log inicial cambió.")
        for name, expected in (
            ("compile-v1-profile.json", PROFILE_V1_SHA),
            ("compile-v1-verifier.py", COMPILE_SHA),
            ("compile-v1-source.sh", profile["compile_profile"]["sources"]["recipe"]),
        ):
            if digest(normal.read_file(control, name)) != expected:
                raise ValueError("Una fuente histórica de compile V1 cambió.")
        docker = store.docker()
        raw = docker.inspect(CID)
        observed = normal.check_container(raw, CID)
        previous = profile["compile_profile"]["backed"]["start"]["receipt"]["container"]
        require_stopped(raw, previous)
        base = {
            "cid": CID,
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "failed_receipt_sha256": FAILED_SHA,
            "container": observed,
            "inspection_sha256": digest(canonical(raw)),
        }
        if request["action"] == "observe-stopped":
            receipt = {**base, "stage": "observe-stopped", "success": True, "sdk_executed": False}
            normal.write_file(control, "resume-v1-stopped.json", canonical(receipt))
            return {"success": True, "result": envelope(receipt)}
        if request["action"] != "restart":
            raise ValueError("Acción resume no admitida.")
        stopped = request.get("stopped", {})
        actual = normal.read_file(control, "resume-v1-stopped.json")
        if (
            stopped != envelope(normal.json_object(actual))
            or stopped["receipt"].get("inspection_sha256") != base["inspection_sha256"]
            or stopped["receipt"].get("profile_sha256") != profile_sha
        ):
            raise ValueError("STOPPED_B64 debe respaldar literalmente el estado observado.")
        os.mkdir(".resume-v1-started", 0o700, dir_fd=control)
        armed = True
        step = "reiniciar_hold"
        normal.write_file(control, "resume-v1-profile.json", canonical(profile))
        source = base64.b64decode(request["resume_source_b64"], validate=True)
        if digest(source) != profile["sources"]["remote"]:
            raise ValueError("Fuente resume diferente antes de copiar.")
        normal.write_file(control, "resume-v1-verifier.py", source)
        started = docker.command(["start", CID], timeout=10)
        if started["returncode"] != 0 or started["output"].decode().strip() != CID:
            raise ValueError("Docker no confirmó el reinicio del CID exacto.")
        current = normal.check_container(docker.inspect(CID), CID)
        require_restarted(current, previous)
        limits = docker.limits(current)
        step = "copiar_verificador"
        result = docker.command(["exec", CID, "/bin/mkdir", "-m", "700", WORK], timeout=5)
        if result["returncode"] != 0:
            raise ValueError("Ya existe el directorio de reanudación.")
        result = docker.command(
            ["cp", normal.CONTROL + "/resume-v1-verifier.py", CID + ":" + WORK + "/verifier.py"],
            timeout=10,
        )
        if result["returncode"] != 0:
            raise ValueError("No se pudo copiar el verificador.")
        checks = docker.capture(
            ["exec", CID, "/usr/bin/sha256sum", WORK + "/verifier.py"], timeout=5
        ).decode()
        if checks != profile["sources"]["remote"] + "  " + WORK + "/verifier.py\n":
            raise ValueError("El verificador copiado no conserva su SHA.")
        docker.heartbeat_fd = request["heartbeat_fd"]
        step = "verificar_bytes_instalados"
        log = os.open(
            "resume-v1-verification.log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=control,
        )
        with os.fdopen(log, "wb", buffering=0) as sink:
            result = docker.command(
                [
                    "exec",
                    "--env",
                    "AGENCY_RESUME_SOURCE_SHA256=" + profile["sources"]["remote"],
                    CID,
                    "/usr/bin/python3",
                    "-B",
                    WORK + "/verifier.py",
                    "inside-verify",
                ],
                timeout=180,
                sink=sink,
            )
            sink.flush()
            os.fsync(sink.fileno())
        if result["returncode"] != 0:
            error = ValueError("La verificación instalada falló.")
            error.process = result["process"]
            raise error
        step = "catalogar_prueba"
        proof_raw = normal.read_file(store.descriptors["outputs"], PROOF, maximum=256 * 1024)
        proof = normal.json_object(proof_raw)
        if (
            proof_raw != canonical(proof)
            or proof.get("verified") is not True
            or proof.get("sdk_executed") is not False
            or proof.get("apk_count") != 110
        ):
            raise ValueError("La prueba instalada no está acreditada.")
        after = normal.check_container(docker.inspect(CID), CID)
        if after != current:
            raise ValueError("La instancia cambió durante la verificación.")
        docker.limits(after)
        compiler.verify_sdk_store(store, normal, profile["compile_profile"])
        if (
            docker.capture(
                ["exec", CID, "/usr/bin/sha256sum", WORK + "/verifier.py"], timeout=5
            ).decode()
            != checks
        ):
            raise ValueError("El verificador cambió durante la lectura del SDK.")
        log_raw = normal.read_file(control, "resume-v1-verification.log", maximum=normal.MAX_OUTPUT)
        receipt = {
            **base,
            "stage": "restart",
            "success": True,
            "container": after,
            "stopped_receipt_sha256": stopped["receipt_sha256"],
            "effective_limits": limits,
            "start_process": started["process"],
            "verification_process": result["process"],
            "proof_sha256": digest(proof_raw),
            "proof": proof,
            "sdk_executed": False,
            "log_sha256": digest(log_raw),
            "log_bytes": len(log_raw),
        }
        normal.write_file(control, "resume-v1-restart.json", canonical(receipt))
        return {"success": True, "result": envelope(receipt)}
    except BaseException as error:
        if hasattr(signal, "alarm"):
            signal.alarm(0)
        cleanup = None
        if armed and docker is not None:
            try:
                cleanup = normal.stop_owned(docker, CID)
            except BaseException as failed:
                cleanup = {"stopped": False, "error_type": type(failed).__name__}
        receipt = {
            "stage": request["action"],
            "step": step,
            "success": False,
            "cid": CID,
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "error_type": type(error).__name__,
            "process": getattr(error, "process", None),
            "cleanup": cleanup,
            "partial_evidence_preserved": True,
        }
        if armed:
            try:
                log_raw = normal.read_file(
                    store.descriptors["control"],
                    "resume-v1-verification.log",
                    maximum=normal.MAX_OUTPUT,
                )
                receipt.update(log_sha256=digest(log_raw), log_bytes=len(log_raw))
            except FileNotFoundError:
                pass
            normal.write_file(
                store.descriptors["control"], "resume-v1-restart.json", canonical(receipt)
            )
        return {"success": False, "result": envelope(receipt)}
    finally:
        store.close()


def exact_read(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            raise ValueError("Frame truncado.")
        result.extend(chunk)
    return bytes(result)


def main():
    if sys.argv[1:] == ["inside-verify"]:
        if (
            os.name != "posix"
            or os.geteuid() != 0
            or not Path("/.dockerenv").is_file()
            or digest(read_regular(__file__)) != os.environ.get("AGENCY_RESUME_SOURCE_SHA256")
        ):
            raise ValueError("Verificación sólo en el CID Linux propio.")
        inside_verify()
        return 0
    request = {}
    try:
        if os.name != "posix" or os.geteuid() != 0 or sys.argv[1:]:
            raise ValueError("El helper requiere root Linux sin argumentos.")
        os.umask(0o077)

        def interrupted(_signal, _frame):
            raise InterruptedError("Reanudación interrumpida.")

        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGALRM"):
            signal.signal(getattr(signal, name), interrupted)
        # Deja veinte segundos adicionales al cliente para parar el CID propio.
        signal.alarm(240)
        stream = sys.stdin.buffer.raw
        header = exact_read(stream, 4)
        length = struct.unpack(">I", header)[0]
        if not 0 < length <= 512 * 1024:
            raise ValueError("Frame excesivo.")
        data = exact_read(stream, length)
        request = json.loads(data)
        request["heartbeat_fd"] = sys.stdin.fileno()
        response = apply(request)
        print(json.dumps(response), flush=True)
        return 0 if response["success"] else 1
    except BaseException as error:
        print(json.dumps({"success": False, "error_type": type(error).__name__}), flush=True)
        return 1
    finally:
        if hasattr(signal, "alarm"):
            signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
