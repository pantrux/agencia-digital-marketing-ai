"""Configuración, acceso ASGI y rutas acotadas del puente GPU sin dependencias."""

import asyncio
import hmac
import ipaddress
import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

CLIENTS = frozenset({"192.168.0.85", "192.168.0.7", "192.168.0.8", "127.0.0.1", "::1"})
DEVICES = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


@dataclass(frozen=True)
class BridgeConfig:
    """Configuración mínima; nunca representa el token en mensajes de diagnóstico."""

    host: str
    token: str = field(repr=False)
    install_root: Path
    model_cache_root: Path

    @classmethod
    def from_env(cls, mapping: Mapping[str, str]):
        host = mapping.get("GPU_BRIDGE_HOST", "")
        token = mapping.get("GPU_BRIDGE_BEARER_TOKEN", "")
        if host != "192.168.0.6":
            raise ValueError("El puente solo puede instalarse en el host GPU declarado.")
        if len(token) < 32 or not token.isascii() or any(char.isspace() for char in token):
            raise ValueError(
                "El token del puente debe estar respaldado y tener al menos 32 caracteres ASCII."
            )
        if any(word in token.upper() for word in ("CHANGE_ME", "PENDIENTE", "REPLACE_ME")):
            raise ValueError("El token del puente sigue pendiente.")
        paths = [mapping.get(key, "") for key in ("GPU_INSTALL_ROOT", "GPU_MODEL_CACHE_ROOT")]
        if any(
            not PureWindowsPath(path).is_absolute() or path.startswith("\\\\") for path in paths
        ):
            raise ValueError("La instalación y caché requieren rutas locales absolutas de Windows.")
        return cls(host, token, *(Path(path) for path in paths))


def allowed_client(value: str) -> bool:
    """Usa exclusivamente la dirección del socket; admite IPv4 representada como IPv6."""
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address) in CLIENTS
    except ValueError:
        return False


async def json_response(send, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


HEALTH_BACKEND_TIMEOUT_SECONDS = 5


class GuardedAsgi:
    """Protege todas las rutas y consulta disponibilidad real con un plazo acotado."""

    def __init__(self, app, config: BridgeConfig, service: str, version: str, readiness):
        self.app, self.config = app, config
        self.service, self.version, self.readiness = service, version, readiness

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            raise ValueError("Protocolo no admitido por el puente.")
        if not client or not allowed_client(client[0]):
            await json_response(send, 403, {"error": "Cliente fuera de la lista permitida."})
            return
        headers = [
            value for key, value in scope.get("headers", []) if key.lower() == b"authorization"
        ]
        expected = f"Bearer {self.config.token}".encode("ascii")
        if len(headers) != 1 or not hmac.compare_digest(headers[0], expected):
            await json_response(send, 401, {"error": "Autenticación requerida."})
            return
        if scope.get("path") in ("/health", "/healthz"):
            if scope.get("method") != "GET":
                await json_response(send, 405, {"error": "Método no permitido."})
                return
            try:
                result = await asyncio.wait_for(
                    self.readiness(), timeout=HEALTH_BACKEND_TIMEOUT_SECONDS
                )
                if not isinstance(result, dict):
                    raise ValueError("Respuesta de disponibilidad inválida.")
                ready = result.get("ready") is True
                result = dict(
                    result,
                    service=self.service,
                    version=self.version,
                    status="ok" if ready else "degraded",
                    ready=ready,
                )
                # Se serializa dentro del bloque protegido para no filtrar errores del backend.
                json.dumps(result, allow_nan=False)
            except Exception:
                ready = False
                result = {
                    "service": self.service,
                    "version": self.version,
                    "status": "degraded",
                    "ready": False,
                    "error": "Backend no disponible.",
                }
            await json_response(send, 200 if ready else 503, result)
            return
        await self.app(scope, receive, send)


def safe_media_path(root: Path, relative: str) -> Path:
    """Resuelve una ruta NAS relativa y rechaza escapes, dispositivos y reanálisis."""
    if not isinstance(relative, str) or not relative or len(relative) > 1024:
        raise ValueError("Ruta de medios inválida.")
    if any(ord(char) < 32 for char in relative) or any(char in relative for char in '\\:*?"<>|'):
        raise ValueError("La ruta contiene caracteres no permitidos.")
    parts = relative.split("/")
    if any(
        not part
        or part in (".", "..")
        or part.endswith((".", " "))
        or DEVICES.match(part)
        or len(part) > 240
        for part in parts
    ):
        raise ValueError("La ruta contiene un segmento ambiguo o reservado.")
    root = root.resolve(strict=True)
    candidate = root
    for part in parts:
        candidate = candidate / part
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("No se admiten enlaces ni puntos de reanálisis en los medios.")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("La ruta sale del almacenamiento permitido.")
    return resolved
