"""Verifica salidas existentes del CID detenido; no ejecuta SDK al importar."""

import base64
import hashlib
import http.client
import io
import json
import os
import re
import socket
import stat
import struct
import tarfile
import types
from datetime import UTC, datetime
from urllib.parse import quote

CID = "f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d"
COMPILE_SOURCE = "e0e2bc46a65563e122a1469ac9ed5040836697748846e0408938e208e7049a3d"
COMPILE_LOCAL = "eeced2b0f9f5572832a8e2cf7e4d18349818280985da9a7990fd4d6859e755cb"
COMPILE_RECIPE = "851cedce1107d751e50a598ef825e585ed82148755b8f83aed66a9e996b1cd19"
COMPILE_PROFILE = "bffefb81179fd767d640faf106965ba7d465e90c3a789b4e7800b988a0152153"
SDK_SOURCE = "a13ab96098b5f739ed88cbacaffc5bed521825aeb033d428bddbf93be57137ed"
SDK_LOCAL = "685bb8a54fc7d65fd25710b5ea317f8eda239b57aa2978a0ff5e33b8aabba608"
SDK_PLAN = "ba4a6f71026bf2af93bdd2b530f5233ff887936b02e61ef60983dc6565a4c15e"
FAILED_SHA = "b460fc786cceadbfa92e84093acfeb15f1d7013d7dedb65a0b68690931051d0e"
STOP_SHA = "7c2a9fa9223d1d9b745fe648637273470fe351a17b1a6c26a637672c214cf95e"
LOG_SHA = "b23d2c02948b0637e483ab9691f71e660d5196616fea1573f9becb0cfeccca54"
ARCHIVE_PATH = (
    "/work/.compile-agency-v3/target/x86_64-unknown-linux-musl/release/build/libsqlite3-sys"
)
MAX_ARCHIVE = 4 * 1024**2
MAX_FILE = 256 * 1024**2
STAGE = "postverify-v3"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def envelope(receipt):
    return {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}


def require_gates(failed, stopped):
    f, s = failed.get("receipt", {}), stopped.get("receipt", {})
    if (
        failed != envelope(f)
        or stopped != envelope(s)
        or failed["receipt_sha256"] != FAILED_SHA
        or f.get("success") is not False
        or f.get("stage") != "compile"
        or f.get("cid") != CID
        or f.get("source_sha256") != COMPILE_SOURCE
        or f.get("profile_sha256") != COMPILE_PROFILE
        or f.get("log_sha256") != LOG_SHA
        or stopped["receipt_sha256"] != STOP_SHA
        or s.get("success") is not True
        or s.get("stage") != "stop"
        or s.get("cid") != CID
        or s.get("source_sha256") != SDK_SOURCE
        or s.get("plan_sha256") != SDK_PLAN
        or s.get("stopped") is not True
        or type(s.get("after_pid")) is not int
        or s["after_pid"] != 0
    ):
        raise ValueError(
            "Se requieren fallo V3 y STOP literales respaldados; no un compile ficticio positivo."
        )


