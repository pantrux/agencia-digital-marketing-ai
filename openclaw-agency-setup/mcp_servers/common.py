"""Lee credenciales privadas y acota las respuestas de los servicios internos."""

import http.client
import json
import os
import re
import stat
from pathlib import Path

MAX_SECRET = 4096
PROJECT = re.compile(r"[a-z][a-z0-9_-]{0,47}")
JOB = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class UpstreamError(ValueError):
    """Fallo de servicio con código público y sin respuesta original."""

    def __init__(self, status: int | None, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def authorize_project(project: str) -> None:
    """Exige el proyecto previsto en el entorno de instalación."""
    allowed = os.environ.get("AGENCY_MEDIA_PROJECTS", "").split(",")
    if (
        not isinstance(project, str)
        or not PROJECT.fullmatch(project)
        or any(PROJECT.fullmatch(name) is None for name in allowed)
        or project not in allowed
    ):
        raise ValueError("PROYECTO_NO_AUTORIZADO")


def _unchanged(left: os.stat_result, right: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(left, name) == getattr(right, name) for name in fields)


def read_secret(env_key: str) -> str:
    """Admite un archivo privado del servicio o un archivo de grupo con sólo lectura."""
    try:
        path = Path(os.environ[env_key])
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            mode = stat.S_IMODE(before.st_mode)
            owner = before.st_uid == os.geteuid() and mode in (0o400, 0o600)
            group = (
                before.st_uid == 0
                and before.st_gid in {os.getegid(), *os.getgroups()}
                and mode == 0o440
            )
            if not (
                stat.S_ISREG(before.st_mode)
                and before.st_nlink == 1
                and 0 < before.st_size <= MAX_SECRET + 2
                and (owner or group)
            ):
                raise ValueError
            raw = stream.read(MAX_SECRET + 3)
            after = os.fstat(stream.fileno())
            if not (
                len(raw) == before.st_size
                and _unchanged(before, after)
                and _unchanged(before, path.lstat())
            ):
                raise ValueError
        value = raw.removesuffix(b"\r\n") if raw.endswith(b"\r\n") else raw.removesuffix(b"\n")
        if re.fullmatch(rb"[!-~]{1,4096}", value) is None or value == b"CHANGE_ME":
            raise ValueError
        return value.decode("ascii")
    except (AttributeError, KeyError, OSError, ValueError):
        raise ValueError("CREDENCIAL_ARCHIVO_INVALIDO") from None


def request_json(
    host: str,
    port: int,
    path: str,
    *,
    method: str = "POST",
    body: dict | None = None,
    header: tuple[str, str],
    timeout: float,
    limit: int,
) -> dict:
    """Consulta un servicio de destino fijado por el adaptador."""
    fields = {"Accept": "application/json", header[0]: header[1]}
    payload = None
    if body is not None:
        fields["Content-Type"] = "application/json"
        payload = json.dumps(body, allow_nan=False, ensure_ascii=False).encode("utf-8")
    client = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        client.request(method, path, body=payload, headers=fields)
        with client.getresponse() as response:
            if not 200 <= response.status < 300:
                raise UpstreamError(response.status, "SERVICIO_HTTP")
            raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except UpstreamError:
        raise
    except (OSError, http.client.HTTPException):
        raise UpstreamError(None, "SERVICIO_TRANSPORTE") from None
    except (RecursionError, ValueError):
        raise UpstreamError(None, "SERVICIO_RESPUESTA_INVALIDA") from None
    finally:
        client.close()
