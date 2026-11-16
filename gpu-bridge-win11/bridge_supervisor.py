"""Supervisión acotada de procesos propios; no busca ni termina procesos por nombre."""

import asyncio
import hashlib
import http.client
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from bridge_common import HEALTH_BACKEND_TIMEOUT_SECONDS, BridgeConfig

HEALTH_PROBE_TIMEOUT_SECONDS = HEALTH_BACKEND_TIMEOUT_SECONDS + 1

WINDOWS_ENVIRONMENT = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "USERNAME",
        "USERDOMAIN",
        "HOMEDRIVE",
        "HOMEPATH",
        "SYSTEMDRIVE",
    }
)


def child_environment(parent: dict[str, str], gpu: dict[str, str]) -> dict[str, str]:
    result = {key: value for key, value in parent.items() if key.upper() in WINDOWS_ENVIRONMENT}
    result.update({key: value for key, value in gpu.items() if key.startswith("GPU_")})
    result.update(
        PYTHONUTF8="1",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        PYTHONNOUSERSITE="1",
    )
    return result


@dataclass
class RestartPolicy:
    """Tres sondas fallidas, gracia inicial y espera creciente entre reinicios propios."""

    started_at: float
    failures: int = 0
    restarts: int = 0
    next_restart_at: float = 0
    startup_grace_seconds: float = 180

    def observe(self, healthy: bool, *, alive: bool, now: float) -> bool:
        if healthy and alive:
            self.failures = 0
            # Tras cinco minutos estables puede volver a la espera inicial.
            if now - self.started_at >= 300:
                self.restarts = 0
            return False
        if now < self.next_restart_at:
            return False
        if not alive:
            return True
        if now - self.started_at < self.startup_grace_seconds:
            return False
        self.failures += 1
        return self.failures >= 3

    def restarted(self, *, now: float):
        self.restarts += 1
        self.started_at, self.failures = now, 0
        self.next_restart_at = now + min(300, 5 * 2 ** min(self.restarts, 6))


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    command: tuple[str, ...]
    cwd: Path
    port: int
    health_service: str
    capability: str


