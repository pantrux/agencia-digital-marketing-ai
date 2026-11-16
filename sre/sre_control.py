"""Sondas paralelas y reversión verificada con presupuesto común inferior a 90 s."""

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

TOTAL_BUDGET_SECONDS = 85
# Se habilita en la Fase 5 tras descubrir y probar la activación del servicio real.
PRODUCTION_ROLLBACK_READY = False
AGENTS = [
    "agency-account",
    "agency-strategist",
    "agency-director",
    "agency-editor",
    "agency-critic",
]
MCPS = [
    "mcp-media-inspector",
    "mcp-immich-broll",
    "mcp-remotion-gpu",
    "mcp-kitsu-review",
]
PROBE_NAMES = [
    "edge",
    "consumer",
    "kitsu",
    "immich",
    "agents",
    "immich_ml",
    "whisper",
    "remotion",
    "gpu_metrics",
    "monitor",
]
HEX64 = re.compile(r"[a-f0-9]{64}\Z")
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")


PROMETHEUS_OTLP = "https://192.168.0.8:9090/api/v1/otlp/v1/metrics"


class Deadline:
    def __init__(self, seconds=TOTAL_BUDGET_SECONDS, expires=None):
        if not 0 < seconds < 90:
            raise ValueError("El presupuesto debe ser positivo e inferior a 90 segundos")
        self.expires = (
            min(time.monotonic() + seconds, expires) if expires else time.monotonic() + seconds
        )

    def remaining(self):
        return max(0.0, self.expires - time.monotonic())


def validate_body(contract, body):
    if contract == "monitor":
        return isinstance(body, str) and body.strip() == "Prometheus Server is Healthy."
    if not isinstance(body, dict):
        return False
    if contract == "immich":
        return body.get("res") == "pong"
    if contract == "kitsu":
        version = body.get("version")
        return (
            isinstance(version, str)
            and bool(version)
            and body.get("database-up") is True
            and body.get("key-value-store-up") is True
        )
    if contract == "edge":
        return all(body.get(key) is True for key in ("ready", "d1", "kv", "r2"))
    if contract == "consumer":
        return body.get("ready") is True and body.get("busy") is False
    if contract == "gateway":
        return body.get("ready") is True and body.get("failing", []) == []
    if body.get("status") != "ok":
        return False
    if contract == "agents":
        agents, mcps = body.get("agents"), body.get("mcps")
        return (
            isinstance(agents, list)
            and isinstance(mcps, list)
            and all(item in agents for item in AGENTS)
            and all(item in mcps for item in MCPS)
        )
    if contract == "cuda":
        return body.get("cuda_available") is True
    if contract == "nvenc":
        return body.get("nvenc_available") is True
    return contract == "status"


def default_probes():
    rows = [
        (
            "edge",
            "https://studio.e-scale.cl/api/healthz",
            "GET",
            "edge",
            None,
        ),
        (
            "consumer",
            "http://192.168.0.85:3010/healthz",
            "GET",
            "consumer",
            None,
        ),
        ("kitsu", "http://192.168.0.85:8081/api/status", "GET", "kitsu", None),
        ("immich", "https://immich.e-scale.cl/api/server/ping", "GET", "immich", None),
        (
            "agents",
            "http://192.168.0.7:18789/readyz",
            "GET",
            "gateway",
            None,
        ),
        (
            "immich_ml",
            "http://192.168.0.6:3003/health",
            "GET",
            "cuda",
            "GPU_BRIDGE_BEARER_TOKEN",
        ),
        (
            "whisper",
            "http://192.168.0.6:8000/health",
            "GET",
            "cuda",
            "GPU_BRIDGE_BEARER_TOKEN",
        ),
        (
            "remotion",
            "http://192.168.0.6:8090/healthz",
            "GET",
            "nvenc",
            "GPU_BRIDGE_BEARER_TOKEN",
        ),
        (
            "gpu_metrics",
            "http://192.168.0.6:9835/health",
            "GET",
            "status",
            "GPU_BRIDGE_BEARER_TOKEN",
        ),
        ("monitor", "https://192.168.0.8:9090/-/healthy", "GET", "monitor", None),
    ]
    return [
        dict(zip(("name", "url", "method", "contract", "token_env"), row, strict=True))
        for row in rows
    ]


