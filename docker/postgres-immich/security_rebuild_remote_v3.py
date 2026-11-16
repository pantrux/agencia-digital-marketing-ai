"""Reconstrucción propia por etapas; importar este archivo no ejecuta operaciones."""

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import signal
import stat
import struct
import sys
import tarfile
import time
import tomllib
import types
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

OWNER = "agencia-digital-marketing-ai:postgres-immich-security-rebuild:v2"
CID = "f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d"
IMAGE = "sha256:c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a"
WORK = "/work/.security-rebuild-v2"
OUTPUT = "/outputs/security-rebuild-v2"
STAGING = "/home/jandrade/.agency-pg-security-rebuild-v2"
CONTROL_BASE = (
    "/var/lib/media-platform/build/postgres-immich/03107096f926423b08a91f58269628bc/control"
)
CONTROL = CONTROL_BASE + "/security-rebuild-v2"
MODES = {
    "receive",
    "prepare",
    "restart",
    "materialize",
    "graph",
    "regression",
    "build",
    "catalog",
    "stop",
}
BUDGETS = {
    "receive": 180,
    "prepare": 120,
    "restart": 60,
    "materialize": 300,
    "graph": 300,
    "regression": 300,
    "build": 1800,
    "catalog": 180,
    "stop": 20,
}
NORMAL_SHA = "a13ab96098b5f739ed88cbacaffc5bed521825aeb033d428bddbf93be57137ed"
COMPILER_SHA = "e0e2bc46a65563e122a1469ac9ed5040836697748846e0408938e208e7049a3d"
AUGMENT_SHA = "997877a7ae20478b7c6cb6afd3f7f2432a325c7aa319d2004508fffdbf3724dc"
OLD_PROFILE_SHA = "bffefb81179fd767d640faf106965ba7d465e90c3a789b4e7800b988a0152153"
RECOVER_SHA = "df3913b08f92f41c85243f5621db65b12562a31160da1562f0cdd48add794108"
SQLITE_SHA = "78711a51904d2ce47d4c16cb53365e85d73bc15cd4aa6d10c4d2ef3754d81c53"
PGVECTOR_SHA = "050bd2625e0efcf8f1b5bebd32135b1958a6f9b98e2c73d13137152488451d3b"
XTASK_SHA = "9055ec9b313bcc827c52705f37bca6ceba4468de2073cb5de319e703684969b1"
REVIEWED = "PG16_SIMD_INIT_CONFIRMED_SECURITY_UPGRADES_ONLY"
SDK_SECURITY_POLICY = {
    "required": True,
    "apk_package_count": 110,
    "apk_database_sha256": "9dd4902449589bccb94034a7e5fd51d280ecd9bee7d7adb7521412b34c07aded",
    "formatter_sha256": "bc01859cf8d5b93e912b62d711500a30a19da8ad2fe52ba7c2d1c5efb67b1c3f",
    "driver_sha256": "626e0c6efaac2ce87441a7fffa7ee53f66c3a90df282803420771698d33b2e2f",
    "state_receipt_sha256": "fc3341c72a1d2c9dfba1f1b25a23116ff457f75fc985a4f6fe5a68c1d3ee8d51",
}
SDK_GATED_MODES = {"restart", "materialize", "graph", "regression", "build", "catalog"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def module_bytes(name, raw, expected):
    if digest(raw) != expected:
        raise ValueError("Fuente de soporte distinta de su pin.")
    result = types.ModuleType(name)
    result.__dict__["_SOURCE_SHA256"] = expected
    exec(compile(raw, "<" + name + ">", "exec"), result.__dict__)
    return result


def safe_relative(name):
    if (
        not isinstance(name, str)
        or not name
        or name.startswith("/")
        or "\\" in name
        or any(p in {"", ".", ".."} or ":" in p for p in name.split("/"))
        or any(ord(c) < 32 for c in name)
    ):
        raise ValueError("Ruta relativa ambigua.")
    return name


def parse_features(text, allowed):
    if not isinstance(text, str) or len(text.encode()) > 256 * 1024:
        raise ValueError("Grafo excesivo.")
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(
            r"([A-Za-z0-9_-]+) v([^ |]+)((?: \([^()\r\n]+\))*)\|([A-Za-z0-9_,+.-]*)(?: \(\*\))?",
            line,
        )
        if match is None or match.group(1, 2) not in allowed:
            raise ValueError("Línea o paquete ajeno al grafo.")
        for annotation in re.findall(r"\(([^()]+)\)", match[3]):
            if annotation in {"proc-macro", "*"}:
                continue
            if not (
                annotation == WORK + "/source"
                or re.fullmatch(re.escape(WORK + "/source/") + r"[A-Za-z0-9_+./-]+", annotation)
            ) or ".." in annotation.split("/"):
                raise ValueError("La fuente no pertenece al workspace exacto nuevo.")
        result.setdefault(match.group(1, 2), set()).update(filter(None, match[4].split(",")))
    if not result or any(
        name in {"git2", "wasm-bindgen", "wasm-bindgen-shared"} for name, _ in result
    ):
        raise ValueError("Grafo vacío o ruta VCS no admitida.")
    return result


def build_environment():
    return {
        "PATH": "/opt/agency-rust/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": WORK + "/home",
        "TMPDIR": WORK + "/tmp",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "CARGO": "/opt/agency-rust/bin/cargo",
        "RUSTC": "/opt/agency-rust/bin/rustc",
        "RUSTDOC": "/opt/agency-rust/bin/rustdoc",
        "RUSTFMT": "/opt/agency-rustfmt-v1/bin/rustfmt",
        "CARGO_HOME": WORK + "/cargo-home",
        "CARGO_TARGET_DIR": WORK + "/target",
        "CARGO_BUILD_JOBS": "2",
        "CARGO_NET_OFFLINE": "true",
        "CARGO_TERM_COLOR": "never",
        "PG_CONFIG": "/usr/local/bin/pg_config",
        "PGRX_PG_CONFIG_PATH": "/usr/local/bin/pg_config",
        "TARGET": "x86_64-unknown-linux-musl",
        "CC": "/usr/bin/clang-21",
        "CXX": "/usr/bin/clang++-21",
        "RUSTC_BOOTSTRAP": "1",
        "CARGO_PROFILE_RELEASE_STRIP": "none",
        "RUSTFLAGS": "-Dwarnings -Ctarget-feature=-crt-static -Cpanic=unwind -Clink-arg=-Wl,-rpath,$ORIGIN",
        "SQLITE3_DYNAMIC": "1",
        "LIBSQLITE3_SYS_USE_PKG_CONFIG": "1",
        "SQLITE3_LIB_DIR": "/opt/agency-sqlite-v3/lib",
        "SQLITE3_INCLUDE_DIR": "/opt/agency-sqlite-v3/include",
        "PKG_CONFIG_PATH": "/opt/agency-sqlite-v3/lib/pkgconfig",
        "LD_LIBRARY_PATH": "/opt/agency-sqlite-v3/lib:/opt/agency-rust/lib",
    }


def exec_arguments(mode, profile_sha, verifier_sha):
    if mode not in {"materialize", "graph", "regression", "build", "catalog"} or any(
        re.fullmatch("[a-f0-9]{64}", s) is None for s in (profile_sha, verifier_sha)
    ):
        raise ValueError("Modo o hash de exec no admitido.")
    environment = {
        **build_environment(),
        "AGENCY_REBUILD_PROFILE_SHA256": profile_sha,
        "AGENCY_REBUILD_VERIFIER_SHA256": verifier_sha,
    }
    return [
        "exec",
        CID,
        "/usr/bin/env",
        "-i",
        *[k + "=" + v for k, v in sorted(environment.items())],
        "/bin/bash",
        WORK + "/recipe.sh",
        mode,
    ]


def require_previous(mode, saved, *, reviewed):
    needs = {
        "receive": [],
        "prepare": ["receive"],
        "restart": ["prepare"],
        "materialize": ["prepare", "restart"],
        "graph": ["materialize"],
        "regression": ["graph"],
        "build": ["graph", "regression"],
        "catalog": ["build"],
        "stop": [],
    }
    if mode not in needs or any(
        saved.get(name, {}).get("success") is not True for name in needs[mode]
    ):
        raise ValueError("Faltan etapas exitosas respaldadas.")
    if mode in {"regression", "build"} and not reviewed:
        raise ValueError("El grafo y alcance de las regresiones requieren revisión respaldada.")


def check_birth(observed, original, *, running):
    if (
        any(observed.get(k) != original.get(k) for k in ("cid", "created", "image_id"))
        or observed.get("cid") != CID
        or observed.get("image_id") != IMAGE
        or observed.get("running") is not running
    ):
        raise ValueError("El SDK no conserva CID, nacimiento, imagen y estado propios.")
    return observed


def check_reused(observed, expected):
    if set(observed) != {"libsqlite3.so.0", "vector.so"} or observed != expected:
        raise ValueError("Una biblioteca reutilizada cambió.")


def check_exec_hashes(raw, expected):
    lines = raw.decode("ascii", "strict").splitlines()
    wanted = [pin + "  " + path for path, pin in expected.items()]
    if lines != wanted:
        raise ValueError(
            "La receta/verificador/perfil dentro del SDK no coincide antes de ejecutar."
        )


def read_payload(stream, inventory):
    if (
        not inventory
        or len(inventory) > 20
        or sum(r["size"] for r in inventory.values()) > 64 * 1024**2
    ):
        raise ValueError("Transferencia excesiva o vacía.")
    result = {}
    for name, row in sorted(inventory.items()):
        safe_relative(name)
        if "/" in name or type(row["size"]) is not int or not 0 < row["size"] <= 48 * 1024**2:
            raise ValueError("Archivo transferido inválido.")
        parts, remaining = [], row["size"]
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise ValueError("Transferencia truncada.")
            parts.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(parts)
        if digest(raw) != row["sha256"]:
            raise ValueError("Checksum transferido distinto.")
        result[name] = raw
    if stream.read(1):
        raise ValueError("Bytes adicionales no declarados.")
    return result


def write_new_at(directory, name, raw):
    safe_relative(name)
    if "/" in name:
        raise ValueError("Sólo nombre simple en publicación.")
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory)


