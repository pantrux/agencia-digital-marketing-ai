"""Entrada explícita de procesos aislados; importar no abre puertos ni lee credenciales."""

import argparse
import ctypes
import hashlib
import importlib
import json
import os
import re
import socket
import sys
from contextlib import contextmanager
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

# -I elimina PYTHONPATH y el directorio del script; sólo se incorpora esta fuente instalada.
SERVICES = Path(__file__).resolve().parent
if str(SERVICES) not in sys.path:
    sys.path.insert(0, str(SERVICES))

from bridge_supervisor import child_environment, read_runtime_config, service_specs  # noqa: E402

SPEACHES_COMMIT = "c78b77d5ce56f8c1042f6bc11209697cca2b2445"


def current_windows_process():
    """Identidad real del intérprete, sin consultar ni terminar otro proceso."""
    if os.name != "nt":
        raise OSError("El recibo requiere Windows nativo.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    security.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    process = kernel.GetCurrentProcess()
    image = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(image))
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.QueryFullProcessImageNameW(
        process, 0, image, ctypes.byref(size)
    ) or not kernel.GetProcessTimes(process, *(ctypes.byref(value) for value in times)):
        raise OSError("No se acreditó la identidad nativa del supervisor.")
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(process, 8, ctypes.byref(token)):
        raise OSError("No se pudo consultar el SID propio.")
    try:
        needed = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 8 <= needed.value <= 65536:
            raise OSError("Tamaño de identidad inválido.")
        buffer = ctypes.create_string_buffer(needed.value)
        if not security.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise OSError("No se acreditó el token propio.")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        sid_text = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise OSError("No se acreditó el SID propio.")
        try:
            user_sid = sid_text.value
        finally:
            kernel.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
    finally:
        kernel.CloseHandle(token)
    creation = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return {
        "ProcessId": os.getpid(),
        "ParentProcessId": os.getppid(),
        "ProcessImagePath": image.value,
        "UserSid": user_sid,
        "HostName": socket.gethostname().upper(),
        "CreationFileTimeUtc": str(creation),
    }


def make_start_receipt(mapping, native):
    """El recibo identifica al Python real y su bundle; no acredita salud ni contiene argv."""
    root = PureWindowsPath(r"C:\GPU-Bridge")
    if (
        PureWindowsPath(mapping["GPU_INSTALL_ROOT"]) != root
        or PureWindowsPath(mapping["GPU_RUNTIME_CONFIG_PATH"]) != root / "runtime.json"
        or PureWindowsPath(mapping["GPU_START_RECEIPT_PATH"]) != root / "supervisor-start.json"
        or PureWindowsPath(native["ProcessImagePath"])
        != PureWindowsPath(mapping["GPU_BASE_PYTHON_PATH"])
        or native["UserSid"] != mapping["GPU_WINDOWS_USER_SID"]
        or native["HostName"] != "W11-HOMELAB"
        or not re.fullmatch(r"[a-f0-9]{64}", mapping["GPU_SERVICES_BUNDLE_SHA256"])
        or type(native["ProcessId"]) is not int
        or native["ProcessId"] <= 0
    ):
        raise ValueError("La identidad real del supervisor no coincide con el respaldo.")
    filetime = int(native["CreationFileTimeUtc"])
    started = datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=filetime // 10)
    return {
        "SchemaVersion": 1,
        "Role": "supervisor",
        "Valid": True,
        "Started": True,
        "Readiness": "pending",
        **{
            key: native[key]
            for key in (
                "ProcessId",
                "ParentProcessId",
                "ProcessImagePath",
                "UserSid",
                "HostName",
                "CreationFileTimeUtc",
            )
        },
        "StartTimeUtc": started.isoformat(),
        "InstallRoot": str(root),
        "InstallationOwner": mapping["GPU_INSTALL_OWNER_MARKER"],
        "RuntimeConfigPath": mapping["GPU_RUNTIME_CONFIG_PATH"],
        "LauncherPath": str(root / "services" / "launch_service.py"),
        "SourceBundleSha256": mapping["GPU_SERVICES_BUNDLE_SHA256"],
    }


