"""Reanuda únicamente el SDK detenido acreditado; importar no ejecuta acciones."""

import base64
import hashlib
import io
import json
import os
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import types
from pathlib import Path, PurePosixPath

CID = "f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d"
COMPILE_SHA = "da8eed33f738138b24407b2a6d9d5d317c8344e2f391af515209d4dcd57cfe85"
PROFILE_V1_SHA = "777b9fa2e4724e633f589585ed1b11a4616095b17f7c987f88afd7c3ea03b22e"
FAILED_SHA = "39443be3a4ff0d85d2496487c4263f698ea4466bda152eb5db975f30e4713c6a"
FAILED_LOG_SHA = "898807c4b5586bc445bb719d841f20effe14fe669d1f40889113e56ffc83b86f"
WORK = "/work/.augment-agency-v1"
PROOF = "augment-v1-sdk-proof.json"
INSTALL_PREFIX = "/opt/agency-rustfmt-v1"
SELECTED = {
    "bin/rustfmt",
    "share/doc/rustfmt/LICENSE-APACHE",
    "share/doc/rustfmt/LICENSE-MIT",
    "share/doc/rustfmt/README.md",
}
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


def read_regular(path, maximum=512 * 1024**2, *, allow_hardlinks=False):
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_nlink != 1 and not allow_hardlinks)
            or info.st_size > maximum
        ):
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


def validate_spec(spec):
    if (
        spec.get("schema_version") != 1
        or spec.get("owner") != "agencia-digital-marketing-ai:postgres-immich-rustfmt:v1"
        or spec.get("install_prefix") != INSTALL_PREFIX
        or spec.get("rust_commit") != "01ddda61fada79bbc835476209518a6f68322984"
        or spec.get("rustfmt_version") != "1.11.0-beta.10"
        or spec.get("rust_channel_manifest_sha256")
        != "325df53718c60181d663c7196f251f4ba9bac44ef2616896d1e33e8396a562db"
        or spec.get("archive_prefix") != "rustfmt-beta-x86_64-unknown-linux-musl/rustfmt-preview/"
        or set(spec.get("files_to_copy", {})) != SELECTED
        or spec.get("archive_url")
        != "https://static.rust-lang.org/dist/2026-11-05/rustfmt-beta-x86_64-unknown-linux-musl.tar.xz"
        or spec.get("sdk_executed") is not False
        or spec.get("installation_performed") is not False
    ):
        raise ValueError("El manifiesto no corresponde al aumento rustfmt aprobado.")
    for item in spec["files_to_copy"].values():
        if (
            set(item) != {"sha256", "size"}
            or type(item["size"]) is not int
            or not 0 < item["size"] <= 32 * 1024**2
            or len(item["sha256"]) != 64
        ):
            raise ValueError("Payload declarado no válido.")
    if spec.get("driver") != {
        "path": "/opt/agency-rust/lib/librustc_driver-03ff6c29172e9b06.so",
        "sha256": "626e0c6efaac2ce87441a7fffa7ee53f66c3a90df282803420771698d33b2e2f",
        "size": 310364368,
    }:
        raise ValueError("Driver distinto del SDK3 fijado.")


def selected_payload(data, spec):
    if (
        len(data) != spec["archive_size"]
        or len(data) > 4 * 1024**2
        or digest(data) != spec["archive_sha256"]
    ):
        raise ValueError("Archivo rustfmt distinto del SHA/tamaño declarado.")
    found, seen, total = {}, set(), 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            safe_relative(name)
            if name in seen or not (
                name.startswith("rustfmt-beta-x86_64-unknown-linux-musl/")
                or (name == "rustfmt-beta-x86_64-unknown-linux-musl" and member.isdir())
            ):
                raise ValueError("Raíz rustfmt o nombre ambiguo.")
            seen.add(name)
            if len(seen) > 128 or not (member.isdir() or member.isfile()):
                raise ValueError("Tipo especial/enlace en rustfmt.")
            if not member.isfile():
                continue
            total += member.size
            if total > 128 * 1024**2 or member.size > 32 * 1024**2:
                raise ValueError("Contenido rustfmt excesivo.")
            relative = name.removeprefix(spec["archive_prefix"])
            if relative not in SELECTED:
                continue
            raw = archive.extractfile(member).read()
            if spec["files_to_copy"][relative] != {"sha256": digest(raw), "size": len(raw)}:
                raise ValueError("Un payload rustfmt no conserva sus bytes.")
            found[relative] = raw
    if set(found) != SELECTED:
        raise ValueError("Falta un payload rustfmt declarado.")
    return found


