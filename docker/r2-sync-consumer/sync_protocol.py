"""Verificación pura del protocolo R2 v1; no almacena claves ni realiza transporte."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass

from sync_storage import AssetJob, StorageError, validate_job

MAX_SYNC_BODY_BYTES = 4096
SYNC_TIME_TOLERANCE_SECONDS = 300
MAX_SAFE_INTEGER = 2**53 - 1
TIMESTAMP = re.compile(r"(?:0|[1-9][0-9]{0,15})")
SIGNATURE = re.compile(r"v1=[0-9a-f]{64}")


class ProtocolError(ValueError):
    """Sólo expone un código fijo, nunca el cuerpo, firma o secreto recibido."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VerifiedRequest:
    job: AssetJob
    fingerprint: str
    protocol: str = "v1"

    @property
    def body_sha256(self) -> str:
        return self.fingerprint

    @property
    def destination_relative_path(self) -> str:
        return self.job.relative_path


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("JSON_UTF8")
        result[key] = value
    return result


def _non_integer(_value):
    raise ProtocolError("CANONICO")


def _parse_body(body, max_object_bytes) -> AssetJob:
    if type(body) is not bytes or not 0 < len(body) <= MAX_SYNC_BODY_BYTES:
        raise ProtocolError("CUERPO")
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique,
            parse_float=_non_integer,
            parse_constant=_non_integer,
        )
    except ProtocolError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("JSON_UTF8") from None
    try:
        job = validate_job(value, max_object_bytes)
    except StorageError:
        raise ProtocolError("JOB_INVALIDO") from None
    # El orden de AssetJob coincide con los siete campos literales del productor TS.
    canonical = json.dumps(
        asdict(job), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if body != canonical:
        raise ProtocolError("CANONICO")
    return job


def _verify_request(
    body: bytes,
    timestamp: str,
    signature: str,
    secret: bytes,
    max_object_bytes: int,
    now_seconds: Callable[[], int],
    version: str,
) -> VerifiedRequest:
    """Exige cabeceras ya desambiguadas; el reloj y la política son obligatorios."""
    job = _parse_body(body, max_object_bytes)
    if type(timestamp) is not str or TIMESTAMP.fullmatch(timestamp) is None:
        raise ProtocolError("TIMESTAMP")
    seconds = int(timestamp)
    if seconds > MAX_SAFE_INTEGER:
        raise ProtocolError("TIMESTAMP")
    if (
        version not in ("v1", "v2")
        or type(signature) is not str
        or re.fullmatch(rf"{version}=[0-9a-f]{{64}}", signature) is None
    ):
        raise ProtocolError("FIRMA")
    if type(secret) is not bytes or not 32 <= len(secret) <= 128:
        raise ProtocolError("CLAVE")
    try:
        now = now_seconds()
    except Exception:
        raise ProtocolError("RELOJ") from None
    if (
        type(now) is not int
        or not 0 <= now <= MAX_SAFE_INTEGER
        or abs(now - seconds) > SYNC_TIME_TOLERANCE_SECONDS
    ):
        raise ProtocolError("RELOJ")
    fingerprint = hashlib.sha256(body).hexdigest()
    # Método, ruta, versión y LF son constantes; ninguna URL llega de la entrada.
    message = f"{version}\n{timestamp}\nPOST\n/{version}/sync\n{fingerprint}".encode("ascii")
    try:
        expected = hmac.digest(secret, message, "sha256")
        supplied = bytes.fromhex(signature[3:])
        authenticated = hmac.compare_digest(expected, supplied)
    except Exception:
        raise ProtocolError("FIRMA") from None
    if not authenticated:
        raise ProtocolError("FIRMA")
    return VerifiedRequest(job, fingerprint, version)


def verify_request(body, timestamp, signature, secret, max_object_bytes, now_seconds):
    """Conserva el contrato v1 para una sola fuente por orden."""
    return _verify_request(body, timestamp, signature, secret, max_object_bytes, now_seconds, "v1")


def verify_source_request(body, timestamp, signature, secret, max_object_bytes, now_seconds):
    """Verifica el contrato v2 para una fuente de una producción múltiple."""
    return _verify_request(body, timestamp, signature, secret, max_object_bytes, now_seconds, "v2")