@contextlib.contextmanager
def operation_lock(directory):
    import fcntl

    fd = os.open(".operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("Lock no privado.")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def regression_inputs():
    manifest = b"""[package]
name = "agency-native-security-regression"
version = "0.0.0"
edition = "2021"
[workspace]
[dependencies]
anyhow = {path = "../vendor/anyhow-1.0.103"}
crossbeam-epoch = {path = "../vendor/crossbeam-epoch-0.9.20", default-features = false, features = ["std"]}
"""
    lock = b"""version = 4
[[package]]
name = "agency-native-security-regression"
version = "0.0.0"
dependencies = ["anyhow", "crossbeam-epoch"]
[[package]]
name = "anyhow"
version = "1.0.103"
[[package]]
name = "crossbeam-epoch"
version = "0.9.20"
dependencies = ["crossbeam-utils"]
[[package]]
name = "crossbeam-utils"
version = "0.8.21"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "d0a5c400df2834b80a4c3327b3aad3a4c4cd4de0629063962b03235697506a28"
"""
    source = b"""// Casos derivados de los avisos y pruebas upstream MIT OR Apache-2.0.
#[cfg(test)] mod tests {
    use crossbeam_epoch::{Atomic, Shared};
    use std::{fmt, mem::MaybeUninit};
    #[derive(Debug)] struct Context(String);
    impl fmt::Display for Context {
        fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { fmt::Display::fmt(&self.0, f) }
    }
    #[test] fn anyhow_context_downcast_mut_behavior() {
        let mut error = anyhow::Error::msg("inner").context(Context("old".into()));
        error.downcast_mut::<Context>().unwrap().0 = "new".into();
        assert_eq!(error.to_string(), "new");
    }
    #[test] fn epoch_official_format_null() {
        let atomic = Atomic::<usize>::null(); assert_eq!(format!("{atomic:p}"), "0x0");
        let atomic = Atomic::<[MaybeUninit<usize>]>::null(); assert_eq!(format!("{atomic:p}"), "0x0");
        let shared = Shared::<usize>::null(); assert_eq!(format!("{shared:p}"), "0x0");
        let shared = Shared::<[MaybeUninit<usize>]>::null(); assert_eq!(format!("{shared:p}"), "0x0");
    }
}
"""
    return {"Cargo.toml": manifest, "Cargo.lock": lock, "src/lib.rs": source}, {
        "executed": False,
        "anyhow_ub_miri_proven": False,
        "scope": "Anyhow sólo comportamiento funcional; no demuestra ausencia de UB sin Miri. Epoch reproduce format_null oficial. Sin dependencias nuevas.",
    }


def check_regression_output(raw):
    text = raw.decode("utf-8", "strict")
    text = text.replace("\r\n", "\n")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ValueError("El registro del harness contiene controles no admitidos.")
    lines = text.split("\n")
    if any("FAILED" in line or "failures:" in line for line in lines):
        raise ValueError("El registro del harness contiene resultados contradictorios.")
    expected = [
        "test tests::anyhow_context_downcast_mut_behavior ... ok",
        "test tests::epoch_official_format_null ... ok",
    ]
    tests = [
        line
        for line in lines
        if "test result:" not in line and (line.startswith("test ") or "test tests::" in line)
    ]
    if sorted(tests) != sorted(expected):
        raise ValueError("El harness no acredita exactamente las dos regresiones completas.")
    summaries = [line for line in lines if "test result:" in line]
    if (
        len(summaries) != 1
        or re.fullmatch(
            r"test result: ok\. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
            r"finished in (?:0|[1-9][0-9]*)(?:\.[0-9]+)?s",
            summaries[0],
        )
        is None
    ):
        raise ValueError("El harness no acredita exactamente dos pruebas aprobadas.")


def load_support(payload, *, stopped_only=False):
    normal = module_bytes(
        "frozen_sdk_v4", base64.b64decode(payload["normal"], validate=True), NORMAL_SHA
    )
    if stopped_only:
        return normal, None, None, None, None
    compiler = module_bytes(
        "frozen_compile_v3", base64.b64decode(payload["compiler"], validate=True), COMPILER_SHA
    )
    augment = module_bytes(
        "frozen_augment_v1", base64.b64decode(payload["augment"], validate=True), AUGMENT_SHA
    )
    old = base64.b64decode(payload["old_profile"], validate=True)
    recover = base64.b64decode(payload["recover"], validate=True)
    if digest(old) != OLD_PROFILE_SHA or digest(recover) != RECOVER_SHA:
        raise ValueError("Las referencias históricas cambiaron.")
    old = normal.json_object(old)
    compiler.validate_profile(old)
    recover = normal.json_object(recover)
    if (
        recover.get("receipt_sha256") != digest(canonical(recover["receipt"]))
        or recover["receipt"].get("success") is not True
    ):
        raise ValueError("El catálogo RECOVER no acredita su recibo.")
    return normal, compiler, augment, old, recover["receipt"]


def validate_plan(plan):
    required = {
        "schema_version",
        "owner",
        "cid",
        "image",
        "original_container",
        "paths",
        "sources",
        "references",
        "files",
        "budgets",
        "environment_sha256",
        "regression_inputs_sha256",
        "local_root",
        "sdk_security_policy",
    }
    if (
        set(plan) != required
        or plan["schema_version"] != 1
        or plan["owner"] != OWNER
        or plan["cid"] != CID
        or plan["image"] != IMAGE
    ):
        raise ValueError("Perfil de reconstrucción ajeno o incompleto.")
    if (
        plan["paths"] != {"staging": STAGING, "control": CONTROL, "work": WORK, "output": OUTPUT}
        or plan["budgets"] != BUDGETS
    ):
        raise ValueError("Rutas o presupuestos distintos del contrato.")
    if plan["sdk_security_policy"] != SDK_SECURITY_POLICY:
        raise ValueError("El perfil omite el gate SCA vigente del SDK.")
    if (
        plan["environment_sha256"] != digest(canonical(build_environment()))
        or plan["regression_inputs_sha256"]
        != digest(canonical({k: digest(v) for k, v in regression_inputs()[0].items()}))
        or set(plan["sources"]) != {"local", "remote", "recipe"}
        or any(re.fullmatch("[a-f0-9]{64}", v) is None for v in plan["sources"].values())
    ):
        raise ValueError("Entorno, regresión o fuentes no coinciden.")
    if len(canonical(plan)) > 32768 or not 0 < len(plan["files"]) <= 20:
        raise ValueError("El plan debe ser pequeño y por referencias.")
    for name, row in plan["files"].items():
        if (
            safe_relative(name) != name
            or "/" in name
            or set(row) != {"sha256", "size"}
            or re.fullmatch("[a-f0-9]{64}", row["sha256"]) is None
            or type(row["size"]) is not int
            or not 0 < row["size"] <= 48 * 1024**2
        ):
            raise ValueError("Inventario de recepción inválido.")
    if sum(row["size"] for row in plan["files"].values()) > 64 * 1024**2:
        raise ValueError("Payload excesivo.")
    return digest(canonical(plan))


def validate_sdk_security(approval, profile, *, now=None):
    if set(approval) != {"raw_b64", "sha256"}:
        raise ValueError("Falta aprobación SCA externa de Root.")
    raw = base64.b64decode(approval["raw_b64"], validate=True)
    if len(raw) > 65536 or digest(raw) != approval["sha256"]:
        raise ValueError("El recibo SCA no coincide con su SHA externo.")
    value = json.loads(raw)
    keys = {
        "schema_version",
        "root_approved",
        "cid",
        "created",
        "apk_database_sha256",
        "apk_package_count",
        "apk_exact_coverage",
        "formatter_sha256",
        "driver_sha256",
        "sdk_state_receipt_sha256",
        "high_critical_count",
        "coverage_scope",
        "reports",
        "scanned_at_utc",
        "db_metadata",
    }
    if (
        set(value) != keys
        or value["schema_version"] != 1
        or value["root_approved"] is not True
        or value["cid"] != CID
        or value["created"] != profile["original_container"]["created"]
        or value["apk_package_count"] != 110
        or value["apk_exact_coverage"] is not True
        or any(
            value[key] != SDK_SECURITY_POLICY[key]
            for key in ("apk_database_sha256", "formatter_sha256", "driver_sha256")
        )
        or value["sdk_state_receipt_sha256"] != SDK_SECURITY_POLICY["state_receipt_sha256"]
        or type(value["high_critical_count"]) is not int
        or value["high_critical_count"] < 0
        or not isinstance(value["coverage_scope"], str)
        or not 1 <= len(value["coverage_scope"]) <= 2048
    ):
        raise ValueError("La aprobación no acredita el sujeto/alcance SDK exacto.")
    reports = value["reports"]
    if (
        not isinstance(reports, list)
        or not 1 <= len(reports) <= 20
        or any(
            not isinstance(row, dict)
            or set(row) != {"path", "sha256"}
            or not isinstance(row["path"], str)
            or re.fullmatch("[a-f0-9]{64}", row["sha256"]) is None
            for row in reports
        )
    ):
        raise ValueError("Faltan referencias físicas del análisis SDK.")

    def date(text):
        if not isinstance(text, str):
            raise ValueError("Fecha de aprobación no textual.")
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("La fecha exige zona horaria.")
        return result.astimezone(UTC)

    current = datetime.now(UTC) if now is None else now
    db = value["db_metadata"]
    if (
        db.get("Version") != 2
        or not date(db["UpdatedAt"]) <= date(value["scanned_at_utc"]) <= current
        or not current < date(db["NextUpdate"])
    ):
        raise ValueError("La SCA/DB del SDK está vencida o no es verificable.")
    # La decisión específica corresponde a Root; no se cambia severidad ni cobertura.
    return {
        "receipt_sha256": approval["sha256"],
        "high_critical_count": value["high_critical_count"],
        "coverage_scope": value["coverage_scope"],
    }


def envelope(receipt):
    return {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}


def verified_saved(request):
    saved = {}
    for mode, value in request.get("backed", {}).items():
        if (
            mode not in MODES
            or set(value) != {"receipt", "receipt_sha256"}
            or digest(canonical(value["receipt"])) != value["receipt_sha256"]
        ):
            raise ValueError("Recibo respaldado inválido.")
        row = value["receipt"]
        if (
            row.get("profile_sha256") != request["profile_sha256"]
            or row.get("owner") != OWNER
            or row.get("stage") != mode
            or row.get("success") is not True
        ):
            raise ValueError("El recibo pertenece a otro perfil o etapa.")
        saved[mode] = row
    return saved


def daemon_identity(docker):
    value = json.loads(docker.capture(["info", "--format", "{{json .ID}}"], timeout=3))
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9:+.-]{1,128}", value) is None:
        raise ValueError("Identidad Docker no acreditada.")
    return value