def require_driver(data, expected):
    if digest(data) != expected:
        raise ValueError("El driver instalado no coincide con el SDK3 original.")


def validate_formatter_version(version):
    # El paquete se etiqueta beta.10; el binario conserva el canal beta.
    if not version.startswith("rustfmt 1.11.0-beta ") or "(01ddda61fa 2026-11-04)" not in version:
        raise ValueError("El formatter no acredita versión/canal/commit fijados.")


def inside_augment():
    spec_raw = read_regular(WORK + "/inputs.json", 65536)
    if digest(spec_raw) != os.environ.get("AGENCY_AUGMENT_INPUTS_SHA256"):
        raise ValueError("El manifiesto copiado no conserva su SHA.")
    spec = json.loads(spec_raw)
    validate_spec(spec)
    payload = selected_payload(read_regular(WORK + "/rustfmt.tar.xz", 4 * 1024**2), spec)
    driver = spec["driver"]
    require_driver(read_regular(driver["path"]), driver["sha256"])
    root = Path(INSTALL_PREFIX)
    root.mkdir(mode=0o700)
    for name, data in payload.items():
        path = root / name
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o700 if name == "bin/rustfmt" else 0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    expected = {name: item["sha256"] for name, item in spec["files_to_copy"].items()}
    copied = verify_tree(root, expected)
    env = {
        "PATH": "/opt/agency-rust/bin:/usr/bin:/bin",
        "HOME": WORK,
        "LANG": "C.UTF-8",
        "LD_LIBRARY_PATH": "/opt/agency-rust/lib",
    }
    binary = INSTALL_PREFIX + "/bin/rustfmt"
    version = (
        subprocess.run(
            [binary, "--version"], cwd=WORK, env=env, capture_output=True, check=True, timeout=5
        )
        .stdout.decode()
        .strip()
    )
    validate_formatter_version(version)
    probe = Path(WORK) / "agency-format-probe.rs"
    with probe.open("x") as stream:
        stream.write("pub fn agency_probe()->u8{7}\n")
    rejected = subprocess.run(
        [binary, str(probe), "--edition", "2021", "--check"],
        cwd=WORK,
        env=env,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if rejected.returncode != 1:
        raise ValueError("Rustfmt no detectó la entrada sin formato.")
    subprocess.run(
        [binary, str(probe), "--edition", "2021"],
        cwd=WORK,
        env=env,
        capture_output=True,
        check=True,
        timeout=10,
    )
    formatted = read_regular(probe, 1024)
    if formatted != b"pub fn agency_probe() -> u8 {\n    7\n}\n":
        raise ValueError("La salida real de rustfmt no coincide con la prueba pública.")
    subprocess.run(
        [binary, str(probe), "--edition", "2021", "--check"],
        cwd=WORK,
        env=env,
        capture_output=True,
        check=True,
        timeout=10,
    )
    if verify_tree(root, expected) != copied:
        raise ValueError("El payload cambió durante la prueba.")
    require_driver(read_regular(driver["path"]), driver["sha256"])
    value = {
        "stage": "rustfmt-augmentation",
        "verified": True,
        "sdk_executed": True,
        "formatter_executed": True,
        "compiler_executed": False,
        "installer_executed": False,
        "cargo_fmt_installed": False,
        "version": version,
        "distribution_version": spec["rustfmt_version"],
        "archive_sha256": spec["archive_sha256"],
        "manifest_sha256": digest(spec_raw),
        "driver": driver,
        "install_prefix": INSTALL_PREFIX,
        "payload": copied,
        "files": spec["files_to_copy"],
        "probe_sha256": digest(formatted),
        "initial_check_returncode": 1,
        "final_check_returncode": 0,
    }
    descriptor = os.open(
        "/outputs/rustfmt-augmentation-receipt.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def inside_copy_check():
    info = os.lstat(WORK)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
        or set(os.listdir(WORK)) != {"verifier.py"}
    ):
        raise ValueError("El namespace del aumento ya tiene entradas o no acredita root.")
    if digest(read_regular(WORK + "/verifier.py")) != os.environ.get(
        "AGENCY_AUGMENT_SOURCE_SHA256"
    ):
        raise ValueError("La fuente no coincide antes de copiar el archivo adicional.")


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
        "/work/.compile-agency-v2",
        "/outputs/compile-v1",
        "/outputs/compile-v2",
        "/opt/agency-sqlite",
        "/opt/agency-sqlite-v2",
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
                        str(path)
                        not in (
                            "/opt/agency-sqlite/lib/libsqlite3.so",
                            "/opt/agency-sqlite-v2/lib/libsqlite3.so",
                        )
                        or os.readlink(path) != "libsqlite3.so.0"
                    ):
                        raise ValueError("Enlace no admitido en los parciales V1.")
                    observed[relative] = {"link": "libsqlite3.so.0"}
                elif path.is_file():
                    observed[relative] = digest(read_regular(path, allow_hardlinks=True))
                elif not path.is_dir():
                    raise ValueError("Archivo especial en los parciales V1.")
                if len(observed) > 25000:
                    raise ValueError("Parciales excesivos.")
        partial[root] = {
            "exists": True,
            "files": len(observed),
            "tree_sha256": digest(canonical(observed)),
        }
    probe = json.loads(read_regular("/outputs/compile-v2/sqlite-probe.json"))
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
    validate_spec(profile["artifact"])
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