def publish_start_receipt(mapping):
    root = Path(mapping["GPU_INSTALL_ROOT"])
    target = Path(mapping["GPU_START_RECEIPT_PATH"])
    if root != Path(r"C:\GPU-Bridge") or target != root / "supervisor-start.json":
        raise ValueError("El recibo debe usar la ruta privada canónica.")
    if any(path.is_symlink() or path.is_junction() for path in (root, target)):
        raise ValueError("El recibo no admite enlaces ni reanálisis.")
    if (root / ".agencia-gpu-owner").read_text(encoding="utf-8") != mapping[
        "GPU_INSTALL_OWNER_MARKER"
    ]:
        raise ValueError("El árbol del recibo no pertenece a esta instalación.")
    if target.exists():
        previous = json.loads(target.read_text(encoding="utf-8-sig"))
        if (
            previous.get("Role") != "supervisor"
            or previous.get("InstallationOwner") != mapping["GPU_INSTALL_OWNER_MARKER"]
        ):
            raise ValueError("El recibo existente no pertenece al supervisor propio.")
    receipt = make_start_receipt(mapping, current_windows_process())
    with Path(mapping["GPU_BASE_PYTHON_PATH"]).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != mapping["GPU_BASE_PYTHON_SHA256"]:
            raise ValueError("La imagen Python no coincide con su huella respaldada.")
    temporary = root / ".supervisor-start.json.new"
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(receipt, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if created and temporary.exists():
            temporary.unlink()


@contextmanager
def reserve_supervisor_socket():
    """La reserva exclusiva impide que un segundo supervisor sobrescriba el recibo."""
    if os.name != "nt":
        raise OSError("El listener de producción requiere Windows.")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("0.0.0.0", 9835))
        listener.listen(128)
        yield listener


def serve(service, mapping):
    if mapping.get("GPU_BRIDGE_BIND_ADDRESS") != "0.0.0.0":
        raise ValueError("La dirección de escucha no coincide con el contrato aprobado.")
    if service == "whisper":
        source = Path(mapping["GPU_INSTALL_ROOT"]) / "whisper" / "vendor" / SPEACHES_COMMIT
        if Path(mapping["GPU_WHISPER_SOURCE_ROOT"]) != source or not (source / "src").is_dir():
            raise ValueError("La fuente Whisper no está en la ruta respaldada.")
        sys.path.insert(0, str(source / "src"))
    module_name, port = {
        "supervisor": ("bridge_supervisor", 9835),
        "immich": ("immich_service", 3003),
        "whisper": ("whisper_service", 8000),
    }[service]
    module = importlib.import_module(module_name)
    import uvicorn

    options = {
        "host": mapping["GPU_BRIDGE_BIND_ADDRESS"],
        "port": port,
        "workers": 1,
        "proxy_headers": False,
        "access_log": False,
        "server_header": False,
        "lifespan": "on",
        "log_level": "info",
    }
    if service == "supervisor":
        with reserve_supervisor_socket() as listener:
            publish_start_receipt(mapping)
            app = module.create_app(mapping)
            uvicorn.Server(uvicorn.Config(app, **options)).run(sockets=[listener])
        return
    app = module.create_app(mapping)
    uvicorn.run(
        app,
        host=mapping["GPU_BRIDGE_BIND_ADDRESS"],
        port=port,
        workers=1,
        proxy_headers=False,
        access_log=False,
        server_header=False,
        lifespan="on",
        log_level="info",
    )


def redirect_supervisor_logs(mapping):
    """El proceso escribe directamente al archivo privado, sin pipes del lanzador PowerShell."""
    root = Path(mapping["GPU_INSTALL_ROOT"])
    logs = Path(mapping["GPU_LOG_ROOT"])
    if logs != root / "logs" or not logs.is_dir() or logs.is_symlink() or logs.is_junction():
        raise ValueError("No existe el directorio privado de registros propio.")
    target = logs / "supervisor.log"
    if target.is_symlink() or target.is_junction():
        raise ValueError("El registro del supervisor no admite enlaces.")
    # Un inicio oculto puede heredar descriptores de consola inexistentes.
    stream = target.open("a", encoding="utf-8", errors="backslashreplace", buffering=1)
    sys.stdout = sys.stderr = stream


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lanza únicamente servicios GPU propios fijados.")
    parser.add_argument("service", choices=("supervisor", "immich", "whisper"))
    parser.add_argument(
        "--check",
        action="store_true",
        help="Valida archivos y configuración sin iniciar servicios.",
    )
    args = parser.parse_args(argv)
    try:
        mapping = read_runtime_config(os.environ["GPU_RUNTIME_CONFIG_PATH"])
        if args.check:
            specs = service_specs(mapping)
            print(json.dumps({"valid": True, "services": [spec.name for spec in specs]}))
            return 0
        sanitized = child_environment(dict(os.environ), mapping)
        os.environ.clear()
        os.environ.update(sanitized)
        if args.service == "supervisor":
            redirect_supervisor_logs(mapping)
        serve(args.service, mapping)
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "error": "No se pudo iniciar el servicio propio del puente.",
                    "error_type": type(error).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