def parse_runtime_config(text):
    """JSON literal plano; no evalúa shell, sustituciones ni variables de otras credenciales."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("La configuración contiene una clave duplicada.")
            result[key] = value
        return result

    result = json.loads(text.removeprefix("\ufeff"), object_pairs_hook=unique)
    if (
        not isinstance(result, dict)
        or not result
        or any(
            not re.fullmatch(r"GPU_[A-Z0-9_]+", key)
            or not isinstance(value, str)
            or not value
            or any(char in value for char in "\r\n\0")
            for key, value in result.items()
        )
    ):
        raise ValueError("La configuración debe ser un objeto plano de variables GPU propias.")
    return result


def read_runtime_config(path):
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("La configuración privada no es un archivo acotado válido.")
    if any(parent.is_symlink() or parent.is_junction() for parent in (path, *path.parents)):
        raise ValueError("La configuración privada no admite enlaces ni reanálisis.")
    mapping = parse_runtime_config(path.read_text(encoding="utf-8-sig"))
    config = BridgeConfig.from_env(mapping)
    if (
        path != config.install_root / "runtime.json"
        or Path(mapping["GPU_RUNTIME_CONFIG_PATH"]) != path
    ):
        raise ValueError("La ruta de configuración no coincide con el ámbito instalado.")
    return mapping


def service_specs(mapping):
    root = Path(mapping["GPU_INSTALL_ROOT"])
    services, renderer = root / "services", root / "render-worker"
    if (
        not root.is_absolute()
        or mapping["GPU_BRIDGE_BIND_ADDRESS"] != "0.0.0.0"
        or Path(mapping["GPU_SERVICES_ROOT"]) != services
        or Path(mapping["GPU_RENDER_WORKER_ROOT"]) != renderer
        or Path(mapping["GPU_LOG_ROOT"]) != root / "logs"
    ):
        raise ValueError("Las rutas o dirección de escucha difieren del contrato propio.")

    def regular(path):
        if not path.is_file() or any(
            part.is_symlink() or part.is_junction() for part in (path, *path.parents)
        ):
            raise ValueError("Falta un archivo propio regular de lanzamiento.")

    launcher = services / "launch_service.py"
    regular(launcher)
    specs = []
    for name, port, identity in (("immich", 3003, "immich-ml"), ("whisper", 8000, "whisper")):
        executable = root / name / "venv" / "Scripts" / "python.exe"
        if Path(mapping[f"GPU_{name.upper()}_PYTHON_PATH"]) != executable:
            raise ValueError("El intérprete no pertenece al entorno aislado del servicio.")
        regular(executable)
        specs.append(
            ServiceSpec(
                name,
                (str(executable), "-I", str(launcher), name),
                services,
                port,
                identity,
                "cuda_available",
            )
        )
    node = Path(mapping["GPU_NODE_PATH"])
    if not node.is_absolute():
        raise ValueError("Node requiere una ruta absoluta respaldada.")
    regular(node)
    with node.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != mapping["GPU_NODE_SHA256"]:
        raise ValueError("La huella de Node no coincide con el respaldo.")
    entry = renderer / "src" / "start.ts"
    regular(entry)
    specs.append(
        ServiceSpec(
            "render",
            (str(node), "--experimental-strip-types", str(entry)),
            renderer,
            8090,
            "remotion",
            "nvenc_available",
        )
    )
    return specs


def health_probe(spec, token, *, timeout=HEALTH_PROBE_TIMEOUT_SECONDS):
    """HTTP loopback sin proxy/redirecciones; el plazo cierra únicamente su socket propio."""
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection("127.0.0.1", spec.port, timeout=timeout)
    timer, response = None, None
    try:
        connection.connect()
        owned_socket = connection.sock

        def expire():
            try:
                owned_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        connection.request(
            "GET", "/health", headers={"Authorization": f"Bearer {token}", "Connection": "close"}
        )
        response = connection.getresponse()
        if response.status != 200 or "application/json" not in response.getheader(
            "Content-Type", ""
        ):
            return False
        body = response.read(65537)
        if len(body) > 65536 or time.monotonic() > deadline:
            return False
        result = json.loads(body.decode("utf-8"))
        return (
            isinstance(result, dict)
            and result.get("service") == spec.health_service
            and result.get("ready") is True
            and result.get("status") == "ok"
            and isinstance(result.get("version"), str)
            and bool(result["version"])
            and result.get(spec.capability) is True
        )
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        if timer:
            timer.cancel()
        if response:
            response.close()
        connection.close()


class BridgeSupervisor:
    """Mantiene tres handles propios; los fallos nunca se resuelven buscando procesos ajenos."""

    def __init__(
        self,
        specs,
        mapping,
        logs,
        *,
        process_factory=None,
        probe=health_probe,
        clock=time.monotonic,
        interval_seconds=10,
    ):
        self.specs, self.mapping, self.logs = tuple(specs), mapping, Path(logs)
        self.factory = process_factory or OwnedProcess.start
        self.probe, self.clock, self.interval = probe, clock, interval_seconds
        self.children, self.policies, self.healthy, self.restarts = {}, {}, {}, {}
        self.task = None
        self.running = False
        self.stop_event = asyncio.Event()

    async def _spawn(self, spec, environment):
        pending = asyncio.create_task(asyncio.to_thread(self.factory, spec, environment, self.logs))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            child = await pending
            await asyncio.to_thread(child.stop)
            raise

    def snapshot(self):
        children = {
            spec.name: {
                "alive": bool(self.children.get(spec.name) and self.children[spec.name].is_alive()),
                "healthy": self.healthy.get(spec.name, False),
                "restarts": self.restarts.get(spec.name, 0),
            }
            for spec in self.specs
        }
        return {
            "healthy": self.running
            and bool(children)
            and all(child["alive"] and child["healthy"] for child in children.values()),
            "children": children,
        }

    async def start(self):
        if self.running:
            return
        self.stop_event.clear()
        try:
            environment = child_environment(dict(os.environ), self.mapping)
            for spec in self.specs:
                self.children[spec.name] = await self._spawn(spec, environment)
                self.policies[spec.name] = RestartPolicy(self.clock())
                self.healthy[spec.name], self.restarts[spec.name] = False, 0
            self.running = True
            self.task = asyncio.create_task(self._monitor())
        except BaseException as error:
            await self.stop()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise RuntimeError("No se pudo iniciar un proceso propio del puente.") from None

    async def _monitor(self):
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.interval)
            except TimeoutError:
                try:
                    await self.check_once()
                except Exception:
                    self.healthy.clear()

    async def check_once(self):
        async def inspect(spec):
            process = self.children.get(spec.name)
            alive = bool(process and process.is_alive())
            healthy = (
                await asyncio.to_thread(
                    self.probe,
                    spec,
                    self.mapping["GPU_BRIDGE_BEARER_TOKEN"],
                    timeout=HEALTH_PROBE_TIMEOUT_SECONDS,
                )
                if alive
                else False
            )
            return spec, alive, healthy

        for spec, alive, healthy in await asyncio.gather(*(inspect(spec) for spec in self.specs)):
            self.healthy[spec.name] = healthy and alive
            policy = self.policies[spec.name]
            if not policy.observe(healthy, alive=alive, now=self.clock()):
                continue
            if not self.running:
                continue
            process = self.children.get(spec.name)
            if process:
                await asyncio.to_thread(process.stop)
            self.children.pop(spec.name, None)
            policy.restarted(now=self.clock())
            self.restarts[spec.name] += 1
            self.healthy[spec.name] = False
            try:
                environment = child_environment(dict(os.environ), self.mapping)
                if self.running:
                    self.children[spec.name] = await self._spawn(spec, environment)
            except Exception:
                # El siguiente intento respeta el backoff; no incluye argumentos ni credenciales.
                self.healthy[spec.name] = False

    async def stop(self):
        self.running = False
        self.stop_event.set()
        if self.task:
            await self.task
            self.task = None
        owned, self.children = tuple(self.children.values()), {}
        results = await asyncio.gather(
            *(asyncio.to_thread(child.stop) for child in owned), return_exceptions=True
        )
        self.healthy.clear()
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError("No se confirmó el cierre de todos los handles propios.")


def windows_job_class():
    """Reutiliza la implementación SRE congelada y comprueba sus bytes antes de importarla."""
    source = Path(__file__).with_name("sre_control.py")
    if not source.exists():
        source = Path(__file__).resolve().parents[1] / "sre" / "sre_control.py"
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest not in {
        "ed016787f6d62b3cf80060bd965cf672ad46093c3068610571ea06f1e97b51c2",
        "1d7e02957106b12bf01071a3dc82ffe613efe744bfbc612a644dd5b2892525c3",
        "bfaba8c35d8a85829750b9173107865191e040eaac0032caa3b4c2af3ac41044",
    }:
        raise ValueError("La contención SRE no coincide con la fuente congelada.")
    spec = importlib.util.spec_from_file_location("_bridge_sre_control", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._WindowsJob


class OwnedProcess:
    """Un Job contiene el árbol antes de reanudarlo; cerrar el handle elimina sus descendientes."""

    def __init__(self, process, job, log):
        self.process, self.job, self.log = process, job, log

    @classmethod
    def start(cls, spec, environment, logs, *, job_factory=None):
        if os.name != "nt":
            raise OSError("La contención de producción requiere Windows.")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", spec.name):
            raise ValueError("La identidad del proceso propio no es válida.")
        job = (job_factory or windows_job_class())()
        process, log = None, None
        try:
            logs = Path(logs)
            if not logs.is_dir() or logs.is_symlink() or logs.is_junction():
                raise ValueError("El directorio privado de registros no es válido.")
            log_path = logs / f"{spec.name}.log"
            if log_path.is_symlink() or log_path.is_junction():
                raise ValueError("El registro no admite enlaces ni puntos de reanálisis.")
            log = log_path.open("ab", buffering=0)
            process = subprocess.Popen(
                spec.command,
                cwd=spec.cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                creationflags=0x4
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW,
            )
            job.assign_and_resume(process)
            return cls(process, job, log)
        except BaseException:
            try:
                job.close()
            finally:
                try:
                    if process:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=3)
                finally:
                    if log:
                        log.close()
            raise

    def is_alive(self):
        return self.process.poll() is None

    def stop(self):
        try:
            self.job.close()
        finally:
            try:
                self.process.wait(timeout=3)
            finally:
                self.log.close()


def create_app(mapping=None):
    from metrics_service import create_app as metrics_app

    mapping = (
        read_runtime_config(os.environ["GPU_RUNTIME_CONFIG_PATH"]) if mapping is None else mapping
    )
    BridgeConfig.from_env(mapping)
    manager = BridgeSupervisor(service_specs(mapping), mapping, Path(mapping["GPU_LOG_ROOT"]))
    return metrics_app(mapping, supervisor=manager)