def probe_one(probe, deadline, environ):
    result = {"name": probe["name"], "ok": False}
    token_name = probe.get("token_env")
    token = environ.get(token_name, "") if token_name else ""
    if token_name and (not token or "CHANGE_ME" in token or "\n" in token or "\r" in token):
        return {**result, "reason": "credencial_ausente"}
    remaining = min(8, deadline.remaining())
    if remaining <= 0:
        return {**result, "reason": "presupuesto_agotado"}
    config = 'header = "Accept: application/json"\n'
    if token:
        escaped = token.replace("\\", "\\\\").replace('"', '\\"')
        config += f'header = "Authorization: Bearer {escaped}"\n'
    if probe["contract"] == "monitor":
        if probe["url"] != "https://192.168.0.8:9090/-/healthy":
            return {**result, "reason": "origen_mtls_invalido"}
        certificate_dir = Path(
            environ.get(
                "SRE_PROMETHEUS_MTLS_DIR",
                str(Path.home() / ".agency-prometheus-bridge-client-v1" / "valid"),
            )
        )
        if not certificate_dir.is_absolute() or any(ch in str(certificate_dir) for ch in '\\"\r\n'):
            return {**result, "reason": "ruta_mtls_invalida"}
        certificate_files = (
            certificate_dir / "server-ca.pem",
            certificate_dir / "client.pem",
            certificate_dir / "client.key",
        )
        if any(not item.is_file() or item.resolve() != item for item in certificate_files):
            return {**result, "reason": "credencial_mtls_ausente"}
        for option, item in zip(("cacert", "cert", "key"), certificate_files, strict=True):
            config += f'{option} = "{item.as_posix()}"\n'
    try:
        command = [
            shutil.which("curl.exe") or "curl",
            "--silent",
            "--show-error",
            "--config",
            "-",
            "--proto",
            "=http,https",
            "--connect-timeout",
            str(min(3, remaining)),
            "--max-time",
            str(remaining),
            "--max-filesize",
            "1048576",
            "--request",
            probe["method"],
            "--write-out",
            "\n%{http_code}",
            probe["url"],
        ]
        response = run_process(command, remaining, input_text=config, capture_output=True)
        if response is None or response[0]:
            return {**result, "reason": "error_transporte"}
        payload, code = response[1].rsplit("\n", 1)
        if code != "200":
            return {**result, "reason": "estado_http", "http_status": code}
        body = payload if probe["contract"] == "monitor" else json.loads(payload)
        return {
            **result,
            "ok": validate_body(probe["contract"], body),
            "reason": "contrato_verificado"
            if validate_body(probe["contract"], body)
            else "contrato_invalido",
        }
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return {**result, "reason": "respuesta_invalida_o_timeout"}


def run_probes(probes, deadline, environ):
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(probe_one, probe, deadline, environ) for probe in probes]
        return [future.result() for future in futures]


def verify_file(path, expected):
    file = Path(path)
    if (
        not file.is_absolute()
        or file.is_symlink()
        or not file.is_file()
        or not HEX64.fullmatch(expected)
    ):
        raise ValueError("Artefacto o huella inválidos")
    if file.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("El artefacto supera el límite de tamaño")
    if hashlib.sha256(file.read_bytes()).hexdigest() != expected:
        raise ValueError("La huella del artefacto no coincide")
    return file