def extract_build_output(data):
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE:
        raise ValueError("Archivo Docker excesivo o inválido.")
    inventory, outputs, roots = {}, [], set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            if name.startswith("./"):
                name = name[2:]
            if (
                not name
                or name.startswith("/")
                or "\\" in name
                or "\0" in name
                or any(x in ("", ".", "..") for x in name.split("/"))
                or name in inventory
                or len(inventory) >= 128
            ):
                raise ValueError("Nombre de archivo ambiguo, repetido o fuera del crate.")
            legacy = re.fullmatch(r"(libsqlite3-sys-[a-f0-9]{16,64})(?:/(.*))?", name)
            nested = re.fullmatch(r"libsqlite3-sys(?:/([a-f0-9]{16,64})(?:/(.*))?)?", name)
            if legacy:
                root, relative, layout = legacy[1], legacy[2], "legacy"
                roots.add(root)
            elif nested:
                root = "libsqlite3-sys/" + nested[1] if nested[1] else "libsqlite3-sys"
                relative, layout = nested[2], "nested"
                if nested[1]:
                    roots.add(root)
            else:
                raise ValueError("Archivo extra ajeno a libsqlite3-sys.")
            if (
                member.uid != 0
                or member.gid != 0
                or member.mode & 0o7022
                or member.issym()
                or member.islnk()
            ):
                raise ValueError("Archivo Docker enlazado o de propietario/permisos ajenos.")
            if member.isdir():
                if relative not in (None, "", "out") and not relative.startswith("out/"):
                    raise ValueError("Directorio auxiliar no permitido.")
                inventory[name] = {"kind": "directory"}
                continue
            if not member.isfile() or not relative or member.size > MAX_ARCHIVE:
                raise ValueError("Raíz/file especial o excesivo en el archivo.")
            if relative not in (
                "output",
                "root-output",
                "stderr",
                "invoked.timestamp",
                "build-script-build",
            ) and not relative.startswith("out/"):
                raise ValueError("Archivo auxiliar no declarado para Cargo.")
            payload = archive.extractfile(member).read(MAX_ARCHIVE + 1)
            if len(payload) != member.size:
                raise ValueError("Archivo Docker truncado.")
            inventory[name] = {"kind": "file", "size": len(payload), "sha256": digest(payload)}
            if relative == "output":
                if not 0 < len(payload) <= 65536:
                    raise ValueError("Build output vacío o excesivo.")
                outputs.append({"path": name, "layout": layout, "output": payload})
    if len(outputs) != 1 or len(roots) != 1:
        raise ValueError("Se requiere un único build output y un único hash Cargo.")
    return {**outputs[0], "archive_sha256": digest(data), "inventory": inventory}


