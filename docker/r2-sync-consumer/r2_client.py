"""GET S3 específico de R2, sin SDK, redirecciones, cookies ni rutas del cliente."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import math
import re
import ssl
import time
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType

from sync_storage import StorageError, validate_job

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SIGNED_HEADERS = "host;x-amz-content-sha256;x-amz-date"
CHUNK_BYTES = 1024 * 1024


class R2Error(ValueError):
    """Código fijo sin URL, credenciales, cabeceras ni excepciones del transporte."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class R2Credentials:
    account_id: str = field(repr=False)
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)

    def __post_init__(self):
        for value, pattern in (
            (self.account_id, r"[0-9a-f]{32}"),
            (self.access_key_id, r"[A-Za-z0-9]{1,128}"),
            (self.secret_access_key, r"[\x21-\x7e]{1,256}"),
        ):
            if type(value) is not str or re.fullmatch(pattern, value) is None:
                raise R2Error("CREDENCIALES_INVALIDAS")


@dataclass(frozen=True, slots=True)
class SignedGet:
    host: str
    path: str
    headers: Mapping[str, str] = field(repr=False)


def sign_get(value, max_object_bytes, credentials: R2Credentials, amz_date: str) -> SignedGet:
    try:
        job = validate_job(value, max_object_bytes)
    except StorageError:
        raise R2Error("JOB_INVALIDO") from None
    if type(credentials) is not R2Credentials:
        raise R2Error("CREDENCIALES_INVALIDAS")
    if type(amz_date) is not str or re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", amz_date) is None:
        raise R2Error("FECHA_INVALIDA")
    try:
        date = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ")
        if date.strftime("%Y%m%dT%H%M%SZ") != amz_date:
            raise ValueError
    except ValueError:
        raise R2Error("FECHA_INVALIDA") from None
    day = amz_date[:8]
    host = credentials.account_id + ".r2.cloudflarestorage.com"
    path = "/studio-media-bucket/" + job.object_key
    scope = day + "/auto/s3/aws4_request"
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{EMPTY_SHA256}\nx-amz-date:{amz_date}\n"
    canonical = "\n".join(["GET", path, "", canonical_headers, SIGNED_HEADERS, EMPTY_SHA256])
    message = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    try:
        key = ("AWS4" + credentials.secret_access_key).encode("ascii")
        for part in (day, "auto", "s3", "aws4_request"):
            key = hmac.digest(key, part.encode("ascii"), "sha256")
        signature = hmac.digest(key, message.encode("ascii"), "sha256").hex()
    except Exception:
        raise R2Error("CRIPTOGRAFIA_FALLIDA") from None
    headers = MappingProxyType(
        {
            "Host": host,
            "x-amz-content-sha256": EMPTY_SHA256,
            "x-amz-date": amz_date,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={credentials.access_key_id}/{scope}, SignedHeaders={SIGNED_HEADERS}, Signature={signature}",
        }
    )
    return SignedGet(host, path, headers)


def _connect(host, timeout):
    return http.client.HTTPSConnection(
        host, 443, timeout=timeout, context=ssl.create_default_context()
    )


def _budget(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum


def download(
    value,
    max_object_bytes,
    credentials: R2Credentials,
    *,
    total_seconds,
    idle_seconds,
) -> Iterator[bytes]:
    """Cuenta todos los bytes; el publicador posterior comprueba el SHA256 real.

    El plazo total se revisa alrededor del I/O. DNS/getaddrinfo no queda acotado
    por el timeout de un socket; el servicio debe añadir un presupuesto externo.
    """
    if (
        not _budget(total_seconds, 300)
        or not _budget(idle_seconds, 15)
        or idle_seconds > total_seconds
    ):
        raise R2Error("PRESUPUESTO_INVALIDO")
    try:
        job = validate_job(value, max_object_bytes)
    except StorageError:
        raise R2Error("JOB_INVALIDO") from None
    request = sign_get(
        asdict(job),
        max_object_bytes,
        credentials,
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
    )
    deadline = time.monotonic() + total_seconds
    connection = response = None

    def remaining():
        left = deadline - time.monotonic()
        if not math.isfinite(left) or left <= 0:
            raise R2Error("PLAZO_AGOTADO")
        return min(idle_seconds, left)

    try:
        connection = _connect(request.host, remaining())
        connection.request("GET", request.path, headers=dict(request.headers))
        response = connection.getresponse()
        if response.status != 200:
            raise R2Error("HTTP_RECHAZADO")
        headers = response.getheaders()
        lengths = [value for name, value in headers if name.lower() == "content-length"]
        encodings = [value for name, value in headers if name.lower() == "content-encoding"]
        if (
            len(lengths) != 1
            or re.fullmatch(r"[1-9][0-9]{0,15}", lengths[0]) is None
            or int(lengths[0]) != job.bytes
            or any(name.lower() == "transfer-encoding" for name, _value in headers)
            or encodings not in ([], ["identity"])
        ):
            raise R2Error("CABECERAS_INVALIDAS")
        size = 0
        while True:
            timeout = remaining()
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            # Se pide un byte adicional para detectar un cuerpo excesivo en transportes
            # que no recortan por Content-Length; nunca se entrega al publicador.
            chunk = response.read(min(CHUNK_BYTES, job.bytes - size + 1))
            remaining()
            if not chunk:
                break
            if type(chunk) is not bytes or len(chunk) > job.bytes - size:
                raise R2Error("CUERPO_EXCESIVO")
            size += len(chunk)
            yield chunk
        if size != job.bytes:
            raise R2Error("CUERPO_TRUNCADO")
    except R2Error:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise R2Error("TRANSPORTE_FALLIDO") from None
    finally:
        for resource in (response, connection):
            if resource is not None:
                try:
                    resource.close()
                except (OSError, http.client.HTTPException):
                    pass