def augment_request(request, normal, compiler, store, docker, observed, base):
    profile = request["profile"]
    control = store.descriptors["control"]
    profile_sha = base["profile_sha256"]
    armed, step = False, "preflight_aumento"
    try:
        restarted = request.get("restarted", {})
        raw = normal.read_file(control, "augment-v1-restart.json")
        receipt = normal.json_object(raw)
        if (
            restarted != envelope(receipt)
            or receipt.get("success") is not True
            or receipt.get("stage") != "restart"
            or receipt.get("profile_sha256") != profile_sha
            or receipt.get("source_sha256") != profile["sources"]["remote"]
            or observed != receipt.get("container")
            or not observed["running"]
        ):
            raise ValueError("El aumento exige exactamente RESTART_B64 respaldado y vivo.")
        proof_raw = normal.read_file(store.descriptors["outputs"], PROOF, maximum=256 * 1024)
        if digest(proof_raw) != receipt.get("proof_sha256") or proof_raw != canonical(
            receipt["proof"]
        ):
            raise ValueError("La verificación del reinicio cambió.")
        spec_raw = base64.b64decode(request["manifest_source_b64"], validate=True)
        archive = base64.b64decode(request["artifact_source_b64"], validate=True)
        if (
            digest(spec_raw) != profile["sources"]["manifest"]
            or json.loads(spec_raw) != profile["artifact"]
        ):
            raise ValueError("Manifiesto adicional distinto del respaldado.")
        # Se valida antes de escribir control o copiar al CID.
        selected_payload(archive, profile["artifact"])
        os.mkdir(".augment-v1-payload-started", 0o700, dir_fd=control)
        armed = True
        normal.write_file(control, "augment-v1-inputs.json", spec_raw)
        normal.write_file(control, "augment-v1-rustfmt.tar.xz", archive)
        step = "copiar_payload_datos"
        checked = docker.command(
            [
                "exec",
                "--env",
                "AGENCY_AUGMENT_SOURCE_SHA256=" + profile["sources"]["remote"],
                CID,
                "/usr/bin/python3",
                "-B",
                WORK + "/verifier.py",
                "inside-copy-check",
            ],
            timeout=5,
        )
        if checked["returncode"] != 0:
            raise ValueError("Los destinos del payload no están vacíos y acreditados.")
        for source, destination in (
            ("augment-v1-inputs.json", "inputs.json"),
            ("augment-v1-rustfmt.tar.xz", "rustfmt.tar.xz"),
        ):
            if normal.check_container(docker.inspect(CID), CID) != observed:
                raise ValueError("La instancia cambió antes de la copia.")
            copied = docker.command(
                ["cp", normal.CONTROL + "/" + source, CID + ":" + WORK + "/" + destination],
                timeout=10,
            )
            if copied["returncode"] != 0:
                raise ValueError("No se pudo copiar el payload propio.")
        docker.heartbeat_fd = request["heartbeat_fd"]
        limits = docker.limits(observed)
        step = "copiar_y_probar_rustfmt_real"
        descriptor = os.open(
            "augment-v1-formatter.log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=control,
        )
        with os.fdopen(descriptor, "wb", buffering=0) as sink:
            process = docker.command(
                [
                    "exec",
                    "--env",
                    "AGENCY_AUGMENT_SOURCE_SHA256=" + profile["sources"]["remote"],
                    "--env",
                    "AGENCY_AUGMENT_INPUTS_SHA256=" + profile["sources"]["manifest"],
                    CID,
                    "/usr/bin/python3",
                    "-B",
                    WORK + "/verifier.py",
                    "inside-augment",
                ],
                timeout=90,
                sink=sink,
            )
            sink.flush()
            os.fsync(sink.fileno())
        if process["returncode"] != 0:
            error = ValueError("El aumento/formato real devolvió fallo.")
            error.process = process["process"]
            raise error
        artifact_raw = normal.read_file(
            store.descriptors["outputs"], "rustfmt-augmentation-receipt.json", maximum=65536
        )
        artifact = normal.json_object(artifact_raw)
        if (
            artifact_raw != canonical(artifact)
            or artifact.get("verified") is not True
            or artifact.get("formatter_executed") is not True
            or artifact.get("installer_executed") is not False
            or artifact.get("cargo_fmt_installed") is not False
            or artifact.get("archive_sha256") != profile["artifact"]["archive_sha256"]
            or artifact.get("manifest_sha256") != profile["sources"]["manifest"]
            or artifact.get("driver") != profile["artifact"]["driver"]
            or artifact.get("files") != profile["artifact"]["files_to_copy"]
            or artifact.get("final_check_returncode") != 0
        ):
            raise ValueError("El aumento no acredita bytes/driver/formateador real.")
        checks = docker.capture(
            [
                "exec",
                CID,
                "/usr/bin/sha256sum",
                WORK + "/verifier.py",
                *[INSTALL_PREFIX + "/" + name for name in sorted(SELECTED)],
            ],
            timeout=5,
        ).decode()
        expected = (
            profile["sources"]["remote"]
            + "  "
            + WORK
            + "/verifier.py\n"
            + "".join(
                profile["artifact"]["files_to_copy"][name]["sha256"]
                + "  "
                + INSTALL_PREFIX
                + "/"
                + name
                + "\n"
                for name in sorted(SELECTED)
            )
        )
        if checks != expected:
            raise ValueError("Los bytes instalados no conservan sus SHA después de ejecutar.")
        after = normal.check_container(docker.inspect(CID), CID)
        if after != observed:
            raise ValueError("La instancia cambió durante el aumento.")
        docker.limits(after)
        compiler.verify_sdk_store(store, normal, profile["compile_profile"])
        log = normal.read_file(control, "augment-v1-formatter.log", maximum=normal.MAX_OUTPUT)
        result = {
            **base,
            "stage": "augment",
            "success": True,
            "container": after,
            "restart_receipt_sha256": restarted["receipt_sha256"],
            "effective_limits": limits,
            "artifact_receipt_sha256": digest(artifact_raw),
            "artifact": artifact,
            "process": process["process"],
            "log_sha256": digest(log),
            "log_bytes": len(log),
            "sdk_executed": True,
            "compiler_executed": False,
        }
        normal.write_file(control, "augment-v1-augmentation.json", canonical(result))
        return {"success": True, "result": envelope(result)}
    except BaseException as error:
        if hasattr(signal, "alarm"):
            signal.alarm(0)
        cleanup = None
        # Esta etapa parte de un CID ya vivo: incluso un preflight fallido debe
        # detenerlo por NoForeign, sin confiar en sus outputs escribibles.
        try:
            cleanup = normal.stop_owned(docker, CID)
        except BaseException as failed:
            cleanup = {"stopped": False, "error_type": type(failed).__name__}
        result = {
            **base,
            "stage": "augment",
            "step": step,
            "success": False,
            "process": getattr(error, "process", None),
            "error_type": type(error).__name__,
            "cleanup": cleanup,
            "partial_evidence_preserved": True,
        }
        if armed:
            try:
                log = normal.read_file(
                    control, "augment-v1-formatter.log", maximum=normal.MAX_OUTPUT
                )
                result.update(log_sha256=digest(log), log_bytes=len(log))
            except FileNotFoundError:
                pass
            normal.write_file(control, "augment-v1-augmentation.json", canonical(result))
        return {"success": False, "result": envelope(result)}