def fetch_archive(connection):
    try:
        connection.request(
            "GET", "/containers/" + CID + "/archive?path=" + quote(ARCHIVE_PATH, safe="")
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("Docker archive no devolvió la ruta exacta del crate detenido.")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_ARCHIVE):
            raise ValueError("Respuesta Docker archive excesiva.")
        result = bytearray()
        while True:
            chunk = response.read(min(65536, MAX_ARCHIVE + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > MAX_ARCHIVE:
                raise ValueError("Docker archive excedió 4 MiB.")
        if length is not None and len(result) != int(length):
            raise ValueError("Respuesta Docker archive truncada.")
        return bytes(result)
    finally:
        connection.close()


class DockerSocket(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=3)

    def connect(self):
        directory = os.open("/run", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            socket_info = os.stat("docker.sock", dir_fd=directory, follow_symlinks=False)
            if (
                info.st_uid != 0
                or info.st_mode & 0o022
                or socket_info.st_uid != 0
                or not stat.S_ISSOCK(socket_info.st_mode)
                or socket_info.st_mode & 0o002
            ):
                raise ValueError("El socket Docker local no acredita root y tipo esperado.")
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect("/run/docker.sock")
            current = os.stat("docker.sock", dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (socket_info.st_dev, socket_info.st_ino):
                self.sock.close()
                raise ValueError("El socket Docker cambió durante la conexión.")
        finally:
            os.close(directory)


def load_module(raw, expected, name, flag=None):
    if digest(raw) != expected:
        raise ValueError("Fuente distinta del helper congelado.")
    module = types.ModuleType(name)
    if flag:
        module.__dict__[flag] = expected
    exec(compile(raw, "<" + name + "-fijado>", "exec"), module.__dict__)
    return module


def validate_profile(profile):
    if (
        profile.get("schema_version") != 1
        or profile.get("cid") != CID
        or profile.get("archive_path") != ARCHIVE_PATH
        or profile.get("budget_seconds") != 180
        or profile.get("maximum_archive_bytes") != MAX_ARCHIVE
    ):
        raise ValueError("El perfil postverify difiere de CID/ruta/presupuesto fijos.")
    compile_profile = profile["compile_profile"]
    if (
        digest(canonical(compile_profile)) != COMPILE_PROFILE
        or compile_profile["sources"]
        != {"local": COMPILE_LOCAL, "remote": COMPILE_SOURCE, "recipe": COMPILE_RECIPE}
        or profile["sdk_plan"] != compile_profile["sdk_plan"]
    ):
        raise ValueError("El perfil compile V3 histórico no coincide con el freeze.")
    expected = {**compile_profile["backed"], "stop": profile["sdk_backed"]["stop"]}
    if expected != profile["sdk_backed"]:
        raise ValueError("Sólo se permite añadir STOP al histórico SDK.")
    require_gates(profile["failed"], profile["sdk_backed"]["stop"])
    if set(profile["sources"]) != {"local", "remote"} or any(
        not re.fullmatch("[a-f0-9]{64}", v) for v in profile["sources"].values()
    ):
        raise ValueError("Pins postverify incompletos.")


def read_owned(normal, fd, name, maximum=MAX_FILE):
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(source, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) not in (0o600, 0o644, 0o755, 0o444)
            or info.st_size > maximum
        ):
            raise ValueError("Salida existente no es archivo propio ordinario de permisos seguros.")
        data = stream.read(maximum + 1)
        if len(data) != info.st_size or normal.identity(
            os.stat(name, dir_fd=fd, follow_symlinks=False)
        ) != normal.identity(info):
            raise ValueError("La salida cambió mientras se leía.")
        return data, {"size": len(data), "sha256": digest(data), "mode": stat.S_IMODE(info.st_mode)}


def catalog(normal, compiled, build_output, texts):
    root = normal.open_directory(normal.OUTPUTS + "/compile-v3")
    runtime = normal.open_directory(normal.OUTPUTS + "/compile-v3/runtime-root")
    try:
        if set(os.listdir(root)) != {"runtime-root", "sqlite-probe.json"}:
            raise ValueError("Compile V3 tiene salidas adicionales o un recibo previo inesperado.")
        files = {}

        def walk(fd, prefix=""):
            info = os.fstat(fd)
            if (
                info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) not in (0o700, 0o755)
            ):
                raise ValueError("Directorio de salidas ajeno o escribible por otros.")
            for entry in os.scandir(fd):
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
                    if not re.fullmatch(
                        r"usr/local/(?:lib/postgresql|share/postgresql/extension|include/postgresql/server/extension|share/licenses)/[A-Za-z0-9_+./-]+",
                        name,
                    ) or ".." in name.split("/"):
                        raise ValueError("Ruta fuera del árbol propio de extensiones.")
                    _, files[name] = read_owned(normal, fd, entry.name)

        walk(runtime)
        if not 5 <= len(files) <= 256:
            raise ValueError("Inventario runtime vacío o excesivo.")

        # open_parent del SDK exige0700; el árbol make install puede ser0755.
        def safe_artifact(name):
            current = os.dup(runtime)
            try:
                for part in name.split("/")[:-1]:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                    )
                    info = os.fstat(child)
                    if (
                        info.st_uid != 0
                        or info.st_gid != 0
                        or stat.S_IMODE(info.st_mode) not in (0o700, 0o755)
                    ):
                        os.close(child)
                        raise ValueError("Ancestro del artefacto ajeno o escribible.")
                    os.close(current)
                    current = child
                return read_owned(normal, current, name.split("/")[-1])[0]
            finally:
                os.close(current)

        pkglib = "usr/local/lib/postgresql/"
        link = compiled.sqlite_link_proof(
            safe_artifact(pkglib + "vchord.so"), safe_artifact(pkglib + "libsqlite3.so.0")
        )
        vector = compiled.elf_report(safe_artifact(pkglib + "vector.so"))
        if vector["glibc_versions"] or "libc.so.6" in vector["needed"]:
            raise ValueError("pgvector no acredita ABI musl.")
        versions = compiled.extension_versions(safe_artifact)
        for name in files:
            if (
                name.startswith("usr/local/lib/postgresql/")
                and name
                not in {
                    "usr/local/lib/postgresql/vector.so",
                    "usr/local/lib/postgresql/vchord.so",
                    "usr/local/lib/postgresql/libsqlite3.so.0",
                }
                and not name.startswith("usr/local/lib/postgresql/bitcode/")
            ):
                raise ValueError("Biblioteca runtime adicional no aprobada: " + name)
            if name.startswith("usr/local/share/postgresql/extension/"):
                leaf = name.rsplit("/", 1)[-1]
                if not re.fullmatch(
                    r"(?:vector|vchord)(?:[.]control|--[0-9][0-9A-Za-z_.+-]*[.]sql)", leaf
                ):
                    raise ValueError("Control o SQL adicional no aprobado.")
                text = safe_artifact(name).decode("utf-8")
                if not text.strip() or len(text.encode()) > 2 * 1024**2 or "\0" in text:
                    raise ValueError("Control/SQL vacío, excesivo o no textual.")
        for name, expected in texts.items():
            if name not in files or safe_artifact(name) != expected:
                raise ValueError("SQL/control/licencia upstream difiere: " + name)
        vector_sql = {
            n for n in files if n.startswith("usr/local/share/postgresql/extension/vector")
        }
        if vector_sql != {
            n for n in texts if n.startswith("usr/local/share/postgresql/extension/vector")
        }:
            raise ValueError("El conjunto SQL pgvector difiere de la fuente fijada.")
        expected_licenses = {
            "pgvector-LICENSE",
            "VectorChord-LICENSE",
            "VectorChord-AGPLv3",
            "VectorChord-ELv2",
            "SQLite-NOTICE",
        }
        if {
            n.rsplit("/", 1)[-1]
            for n in files
            if n.startswith("usr/local/share/licenses/agency-postgres-immich/")
        } != expected_licenses or any(
            not safe_artifact("usr/local/share/licenses/agency-postgres-immich/" + n)
            for n in expected_licenses
        ):
            raise ValueError("Licencias/avisos runtime incompletos.")
        probe_raw = normal.read_file(root, "sqlite-probe.json", maximum=65536)
        probe = normal.json_object(probe_raw)
        if probe_raw != canonical(probe):
            raise ValueError("Sonda SQLite no canónica.")
        compiled.validate_sqlite_probe(probe)
        compiled.validate_sqlite_build_output(build_output.decode())
        return {
            "stage": STAGE,
            "verified": True,
            "compile_profile_sha256": COMPILE_PROFILE,
            "input_manifest_sha256": normal.KIT_SHA,
            "features_sha256": compiled.FEATURE_RAW_SHA,
            "settings": compiled.settings(),
            "sqlite_probe": probe,
            "link_proof": link,
            "vector_elf": vector,
            "extension_versions": versions,
            "runtime_files": files,
            "build_script_output_sha256": digest(build_output),
            "sql_verified": False,
            "final_sca_verified": False,
        }
    finally:
        os.close(runtime)
        os.close(root)


def source_texts(normal, store):
    import zipfile

    source = store.descriptors["inputs"]
    expected = {}
    for name, prefix, licenses in (
        (
            "pgvector-source.tar.gz",
            "pgvector-159b79aaad5983fb7459c1e3df2897fbb2d11788/",
            {"LICENSE": "pgvector-LICENSE"},
        ),
        (
            "vectorchord-source.tar.gz",
            "VectorChord-f9b5e234c44cee69b266b8431f92b7deed4e99d3/",
            {
                "LICENSE": "VectorChord-LICENSE",
                "licenses/LICENSE.AGPLv3": "VectorChord-AGPLv3",
                "licenses/LICENSE.ELv2": "VectorChord-ELv2",
            },
        ),
    ):
        raw = normal.read_file(source, name, maximum=32 * 1024**2)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            seen = set()
            for member in archive:
                relative = member.name.removeprefix(prefix)
                if not member.name.startswith(prefix):
                    continue
                target = None
                if relative in licenses:
                    target = "usr/local/share/licenses/agency-postgres-immich/" + licenses[relative]
                elif name == "pgvector-source.tar.gz":
                    if relative == "vector.control":
                        target = "usr/local/share/postgresql/extension/vector.control"
                    elif relative == "sql/vector.sql":
                        target = "usr/local/share/postgresql/extension/vector--0.8.5.sql"
                    elif re.fullmatch(r"sql/vector--[0-9A-Za-z_.+-]+[.]sql", relative):
                        target = "usr/local/share/postgresql/extension/" + relative.split("/")[-1]
                if target:
                    if target in seen or not member.isfile() or member.size > 1024 * 1024:
                        raise ValueError("Texto upstream ambiguo o no ordinario.")
                    seen.add(target)
                    expected[target] = archive.extractfile(member).read()
    raw = normal.read_file(source, "sqlite-amalgamation-3540100.zip", maximum=32 * 1024**2)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        member = "sqlite-amalgamation-3540100/sqlite3.c"
        if archive.namelist().count(member) != 1:
            raise ValueError("Fuente SQLite ambigua.")
        text = archive.read(member)
        expected["usr/local/share/licenses/agency-postgres-immich/SQLite-NOTICE"] = b"".join(
            text.splitlines(keepends=True)[:25]
        )
    return expected


def stopped_container(normal, docker):
    observed = normal.check_container(docker.inspect(CID), CID)
    if observed["running"] or observed["pid"] != 0:
        raise ValueError("Postverify sólo admite el CID propio detenido/PID0.")
    return observed


def apply(request):
    profile = request["profile"]
    validate_profile(profile)
    profile_sha = digest(canonical(profile))
    if profile_sha != request["profile_sha256"] or profile["sources"]["remote"] != globals().get(
        "_POSTVERIFY_SOURCE_SHA256"
    ):
        raise ValueError("Perfil/fuente distintos de los pins externos.")
    normal = load_module(
        base64.b64decode(request["sdk_source_b64"], validate=True),
        SDK_SOURCE,
        "postverify_sdk",
        "_SOURCE_SHA256",
    )
    compiled = load_module(
        base64.b64decode(request["compile_source_b64"], validate=True),
        COMPILE_SOURCE,
        "postverify_compile",
        "_COMPILE_SOURCE_SHA256",
    )
    compiled.validate_profile(profile["compile_profile"])
    context = {"plan": profile["sdk_plan"], "layout_source_b64": request["layout_source_b64"]}
    normal.check_request(context)
    normal.verify_host(context)
    store = normal.Store(context)
    armed = False
    try:
        os.umask(0o077)
        store.open()
        store.verify()
        actual = store.receipts()
        if actual != {k: v for k, v in profile["sdk_backed"].items() if k != "transfer"}:
            raise ValueError("Las etapas físicas SDK incluido STOP difieren del respaldo.")
        normal.validate_artifact(store, "sdk", actual)
        normal.validate_artifact(store, "graph", actual)
        if (
            digest(
                normal.read_file(
                    store.descriptors["outputs"], "vchord-features.txt", maximum=256 * 1024
                )
            )
            != compiled.FEATURE_RAW_SHA
        ):
            raise ValueError("La revisión de features cambió.")
        control = store.descriptors["control"]
        failed_raw = normal.read_file(control, "compile-v3-receipt.json", maximum=512 * 1024)
        if failed_raw != canonical(profile["failed"]["receipt"]):
            raise ValueError("El fallo V3 físico no coincide con el respaldo literal.")
        log = normal.read_file(control, "compile-v3.log", maximum=normal.MAX_OUTPUT)
        if (
            digest(log) != LOG_SHA
            or len(log) != profile["failed"]["receipt"]["log_bytes"]
            or b"Finished `release` profile [optimized]" not in log
        ):
            raise ValueError(
                "El log físico no acredita la compilación completada y el fallo de catálogo."
            )
        if (
            normal.read_file(control, "compile-v3-profile.json", maximum=512 * 1024)
            != canonical(profile["compile_profile"])
            or digest(normal.read_file(control, "compile-v3-source.sh")) != COMPILE_RECIPE
            or digest(normal.read_file(control, "compile-v3-verifier.py")) != COMPILE_SOURCE
        ):
            raise ValueError("Las fuentes/flags ejecutados difieren del perfil V3.")
        if normal.read_file(control, "cid.txt", maximum=128).decode() != CID:
            raise ValueError("El CID físico no coincide.")
        docker = store.docker()
        before = stopped_container(normal, docker)
        for name in (
            STAGE + ".json",
            STAGE + "-artifact.json",
            STAGE + "-archive.tar",
            STAGE + "-build-output.txt",
            STAGE + "-profile.json",
        ):
            try:
                os.stat(name, dir_fd=control, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("Existe evidencia postverify anterior; no se permite replay.")
        os.mkdir("." + STAGE + "-started", 0o700, dir_fd=control)
        armed = True
        normal.write_file(control, STAGE + "-profile.json", canonical(profile))
        raw = fetch_archive(DockerSocket())
        archive = extract_build_output(raw)
        compiled.validate_sqlite_build_output(archive["output"].decode())
        artifact = catalog(normal, compiled, archive["output"], source_texts(normal, store))
        after = stopped_container(normal, docker)
        if before != after:
            raise ValueError("La identidad del CID detenido cambió durante la lectura.")
        store.verify()
        if (
            store.receipts() != actual
            or normal.read_file(control, "compile-v3-receipt.json", maximum=512 * 1024)
            != failed_raw
        ):
            raise ValueError("Los recibos anteriores cambiaron durante postverify.")
        normal.write_file(control, STAGE + "-archive.tar", raw)
        normal.write_file(control, STAGE + "-build-output.txt", archive["output"])
        normal.write_file(control, STAGE + "-artifact.json", canonical(artifact))
        receipt = {
            "stage": STAGE,
            "success": True,
            "cid": CID,
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "failed_compile_receipt_sha256": FAILED_SHA,
            "stop_receipt_sha256": STOP_SHA,
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "container": after,
            "archive": {k: v for k, v in archive.items() if k != "output"},
            "artifact_sha256": digest(canonical(artifact)),
            "artifact": artifact,
            "sdk_executed": False,
            "compiler_executed": False,
            "sql_verified": False,
            "final_sca_verified": False,
        }
        normal.write_file(control, STAGE + ".json", canonical(receipt))
        return envelope(receipt)
    except BaseException as error:
        receipt = {
            "stage": STAGE,
            "success": False,
            "cid": CID,
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "error_type": type(error).__name__,
            "detail": str(error)[:400],
            "original_failure_preserved": True,
            "sdk_executed": False,
            "compiler_executed": False,
        }
        if armed:
            try:
                normal.write_file(store.descriptors["control"], STAGE + ".json", canonical(receipt))
            except BaseException:
                receipt["receipt_written"] = False
        return envelope(receipt)
    finally:
        store.close()


def main():
    import signal
    import sys

    def timeout(_signum, _frame):
        raise TimeoutError("Postverify agotó 180s de lectura de datos.")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(180)
    try:
        header = sys.stdin.buffer.read(4)
        if len(header) != 4:
            raise ValueError("Petición truncada.")
        size = struct.unpack(">I", header)[0]
        if not 0 < size <= 512 * 1024:
            raise ValueError("Petición excesiva.")
        raw = sys.stdin.buffer.read(size)
        if len(raw) != size:
            raise ValueError("Petición truncada.")
        request = json.loads(raw)
        result = apply(request)
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