def control_open(normal):
    parent = normal.marked_directory(CONTROL_BASE)
    try:
        fd = os.open(
            "security-rebuild-v2", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        normal.checked_node(os.fstat(fd), 0, directory=True)
        return fd
    finally:
        os.close(parent)


def check_control(normal, descriptor, request, saved):
    marker = normal.json_object(normal.read_file(descriptor, ".owner.json"))
    current = normal.open_directory(CONTROL)
    try:
        identity = normal.identity(os.fstat(descriptor))
        if normal.identity(os.fstat(current)) != identity or marker != {
            "owner": OWNER,
            "profile_sha256": request["profile_sha256"],
            "directory": identity,
        }:
            raise ValueError("El control SSD cambió de propietario o identidad.")
    finally:
        os.close(current)
    for mode, row in saved.items():
        if mode == "receive":
            continue
        if normal.read_file(descriptor, mode + ".json", maximum=1024 * 1024) != canonical(row):
            raise ValueError("Un recibo físico no coincide con su respaldo.")
    configuration = normal.open_directory(CONTROL_BASE + "/docker-config")
    try:
        if normal.read_file(configuration, "config.json") != b"{}":
            raise ValueError("Docker config privado alterado.")
    finally:
        os.close(configuration)
    return normal.Docker(CONTROL_BASE + "/docker-config")


def receive_data(request, stream, normal):
    if os.geteuid() != 1000:
        raise ValueError("La recepción requiere UID1000.")
    # Se recibe y valida antes de escribir; no se promueve todavía a root.
    content = read_payload(stream, request["profile"]["files"])
    home = normal.open_directory("/home/jandrade", uid=1000)
    fd = None
    try:
        name = PurePosixPath(STAGING).name
        os.mkdir(name, 0o700, dir_fd=home)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
        normal.checked_node(os.fstat(fd), 1000, directory=True)
        marker = {
            "owner": OWNER,
            "profile_sha256": request["profile_sha256"],
            "directory": normal.identity(os.fstat(fd)),
        }
        normal.write_file(fd, ".owner.json", canonical(marker), uid=1000)
        for name, raw in sorted(content.items()):
            normal.write_file(fd, name, raw, uid=1000)
        return {
            "directory": marker["directory"],
            "marker_sha256": digest(canonical(marker)),
            "files": len(content),
            "sdk_executed": False,
        }
    finally:
        if fd is not None:
            os.close(fd)
        os.close(home)


def prepare_control(request, normal, old, saved):
    normal.verify_host({"plan": old["sdk_plan"], "layout_source_b64": request["support"]["layout"]})
    stage = normal.open_directory(STAGING, uid=1000)
    parent, result = None, None
    try:
        normal.checked_node(os.fstat(stage), 1000, directory=True)
        marker = normal.read_file(stage, ".owner.json", uid=1000)
        expected = {
            "owner": OWNER,
            "profile_sha256": request["profile_sha256"],
            "directory": normal.identity(os.fstat(stage)),
        }
        transfer = saved["receive"]
        if (
            marker != canonical(expected)
            or digest(marker) != transfer["marker_sha256"]
            or transfer["directory"] != expected["directory"]
        ):
            raise ValueError("La recepción cambió antes de promoción.")
        normal.exact_tree(stage, set(request["profile"]["files"]) | {".owner.json"}, 1000)
        content = {}
        for name, meta in request["profile"]["files"].items():
            raw = normal.read_file(stage, name, uid=1000, maximum=48 * 1024**2)
            if len(raw) != meta["size"] or digest(raw) != meta["sha256"]:
                raise ValueError("Archivo recibido alterado.")
            content[name] = raw
        parent = normal.marked_directory(CONTROL_BASE)
        os.mkdir("security-rebuild-v2", 0o700, dir_fd=parent)
        result = os.open(
            "security-rebuild-v2", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        normal.checked_node(os.fstat(result), 0, directory=True)
        identity = normal.identity(os.fstat(result))
        normal.write_file(
            result,
            ".owner.json",
            canonical(
                {"owner": OWNER, "profile_sha256": request["profile_sha256"], "directory": identity}
            ),
        )
        for name, raw in sorted(content.items()):
            normal.write_file(result, name, raw)
        normal.write_file(result, "profile.json", canonical(request["profile"]))
        docker = normal.Docker(CONTROL_BASE + "/docker-config")
        observed = normal.check_container(docker.inspect(CID), CID)
        check_birth(observed, request["profile"]["original_container"], running=False)
        return {
            "directory": identity,
            "daemon_id": daemon_identity(docker),
            "container": observed,
            "files": len(content),
            "sdk_executed": False,
        }
    finally:
        os.close(stage)
        if parent is not None:
            os.close(parent)
        if result is not None:
            os.close(result)


def operating(request, normal, compiler, augment, old, recovered, saved):
    mode, profile = request["action"], request["profile"]
    control = control_open(normal)
    docker = None
    cleanup = None
    owned_epoch = False
    try:
        docker = check_control(normal, control, request, saved)
        if daemon_identity(docker) != saved["prepare"]["daemon_id"]:
            raise ValueError("Daemon Docker distinto del respaldado.")
        observed = normal.check_container(docker.inspect(CID), CID)
        if mode == "stop":
            check_birth(observed, profile["original_container"], running=observed["running"])
            cleanup = normal.stop_owned(docker, CID)
            # Stop no depende de input/source/vendor ni adquiere el lock de build.
            return {
                "cleanup": cleanup,
                "container": normal.check_container(docker.inspect(CID), CID),
                "sdk_executed": False,
            }
        with operation_lock(control):
            os.mkdir("." + mode + "-started", 0o700, dir_fd=control)
            if mode == "restart":
                check_birth(observed, profile["original_container"], running=False)
                owned_epoch = True
                result = docker.command(["start", CID], timeout=10)
                if result["returncode"]:
                    raise ValueError("Reinicio propio falló.")
                observed = normal.check_container(docker.inspect(CID), CID)
                check_birth(observed, profile["original_container"], running=True)
                return {
                    "container": observed,
                    "limits": docker.limits(observed),
                    "sdk_executed": False,
                }
            check_birth(observed, profile["original_container"], running=True)
            epoch = saved["restart"]["container"]
            if any(observed[k] != epoch[k] for k in ("pid", "started_at")):
                raise ValueError("El SDK vivo no coincide con el reinicio respaldado.")
            owned_epoch = True
            limits = docker.limits(observed)
            if mode == "materialize":
                result = docker.command(["exec", CID, "/bin/mkdir", "-m", "700", WORK], timeout=5)
                if result["returncode"]:
                    raise ValueError("El namespace de trabajo debe estar ausente.")
                for name, meta in profile["files"].items():
                    raw = normal.read_file(control, name, maximum=48 * 1024**2)
                    if digest(raw) != meta["sha256"] or len(raw) != meta["size"]:
                        raise ValueError("Una fuente física cambió antes de copiarla al SDK.")
                    result = docker.command(
                        ["cp", CONTROL + "/" + name, CID + ":" + WORK + "/" + name], timeout=15
                    )
                    if result["returncode"]:
                        raise ValueError("Copia de datos propia falló.")
                result = docker.command(
                    ["cp", CONTROL + "/profile.json", CID + ":" + WORK + "/profile.json"], timeout=5
                )
                if result["returncode"]:
                    raise ValueError("Copia del perfil falló.")
            logs = io.BytesIO()
            docker.heartbeat_fd = request.get("heartbeat_fd")
            expected_exec = {
                WORK + "/recipe.sh": profile["sources"]["recipe"],
                WORK + "/verifier.py": profile["sources"]["remote"],
                WORK + "/profile.json": request["profile_sha256"],
            }
            check_exec_hashes(
                docker.capture(["exec", CID, "/usr/bin/sha256sum", *expected_exec], timeout=5),
                expected_exec,
            )
            result = docker.command(
                exec_arguments(mode, request["profile_sha256"], profile["sources"]["remote"]),
                timeout=max(1, BUDGETS[mode] - 20),
                sink=logs,
            )
            raw_log = logs.getvalue()
            normal.write_file(control, mode + ".log", raw_log)
            if result["returncode"] != 0:
                raise ValueError("Etapa SDK rechazada; log propio conservado.")
            # La receta emite exactamente un JSON del verificador, no el log Cargo.
            value = normal.json_object(raw_log)
            if (
                value.get("success") is not True
                or value.get("stage") != mode
                or value.get("profile_sha256") != request["profile_sha256"]
            ):
                raise ValueError("La salida SDK no está correlacionada al modo/perfil.")
            return {
                "artifact": value,
                "log_sha256": digest(raw_log),
                "process": {k: v for k, v in result.items() if k != "output"},
                "container": observed,
                "limits": limits,
                "sdk_executed": mode in {"graph", "regression", "build"},
            }
    except BaseException:
        if docker is not None and owned_epoch and mode != "stop":
            # Se suspende la alarma de operación para disponer de la reserva de limpieza.
            signal.alarm(0)
            try:
                cleanup = normal.stop_owned(docker, CID)
            except BaseException:
                cleanup = {"cid": CID, "stopped": False}
            try:
                normal.write_file(
                    control,
                    mode + ".failure.json",
                    canonical(
                        {
                            "success": False,
                            "cleanup": cleanup,
                            "profile_sha256": request["profile_sha256"],
                        }
                    ),
                )
            except (OSError, ValueError):
                pass
        raise
    finally:
        os.close(control)


def apply(request, stream):
    mode = request.get("action")
    if mode not in MODES or validate_plan(request["profile"]) != request.get("profile_sha256"):
        raise ValueError("Petición ajena al plan fijado.")
    if request["profile"]["sources"]["remote"] != globals().get("_REBUILD_SOURCE_SHA256"):
        raise ValueError("El ejecutor remoto no coincide con el perfil.")
    saved = verified_saved(request)
    require_previous(mode, saved, reviewed=request.get("reviewed") == REVIEWED)
    sdk_approval = (
        validate_sdk_security(request.get("sdk_approval", {}), request["profile"])
        if mode in SDK_GATED_MODES
        else None
    )
    normal, compiler, augment, old, recovered = load_support(
        request["support"], stopped_only=mode == "stop"
    )
    if mode == "receive":
        fields = receive_data(request, stream, normal)
    elif mode == "prepare":
        if os.geteuid() != 0:
            raise ValueError("Promoción requiere UID0.")
        fields = prepare_control(request, normal, old, saved)
    else:
        if os.geteuid() != 0:
            raise ValueError("Operación requiere UID0.")
        fields = operating(request, normal, compiler, augment, old, recovered, saved)
    receipt = {
        "success": True,
        "owner": OWNER,
        "stage": mode,
        "profile_sha256": request["profile_sha256"],
        **fields,
        "sdk_security_approval": sdk_approval,
    }
    if mode not in {"receive"}:
        fd = None
        try:
            fd = control_open(normal)
            name = mode + ".json" if mode != "stop" else "stop-" + str(time.time_ns()) + ".json"
            normal.write_file(fd, name, canonical(receipt))
        except BaseException:
            if mode not in {"prepare", "stop"}:
                signal.alarm(0)
                docker = normal.Docker(CONTROL_BASE + "/docker-config")
                observed = normal.check_container(docker.inspect(CID), CID)
                check_birth(
                    observed, request["profile"]["original_container"], running=observed["running"]
                )
                normal.stop_owned(docker, CID)
            raise
        finally:
            if fd is not None:
                os.close(fd)
    return {"success": True, "result": envelope(receipt)}


NEW_CONFIG = (
    '[source.crates-io]\nreplace-with = "vendored-sources"\n'
    '[source.vendored-sources]\ndirectory = "' + WORK + '/vendor"\n'
    "[net]\noffline = true\n"
).encode()
SOURCE_TAR_SHA = "7cc81fe827f41e66dcca37dd4cb6dc171da9f6807ebf3d5892d72edda61fdf28"
SOURCE_PREFIX = "VectorChord-f9b5e234c44cee69b266b8431f92b7deed4e99d3/"
SOURCE_RECIPE_SHA = "c0518a09215262f9ab2c6daa1f75a35850abed946dfa3430bc745085f461d10f"


def source_members(archive, prefix):
    """Lee miembros como datos; nunca extrae permisos, propietarios ni enlaces del TAR."""
    if not prefix.endswith("/"):
        raise ValueError("Prefijo de fuente ambiguo.")
    root_name = safe_relative(prefix[:-1])
    result, seen, files, parents = {}, set(), set(), set()
    total, root_seen = 0, False
    for member in archive:
        name = member.name.rstrip("/")
        safe_relative(name)
        if len(name) > 4096 or not (member.isdir() or member.isreg()):
            raise ValueError("Tipo o nombre de miembro de fuente no permitido.")
        if name == root_name:
            if root_seen or not member.isdir():
                raise ValueError("Raíz de fuente duplicada o no directorio.")
            root_seen = True
            continue
        if not name.startswith(prefix):
            raise ValueError("Miembro fuera de la raíz de fuente fijada.")
        relative = safe_relative(name[len(prefix) :])
        ancestors = ["/".join(relative.split("/")[:i]) for i in range(1, len(relative.split("/")))]
        if relative in seen or any(parent in files for parent in ancestors):
            raise ValueError("Miembro duplicado o descendiente de un archivo.")
        if member.isreg() and relative in parents:
            raise ValueError("Archivo de fuente superpuesto a un directorio.")
        seen.add(relative)
        parents.update(ancestors)
        if len(seen) > 25000:
            raise ValueError("Inventario de fuente excesivo.")
        if member.isdir():
            continue
        if not 0 <= member.size <= 512 * 1024**2 or total + member.size > 2 * 1024**3:
            raise ValueError("Payload de fuente excesivo.")
        with archive.extractfile(member) as stream:
            raw = stream.read(member.size + 1)
        if len(raw) != member.size:
            raise ValueError("Miembro de fuente truncado.")
        total += len(raw)
        files.add(relative)
        result[relative] = (raw, 0o700 if member.mode & 0o111 else 0o600)
    if not root_seen or not result:
        raise ValueError("Raíz de fuente ausente o vacía.")
    return result


def source_archive_payloads(raw):
    if digest(raw) != SOURCE_TAR_SHA:
        raise ValueError("TAR original distinto del pin fijado.")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        return source_members(archive, SOURCE_PREFIX)


def derive_private_source(archive_raw, new_lock, recipe_raw, expected_old, old_config):
    """Deriva los overlays por bytes; no consulta /work/source para copiar o stat."""
    payload = source_archive_payloads(archive_raw)
    if not isinstance(new_lock, bytes) or not 0 < len(new_lock) <= 2 * 1024**2:
        raise ValueError("Cargo.lock derivado inválido o excesivo.")
    if ".cargo/config.toml" in payload or "crates/xtask/src/main.rs" not in payload:
        raise ValueError("Forma del TAR original distinta de la revisada.")
    recipe = module_bytes("frozen_source_recipe", recipe_raw, SOURCE_RECIPE_SHA)
    relative = "crates/xtask/src/main.rs"
    patched = recipe.frozen_xtask(payload[relative][0])
    if digest(patched) != XTASK_SHA:
        raise ValueError("Overlay xtask distinto del resultado fijado.")
    old_hashes = {name: digest(raw) for name, (raw, _) in payload.items()}
    old_hashes[relative] = XTASK_SHA
    old_hashes[".cargo/config.toml"] = digest(old_config)
    if old_hashes != expected_old:
        raise ValueError("El TAR y overlays no corresponden al árbol histórico acreditado.")
    payload[relative] = (patched, payload[relative][1])
    payload["Cargo.lock"] = (new_lock, 0o600)
    payload[".cargo/config.toml"] = (NEW_CONFIG, 0o600)
    return payload


def inside_read(path, maximum=512 * 1024**2):
    path = Path(path)
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Ancestro SDK inseguro.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise ValueError("Archivo SDK no regular o excesivo.")
        raw = stream.read(maximum + 1)
        if len(raw) != info.st_size:
            raise ValueError("Archivo SDK cambió de tamaño.")
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        ):
            raise ValueError("Archivo SDK cambió de identidad.")
    return raw


def inside_write(normal, root, relative, raw, mode=0o600):
    parent, leaf = normal.open_parent(root, safe_relative(relative), create=True)
    try:
        descriptor = os.open(
            leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def original_source(augment):
    with tarfile.open("/inputs/vectorchord-source.tar.gz", "r:gz") as archive:
        prefix = archive.getnames()[0].split("/")[0] + "/"
        expected = augment.tar_files(archive, prefix)
    expected["crates/xtask/src/main.rs"] = XTASK_SHA
    expected[".cargo/config.toml"] = digest(augment.CONFIG)
    augment.verify_tree("/work/source", expected)
    return expected


def sdk_proof(augment, compiler, old, recovered):
    # Usa verificadores congelados sin invocar sus funciones que escriben outputs.
    for name, meta in old["sdk_plan"]["files"].items():
        if digest(inside_read("/inputs/" + safe_relative(name))) != meta["sha256"]:
            raise ValueError("Input SDK histórico alterado.")
    lock = json.loads(inside_read("/inputs/native-sdk-lock.json"))
    database = digest(inside_read("/lib/apk/db/installed", 4 * 1024**2))
    if database != old["augmentation"]["restart"]["receipt"]["proof"]["apk_database_sha256"]:
        raise ValueError("La base de paquetes SDK cambió.")
    expected_sdk = {}
    for item in lock["rust_sdk"]:
        path = "/inputs/rust/" + item["component"] + ".tar.xz"
        with tarfile.open(path, "r:xz") as archive:
            prefix = archive.getnames()[0].split("/")[0] + "/"
            components = archive.extractfile(prefix + "components").read().decode().splitlines()
            if len(components) != 1:
                raise ValueError("Componente SDK ambiguo.")
            component = prefix + augment.safe_relative(components[0]) + "/"
            rows = archive.extractfile(component + "manifest.in").read().decode().splitlines()
            entries = augment.tar_files(archive, component)
            for row in rows:
                kind, name = row.split(":", 1)
                augment.safe_relative(name)
                if kind != "file" or (name in expected_sdk and expected_sdk[name] != entries[name]):
                    raise ValueError("Payload SDK no revisado o superpuesto.")
                expected_sdk[name] = entries[name]
                if digest(inside_read("/opt/agency-rust/" + name)) != entries[name]:
                    raise ValueError("SDK instalado distinto del payload firmado/fijado.")
    for path, pin in [
        (compiler.RUSTFMT, compiler.RUSTFMT_SHA),
        (compiler.DRIVER, compiler.DRIVER_SHA),
    ]:
        if digest(inside_read(path)) != pin:
            raise ValueError("Formatter/driver cambiaron.")
    old_runtime = recovered["artifact"]["runtime_files"]
    for name, meta in old_runtime.items():
        if (
            digest(inside_read("/outputs/compile-v3/runtime-root/" + safe_relative(name)))
            != meta["sha256"]
        ):
            raise ValueError("Un output histórico cambió.")
    reused = {
        "libsqlite3.so.0": digest(inside_read("/opt/agency-sqlite-v3/lib/libsqlite3.so.0")),
        "vector.so": digest(
            inside_read("/outputs/compile-v3/runtime-root/usr/local/lib/postgresql/vector.so")
        ),
    }
    check_reused(reused, {"libsqlite3.so.0": SQLITE_SHA, "vector.so": PGVECTOR_SHA})
    source = original_source(augment)
    with tarfile.open("/inputs/vendor.tar.gz", "r:gz") as archive:
        old_vendor = augment.tar_files(archive, "")
    augment.verify_tree("/work/vendor", old_vendor)
    return {
        "sdk_payload_sha256": digest(canonical(expected_sdk)),
        "sdk_files": len(expected_sdk),
        "apk_database_sha256": database,
        "old_runtime_sha256": digest(canonical(old_runtime)),
        "reused": reused,
        "original_source_sha256": digest(canonical(source)),
        "original_vendor_sha256": digest(canonical(old_vendor)),
    }


def inside_modules():
    profile_raw = inside_read(WORK + "/profile.json", 32768)
    if digest(profile_raw) != os.environ.get("AGENCY_REBUILD_PROFILE_SHA256"):
        raise ValueError("Perfil interno no coincide con el pin de operación.")
    profile = json.loads(profile_raw)
    validate_plan(profile)
    if digest(inside_read(__file__, 128 * 1024)) != os.environ.get(
        "AGENCY_REBUILD_VERIFIER_SHA256"
    ) or profile["sources"]["remote"] != os.environ.get("AGENCY_REBUILD_VERIFIER_SHA256"):
        raise ValueError("Verificador interno no acreditado.")
    info = os.lstat(WORK)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("Workspace nuevo no es root:0700.")
    payload = {
        name: base64.b64encode(inside_read(WORK + "/" + filename, 128 * 1024)).decode()
        for name, filename in [
            ("normal", "normal.py"),
            ("compiler", "compiler.py"),
            ("augment", "augment.py"),
            ("old_profile", "old-profile.json"),
            ("recover", "recover.json"),
        ]
    }
    normal, compiler, augment, old, recovered = load_support(payload)
    for name, meta in profile["files"].items():
        value = inside_read(WORK + "/" + safe_relative(name), 48 * 1024**2)
        if len(value) != meta["size"] or digest(value) != meta["sha256"]:
            raise ValueError("Una entrada del workspace nuevo cambió.")
    for directory in build_environment()["PATH"].split(":"):
        if Path(directory, "git").exists():
            raise ValueError("Hay ejecutable Git en PATH; no se ejecuta.")
    return profile, normal, compiler, augment, old, recovered


def materialize_inside(profile, normal, augment):
    expected = original_source(augment)
    payload = derive_private_source(
        inside_read("/inputs/vectorchord-source.tar.gz"),
        inside_read(WORK + "/Cargo.lock", 2 * 1024**2),
        inside_read("/inputs/offline_recipe.py", 128 * 1024),
        expected,
        augment.CONFIG,
    )
    root = normal.open_directory(WORK)
    descriptors = {}
    try:
        normal.write_file(
            root,
            ".owner.json",
            canonical(
                {
                    "owner": OWNER,
                    "profile_sha256": digest(canonical(profile)),
                    "directory": normal.identity(os.fstat(root)),
                }
            ),
        )
        for name in ("source", "vendor", "cargo-home", "home", "tmp", "target", "regression"):
            os.mkdir(name, 0o700, dir_fd=root)
            descriptors[name] = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
            )
        for relative, (raw, mode) in sorted(payload.items()):
            inside_write(normal, descriptors["source"], relative, raw, mode)
        inventory = json.loads(inside_read(WORK + "/vendor-lock.json", 4 * 1024**2))
        expected_vendor = {}
        for row in inventory["crates"]:
            prefix = row["name"] + "-" + row["version"]
            expected_vendor.update({prefix + "/" + f: pin for f, pin in row["files"].items()})
            expected_vendor[prefix + "/.cargo-checksum.json"] = digest(
                canonical({"files": row["files"], "package": row["archive_sha256"]})
            )
        if len(inventory["crates"]) != 244:
            raise ValueError("Vendor derivado incompleto.")
        observed, total = set(), 0
        with tarfile.open(WORK + "/vendor.tar.gz", "r:gz") as archive:
            for member in archive:
                name = safe_relative(member.name.rstrip("/"))
                if member.type == tarfile.DIRTYPE:
                    continue
                if (
                    member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                    or name in observed
                    or name not in expected_vendor
                    or member.size > 64 * 1024**2
                ):
                    raise ValueError("Vendor con miembro ajeno o no regular.")
                raw = archive.extractfile(member).read(64 * 1024**2 + 1)
                total += len(raw)
                if (
                    total > 2 * 1024**3
                    or len(raw) != member.size
                    or digest(raw) != expected_vendor[name]
                ):
                    raise ValueError("Vendor derivado alterado o excesivo.")
                observed.add(name)
                inside_write(
                    normal,
                    descriptors["vendor"],
                    name,
                    raw,
                    0o700 if member.mode & 0o111 else 0o600,
                )
        if observed != set(expected_vendor):
            raise ValueError("Faltan archivos vendor.")
        augment.verify_tree(WORK + "/vendor", expected_vendor)
        expected_new = {
            **expected,
            "Cargo.lock": profile["files"]["Cargo.lock"]["sha256"],
            ".cargo/config.toml": digest(NEW_CONFIG),
        }
        augment.verify_tree(WORK + "/source", expected_new)
        for name, raw in regression_inputs()[0].items():
            inside_write(normal, descriptors["regression"], name, raw)
        normal.write_file(
            descriptors["regression"],
            ".cargo-config-reference.json",
            canonical({"config_sha256": digest(NEW_CONFIG)}),
        )
        # El harness es un workspace separado, por eso necesita su propia config.
        inside_write(normal, descriptors["regression"], ".cargo/config.toml", NEW_CONFIG)
        return {
            "source_archive_sha256": SOURCE_TAR_SHA,
            "source_recipe_sha256": SOURCE_RECIPE_SHA,
            "source_materialization": "pinned_tar_and_exact_overlays",
            "historical_source_copied_or_chmod": False,
            "source_files": len(expected_new),
            "vendor_files": len(expected_vendor),
            "vendor_crates": 244,
            "original_source_sha256": digest(canonical(expected)),
            "source_sha256": digest(canonical(expected_new)),
            "vendor_sha256": digest(canonical(expected_vendor)),
            "sdk_executed": False,
        }
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)
        os.close(root)