def load_manifest(path, expected_hash):
    manifest_path = verify_file(str(Path(path).absolute()), expected_hash)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        set(data) != {"schema_version", "release_sha", "cloudflare", "compose", "openclaw"}
        or data["schema_version"] not in (1, 2)
        or not re.fullmatch(r"[a-f0-9]{40}", data["release_sha"])
    ):
        raise ValueError("Esquema de manifiesto inválido")
    cf, compose, agents = data["cloudflare"], data["compose"], data["openclaw"]
    if data["schema_version"] == 2:
        return validate_live_manifest(data)
    if (
        set(cf) != {"worker", "version_id"}
        or cf["worker"] != "studio-api-worker"
        or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", cf["version_id"])
    ):
        raise ValueError("Identidad de Worker inválida")
    if set(compose) != {"path", "sha256"} or set(agents) != {
        "path",
        "sha256",
        "current_sha256",
        "ssh_user",
    }:
        raise ValueError("Esquema de artefactos inválido")
    compose_file = verify_file(compose["path"], compose["sha256"])
    agents_file = verify_file(agents["path"], agents["sha256"])
    compose_text = compose_file.read_text(encoding="utf-8")
    if re.search(r"^\s*(?:build|include|extends)\s*:", compose_text, flags=re.MULTILINE):
        raise ValueError("La reversión no puede reconstruir ni incorporar archivos externos")
    images = re.findall(r"^\s+image:\s*['\"]?([^\s'\"]+)", compose_text, flags=re.MULTILINE)
    if not images or not all(
        re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image) for image in images
    ):
        raise ValueError("Cada imagen de reversión debe estar fijada por digest")
    config = json.loads(agents_file.read_text(encoding="utf-8"))
    entries = {item["id"]: item for item in config.get("agents", {}).get("list", [])}
    for name, model in zip(AGENTS, ("terra", "sol", "sol", "terra", "sol"), strict=True):
        if entries.get(name, {}).get("model") != "openai/gpt-6.1-" + model:
            raise ValueError("El artefacto no contiene el equipo previo esperado")
    if not HEX64.fullmatch(agents["current_sha256"]) or not re.fullmatch(
        r"[a-z_][a-z0-9_-]{0,31}", agents["ssh_user"]
    ):
        raise ValueError("Identidad o huella OpenClaw inválida")
    return data


def validate_live_manifest(data):
    cf, compose, agents = data["cloudflare"], data["compose"], data["openclaw"]
    if (
        set(cf) != {"worker", "version_id", "current_version_id"}
        or cf["worker"] != "studio-api-worker"
    ):
        raise ValueError("Identidad del Worker inválida")
    if not all(UUID.fullmatch(cf[key]) for key in ("version_id", "current_version_id")):
        raise ValueError("Versiones del Worker inválidas")
    if set(compose) != {"path", "sha256", "project", "services"}:
        raise ValueError("Alcance Compose inválido")
    file = verify_file(compose["path"], compose["sha256"])
    if not file.is_relative_to(Path("/opt/media-platform")) or file.resolve() != file:
        raise ValueError("Ruta Compose inválida")
    if not re.fullmatch(r"agency-[a-z0-9][a-z0-9-]{1,63}", compose["project"]):
        raise ValueError("Proyecto Compose ajeno")
    services = compose["services"]
    if (
        type(services) is not list
        or not 1 <= len(services) <= 24
        or len(set(services)) != len(services)
    ):
        raise ValueError("Servicios Compose inválidos")
    if not all(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name) for name in services):
        raise ValueError("Nombre de servicio inválido")
    if set(agents) != {"path", "sha256", "ssh_user"} or not re.fullmatch(
        r"[a-z_][a-z0-9_-]{0,31}", agents["ssh_user"]
    ):
        raise ValueError("Alcance OpenClaw inválido")
    patch = json.loads(verify_file(agents["path"], agents["sha256"]).read_bytes())
    if set(patch) != {"expected", "previous"} or set(patch["expected"]) != set(patch["previous"]):
        raise ValueError("Proyección OpenClaw inválida")
    if not patch["expected"] or not set(patch["expected"]).issubset(AGENTS):
        raise ValueError("Agentes fuera de alcance")
    for values in patch.values():
        for key, name in values.items():
            if (
                type(name) is not str
                or not 1 <= len(name) <= 120
                or any(ord(ch) < 32 for ch in name)
            ):
                raise ValueError("Etiqueta de agente inválida: " + key)
    return data


