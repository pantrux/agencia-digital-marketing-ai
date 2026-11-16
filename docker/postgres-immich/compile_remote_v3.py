"""Control de compile y catálogo puro; no ejecuta acciones al importar."""

import base64
import ctypes
import hashlib
import json
import os
import re
import signal
import stat
import struct
import sys
import tarfile
import time
import types
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

CID = "f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d"
V4_LOCAL_SHA = "685bb8a54fc7d65fd25710b5ea317f8eda239b57aa2978a0ff5e33b8aabba608"
V4_REMOTE_SHA = "a13ab96098b5f739ed88cbacaffc5bed521825aeb033d428bddbf93be57137ed"
RESUME_SOURCE_SHA = "f878b9dc1f9fdfec0bf61d53ed1210a0a3bbb713ee4e8c759aae29c4943755fc"
RESUME_PROFILE_SHA = "ad82bc28d9b5770ecfb1c2753de16177e4058ffd9a924a7671d1e211a1b4f939"
FAILED_V1_SHA = "5878733d8f7d09e3f1e7a2ff65df6f8a6c7d1318b74442da77672492b0a99d6d"
AUGMENT_SOURCE_SHA = "997877a7ae20478b7c6cb6afd3f7f2432a325c7aa319d2004508fffdbf3724dc"
AUGMENT_PROFILE_SHA = "60f6d8b0db156abeb0fcd0c73e764484bb4a8530f7b1c9aa9a705eea55b28a80"
AUGMENT_MANIFEST_SHA = "58de0656a6f05b76f3845449b8ec9a946abf62485881f976df09f2bdd1dab64f"
FAILED_V2_SHA = "39443be3a4ff0d85d2496487c4263f698ea4466bda152eb5db975f30e4713c6a"
RUSTFMT = "/opt/agency-rustfmt-v1/bin/rustfmt"
RUSTFMT_SHA = "bc01859cf8d5b93e912b62d711500a30a19da8ad2fe52ba7c2d1c5efb67b1c3f"
DRIVER = "/opt/agency-rust/lib/librustc_driver-03ff6c29172e9b06.so"
DRIVER_SHA = "626e0c6efaac2ce87441a7fffa7ee53f66c3a90df282803420771698d33b2e2f"
FEATURE_WRAPPER_SHA = "8e74968c1d6cf30668db4207b27d28432ef6f9cbf81c2d62fc1ed75d18deb8ca"
FEATURE_RAW_SHA = "21034e95a883742864ca39d5fa594435ba2a9223e70bdf79eff429cf1d9201c0"
FEATURE_STATUS = "PG16_SIMD_INIT_CONFIRMED_SQLITE_CUSTOM_LINK_REQUIRED_COMPILE_PENDING"
SQLITE_SOURCE_ID = (
    "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
)
WORK = "/work/.compile-agency-v3"
OUTPUT = "/outputs/compile-v3"
SQLITE = "/opt/agency-sqlite-v3"
RECIPE = WORK + "/recipe.sh"
VERIFIER = WORK + "/verifier.py"
RUSTFLAGS = "-Dwarnings -Ctarget-feature=-crt-static -Cpanic=unwind -Clink-arg=-Wl,-rpath,$ORIGIN"
MAX_FILE = 256 * 1024**2
SOURCE_FILES = {
    "sqlite3.c": "0c657666d819f261cc4007526ad57e1669de9949df7a8ea847aa284a2bf0ee52",
    "sqlite3.h": "d6c84557261893439b341d2e6c35187ae3ea24fbfd843ad94616f72d89242b8c",
    "sqlite3ext.h": "48963abfefb41ac329ef9d706acb5eadba952a99a748285c9ded6b5e0942721b",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def settings():
    return {
        "jobs": 2,
        "timeout_seconds": 1800,
        "cleanup_seconds": 20,
        "sqlite_dynamic": True,
        "runpath": "$ORIGIN",
        "strip": "none",
        "rustflags": RUSTFLAGS,
        "target": "x86_64-unknown-linux-musl",
        "rustfmt": RUSTFMT,
        "rustfmt_sha256": RUSTFMT_SHA,
        "rustfmt_driver_sha256": DRIVER_SHA,
        "ld_library_path": SQLITE + "/lib:/opt/agency-rust/lib",
    }


def validate_settings(value):
    if canonical(value) != canonical(settings()):
        raise ValueError("Los parámetros difieren de jobs2/1800s y enlace aprobados.")


def validate_feature_wrapper(data):
    if digest(data) != FEATURE_WRAPPER_SHA:
        raise ValueError("El wrapper de features no coincide con su SHA.")
    value = json.loads(data)
    raw = value["features"].encode()
    if value.get("sha256") != FEATURE_RAW_SHA or digest(raw) != FEATURE_RAW_SHA:
        raise ValueError("Los bytes de features no coinciden con el SHA independiente.")
    lines = raw.decode().splitlines()
    for expected in (
        "vchord v0.0.0 (/work/source)|default,pg16",
        "pgrx v0.17.0|cshim,pg16",
        "simd v0.0.0 (/work/source/crates/simd)|init",
    ):
        if expected not in lines:
            raise ValueError("Falta una configuración de features aprobada.")
    if any(line.startswith(("wasm-bindgen-shared ", "wasm-bindgen ", "git2 ")) for line in lines):
        raise ValueError("El target selecciona una ruta Git no admitida.")
    return {"wrapper_sha256": digest(data), "raw_sha256": digest(raw)}


def bounded_slice(data, offset, size):
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise ValueError("ELF con rango fuera de archivo.")
    return data[offset : offset + size]


def elf_report(data):
    if len(data) < 64 or len(data) > MAX_FILE or data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError("ELF64 little endian inválido.")
    kind, machine = struct.unpack_from("<HH", data, 16)
    if kind != 3 or machine != 62:
        raise ValueError("Se requiere biblioteca ET_DYN amd64.")
    offset = struct.unpack_from("<Q", data, 40)[0]
    width, count = struct.unpack_from("<HH", data, 58)
    if width != 64 or not 1 <= count <= 16384:
        raise ValueError("Tabla de secciones ELF no admitida.")
    raw = bounded_slice(data, offset, width * count)
    sections = [struct.unpack_from("<IIQQQQIIQQ", raw, i * 64) for i in range(count)]
    result = {
        "machine": machine,
        "needed": [],
        "soname": None,
        "runpath": None,
        "rpath": None,
        "symtab": False,
        "sqlite_defined": [],
        "sqlite_undefined": [],
        "glibc_versions": [],
    }
    defined, undefined, glibc = set(), set(), set()

    def table(index):
        if not 0 <= index < len(sections) or sections[index][1] != 3:
            raise ValueError("Tabla de cadenas ELF inválida.")
        row = sections[index]
        return bounded_slice(data, row[4], row[5])

    def text(strings, index):
        if index >= len(strings) or index < 0:
            raise ValueError("Nombre ELF fuera de tabla.")
        end = strings.find(b"\0", index)
        if end < 0 or end - index > 65536:
            raise ValueError("Nombre ELF truncado o excesivo.")
        return strings[index:end].decode("utf-8", errors="strict")

    for section in sections:
        if section[1] in (2, 11):
            if section[9] != 24 or section[5] % 24:
                raise ValueError("Tabla de símbolos ELF inválida.")
            if section[1] == 2:
                result["symtab"] = True
            strings = table(section[6])
            symbols = bounded_slice(data, section[4], section[5])
            for at in range(0, len(symbols), 24):
                name, _info, _other, shndx, _value, _size = struct.unpack_from(
                    "<IBBHQQ", symbols, at
                )
                label = text(strings, name)
                if label.startswith("sqlite3_"):
                    (defined if shndx else undefined).add(label)
        elif section[1] == 6:
            if section[5] % 16:
                raise ValueError("Tabla dinámica ELF inválida.")
            strings = table(section[6])
            glibc.update(v.decode() for v in re.findall(rb"GLIBC_[0-9.]+", strings))
            entries = bounded_slice(data, section[4], section[5])
            for at in range(0, len(entries), 16):
                tag, value = struct.unpack_from("<qQ", entries, at)
                if tag == 0:
                    break
                if tag == 1:
                    result["needed"].append(text(strings, value))
                elif tag in (14, 15, 29):
                    key = {14: "soname", 15: "rpath", 29: "runpath"}[tag]
                    if result[key] is not None:
                        raise ValueError("Etiqueta dinámica ELF repetida.")
                    result[key] = text(strings, value)
    result.update(
        sqlite_defined=sorted(defined),
        sqlite_undefined=sorted(undefined),
        glibc_versions=sorted(glibc),
    )
    return result


def sqlite_link_proof(vchord, sqlite):
    library, provider = elf_report(vchord), elf_report(sqlite)
    if (
        "libsqlite3.so.0" not in library["needed"]
        or library["sqlite_defined"]
        or not library["sqlite_undefined"]
        or not library["symtab"]
        or library["runpath"] != "$ORIGIN"
        or library["rpath"] is not None
        or provider["soname"] != "libsqlite3.so.0"
        or "sqlite3_sourceid" not in provider["sqlite_defined"]
        or any(
            item["glibc_versions"] or "libc.so.6" in item["needed"] for item in (library, provider)
        )
    ):
        raise ValueError("No se acredita SQLite dinámico custom, símbolos y RUNPATH musl.")
    return {"vchord": library, "sqlite": provider}


def validate_sqlite_probe(value):
    if value != {
        "version": "3.54.1",
        "number": 3054001,
        "source_id": SQLITE_SOURCE_ID,
        "fts5": True,
        "threadsafe": True,
    }:
        raise ValueError("El SQLite cargado no acredita versión, sourceid y opciones.")


def control_version(data, expected):
    if len(data) > 65536:
        raise ValueError("Control de extensión excesivo.")
    versions = re.findall(
        r"^\s*default_version\s*=\s*'([^']+)'\s*(?:#.*)?$",
        data.decode("utf-8").split("\0", 1)[0],
        re.M,
    )
    if b"\0" in data or versions != [expected]:
        raise ValueError("La versión del control de extensión no coincide o es ambigua.")
    return expected


def extension_versions(read):
    versions = {}
    prefix = "usr/local/share/postgresql/extension/"
    for name, expected in (("vector", "0.8.5"), ("vchord", "1.1.1")):
        versions[name] = control_version(read(prefix + name + ".control"), expected)
        if not read(prefix + name + "--" + expected + ".sql"):
            raise ValueError("Falta el SQL de instalación de una extensión.")
    return versions


def validate_sqlite_build_output(value):
    if not isinstance(value, str) or len(value.encode()) > 65536:
        raise ValueError("Build-script output excesivo o inválido.")
    searches = [
        line.split("=", 1)[1].removeprefix("native=")
        for line in value.splitlines()
        if line.startswith("cargo:rustc-link-search=")
    ]
    links = [
        line.split("=", 1)[1]
        for line in value.splitlines()
        if line.startswith("cargo:rustc-link-lib=")
    ]
    if (
        searches != [SQLITE + "/lib"]
        or links not in (["sqlite3"], ["dylib=sqlite3"])
        or "static=sqlite3" in value
    ):
        raise ValueError("Build script no demuestra enlace exclusivo al SQLite custom.")


def execute_compile(docker, normal, profile, sink, budget):
    if not 20 < budget <= 1800:
        raise ValueError("Presupuesto sin reserva de limpieza.")
    deadline = time.monotonic() + budget
    before = normal.check_container(docker.inspect(CID), CID)
    if before["running"] is not True:
        raise ValueError("El CID del SDK no está en ejecución.")
    try:
        limits = docker.limits(before)
        result = docker.command(
            [
                "exec",
                "--env",
                "AGENCY_COMPILE_PROFILE_SHA256=" + profile["profile_sha256"],
                "--env",
                "AGENCY_COMPILE_VERIFIER_SHA256=" + profile["sources"]["remote"],
                CID,
                "/bin/bash",
                RECIPE,
                "compile",
            ],
            timeout=max(0.1, deadline - time.monotonic() - 20),
            sink=sink,
        )
        if result["returncode"] != 0:
            failure = ValueError("La receta de compilación falló.")
            failure.process = result["process"]
            raise failure
        after = normal.check_container(docker.inspect(CID), CID)
        if not after["running"] or any(
            after[k] != before[k] for k in ("cid", "pid", "started_at", "created")
        ):
            raise ValueError("La instancia SDK cambió durante compile.")
        docker.limits(after)
        return {"before": before, "after": after, "limits": limits, "process": result["process"]}
    except BaseException as error:
        if hasattr(signal, "alarm"):
            signal.alarm(0)
        try:
            cleanup = normal.stop_owned(docker, CID)
        except BaseException as failed:
            cleanup = {"stopped": False, "error_type": type(failed).__name__}
        raise normal.StageError(error, cleanup) from error


def read_regular(path, maximum=MAX_FILE):
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("Artefacto irregular o excesivo.")
        data = stream.read(maximum + 1)
    if len(data) != info.st_size:
        raise ValueError("El artefacto cambió de tamaño.")
    return data


def write_json_new(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def inside_sqlite():
    # Se invoca únicamente por la receta respaldada, dentro del CID propio.
    library = ctypes.CDLL(SQLITE + "/lib/libsqlite3.so.0")
    library.sqlite3_libversion.restype = ctypes.c_char_p
    library.sqlite3_libversion_number.restype = ctypes.c_int
    library.sqlite3_sourceid.restype = ctypes.c_char_p
    library.sqlite3_compileoption_used.argtypes = [ctypes.c_char_p]
    library.sqlite3_compileoption_used.restype = ctypes.c_int
    value = {
        "version": library.sqlite3_libversion().decode(),
        "number": library.sqlite3_libversion_number(),
        "source_id": library.sqlite3_sourceid().decode(),
        "fts5": bool(library.sqlite3_compileoption_used(b"ENABLE_FTS5")),
        "threadsafe": bool(library.sqlite3_compileoption_used(b"THREADSAFE=1")),
    }
    validate_sqlite_probe(value)
    write_json_new(OUTPUT + "/sqlite-probe.json", value)


def inside_inputs():
    header = None
    target = Path(WORK) / "sqlite"
    target.mkdir(mode=0o700)
    with zipfile.ZipFile("/inputs/sqlite-amalgamation-3540100.zip") as archive:
        for name, expected in SOURCE_FILES.items():
            item = archive.getinfo("sqlite-amalgamation-3540100/" + name)
            if item.file_size > 16 * 1024**2 or item.flag_bits & 1:
                raise ValueError("Entrada SQLite excesiva o cifrada.")
            data = archive.read(item)
            if digest(data) != expected:
                raise ValueError("Fuente SQLite distinta de sus tres SHA fijados.")
            with (target / name).open("xb") as stream:
                stream.write(data)
            if name == "sqlite3.h":
                header = data.decode()
    if (
        header is None
        or re.findall(r'^#define SQLITE_VERSION\s+"([^"]+)"', header, re.M) != ["3.54.1"]
        or re.findall(r'^#define SQLITE_SOURCE_ID\s+"([^"]+)"', header, re.M) != [SQLITE_SOURCE_ID]
    ):
        raise ValueError("Las macros SQLite no coinciden.")
    prefix = Path(SQLITE)
    prefix.mkdir(mode=0o700)
    (prefix / "include").mkdir(mode=0o700)
    (prefix / "lib").mkdir(mode=0o700)
    (prefix / "lib/pkgconfig").mkdir(mode=0o700)
    for name in ("sqlite3.h", "sqlite3ext.h"):
        with (prefix / "include" / name).open("xb") as stream:
            stream.write(read_regular(target / name))
    content = "prefix=/opt/agency-sqlite-v3\nlibdir=${prefix}/lib\nincludedir=${prefix}/include\nName: SQLite\nDescription: SQLite custom de la agencia\nVersion: 3.54.1\nLibs: -L${libdir} -lsqlite3\nLibs.private: -lm -ldl -lpthread\nCflags: -I${includedir}\n"
    with (prefix / "lib/pkgconfig/sqlite3.pc").open("x") as stream:
        stream.write(content)
    destination = Path(WORK) / "pgvector"
    destination.mkdir(mode=0o700)
    total, seen, root = 0, set(), None
    with tarfile.open("/inputs/pgvector-source.tar.gz", "r:gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if (
                not parts
                or member.name.startswith("/")
                or any(p in ("", ".", "..") for p in parts)
                or len(seen) >= 10000
            ):
                raise ValueError("Ruta pgvector no admitida.")
            root = parts[0] if root is None else root
            if parts[0] != root or member.name in seen:
                raise ValueError("Raíz pgvector ambigua.")
            seen.add(member.name)
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError("Raíz pgvector no es directorio.")
                continue
            path = destination.joinpath(*parts[1:])
            if member.isdir():
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif member.isfile() and member.size <= 16 * 1024**2:
                total += member.size
                if total > 128 * 1024**2:
                    raise ValueError("Fuente pgvector excesiva.")
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    data = archive.extractfile(member).read(member.size + 1)
                    if len(data) != member.size:
                        raise ValueError("Fuente pgvector truncada.")
                    stream.write(data)
            else:
                raise ValueError("Tipo especial o enlace en pgvector.")


def inside_catalog():
    root = Path(OUTPUT)
    runtime = root / "runtime-root"
    pkglib = runtime / "usr/local/lib/postgresql"
    proof = sqlite_link_proof(
        read_regular(pkglib / "vchord.so"), read_regular(pkglib / "libsqlite3.so.0")
    )
    probe = json.loads(read_regular(root / "sqlite-probe.json", 65536))
    validate_sqlite_probe(probe)
    vector = elf_report(read_regular(pkglib / "vector.so"))
    if vector["glibc_versions"] or "libc.so.6" in vector["needed"]:
        raise ValueError("pgvector no es musl.")
    versions = extension_versions(lambda name: read_regular(runtime / name))
    candidates = list(
        Path(WORK + "/target/x86_64-unknown-linux-musl/release/build").glob(
            "libsqlite3-sys-*/output"
        )
    )
    if len(candidates) != 1:
        raise ValueError("Build-script output SQLite ausente o ambiguo.")
    build_output = read_regular(candidates[0], 65536)
    validate_sqlite_build_output(build_output.decode())
    descriptor = os.open(
        root / "sqlite-build-output.txt",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(build_output)
    inventory = {}
    for base, directories, files in os.walk(runtime, followlinks=False):
        for name in directories + files:
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError("Enlace inesperado en las salidas.")
        for name in files:
            path = Path(base) / name
            data = read_regular(path)
            os.chmod(path, 0o600)
            inventory[str(path.relative_to(runtime))] = {"sha256": digest(data), "size": len(data)}
        os.chmod(base, 0o700)
    if not 5 <= len(inventory) <= 256:
        raise ValueError("Inventario runtime vacío o excesivo.")
    receipt = {
        "stage": "compile",
        "verified": True,
        "input_manifest_sha256": os.environ["AGENCY_INPUT_MANIFEST_SHA256"],
        "profile_sha256": os.environ["AGENCY_COMPILE_PROFILE_SHA256"],
        "features_sha256": FEATURE_RAW_SHA,
        "settings": settings(),
        "sqlite_probe": probe,
        "link_proof": proof,
        "vector_elf": vector,
        "extension_versions": versions,
        "build_script_output_sha256": digest(build_output),
        "runtime_files": inventory,
        "sql_verified": False,
        "final_sca_verified": False,
    }
    write_json_new(root / "compile-receipt.json", receipt)


def load_sdk(request):
    raw = base64.b64decode(request["sdk_source_b64"], validate=True)
    if digest(raw) != V4_REMOTE_SHA:
        raise ValueError("El ejecutor V4 cambió de fuente.")
    normal = types.ModuleType("compile_fixed_sdk_v4")
    normal.__dict__["_SOURCE_SHA256"] = V4_REMOTE_SHA
    exec(compile(raw, "<sdk-v4-fijado>", "exec"), normal.__dict__)
    return normal


def validate_profile(profile):
    validate_settings(profile["settings"])
    validate_feature_wrapper(base64.b64decode(profile["feature_wrapper_b64"], validate=True))
    if (
        profile.get("schema_version") != 1
        or profile.get("cid") != CID
        or profile.get("feature_status") != FEATURE_STATUS
    ):
        raise ValueError("Perfil, CID o revisión de features no autorizados.")
    plan = profile["sdk_plan"]
    if plan.get("controller_sha256") != V4_LOCAL_SHA or plan.get("remote_sha256") != V4_REMOTE_SHA:
        raise ValueError("El plan SDK no es la revisión V4 congelada.")
    if set(profile["sources"]) != {"local", "remote", "recipe"} or any(
        re.fullmatch(r"[a-f0-9]{64}", v) is None for v in profile["sources"].values()
    ):
        raise ValueError("Pins de fuentes compile incompletos.")
    for name in ("prepare", "create", "start", "sdk", "graph"):
        value = profile["backed"].get(name, {})
        receipt = value.get("receipt", {})
        if (
            value != {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}
            or receipt.get("success") is not True
            or receipt.get("source_sha256") != V4_REMOTE_SHA
            or receipt.get("plan_sha256") != plan["plan_sha256"]
        ):
            raise ValueError("Un recibo SDK/grafo no está respaldado íntegramente.")
        if name in ("create", "start") and receipt.get("cid") != CID:
            raise ValueError("Un recibo corresponde a otro CID.")
    if "stop" in profile["backed"]:
        raise ValueError("El perfil contiene una instancia SDK ya detenida.")
    validate_resume(profile["resume"], profile["backed"]["start"]["receipt"]["container"])
    validate_augmentation(
        profile["augmentation"], profile["backed"]["start"]["receipt"]["container"]
    )


def validate_resume(value, original):
    receipt = value.get("restart", {}).get("receipt", {})
    instance = receipt.get("container", {})
    proof = receipt.get("proof", {})
    if (
        value.get("profile_sha256") != RESUME_PROFILE_SHA
        or value.get("restart")
        != {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}
        or receipt.get("profile_sha256") != RESUME_PROFILE_SHA
        or receipt.get("source_sha256") != RESUME_SOURCE_SHA
        or receipt.get("success") is not True
        or receipt.get("stage") != "restart"
        or receipt.get("cid") != CID
        or receipt.get("failed_receipt_sha256") != FAILED_V1_SHA
        or receipt.get("sdk_executed") is not False
        or proof.get("verified") is not True
        or proof.get("sdk_executed") is not False
        or proof.get("compiler_executed") is not False
        or proof.get("apk_count") != 110
        or receipt.get("proof_sha256") != digest(canonical(proof))
        or instance.get("cid") != CID
        or instance.get("created") != original["created"]
        or instance.get("running") is not True
        or type(instance.get("pid")) is not int
        or instance["pid"] <= 0
        or not instance.get("started_at")
        or instance["started_at"] == original["started_at"]
    ):
        raise ValueError("Compile V2 exige exactamente el reinicio propio respaldado.")


def validate_augmentation(value, original):
    augmented = value.get("augment", {}).get("receipt", {})
    restarted = value.get("restart", {}).get("receipt", {})
    artifact = augmented.get("artifact", {})
    instance = augmented.get("container", {})
    for name, receipt, stage in (
        ("augment", augmented, "augment"),
        ("restart", restarted, "restart"),
    ):
        if (
            value.get(name) != {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}
            or receipt.get("success") is not True
            or receipt.get("stage") != stage
            or receipt.get("cid") != CID
            or receipt.get("profile_sha256") != AUGMENT_PROFILE_SHA
            or receipt.get("source_sha256") != AUGMENT_SOURCE_SHA
        ):
            raise ValueError("Compile V3 exige el aumento y reinicio exactos respaldados.")
    if (
        value.get("profile_sha256") != AUGMENT_PROFILE_SHA
        or augmented.get("restart_receipt_sha256") != value["restart"]["receipt_sha256"]
        or augmented.get("artifact_receipt_sha256") != digest(canonical(artifact))
        or augmented.get("sdk_executed") is not True
        or augmented.get("compiler_executed") is not False
        or instance != restarted.get("container")
        or instance.get("cid") != CID
        or instance.get("created") != original["created"]
        or instance.get("running") is not True
        or type(instance.get("pid")) is not int
        or instance["pid"] <= 0
        or not instance.get("started_at")
        or instance["started_at"] == original["started_at"]
        or artifact.get("stage") != "rustfmt-augmentation"
        or artifact.get("verified") is not True
        or artifact.get("sdk_executed") is not True
        or artifact.get("manifest_sha256") != AUGMENT_MANIFEST_SHA
        or artifact.get("formatter_executed") is not True
        or artifact.get("compiler_executed") is not False
        or artifact.get("installer_executed") is not False
        or artifact.get("cargo_fmt_installed") is not False
        or artifact.get("install_prefix") != "/opt/agency-rustfmt-v1"
        or artifact.get("files", {}).get("bin/rustfmt", {}).get("sha256") != RUSTFMT_SHA
        or artifact.get("driver", {}).get("path") != DRIVER
        or artifact["driver"].get("sha256") != DRIVER_SHA
        or artifact.get("initial_check_returncode") != 1
        or artifact.get("final_check_returncode") != 0
    ):
        raise ValueError("El aumento no acredita formatter, driver y prueba real antes de V3.")
    if (
        artifact.get("version") != "rustfmt 1.11.0-beta (01ddda61fa 2026-11-04)"
        or artifact.get("distribution_version") != "1.11.0-beta.10"
        or artifact.get("probe_sha256") != digest(b"pub fn agency_probe() -> u8 {\n    7\n}\n")
    ):
        raise ValueError("La versión o el formato real no corresponden al aumento aprobado.")


def validate_running(normal, docker, profile):
    observed = normal.check_container(docker.inspect(CID), CID)
    previous = profile["augmentation"]["augment"]["receipt"]["container"]
    if not observed["running"] or any(
        observed[k] != previous[k] for k in ("cid", "created", "pid", "started_at")
    ):
        raise ValueError("La instancia viva no coincide con start respaldado.")
    return observed


def verify_sdk_store(store, normal, profile):
    store.verify()
    actual = store.receipts()
    for name in ("prepare", "create", "start", "sdk", "graph"):
        if actual.get(name) != profile["backed"][name]:
            raise ValueError("Un recibo host no coincide literalmente con la bóveda.")
    if "stop" in actual:
        raise ValueError("Existe un recibo de parada del SDK.")
    normal.validate_artifact(store, "sdk", actual)
    normal.validate_artifact(store, "graph", actual)
    raw = normal.read_file(store.descriptors["outputs"], "vchord-features.txt", maximum=256 * 1024)
    if digest(raw) != FEATURE_RAW_SHA:
        raise ValueError("Los bytes de features remotos cambiaron.")
    control = store.descriptors["control"]
    restart = profile["resume"]["restart"]["receipt"]
    if normal.read_file(control, "resume-v1-restart.json") != canonical(restart):
        raise ValueError("El reinicio host difiere del respaldado.")
    if digest(normal.read_file(control, "resume-v1-stopped.json")) != restart.get(
        "stopped_receipt_sha256"
    ):
        raise ValueError("La parada previa cambió.")
    proof = normal.read_file(
        store.descriptors["outputs"], "resume-v1-sdk-proof.json", maximum=256 * 1024
    )
    if proof != canonical(restart["proof"]) or digest(proof) != restart["proof_sha256"]:
        raise ValueError("La verificación instalada cambió desde el reinicio respaldado.")
    if digest(normal.read_file(control, "compile-v1-receipt.json")) != FAILED_V1_SHA:
        raise ValueError("El fallo V1 no conserva sus bytes canónicos.")
    augmented = profile["augmentation"]["augment"]["receipt"]
    augmented_restart = profile["augmentation"]["restart"]["receipt"]
    for filename, value in (
        ("augment-v1-augmentation.json", augmented),
        ("augment-v1-restart.json", augmented_restart),
    ):
        if normal.read_file(control, filename) != canonical(value):
            raise ValueError("Un recibo del aumento cambió después del respaldo.")
    if digest(normal.read_file(control, "augment-v1-stopped.json")) != augmented_restart.get(
        "stopped_receipt_sha256"
    ):
        raise ValueError("La parada previa al aumento cambió.")
    sdk_proof = normal.read_file(
        store.descriptors["outputs"], "augment-v1-sdk-proof.json", maximum=256 * 1024
    )
    if (
        sdk_proof != canonical(augmented_restart["proof"])
        or digest(sdk_proof) != augmented_restart["proof_sha256"]
    ):
        raise ValueError("El catálogo SDK previo al aumento cambió.")
    artifact = normal.read_file(
        store.descriptors["outputs"], "rustfmt-augmentation-receipt.json", maximum=65536
    )
    if (
        artifact != canonical(augmented["artifact"])
        or digest(artifact) != augmented["artifact_receipt_sha256"]
    ):
        raise ValueError("La prueba real del formatter cambió.")
    if digest(normal.read_file(control, "compile-v2-receipt.json")) != FAILED_V2_SHA:
        raise ValueError("El fallo V2 no conserva sus bytes canónicos.")
    return actual


def artifact_catalog(store, normal, profile_sha):
    root = normal.open_directory(normal.OUTPUTS + "/compile-v3")
    try:
        normal.checked_node(os.fstat(root), 0, directory=True)
        if set(os.listdir(root)) != {
            "runtime-root",
            "sqlite-probe.json",
            "sqlite-build-output.txt",
            "compile-receipt.json",
        }:
            raise ValueError("Salidas compile incompletas o adicionales.")
        raw = normal.read_file(root, "compile-receipt.json", maximum=512 * 1024)
        receipt = normal.json_object(raw)
        if (
            raw != canonical(receipt)
            or receipt.get("stage") != "compile"
            or receipt.get("verified") is not True
            or receipt.get("profile_sha256") != profile_sha
            or receipt.get("input_manifest_sha256") != normal.KIT_SHA
            or receipt.get("features_sha256") != FEATURE_RAW_SHA
            or receipt.get("sql_verified") is not False
            or receipt.get("final_sca_verified") is not False
        ):
            raise ValueError("El recibo compile no acredita perfil, kit y límites.")
        validate_settings(receipt["settings"])
        validate_sqlite_probe(receipt["sqlite_probe"])
        if normal.read_file(root, "sqlite-probe.json", maximum=65536) != canonical(
            receipt["sqlite_probe"]
        ):
            raise ValueError("La sonda SQLite física difiere del recibo.")
        build_output = normal.read_file(root, "sqlite-build-output.txt", maximum=65536)
        validate_sqlite_build_output(build_output.decode())
        if digest(build_output) != receipt["build_script_output_sha256"]:
            raise ValueError("Build output SQLite alterado.")
        runtime = normal.open_directory(normal.OUTPUTS + "/compile-v3/runtime-root")
        try:
            found = set()

            def walk(fd, prefix=""):
                for entry in os.scandir(fd):
                    info = entry.stat(follow_symlinks=False)
                    name = prefix + entry.name
                    if stat.S_ISDIR(info.st_mode):
                        normal.checked_node(info, 0, directory=True)
                        child = os.open(
                            entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                        )
                        try:
                            walk(child, name + "/")
                        finally:
                            os.close(child)
                    else:
                        normal.checked_node(info, 0)
                        if (
                            not re.fullmatch(
                                r"usr/local/(?:lib/postgresql|share/postgresql/extension|include/postgresql/server/extension|share/licenses)/[A-Za-z0-9_+./-]+",
                                name,
                            )
                            or ".." in name.split("/")
                            or name not in receipt["runtime_files"]
                        ):
                            raise ValueError("Ruta de artefacto ajena a las salidas propias.")
                        found.add(name)

            walk(runtime)
            if found != set(receipt["runtime_files"]) or not 5 <= len(found) <= 256:
                raise ValueError("El catálogo runtime no coincide con su árbol.")
            for name, expected in receipt["runtime_files"].items():
                if (
                    set(expected) != {"size", "sha256"}
                    or type(expected["size"]) is not int
                    or not 0 < expected["size"] <= MAX_FILE
                    or re.fullmatch(r"[a-f0-9]{64}", expected["sha256"]) is None
                ):
                    raise ValueError("Registro de artefacto inválido.")
                normal.file_digest(runtime, name, expected)

            def artifact(name):
                parent, leaf = normal.open_parent(runtime, name)
                try:
                    return normal.read_file(parent, leaf, maximum=MAX_FILE)
                finally:
                    os.close(parent)

            pkglib = "usr/local/lib/postgresql/"
            proof = sqlite_link_proof(
                artifact(pkglib + "vchord.so"), artifact(pkglib + "libsqlite3.so.0")
            )
            if (
                proof != receipt["link_proof"]
                or elf_report(artifact(pkglib + "vector.so")) != receipt["vector_elf"]
                or extension_versions(artifact) != receipt.get("extension_versions")
            ):
                raise ValueError("El ELF real no coincide con la evidencia del catálogo.")
        finally:
            os.close(runtime)
        return {"artifact_receipt_sha256": digest(raw), "artifact": receipt}
    finally:
        os.close(root)


def apply(request):
    started = time.monotonic()
    profile = request["profile"]
    validate_profile(profile)
    profile_sha = digest(canonical(profile))
    if (
        profile_sha != request["profile_sha256"]
        or globals().get("_COMPILE_SOURCE_SHA256") != profile["sources"]["remote"]
    ):
        raise ValueError("Perfil o helper diferentes de los pins externos.")
    sources = {}
    for key in ("recipe", "remote"):
        data = base64.b64decode(request[key + "_source_b64"], validate=True)
        if len(data) > 128 * 1024 or digest(data) != profile["sources"][key]:
            raise ValueError("Fuente de compile distinta de su pin.")
        sources[key] = data
    normal = load_sdk(request)
    context = {"plan": profile["sdk_plan"], "layout_source_b64": request["layout_source_b64"]}
    normal.check_request(context)
    normal.verify_host(context)
    store = normal.Store(context)
    docker, armed, log = None, False, None
    try:
        if request.get("action") == "stop":
            store.descriptors["control"] = normal.marked_directory(normal.CONTROL)
            original = profile["backed"]["prepare"]["receipt"]["directories"]["control"][
                "directory"
            ]
            if normal.identity(os.fstat(store.descriptors["control"])) != original:
                raise ValueError("El control cambió de inodo antes de detener el CID.")
            docker = store.docker()
            return {
                "success": True,
                "profile_sha256": profile_sha,
                "cleanup": normal.stop_owned(docker, CID),
                "sdk_stage": "compile",
            }
        if request.get("action") != "compile":
            raise ValueError("Acción compile no permitida.")
        store.open()
        verify_sdk_store(store, normal, profile)
        docker = store.docker()
        before = validate_running(normal, docker, profile)
        limits = docker.limits(before)
        descriptor = store.descriptors["control"]
        for name in (
            "compile-v3-profile.json",
            "compile-v3-source.sh",
            "compile-v3-verifier.py",
            "compile-v3.log",
            "compile-v3-receipt.json",
        ):
            try:
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("Compile conserva estado anterior; no se permite replay.")
        # EXCL de la etapa impide replay sobre un SDK parcialmente compilado.
        os.mkdir(".compile-v3-started", 0o700, dir_fd=descriptor)
        armed = True
        normal.write_file(descriptor, "compile-v3-profile.json", canonical(profile))
        for key, filename in (
            ("recipe", "compile-v3-source.sh"),
            ("remote", "compile-v3-verifier.py"),
        ):
            normal.write_file(descriptor, filename, sources[key])
        docker.heartbeat_fd = request["heartbeat_fd"]
        provision = docker.command(["exec", CID, "/bin/mkdir", "-m", "700", WORK], timeout=5)
        if provision["returncode"] != 0:
            raise ValueError("El directorio de receta ya existe o no pudo crearse.")
        for source, target in (
            ("compile-v3-source.sh", RECIPE),
            ("compile-v3-verifier.py", VERIFIER),
        ):
            validate_running(normal, docker, profile)
            result = docker.command(
                ["cp", normal.CONTROL + "/" + source, CID + ":" + target], timeout=10
            )
            if result["returncode"] != 0:
                raise ValueError("No se pudo copiar una fuente propia al CID.")
        checks = docker.capture(
            ["exec", CID, "/usr/bin/sha256sum", RECIPE, VERIFIER], timeout=5
        ).decode()
        expected = (
            profile["sources"]["recipe"]
            + "  "
            + RECIPE
            + "\n"
            + profile["sources"]["remote"]
            + "  "
            + VERIFIER
            + "\n"
        )
        if checks != expected:
            raise ValueError("Las fuentes copiadas al CID no conservan su SHA.")
        validate_running(normal, docker, profile)
        log = os.open(
            "compile-v3.log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        with os.fdopen(log, "wb", buffering=0) as sink:
            log = None
            budget = 1800 - (time.monotonic() - started)
            result = execute_compile(
                docker,
                normal,
                {"profile_sha256": profile_sha, "sources": profile["sources"]},
                sink,
                budget,
            )
            sink.flush()
            os.fsync(sink.fileno())
        verify_sdk_store(store, normal, profile)
        if (
            docker.capture(
                ["exec", CID, "/usr/bin/sha256sum", RECIPE, VERIFIER], timeout=5
            ).decode()
            != expected
        ):
            raise ValueError("El SDK alteró las fuentes de verificación.")
        result.update(artifact_catalog(store, normal, profile_sha))
        validate_running(normal, docker, profile)
        docker.limits(before)
        raw_log = normal.read_file(descriptor, "compile-v3.log", maximum=normal.MAX_OUTPUT)
        receipt = {
            "stage": "compile",
            "success": True,
            "cid": CID,
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "settings": settings(),
            "initial_limits": limits,
            "log_sha256": digest(raw_log),
            "log_bytes": len(raw_log),
            **result,
        }
        normal.write_file(descriptor, "compile-v3-receipt.json", canonical(receipt))
        return {"success": True, "result": normal.envelope(receipt), "profile_sha256": profile_sha}
    except BaseException as error:
        if hasattr(signal, "alarm"):
            signal.alarm(0)
        cleanup = getattr(error, "cleanup", None)
        if armed and docker is not None:
            try:
                cleanup = normal.stop_owned(docker, CID)
            except BaseException as failed:
                cleanup = {"stopped": False, "error_type": type(failed).__name__}
        cause = error.cause if isinstance(error, normal.StageError) else error
        failure = {
            "stage": "compile",
            "success": False,
            "cid": CID,
            "profile_sha256": profile_sha,
            "source_sha256": profile["sources"]["remote"],
            "error_type": type(cause).__name__,
            "process": getattr(cause, "process", None),
            "cleanup": cleanup,
            "partial_evidence_preserved": True,
        }
        if armed:
            try:
                raw_log = normal.read_file(
                    store.descriptors["control"], "compile-v3.log", maximum=normal.MAX_OUTPUT
                )
                failure.update(log_sha256=digest(raw_log), log_bytes=len(raw_log))
            except FileNotFoundError:
                pass
            normal.write_file(
                store.descriptors["control"], "compile-v3-receipt.json", canonical(failure)
            )
        return {"success": False, "result": normal.envelope(failure), "profile_sha256": profile_sha}
    finally:
        if log is not None:
            os.close(log)
        store.close()


def exact_read(stream, size):
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            raise ValueError("Petición compile truncada.")
        result.extend(chunk)
    return bytes(result)


def main():
    inside = {
        "inside-inputs": inside_inputs,
        "inside-sqlite": inside_sqlite,
        "inside-catalog": inside_catalog,
    }
    if len(sys.argv) == 2 and sys.argv[1] in inside:
        if (
            os.name != "posix"
            or os.geteuid() != 0
            or not Path("/.dockerenv").is_file()
            or digest(Path(__file__).read_bytes())
            != os.environ.get("AGENCY_COMPILE_VERIFIER_SHA256")
        ):
            raise ValueError("El verificador interno no acredita contenedor, root y fuente.")
        inside[sys.argv[1]]()
        return 0
    request = {}
    try:
        if os.name != "posix" or os.geteuid() != 0 or sys.argv[1:]:
            raise ValueError("El helper compile exige root POSIX y ninguna opción arbitraria.")
        os.umask(0o077)

        def interrupted(_signum, _frame):
            raise InterruptedError("Compile interrumpido.")

        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGALRM"):
            signal.signal(getattr(signal, name), interrupted)
        # Reservar los últimos veinte segundos para detener el CID propio.
        signal.alarm(1780)
        stream = sys.stdin.buffer.raw
        raw = exact_read(stream, 4)
        length = struct.unpack(">I", raw)[0]
        if not 0 < length <= 512 * 1024:
            raise ValueError("Petición compile excesiva.")
        request = json.loads(exact_read(stream, length))
        request["heartbeat_fd"] = sys.stdin.fileno()
        response = apply(request)
        print(json.dumps(response, sort_keys=True), flush=True)
        return 0 if response["success"] else 1
    except BaseException as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "partial_evidence_preserved": True,
                }
            ),
            flush=True,
        )
        return 1
    finally:
        if hasattr(signal, "alarm"):
            signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