def cargo_step(normal, arguments, cwd, deadline, filename):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("No queda presupuesto SDK.")
    # Se retiene/restaura cwd por descriptor sin cambiar la API del recolector congelado.
    arguments = ["/opt/agency-rust/bin/cargo", *arguments]
    if cwd not in {WORK + "/source", WORK + "/regression"}:
        raise ValueError("Directorio Cargo no permitido.")
    previous = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    target = normal.open_directory(cwd)
    try:
        os.fchdir(target)
        result = normal.run_bounded(arguments, remaining, environment=build_environment())
    finally:
        os.fchdir(previous)
        os.close(target)
        os.close(previous)
    raw = result["output"]
    if len(raw) > 4 * 1024**2:
        raise ValueError("Log Cargo excesivo.")
    fd = normal.open_directory(WORK)
    try:
        normal.write_file(fd, filename, raw)
    finally:
        os.close(fd)
    if result["returncode"]:
        raise ValueError("Cargo rechazó el comando cerrado; evidencia conservada.")
    return raw, result["process"]


def graph_inside(profile, normal, compiler, old, deadline):
    lock = tomllib.loads(inside_read(WORK + "/source/Cargo.lock", 2 * 1024**2).decode())
    allowed = {(row["name"], row["version"]) for row in lock["package"]}
    graphs, processes = {}, []
    for package in ("vchord", "xtask"):
        raw, process = cargo_step(
            normal,
            [
                "tree",
                "--manifest-path",
                WORK + "/source/Cargo.toml",
                "--frozen",
                "--offline",
                "--target",
                "x86_64-unknown-linux-musl",
                "--edges",
                "normal,build",
                "--prefix",
                "none",
                "--format",
                "{p}|{f}",
                "-p",
                package,
            ],
            WORK + "/source",
            deadline,
            "graph-" + package + ".log",
        )
        graphs[package] = parse_features(raw.decode("utf-8", "strict").rstrip("\n"), allowed)
        processes.append(process)
    trusted = json.loads(base64.b64decode(old["feature_wrapper_b64"], validate=True))["features"]
    # Se adapta únicamente el baseline antiguo autenticado, nunca el input real.
    trusted = trusted.replace("(/work/source)", "(" + WORK + "/source)").replace(
        "(/work/source/", "(" + WORK + "/source/"
    )
    trusted = trusted.replace("anyhow v1.0.101|", "anyhow v1.0.103|").replace(
        "crossbeam-epoch v0.9.18|", "crossbeam-epoch v0.9.20|"
    )
    expected = parse_features(trusted.rstrip("\n"), allowed)
    if graphs["vchord"] != expected:
        raise ValueError("El grafo efectivo cambia algo además de las dos versiones aprobadas.")
    expected_xtask = {
        tuple([name, {"anyhow": "1.0.103", "crossbeam-epoch": "0.9.20"}.get(name, version)])
        for name, version in old["backed"]["graph"]["receipt"]["artifact"]["trees"]["xtask"]
    }
    if set(graphs["xtask"]) != expected_xtask:
        raise ValueError("El grafo xtask cambia paquetes no aprobados.")
    for key, features in [
        (("vchord", "0.0.0"), {"default", "pg16"}),
        (("simd", "0.0.0"), {"init"}),
        (("pgrx", "0.17.0"), {"cshim", "pg16"}),
    ]:
        if not features <= graphs["vchord"].get(key, set()):
            raise ValueError("Faltan features PG16/SIMD obligatorias.")
    return {
        "expected_graph_match": True,
        "selected_registry": len([key for key in graphs["vchord"] if key[1] != "0.0.0"]),
        "features_sha256": digest(
            canonical(
                {
                    name + "@" + version: sorted(features)
                    for (name, version), features in graphs["vchord"].items()
                }
            )
        ),
        "processes": processes,
        "sdk_executed": True,
    }