class _WindowsJob:
    """Contiene el árbol antes de ejecutar código, incluidos los redirectores venv."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Limits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("ExitStatus", wintypes.DWORD),
                ("TebBaseAddress", ctypes.c_void_p),
                ("ProcessId", wintypes.DWORD),
                ("ThreadId", wintypes.DWORD),
                ("AffinityMask", ctypes.c_size_t),
                ("Priority", ctypes.c_int),
                ("BasePriority", ctypes.c_int),
                ("LastSyscallFirstArgument", ctypes.c_void_p),
                ("LastSyscallNumber", wintypes.WORD),
                ("CreateTime", wintypes.FILETIME),
                ("ExitTime", wintypes.FILETIME),
                ("KernelTime", wintypes.FILETIME),
                ("UserTime", wintypes.FILETIME),
                ("Win32StartAddress", ctypes.c_void_p),
                ("CaptureTime", wintypes.FILETIME),
                ("Flags", wintypes.DWORD),
                ("SuspendCount", wintypes.WORD),
                ("SizeOfContextRecord", wintypes.WORD),
                ("ContextRecord", ctypes.c_void_p),
            ]

        self.thread_entry = ThreadEntry
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "PssCaptureSnapshot": (
                [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
                wintypes.DWORD,
            ),
            "PssFreeSnapshot": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.DWORD),
            "PssWalkMarkerCreate": (
                [ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)],
                wintypes.DWORD,
            ),
            "PssWalkMarkerFree": ([wintypes.HANDLE], wintypes.DWORD),
            "PssWalkSnapshot": (
                [wintypes.HANDLE, ctypes.c_int, wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD],
                wintypes.DWORD,
            ),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetProcessIdOfThread": ([wintypes.HANDLE], wintypes.DWORD),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Limits()
        # KILL_ON_JOB_CLOSE, sin permitir que los descendientes abandonen el Job.
        # https://learn.microsoft.com/windows/win32/procthread/job-objects
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        # PSS_CAPTURE_THREADS consulta solo este HANDLE, sin recorrer miles de
        # hilos del host. No clona memoria ni crea otro proceso.
        # https://learn.microsoft.com/windows/win32/api/processsnapshot/nf-processsnapshot-psscapturesnapshot
        snapshot, marker = self.ctypes.c_void_p(), self.ctypes.c_void_p()
        result = self.kernel.PssCaptureSnapshot(
            int(process._handle), 0x80, 0, self.ctypes.byref(snapshot)
        )
        if result:
            raise self.ctypes.WinError(result)
        identifiers = []
        try:
            result = self.kernel.PssWalkMarkerCreate(None, self.ctypes.byref(marker))
            if result:
                raise self.ctypes.WinError(result)
            entry = self.thread_entry()
            while True:
                result = self.kernel.PssWalkSnapshot(
                    snapshot, 3, marker, self.ctypes.byref(entry), self.ctypes.sizeof(entry)
                )
                if result == 259:
                    break
                if result:
                    raise self.ctypes.WinError(result)
                if entry.ProcessId != process.pid:
                    raise OSError("La captura no pertenece al proceso propio")
                identifiers.append(entry.ThreadId)
        finally:
            try:
                if marker.value:
                    self.kernel.PssWalkMarkerFree(marker)
            finally:
                self.kernel.PssFreeSnapshot(self.kernel.GetCurrentProcess(), snapshot)
        if not identifiers:
            raise OSError("No se encontró el hilo del proceso propio suspendido")
        for identifier in identifiers:
            thread = self.kernel.OpenThread(0x2 | 0x800, False, identifier)
            if not thread:
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            try:
                # El handle vivo del proceso y esta comprobación evitan adoptar IDs reutilizados.
                if self.kernel.GetProcessIdOfThread(thread) != process.pid:
                    raise OSError("El hilo ya no pertenece al proceso propio")
                if self.kernel.ResumeThread(thread) == 0xFFFFFFFF:
                    raise self.ctypes.WinError(self.ctypes.get_last_error())
            finally:
                self.kernel.CloseHandle(thread)

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.kernel.CloseHandle(handle):
                raise self.ctypes.WinError(self.ctypes.get_last_error())


def _close_process_pipes(process):
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _stop_process(process, job, expires):
    if job is not None:
        job.close()
    elif os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.communicate(timeout=max(0, expires - time.monotonic()))
    except (subprocess.TimeoutExpired, OSError):
        # El árbol ya recibió terminación. Se conserva el Popen hasta recoger el
        # estado y cerrar los pipes, aunque el kernel demore en entregar el EOF.
        def reap():
            try:
                process.wait()
            finally:
                _close_process_pipes(process)

        threading.Thread(target=reap, daemon=True, name="sre-recoger-proceso").start()
        return False
    else:
        _close_process_pipes(process)
        return True


def run_process(args, timeout, input_text=None, capture_output=False, environ=None):
    """El presupuesto incluye arranque; la limpieza nativa termina el árbol propio."""
    if timeout <= 0:
        return None
    expires = time.monotonic() + timeout
    # La terminación es nativa; 50 ms de reserva no recortan segundos a las sondas.
    work_expires = expires - min(0.05, timeout / 10)
    job = _WindowsJob() if os.name == "nt" else None
    process = None
    owns_pipes = True
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environ,
            start_new_session=os.name == "posix",
            # CREATE_SUSPENDED cierra la carrera entre crear un hijo y asignar el Job.
            creationflags=0x4 | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            if job is not None
            else 0,
        )
        if job is not None:
            job.assign_and_resume(process)
        output, _error = process.communicate(
            input=input_text, timeout=max(0, work_expires - time.monotonic())
        )
        return process.returncode, output or ""
    except subprocess.TimeoutExpired:
        owns_pipes = _stop_process(process, job, expires)
        return None
    except BaseException:
        if process is not None:
            owns_pipes = _stop_process(process, job, expires)
        raise
    finally:
        if job is not None:
            job.close()
        if owns_pipes and process is not None and process.poll() is not None:
            _close_process_pipes(process)


def run_command(args, deadline, input_text=None, maximum=40):
    remaining = min(maximum, deadline.remaining())
    if remaining <= 0:
        return False
    try:
        environ = os.environ.copy()
        if args[0] == "wrangler":
            environ["CLOUDFLARE_API_TOKEN"] = environ.get("CF_API_TOKEN", "")
            environ["CLOUDFLARE_ACCOUNT_ID"] = environ.get("CF_ACCOUNT_ID", "")
            if not environ["CLOUDFLARE_API_TOKEN"] or not environ["CLOUDFLARE_ACCOUNT_ID"]:
                return False
        result = run_process(args, remaining, input_text=input_text, environ=environ)
        return result is not None and result[0] == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def rollback_component(component, manifest, deadline):
    if component == "cloudflare":
        cf = manifest["cloudflare"]
        # Sintaxis oficial: https://developers.cloudflare.com/workers/wrangler/commands/workers/#rollback
        return run_command(
            [
                "wrangler",
                "rollback",
                cf["version_id"],
                "--name",
                cf["worker"],
                "--message",
                "Reversión automática SRE",
            ],
            deadline,
        )
    if component == "compose":
        if not Path("/opt/media-platform").is_dir():
            return False
        return run_command(
            [
                "docker",
                "compose",
                "--project-directory",
                "/opt/media-platform",
                "--env-file",
                "/opt/media-platform/.env",
                "-f",
                manifest["compose"]["path"],
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "30",
                "--remove-orphans",
            ],
            deadline,
        )
    agents = manifest["openclaw"]
    target = agents["ssh_user"] + "@192.168.0.7"
    # La ruta remota es fija; el código compara antes de reemplazar para evitar pisar cambios concurrentes.
    remote = (
        "import hashlib,json,os,pathlib,sys; p=pathlib.Path.home()/'.openclaw/openclaw.json'; b=sys.stdin.buffer.read(); assert hashlib.sha256(p.read_bytes()).hexdigest()=='"
        + agents["current_sha256"]
        + "'; assert hashlib.sha256(b).hexdigest()=='"
        + agents["sha256"]
        + "'; json.loads(b); q=p.with_name('openclaw.rollback.tmp'); q.write_bytes(b); q.chmod(0o600); os.replace(q,p)"
    )
    import shlex

    return run_command(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=5",
            target,
            "python3 -c " + shlex.quote(remote),
        ],
        deadline,
        Path(agents["path"]).read_text(encoding="utf-8"),
    )


def active_cloudflare_version(deployments):
    active = deployments["deployments"][0]["versions"]
    return active[0]["version_id"] if len(active) == 1 and active[0]["percentage"] == 100 else None


class FixedOrigin(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def rollback_alert_payload(report, release, timestamp_ns):
    def label(key, value):
        return {"key": key, "value": {"stringValue": value}}

    failed_probes = sorted(
        {probe["name"] for probe in report["probes"] if not probe["ok"]} & set(PROBE_NAMES)
    )
    components = report["rollback_components"]
    failed_components = [
        name for name in ("cloudflare", "compose", "openclaw") if components.get(name) is not True
    ]
    point = {
        "timeUnixNano": str(timestamp_ns),
        "asInt": str(timestamp_ns // 1_000_000_000),
        "attributes": [
            label("entrega", release[:12]),
            label("sondas", ",".join(failed_probes) or "sin_detalle"),
            label("componentes", ",".join(failed_components) or "ninguno"),
            label("resultado", "restaurado" if report["ok"] else "pendiente"),
            label("modo", "ensayo" if "fault_injected" in report else "operacion"),
            label("duracion_segundos", str(report["elapsed_seconds"])),
        ],
    }
    metric = {"name": "agency_studio_rollback_timestamp_seconds", "gauge": {"dataPoints": [point]}}
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        label("service.name", "agency-studio-sre"),
                        label("service.instance.id", "vm-media-01"),
                    ]
                },
                "scopeMetrics": [{"scope": {"name": "studio.sre"}, "metrics": [metric]}],
            }
        ]
    }


def send_rollback_alert(report, release, environ):
    try:
        directory = Path(
            environ.get(
                "SRE_PROMETHEUS_MTLS_DIR",
                str(Path.home() / ".agency-prometheus-bridge-client-v1" / "valid"),
            )
        )
        ca, cert, key = (directory / f for f in ("server-ca.pem", "client.pem", "client.key"))
        if not directory.is_absolute() or any(
            not item.is_file() or item.resolve() != item for item in (ca, cert, key)
        ):
            return False
        tls = ssl.create_default_context(cafile=str(ca))
        tls.minimum_version = ssl.TLSVersion.TLSv1_3
        tls.load_cert_chain(str(cert), str(key))
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=tls), FixedOrigin())
        payload = rollback_alert_payload(report, release, time.time_ns())
        request = Request(
            PROMETHEUS_OTLP,
            data=json.dumps(payload, separators=(",", ":")).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with opener.open(request, timeout=3) as response:
            raw = response.read(16385)
            if response.status != 200 or len(raw) > 16384:
                return False
        partial = (json.loads(raw) if raw.strip() else {}).get("partialSuccess", {})
        return int(partial.get("rejectedDataPoints", 0)) == 0 and not partial.get("errorMessage")
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def rollback_cloudflare(settings, deadline):
    account = os.environ.get("CF_ACCOUNT_ID", "")
    token = os.environ.get("CF_PLATFORM_API_TOKEN", "")
    if not re.fullmatch(r"[0-9a-f]{32}", account) or not re.fullmatch(
        r"[A-Za-z0-9_-]{30,256}", token
    ):
        return False
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/workers/scripts/studio-api-worker/deployments"
    opener = build_opener(FixedOrigin())

    def request(data=None):
        remaining = min(8, deadline.remaining())
        if remaining <= 0:
            raise TimeoutError
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        with opener.open(Request(url, data=body, headers=headers), timeout=remaining) as reply:
            payload = json.loads(reply.read(512 * 1024 + 1))
        if payload.get("success") is not True:
            raise ValueError("Cloudflare rechazó la operación")
        return payload["result"]

    current = active_cloudflare_version(request())
    target = settings["version_id"]
    if current == target:
        return True
    if current != settings["current_version_id"]:
        return False
    try:
        request(
            {
                "strategy": "percentage",
                "versions": [{"version_id": target, "percentage": 100}],
                "annotations": {"workers/message": "Reversión automática de Studio"},
            }
        )
    except (OSError, ValueError):
        # La petición pudo llegar. Sólo se concilia con una lectura de la versión activa.
        pass
    return active_cloudflare_version(request()) == target


def rollback_compose(settings, deadline):
    file = verify_file(settings["path"], settings["sha256"])
    prefix = (["sudo", "-n"] if os.name == "posix" and os.geteuid() != 0 else []) + [
        "docker",
        "compose",
        "--project-name",
        settings["project"],
        "--project-directory",
        str(file.parent),
        "-f",
        str(file),
    ]
    result = run_process(
        [*prefix, "config", "--format", "json"], min(8, deadline.remaining()), capture_output=True
    )
    if not result or result[0] != 0:
        return False
    configuration = json.loads(result[1])
    services = configuration["services"]
    selected = settings["services"]
    if not set(selected).issubset(services):
        return False
    for name in selected:
        item = services[name]
        if item.get("build") or not re.fullmatch(
            r"(?:[^\s]+@)?sha256:[a-f0-9]{64}", item.get("image", "")
        ):
            return False
        if not run_command(["docker", "image", "inspect", item["image"]], deadline):
            return False
    if not run_command(
        [
            *prefix,
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "25",
            *selected,
        ],
        deadline,
    ):
        return False
    verified = run_process(
        [*prefix, "ps", "--format", "json", *selected],
        min(8, deadline.remaining()),
        capture_output=True,
    )
    if not verified or verified[0] != 0:
        return False
    raw = verified[1].strip()
    rows = (
        json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines()]
    )
    return set(selected) == {
        row.get("Service")
        for row in rows
        if row.get("Project") == settings["project"]
        and row.get("State") == "running"
        and row.get("Health") in {"", "healthy"}
    }


def rollback_openclaw(settings, deadline):
    identity = os.environ.get("SRE_OPENCLAW_SSH_IDENTITY", "")
    known = os.environ.get("SRE_OPENCLAW_KNOWN_HOSTS", "")
    if not all(Path(path).is_absolute() and Path(path).is_file() for path in (identity, known)):
        return False
    helper = Path(__file__).with_name("rollback_openclaw_roster.py")
    source = base64.b64encode(helper.read_bytes()).decode("ascii")
    loader = (
        "import base64; exec(compile(base64.b64decode("
        + repr(source)
        + "), 'restore_roster', 'exec'))"
    )
    target = settings["ssh_user"] + "@192.168.0.7"
    patch = verify_file(settings["path"], settings["sha256"]).read_text(encoding="utf-8")
    return run_command(
        [
            "ssh",
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "UserKnownHostsFile=" + known,
            "-o",
            "ConnectTimeout=5",
            "-i",
            identity,
            target,
            "/usr/bin/python3 -I -S -B -c " + shlex.quote(loader),
        ],
        deadline,
        patch,
        maximum=65,
    )


def rollback_live_component(component, manifest, deadline):
    handlers = {
        "cloudflare": rollback_cloudflare,
        "compose": rollback_compose,
        "openclaw": rollback_openclaw,
    }
    try:
        return handlers[component](manifest[component], deadline)
    except (IndexError, KeyError, OSError, TypeError, ValueError, subprocess.TimeoutExpired):
        return False


def run_rollback(manifest, deadline, simulation=None):
    if simulation is not None:
        return {
            name: simulation.get("rollback", {}).get(name) is True
            for name in ("cloudflare", "compose", "openclaw")
        }
    live = manifest.get("schema_version") == 2
    if not live and not PRODUCTION_ROLLBACK_READY:
        raise ValueError(
            "Falta descubrir y probar la activación del servicio OpenClaw antes de habilitar reversión productiva"
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {
            name: pool.submit(
                rollback_live_component if live else rollback_component, name, manifest, deadline
            )
            for name in ("cloudflare", "compose", "openclaw")
        }
        return {name: job.result() for name, job in jobs.items()}


def load_probes(path):
    probes = json.loads(Path(path).read_text(encoding="utf-8")) if path else default_probes()
    if not isinstance(probes, list) or sorted(item["name"] for item in probes) != sorted(
        PROBE_NAMES
    ):
        raise ValueError("Deben declararse las diez sondas de la plataforma")
    expected = {probe["name"]: probe for probe in default_probes()}
    for item in probes:
        parsed = urlsplit(item["url"])
        if (
            parsed.scheme not in ("http", "https")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("URL de sonda inválida")
        original = urlsplit(expected[item["name"]]["url"])
        if (parsed.scheme, parsed.hostname, parsed.port) != (
            original.scheme,
            original.hostname,
            original.port,
        ):
            raise ValueError("No se permite enviar credenciales a otro origen")
        if any(
            item.get(key) != expected[item["name"]].get(key)
            for key in ("method", "contract", "token_env")
        ):
            raise ValueError("El contrato y autenticación de las sondas son obligatorios")
    return probes


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verificación y reversión con límite total de 85 segundos"
    )
    parser.add_argument("mode", choices=("smoke", "rollback"))
    parser.add_argument("--simulate", metavar="ARCHIVO")
    parser.add_argument("--probes-json", metavar="ARCHIVO")
    parser.add_argument("--failure-drill", choices=PROBE_NAMES, metavar="SONDA")
    parser.add_argument("--manifest", "--rollback-manifest", dest="manifest", metavar="ARCHIVO")
    parser.add_argument(
        "--manifest-sha256",
        "--rollback-manifest-sha256",
        dest="manifest_hash",
        metavar="HUELLA",
    )
    args = parser.parse_args(argv)
    deadline = Deadline()
    started = time.monotonic()
    try:
        simulation = (
            json.loads(Path(args.simulate).read_text(encoding="utf-8")) if args.simulate else None
        )
        manifest = (
            load_manifest(args.manifest, args.manifest_hash)
            if args.manifest and args.manifest_hash
            else None
        )
        if bool(args.manifest) != bool(args.manifest_hash):
            raise ValueError("El manifiesto requiere huella externa")
        if args.failure_drill and (
            args.mode != "smoke"
            or simulation is not None
            or not manifest
            or manifest["schema_version"] != 2
        ):
            raise ValueError("El ensayo de fallo requiere smoke real y manifiesto acotado")
        report = {
            "simulation": simulation is not None,
            "production_verified": False,
            "budget_seconds": TOTAL_BUDGET_SECONDS,
        }
        if args.mode == "rollback" and manifest is None:
            raise ValueError("La reversión requiere manifiesto validado")
        if args.mode == "smoke":
            if simulation is not None:
                probes = [
                    {
                        "name": name,
                        "ok": simulation.get("probes", {}).get(name, {}).get("ok") is True,
                    }
                    for name in PROBE_NAMES
                ]
            else:
                probes = run_probes(
                    load_probes(args.probes_json),
                    Deadline(15, expires=deadline.expires),
                    os.environ,
                )
            report["probes"] = probes
            if args.failure_drill and all(item["ok"] for item in probes):
                failed = next(item for item in probes if item["name"] == args.failure_drill)
                report["fault_injected"] = args.failure_drill
                failed.update(ok=False, reason="fallo_controlado_post_despliegue")
            if all(item["ok"] for item in probes):
                report.update(
                    ok=True,
                    production_verified=simulation is None,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
                print(json.dumps(report, ensure_ascii=False))
                return 0
            if manifest is None:
                report.update(
                    ok=False,
                    rollback="bloqueado_sin_manifiesto",
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
                print(json.dumps(report, ensure_ascii=False))
                return 1
        restored = run_rollback(manifest, deadline, simulation)
        report["rollback_components"] = restored
        if all(restored.values()):
            report["post_rollback_probes"] = (
                [
                    {
                        "name": name,
                        "ok": simulation.get("post_rollback_probes", {}).get(name, {}).get("ok")
                        is True,
                    }
                    for name in PROBE_NAMES
                ]
                if simulation is not None
                else run_probes(load_probes(args.probes_json), deadline, os.environ)
            )
        report["ok"] = all(restored.values()) and all(
            item["ok"] for item in report.get("post_rollback_probes", [{"ok": False}])
        )
        report["production_verified"] = report["ok"] and simulation is None
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if args.mode == "smoke" and simulation is None and manifest["schema_version"] == 2:
            sent = send_rollback_alert(report, manifest["release_sha"], os.environ)
            report["alerta_sre"] = "aceptada_por_prometheus" if sent else "error_de_entrega"
        print(json.dumps(report, ensure_ascii=False))
        return 0 if args.mode == "rollback" and report["ok"] else 1
    except (ValueError, OSError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Configuración inválida o artefacto ausente; no se acredita recuperación",
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