def apply(request):
    profile = request["profile"]
    validate_profile(profile)
    profile_sha = digest(canonical(profile))
    if (
        profile_sha != request["profile_sha256"]
        or globals().get("_AUGMENT_SOURCE_SHA256") != profile["sources"]["remote"]
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
        if normal.read_file(control, "compile-v2-receipt.json") != canonical(
            profile["failure"]["receipt"]
        ):
            raise ValueError("El fallo remoto no coincide con el respaldo.")
        if (
            digest(normal.read_file(control, "compile-v2.log", maximum=normal.MAX_OUTPUT))
            != FAILED_LOG_SHA
        ):
            raise ValueError("El log inicial cambió.")
        for name, expected in (
            ("compile-v2-profile.json", PROFILE_V1_SHA),
            ("compile-v2-verifier.py", COMPILE_SHA),
            ("compile-v2-source.sh", profile["compile_profile"]["sources"]["recipe"]),
        ):
            if digest(normal.read_file(control, name)) != expected:
                raise ValueError("Una fuente histórica de compile V1 cambió.")
        docker = store.docker()
        raw = docker.inspect(CID)
        observed = normal.check_container(raw, CID)
        previous = profile["compile_profile"]["resume"]["restart"]["receipt"]["container"]
        if request["action"] != "augment":
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
            normal.write_file(control, "augment-v1-stopped.json", canonical(receipt))
            return {"success": True, "result": envelope(receipt)}
        if request["action"] == "augment":
            return augment_request(request, normal, compiler, store, docker, observed, base)
        if request["action"] != "restart":
            raise ValueError("Acción resume no admitida.")
        stopped = request.get("stopped", {})
        actual = normal.read_file(control, "augment-v1-stopped.json")
        if (
            stopped != envelope(normal.json_object(actual))
            or stopped["receipt"].get("inspection_sha256") != base["inspection_sha256"]
            or stopped["receipt"].get("profile_sha256") != profile_sha
        ):
            raise ValueError("STOPPED_B64 debe respaldar literalmente el estado observado.")
        os.mkdir(".augment-v1-started", 0o700, dir_fd=control)
        armed = True
        step = "reiniciar_hold"
        normal.write_file(control, "augment-v1-profile.json", canonical(profile))
        source = base64.b64decode(request["resume_source_b64"], validate=True)
        if digest(source) != profile["sources"]["remote"]:
            raise ValueError("Fuente resume diferente antes de copiar.")
        normal.write_file(control, "augment-v1-verifier.py", source)
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
            ["cp", normal.CONTROL + "/augment-v1-verifier.py", CID + ":" + WORK + "/verifier.py"],
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
            "augment-v1-verification.log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=control,
        )
        with os.fdopen(log, "wb", buffering=0) as sink:
            result = docker.command(
                [
                    "exec",
                    "--env",
                    "AGENCY_AUGMENT_SOURCE_SHA256=" + profile["sources"]["remote"],
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
        log_raw = normal.read_file(
            control, "augment-v1-verification.log", maximum=normal.MAX_OUTPUT
        )
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
        normal.write_file(control, "augment-v1-restart.json", canonical(receipt))
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
                    "augment-v1-verification.log",
                    maximum=normal.MAX_OUTPUT,
                )
                receipt.update(log_sha256=digest(log_raw), log_bytes=len(log_raw))
            except FileNotFoundError:
                pass
            normal.write_file(
                store.descriptors["control"], "augment-v1-restart.json", canonical(receipt)
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
    if sys.argv[1:] in (["inside-verify"], ["inside-augment"], ["inside-copy-check"]):
        if (
            os.name != "posix"
            or os.geteuid() != 0
            or not Path("/.dockerenv").is_file()
            or digest(read_regular(__file__)) != os.environ.get("AGENCY_AUGMENT_SOURCE_SHA256")
        ):
            raise ValueError("Verificación sólo en el CID Linux propio.")
        if sys.argv[1] == "inside-verify":
            inside_verify()
        elif sys.argv[1] == "inside-copy-check":
            inside_copy_check()
        else:
            inside_augment()
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
        if not 0 < length <= 8 * 1024**2:
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