def catalog_inside(normal, compiler, augment, recovered):
    runtime = OUTPUT + "/runtime-root"
    os.mkdir(OUTPUT, 0o700)
    os.mkdir(runtime, 0o700)
    root = normal.open_directory(runtime)
    try:
        old_files = recovered["artifact"]["runtime_files"]
        generated = augment.tree(WORK + "/vchord-build")
        expected_names = {"pkglibdir/vchord.so"} | {
            "sharedir/extension/" + PurePosixPath(name).name
            for name in old_files
            if name.startswith("usr/local/share/postgresql/extension/vchord")
        }
        if set(generated) != expected_names:
            raise ValueError("El build contiene archivos generados no declarados.")
        for name, meta in sorted(old_files.items()):
            if name == "usr/local/lib/postgresql/vchord.so":
                continue
            raw = inside_read("/outputs/compile-v3/runtime-root/" + safe_relative(name))
            if digest(raw) != meta["sha256"]:
                raise ValueError("Biblioteca/SQL/licencia reutilizada cambió.")
            inside_write(normal, root, name, raw)
        vchord = inside_read(WORK + "/vchord-build/pkglibdir/vchord.so", 64 * 1024**2)
        sqlite = inside_read("/opt/agency-sqlite-v3/lib/libsqlite3.so.0", 4 * 1024**2)
        proof = compiler.sqlite_link_proof(vchord, sqlite)
        # Rechaza cambios de SQL/control generados, sin ejecutarlos.
        for name, meta in old_files.items():
            if name.startswith("usr/local/share/postgresql/extension/vchord"):
                raw = inside_read(
                    WORK + "/vchord-build/sharedir/extension/" + PurePosixPath(name).name,
                    1024 * 1024,
                )
                if digest(raw) != meta["sha256"]:
                    raise ValueError("SQL/control VectorChord requiere revisión adicional.")
        inside_write(normal, root, "usr/local/lib/postgresql/vchord.so", vchord)
        result = {
            name: {"sha256": meta["sha256"], "size": meta["size"]}
            for name, meta in old_files.items()
        }
        result["usr/local/lib/postgresql/vchord.so"] = {
            "sha256": digest(vchord),
            "size": len(vchord),
        }
        return {
            "files": result,
            "sqlite_link_proof": proof,
            "reused_sqlite_sha256": SQLITE_SHA,
            "reused_pgvector_sha256": PGVECTOR_SHA,
            "sdk_executed": False,
            "sql_executed": False,
            "security_clean": False,
        }
    finally:
        os.close(root)


