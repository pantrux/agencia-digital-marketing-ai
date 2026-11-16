"""Constructor V2 para R2; sólo fuentes fijadas, sin contenedores de aplicación."""

# Copia explícita del helper V1 697aca9306045238; algoritmos POSIX conservados.

import argparse
import base64
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

OWNER = "agencia-digital-marketing-ai:component-build:v2"
LAYOUT_OWNER = "agencia-digital-marketing-ai:media-platform:v4.1"
SOURCE_FILES = (
    "r2_client.py",
    "sync_http.py",
    "sync_ledger.py",
    "sync_protocol.py",
    "sync_runtime.py",
    "sync_storage.py",
)
FILES = {"r2-sync-consumer": ("Dockerfile", *SOURCE_FILES)}
BASE = "python:3.12.16-alpine3.24@sha256:213242b537a1c166a0c53e903461fc18d5518e7196c93dbecda452660a848918"
ROOTS = {"/opt/media-platform": "code", "/var/lib/media-platform": "state"}
CONTEXT_ROOT = "/opt/media-platform/build-v2/contexts"
LOGS_ROOT = "/var/lib/media-platform/build-v2"
MAX_CONTEXT = 256 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
SHA = re.compile(r"[a-f0-9]{64}")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
NO_GIT = {
    "BUILDX_GIT_INFO": "false",
    "BUILDX_GIT_LABELS": "false",
    "BUILDX_GIT_CHECK_DIRTY": "false",
    "BUILDX_NO_DEFAULT_ATTESTATIONS": "true",
    "BUILDX_METADATA_PROVENANCE": "disabled",
    "DOCKER_BUILDKIT": "1",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def plan_hash(plan):
    return digest(canonical({key: value for key, value in plan.items() if key != "plan_sha256"}))


def source_manifest(files):
    sources = {name: files[name]["sha256"] for name in SOURCE_FILES}
    return {"schema_version": 1, "files": sources, "root_sha256": digest(canonical(sources))}


def paths(component, entry):
    context = f"{CONTEXT_ROOT}/{component}/{entry['build_id']}"
    record = f"{LOGS_ROOT}/{component}/{entry['build_id']}"
    return {
        "context_path": context,
        "record_path": record,
        "log_path": record + "/build.log",
        "iid_path": record + "/image.iid",
    }


def validate_entry(component, entry):
    if (
        component not in FILES
        or not isinstance(entry, dict)
        or set(entry) != {"build_id", "tag", "base", "files", "context_root", "logs_root"}
    ):
        raise ValueError("Componente o esquema no autorizado.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{5,63}", entry["build_id"]):
        raise ValueError("Identificador de build no permitido.")
    if entry["tag"] != f"media-platform/{component}:{entry['build_id']}":
        raise ValueError("El tag no pertenece al componente y build respaldados.")
    if entry["base"] != BASE:
        raise ValueError("La base requiere repositorio oficial y digest exacto.")
    if entry["context_root"] != CONTEXT_ROOT or entry["logs_root"] != LOGS_ROOT:
        raise ValueError("Raíces de build fuera del contrato.")
    if (
        not isinstance(entry["files"], dict)
        or set(entry["files"]) != set(FILES[component])
        or any(
            not isinstance(value, str) or not SHA.fullmatch(value)
            for value in entry["files"].values()
        )
    ):
        raise ValueError("La whitelist de archivos no coincide con el componente.")


def validate_layout(value):
    if (
        not isinstance(value, dict)
        or value.get("owner") != LAYOUT_OWNER
        or value.get("success") is not True
    ):
        raise ValueError("Falta el recibo de layout respaldado.")
    targets = value.get("targets")
    if not isinstance(targets, list) or len(targets) != 2:
        raise ValueError("Se requieren las dos identidades del layout.")
    if {row.get("path") for row in targets} != set(ROOTS):
        raise ValueError("Las identidades no pertenecen a las raíces fijas.")
    for row in targets:
        directory = row.get("directory")
        if (
            row.get("role") != ROOTS[row["path"]]
            or not isinstance(directory, dict)
            or set(directory) != {"device", "inode"}
            or any(type(number) is not int or number <= 0 for number in directory.values())
            or any(
                not SHA.fullmatch(str(row.get(key, "")))
                for key in ("marker_sha256", "receipt_sha256")
            )
        ):
            raise ValueError("Una identidad del layout no está completa.")


def validate_plan(plan):
    if type(plan) is not dict or set(plan) != {
        "schema_version",
        "owner",
        "component",
        "build_id",
        "tag",
        "base",
        "context_root",
        "logs_root",
        "context_path",
        "record_path",
        "log_path",
        "iid_path",
        "files",
        "source_manifest",
        "controller_sha256",
        "helper_sha256",
        "layout_helper_sha256",
        "layout",
        "target",
        "plan_sha256",
    }:
        raise ValueError("El plan V2 debe respetar su esquema cerrado.")
    component = plan.get("component")
    entry = {key: plan[key] for key in ("build_id", "tag", "base", "context_root", "logs_root")}
    entry["files"] = {name: value["sha256"] for name, value in plan["files"].items()}
    validate_entry(component, entry)
    validate_layout(plan["layout"])
    expected_target = {
        "hostname": "vm-media-01",
        "ip": "192.168.0.85",
        "port": 22,
        "user": "jandrade",
    }
    target = plan.get("target", {})
    if type(target) is not dict or set(target) != {*expected_target, "host_fingerprint"}:
        raise ValueError("El destino no respeta el esquema cerrado.")
    if any(target.get(key) != value for key, value in expected_target.items()):
        raise ValueError("El destino SSH no es el host autorizado.")
    if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", target.get("host_fingerprint", "")):
        raise ValueError("El plan requiere la huella SSH respaldada.")
    if (
        type(plan.get("schema_version")) is not int
        or plan["schema_version"] != 2
        or plan.get("owner") != OWNER
        or any(plan.get(key) != value for key, value in paths(component, entry).items())
        or any(
            not SHA.fullmatch(str(plan.get(key, "")))
            for key in ("controller_sha256", "helper_sha256", "layout_helper_sha256")
        )
    ):
        raise ValueError("El contrato o las rutas derivadas no coinciden.")
    if (
        any(
            set(item) != {"sha256", "size"} or type(item["size"]) is not int or item["size"] <= 0
            for item in plan["files"].values()
        )
        or sum(item["size"] for item in plan["files"].values()) > MAX_CONTEXT
    ):
        raise ValueError("El contexto supera su tamaño permitido.")
    if plan["source_manifest"] != source_manifest(plan["files"]):
        raise ValueError("El manifiesto de seis fuentes no coincide con el contexto.")
    if (
        not SHA.fullmatch(str(plan.get("plan_sha256", "")))
        or plan_hash(plan) != plan["plan_sha256"]
    ):
        raise ValueError("El plan fue alterado.")


def validate_payload(payload):
    if type(payload) is not dict or set(payload) != {
        "plan",
        "approved_plan_sha256",
        "files",
        "layout_source_b64",
    }:
        raise ValueError("El payload V2 debe respetar su esquema cerrado.")
    plan = payload["plan"]
    validate_plan(plan)
    if payload.get("approved_plan_sha256") != plan["plan_sha256"] or set(payload["files"]) != set(
        FILES[plan["component"]]
    ):
        raise ValueError("Falta aprobación o la whitelist de bytes fue alterada.")
    files = {}
    for name, encoded in payload["files"].items():
        data = base64.b64decode(encoded, validate=True)
        if (
            len(data) != plan["files"][name]["size"]
            or digest(data) != plan["files"][name]["sha256"]
        ):
            raise ValueError("Los bytes del contexto difieren del plan.")
        files[name] = data
    dockerfile = files["Dockerfile"].decode("utf-8")
    instructions = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    bases = [line.split(None, 1)[1] for line in instructions if line.upper().startswith("FROM ")]
    if (
        bases != [plan["base"]]
        or re.search(r"(?im)^\s*#\s*(?:syntax|escape|check)\s*=", dockerfile)
        or any("\\" in line for line in instructions)
    ):
        raise ValueError("No se admiten frontend externo ni sintaxis multilinea.")
    expected = [
        "FROM " + BASE,
        "COPY --chown=0:0 --chmod=0555 " + " ".join(SOURCE_FILES) + " /app/",
        "WORKDIR /app",
        "USER 65532:65532",
        'ENTRYPOINT ["python", "-I", "-B", "-S", "/app/sync_http.py", "serve"]',
        "CMD []",
    ]
    if instructions != expected:
        raise ValueError("Sólo se admite la receta cerrada de copia y metadatos sin ejecución.")
    layout_source = base64.b64decode(payload["layout_source_b64"], validate=True)
    if digest(layout_source) != plan["layout_helper_sha256"]:
        raise ValueError("El helper de propiedad difiere del pin respaldado.")
    return files, layout_source


def build_environment(record_path=None):
    result = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", **NO_GIT}
    if record_path:
        result.update(
            {
                "DOCKER_CONFIG": record_path + "/docker-config",
                "BUILDX_CONFIG": record_path + "/buildx-config",
            }
        )
    return result


def build_command(plan):
    return [
        "/usr/bin/docker",
        "buildx",
        "build",
        "--builder=default",
        "--platform=linux/amd64",
        "--network=none",
        "--provenance=false",
        "--load",
        "--progress=plain",
        "--tag",
        plan["tag"],
        "--iidfile",
        plan["iid_path"],
        "--file",
        str(PurePosixPath(plan["context_path"]) / "Dockerfile"),
        plan["context_path"],
    ]


def result_fields(plan):
    return {
        "success": True,
        "owner": OWNER,
        "component": plan["component"],
        "build_id": plan["build_id"],
        "plan_sha256": plan["plan_sha256"],
        "tag": plan["tag"],
        "base": plan["base"],
        "files": plan["files"],
        "source_manifest": plan["source_manifest"],
        "controller_sha256": plan["controller_sha256"],
        "helper_sha256": plan["helper_sha256"],
        "layout_helper_sha256": plan["layout_helper_sha256"],
        "log_path": plan["log_path"],
        "receipt_path": plan["record_path"] + "/receipt.json",
        "containers_created": False,
        "final_sca_verified": False,
    }


def run_apply(payload, store, runner):
    plan = payload["plan"]
    files, _layout_source = validate_payload(payload)
    existing = store.existing()
    if existing is not None:
        runner.verify_versions()
        if runner.tag_ids(plan["tag"]) != [existing["image_id"]]:
            raise ValueError("El tag existente dejó de corresponder al recibo propio.")
        return {**existing, "changed": False}
    store.prepare(files)
    runner.verify_versions()
    if runner.tag_ids(plan["tag"]):
        raise ValueError("El tag ya existe sin recibo propio; no se sobrescribe.")
    if store.log_build(runner, build_command(plan)) != 0:
        raise ValueError("El build falló; revisar únicamente su log privado.")
    image_id = store.image_id()
    if not IMAGE_ID.fullmatch(image_id) or runner.tag_ids(plan["tag"]) != [image_id]:
        raise ValueError("El resultado no acredita un ID de imagen y tag coincidentes.")
    return store.complete({**result_fields(plan), "image_id": image_id, "changed": True})


def verify_layout(plan, layout):
    identity = layout.inspect_host()
    layout.host_contract(identity, apply=True)
    with layout.LocalFilesystem() as filesystem:
        current = layout.make_plan(filesystem.inspect(), identity)["inventory"]["targets"]
    for expected in plan["layout"]["targets"]:
        actual = current[expected["path"]]
        if (
            layout.directory_identity(actual["node"]) != expected["directory"]
            or layout.digest(actual["marker"]) != expected["marker_sha256"]
            or layout.digest(actual["receipt"]) != expected["receipt_sha256"]
        ):
            raise ValueError("Las raíces no corresponden a los IDs y recibos respaldados.")


def private(path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (
        not expected(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory and info.st_nlink != 1)
    ):
        raise ValueError("Un recurso de build no es privado y propio.")
    return {"device": info.st_dev, "inode": info.st_ino}


def read_private(path, maximum=65536):
    private(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if node_identity(os.fstat(stream.fileno())) != private(path):
            raise ValueError("El archivo cambió al abrirse.")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("El archivo supera el límite del build.")
    return data


def node_identity(info):
    return {"device": info.st_dev, "inode": info.st_ino}


def put_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchown(stream.fileno(), 0, 0)
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe_chain(path):
    for ancestor in (*reversed(path.parents), path):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ValueError(
                "Un ancestro del build es un enlace, ajeno o modificable por terceros."
            )


class BuildStore:
    """Contextos exactos y recibos privados; nunca elimina ni adopta recursos por su nombre."""

    marker_name = ".component-build-v2-owner.json"

    def __init__(self, plan):
        self.plan = plan
        self.context = Path(plan["context_path"])
        self.record = Path(plan["record_path"])
        self.common = [
            Path("/opt/media-platform/build-v2"),
            Path(CONTEXT_ROOT),
            Path(CONTEXT_ROOT) / plan["component"],
            Path(LOGS_ROOT),
            Path(LOGS_ROOT) / plan["component"],
        ]

    def owner(self, path):
        return {"owner": OWNER, "path": str(path), "directory": private(path, directory=True)}

    def check_common(self, path):
        safe_chain(path)
        if path.exists() and json.loads(read_private(path / self.marker_name)) != self.owner(path):
            raise ValueError("Un directorio común no tiene marcador propio exacto.")

    def make_common(self, path):
        self.check_common(path)
        if not path.exists():
            path.mkdir(mode=0o700)
            path.chmod(0o700)
            os.chown(path, 0, 0)
            put_private(path / self.marker_name, canonical(self.owner(path)))

    def record_owner(self):
        return {
            **self.owner(self.record),
            "plan_sha256": self.plan["plan_sha256"],
            "context": {
                "path": str(self.context),
                "directory": private(self.context, directory=True),
            },
        }

    def preflight(self):
        for path in self.common:
            self.check_common(path)
        safe_chain(self.context)
        safe_chain(self.record)
        if self.context.exists() != self.record.exists():
            raise ValueError(
                "El build tiene directorios parciales; requiere diagnóstico y otro build_id."
            )
        if self.record.exists():
            if json.loads(read_private(self.record / self.marker_name)) != self.record_owner():
                raise ValueError("El registro no acredita contexto, plan e inodos propios.")
            for name in ("docker-config", "buildx-config"):
                safe_chain(self.record / name)
                private(self.record / name, directory=True)
            if read_private(self.record / "docker-config/config.json") != b"{}":
                raise ValueError("La configuración Docker privada fue alterada.")
            if set(item.name for item in self.context.iterdir()) != set(
                FILES[self.plan["component"]]
            ):
                raise ValueError("El contexto contiene entradas ajenas a la whitelist.")
            for name, expected in self.plan["files"].items():
                if digest(read_private(self.context / name, MAX_CONTEXT)) != expected["sha256"]:
                    raise ValueError("Un archivo del contexto existente fue alterado.")

    def existing(self):
        self.preflight()
        if not self.record.exists():
            return None
        receipt_path = self.record / "receipt.json"
        if not receipt_path.exists():
            raise ValueError("Existe un build incompleto; no se repite automáticamente.")
        receipt = json.loads(read_private(receipt_path))
        if any(receipt.get(key) != value for key, value in result_fields(self.plan).items()):
            raise ValueError("El recibo no corresponde a este plan.")
        if receipt.get("image_id") != self.image_id() or receipt.get("log_sha256") != digest(
            read_private(Path(self.plan["log_path"]), MAX_OUTPUT)
        ):
            raise ValueError("La imagen registrada o el log fueron alterados.")
        return receipt

    def prepare(self, files):
        self.preflight()
        if self.context.exists() or self.record.exists():
            raise ValueError("No se reemplazan contextos existentes.")
        for path in self.common:
            self.make_common(path)
        self.context.mkdir(mode=0o700)
        self.record.mkdir(mode=0o700)
        for path in (self.context, self.record):
            path.chmod(0o700)
            os.chown(path, 0, 0)
        put_private(self.record / self.marker_name, canonical(self.record_owner()))
        for name, data in files.items():
            put_private(self.context / name, data)
        for name in ("docker-config", "buildx-config"):
            path = self.record / name
            path.mkdir(mode=0o700)
            path.chmod(0o700)
            os.chown(path, 0, 0)
        put_private(self.record / "docker-config/config.json", b"{}")

    def log_build(self, runner, command):
        descriptor = os.open(
            self.plan["log_path"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb", buffering=0) as stream:
            os.fchown(stream.fileno(), 0, 0)
            os.fchmod(stream.fileno(), 0o600)
            result = runner.build(command, stream)
            os.fsync(stream.fileno())
            return result

    def image_id(self):
        return read_private(Path(self.plan["iid_path"]), 128).decode("ascii").strip()

    def complete(self, result):
        self.preflight()
        result = {
            **result,
            "log_sha256": digest(read_private(Path(self.plan["log_path"]), MAX_OUTPUT)),
            "completed_at_utc": datetime.now(UTC).isoformat(),
        }
        put_private(self.record / "receipt.json", canonical(result))
        self.existing()
        return result


class DockerRunner:
    """Sólo metadatos y build local; comandos constantes y entorno mínimo sin credenciales."""

    def __init__(self, plan):
        self.environment = build_environment(plan["record_path"])

    def capture(self, arguments):
        response = subprocess.run(
            ["/usr/bin/docker", *arguments],
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
            check=True,
        )
        if len(response.stdout) > 65536 or len(response.stderr) > 65536:
            raise ValueError("La respuesta Docker supera el límite.")
        return response.stdout.decode("utf-8").strip()

    def verify_versions(self):
        if self.capture(["version", "--format", "{{.Server.Version}}"]) != "29.9.3":
            raise ValueError("La versión Docker no es la revisada.")
        if not re.search(r"\bv0\.38\.1\b", self.capture(["buildx", "version"])):
            raise ValueError("La versión Buildx no es la revisada.")
        inspection = self.capture(["buildx", "inspect", "default"])
        if re.findall(r"(?m)^Driver:\s+(\S+)\s*$", inspection) != ["docker"]:
            raise ValueError(
                "Se requiere el builder default integrado, sin crear un contenedor BuildKit."
            )

    def tag_ids(self, tag):
        output = self.capture(
            ["image", "ls", "--quiet", "--no-trunc", "--filter", "reference=" + tag]
        )
        ids = output.splitlines() if output else []
        if any(not IMAGE_ID.fullmatch(value) for value in ids) or len(ids) > 1:
            raise ValueError("Docker no devolvió un único ID verificable.")
        return ids

    def build(self, command, log):
        process = subprocess.Popen(
            command,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            umask=0o077,
        )
        total, deadline = 0, time.monotonic() + 600
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("El build agotó su presupuesto.")
                    for key, _ in selector.select(min(0.2, deadline - time.monotonic())):
                        block = os.read(key.fileobj.fileno(), 65536)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(block)
                        if total > MAX_OUTPUT:
                            raise ValueError("El log agotó su límite privado.")
                        log.write(block)
            # No recolectar al líder todavía: reserva su PID/PGID mientras se
            # cierra el grupo, incluso si un hijo heredó stdout y el líder salió.
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("El líder del build no terminó dentro del presupuesto.")
                time.sleep(0.025)
        finally:
            try:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                else:
                    time.sleep(0.1)
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=2)
            finally:
                process.stdout.close()
        return process.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("La entrada supera su límite.")
        payload = json.loads(raw)
        _files, source = validate_payload(payload)
        layout = types.ModuleType("component_layout_pinned")
        exec(compile(source, "<layout-fijado>", "exec"), layout.__dict__)
        verify_layout(payload["plan"], layout)
        if not arguments.apply:
            result = {
                "success": True,
                "mode": "plan",
                "remote_layout_verified": True,
                "plan_sha256": payload["plan"]["plan_sha256"],
                "mutated": False,
            }
        else:
            os.umask(0o077)
            result = run_apply(payload, BuildStore(payload["plan"]), DockerRunner(payload["plan"]))
        print(json.dumps(result))
        return 0
    except Exception:
        print(
            json.dumps({"success": False, "error": "BUILD_RECHAZADO", "final_sca_verified": False})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