def inside(mode):
    if (
        mode not in {"materialize", "graph", "regression", "build", "catalog"}
        or os.name != "posix"
        or os.geteuid() != 0
        or not Path("/.dockerenv").is_file()
    ):
        raise ValueError("Modo interno o contenedor inválidos.")
    started = time.monotonic()
    profile, normal, compiler, augment, old, recovered = inside_modules()
    proof = sdk_proof(augment, compiler, old, recovered)
    deadline = started + BUDGETS[mode] - 40
    if mode == "materialize":
        value = materialize_inside(profile, normal, augment)
    else:
        source = original_source(augment)
        source.update(
            {
                "Cargo.lock": profile["files"]["Cargo.lock"]["sha256"],
                ".cargo/config.toml": digest(NEW_CONFIG),
            }
        )
        augment.verify_tree(WORK + "/source", source)
        if mode == "graph":
            value = graph_inside(profile, normal, compiler, old, deadline)
        elif mode == "regression":
            expected, scope = regression_inputs()
            for name, raw in expected.items():
                if inside_read(WORK + "/regression/" + name) != raw:
                    raise ValueError("El harness corregido cambió.")
            raw, process = cargo_step(
                normal,
                [
                    "test",
                    "--manifest-path",
                    WORK + "/regression/Cargo.toml",
                    "--frozen",
                    "--offline",
                    "--target",
                    "x86_64-unknown-linux-musl",
                    "--lib",
                    "--",
                    "--test-threads=1",
                ],
                WORK + "/regression",
                deadline,
                "regression.log",
            )
            check_regression_output(raw)
            value = {
                "functional_passed": True,
                "tests_passed": 2,
                "anyhow_ub_miri_proven": False,
                "scope": scope["scope"],
                "log_sha256": digest(raw),
                "process": process,
                "sdk_executed": True,
            }
        elif mode == "build":
            raw, process = cargo_step(
                normal,
                [
                    "run",
                    "--manifest-path",
                    WORK + "/source/Cargo.toml",
                    "--frozen",
                    "--offline",
                    "-p",
                    "xtask",
                    "--",
                    "build",
                    "--target",
                    "x86_64-unknown-linux-musl",
                    "--profile",
                    "release",
                    "--output",
                    WORK + "/vchord-build",
                ],
                WORK + "/source",
                deadline,
                "build.log",
            )
            value = {
                "build_passed": True,
                "log_sha256": digest(raw),
                "process": process,
                "sdk_executed": True,
            }
        else:
            value = catalog_inside(normal, compiler, augment, recovered)
        augment.verify_tree(WORK + "/source", source)
    after = sdk_proof(augment, compiler, old, recovered)
    if after != proof:
        raise ValueError("SDK, originales o bibliotecas reutilizadas cambiaron.")
    value.update(
        success=True, stage=mode, profile_sha256=digest(canonical(profile)), immutable_proof=proof
    )
    print(json.dumps(value, sort_keys=True), flush=True)


def main():
    if len(sys.argv) == 2 and sys.argv[1].startswith("inside-"):
        inside(sys.argv[1][7:])
        return 0
    request = {}
    try:
        if os.name != "posix" or sys.argv[1:]:
            raise ValueError("El ejecutor remoto sólo admite el protocolo cerrado POSIX.")
        os.umask(0o077)

        def interrupted(_signum, _frame):
            raise InterruptedError("Operación interrumpida.")

        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGALRM"):
            signal.signal(getattr(signal, name), interrupted)
        signal.alarm(60)
        started = time.monotonic()
        stream = sys.stdin.buffer.raw

        def read_exact(size):
            parts, left = [], size
            while left:
                raw = stream.read(left)
                if not raw:
                    raise ValueError("Frame truncado.")
                parts.append(raw)
                left -= len(raw)
            return b"".join(parts)

        length = struct.unpack(">I", read_exact(4))[0]
        if not 0 < length <= 1024 * 1024:
            raise ValueError("Frame excesivo.")
        request = json.loads(read_exact(length))
        mode = request.get("action")
        if mode not in MODES:
            raise ValueError("Modo no permitido.")
        available = (
            BUDGETS[mode]
            - (20 if mode not in {"receive", "prepare", "stop"} else 0)
            - (time.monotonic() - started)
        )
        signal.alarm(max(1, int(available)))
        request["heartbeat_fd"] = sys.stdin.fileno() if mode != "receive" else None
        result = apply(request, stream)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except BaseException as error:
        print(
            json.dumps(
                {
                    "success": False,
                    "error_type": type(error).__name__,
                    "owned_evidence_preserved": True,
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
