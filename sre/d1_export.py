"""Exportación D1 explícita, acotada y privada; plan sin red por defecto."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import re
import sqlite3
import ssl
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import httpx

MAX_SQL = 64 * 1024 * 1024
MAX_SECONDS = 180
OWNER = "agencia-digital-marketing-ai:d1-export:v1"
RESOURCE = "studio-saas-db"
EXPECTED_OWNER = {
    "id": 1,
    "project": "agencia-digital-marketing-ai",
    "resource": RESOURCE,
    "schema_version": 1,
}
STAGE_MARKER = ".agency-d1-export-owner.json"
OUTPUT_NAME = "studio-saas-db.sql"
RECEIPT_NAME = "d1-export-receipt.json"
PARTIAL_NAME = ".d1-export.partial"
MAX_JSON = 1024 * 1024
RETRY_DELAYS = (0.5, 1.0)
RETRY_HTTP_STATUSES = {502, 503, 504}
PROJECT = Path(__file__).resolve().parents[1]
_LOG_LOCK = threading.RLock()
_LOG_USERS = 0
_LOG_PREVIOUS = {}


class ExportError(ValueError):
    """Error saneado: nunca incorpora URL, token, bookmark, SQL o cuerpo remoto."""


class TransientExportError(ExportError):
    """Fallo temporal de un intercambio que puede repetirse si es seguro hacerlo."""


def public_error_code(error):
    if isinstance(error, ExportError) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", str(error)):
        return str(error)
    return "EXPORTACION_D1_RECHAZADA"


@contextmanager
def quiet_http_logs():
    """HTTPX registra URLs a nivel INFO; suspende sólo sus trazas y las de HTTPCore."""
    global _LOG_USERS
    with _LOG_LOCK:
        names = {
            "httpx",
            "httpcore",
            "httpcore.connection",
            "httpcore.http11",
            "httpcore.http2",
            "httpcore.proxy",
            "httpcore.socks",
        }
        names.update(
            name
            for name in list(logging.Logger.manager.loggerDict)
            if name.startswith(("httpx.", "httpcore."))
        )
        for name in names:
            if name not in _LOG_PREVIOUS:
                logger = logging.getLogger(name)
                _LOG_PREVIOUS[name] = logger.disabled
                logger.disabled = True
        _LOG_USERS += 1
    try:
        yield
    finally:
        with _LOG_LOCK:
            _LOG_USERS -= 1
            if _LOG_USERS == 0:
                for name, previous in _LOG_PREVIOUS.items():
                    logging.getLogger(name).disabled = previous
                _LOG_PREVIOUS.clear()


def validate_signed_url(value):
    try:
        if (
            not isinstance(value, str)
            or len(value) > 8192
            or not value.isascii()
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError
        parsed = urlsplit(value)
        pattern = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+r2\.cloudflarestorage\.com"
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or not re.fullmatch(pattern, parsed.hostname or "")
            or parsed.path in ("", "/")
        ):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise ExportError("URL_DE_DESCARGA_RECHAZADA") from None


def envelope(document):
    if (
        not isinstance(document, dict)
        or document.get("success") is not True
        or document.get("errors") not in (None, [])
    ):
        raise ExportError("RESPUESTA_API_RECHAZADA")
    return document.get("result")


def parse_poll(document):
    result = envelope(document)
    if (
        not isinstance(result, dict)
        or result.get("success", True) is not True
        or result.get("error")
        or result.get("type", "export") != "export"
    ):
        raise ExportError("EXPORTACION_RECHAZADA")
    status = result.get("status")
    if status == "complete":
        output = result.get("result")
        if not isinstance(output, dict):
            raise ExportError("EXPORTACION_INCOMPLETA")
        return "complete", validate_signed_url(output.get("signed_url"))
    bookmark = result.get("at_bookmark")
    if (
        "status" not in result
        and isinstance(bookmark, str)
        and re.fullmatch(r"[!-~]{1,2048}", bookmark)
    ):
        return "pending", bookmark
    raise ExportError("ESTADO_DE_EXPORTACION_RECHAZADO")


def check_owner(rows):
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or rows[0] != EXPECTED_OWNER
        or type(rows[0].get("id")) is not int
        or type(rows[0].get("schema_version")) is not int
    ):
        raise ExportError("PROPIEDAD_D1_NO_ACREDITADA")


def validate_dump(content, *, deadline=None):
    """Ejecuta sólo en SQLite en memoria con autorizador y presupuesto de instrucciones."""
    if not content or len(content) > MAX_SQL:
        raise ExportError("TAMANO_SQL_RECHAZADO")
    try:
        sql = content.decode("utf-8-sig")
        if "\x00" in sql or not sqlite3.complete_statement(sql):
            raise ExportError("SQL_TRUNCADO_O_INVALIDO")
        database = sqlite3.connect(":memory:")
        try:
            database.execute("PRAGMA page_size=4096")
            database.execute("PRAGMA temp_store=MEMORY")
            database.execute("PRAGMA max_page_count=32768")
            database.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
            database.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQL)
            database.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL)
            end = min(deadline if deadline is not None else float("inf"), time.monotonic() + 10)
            steps = 0

            def progress():
                nonlocal steps
                steps += 1
                return int(steps > 100000 or time.monotonic() >= end)

            denied = {
                sqlite3.SQLITE_ATTACH,
                sqlite3.SQLITE_DETACH,
                sqlite3.SQLITE_CREATE_VTABLE,
                sqlite3.SQLITE_DROP_VTABLE,
                sqlite3.SQLITE_CREATE_TEMP_TABLE,
                sqlite3.SQLITE_CREATE_TEMP_INDEX,
                sqlite3.SQLITE_CREATE_TEMP_VIEW,
                sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            }

            def authorizer(action, first, second, _database, _source):
                if action in denied:
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in {
                    "load_extension",
                    "readfile",
                    "writefile",
                }:
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA and (first or "").lower() not in {
                    "foreign_keys",
                    "defer_foreign_keys",
                }:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            database.set_progress_handler(progress, 1000)
            database.set_authorizer(authorizer)
            database.executescript(sql)
            if database.in_transaction:
                raise ExportError("SQL_TRUNCADO_O_INVALIDO")
            database.set_authorizer(None)
            if database.execute("PRAGMA quick_check").fetchmany(2) != [("ok",)]:
                raise ExportError("INTEGRIDAD_SQL_NO_ACREDITADA")
            if database.execute("PRAGMA foreign_key_check").fetchmany(1):
                raise ExportError("INTEGRIDAD_REFERENCIAL_NO_ACREDITADA")
            database.row_factory = sqlite3.Row
            rows = [
                dict(row)
                for row in database.execute(
                    "SELECT id,project,resource,schema_version FROM _agency_resource_owner ORDER BY id"
                ).fetchmany(2)
            ]
            check_owner(rows)
            tables = database.execute("PRAGMA table_list").fetchall()
            if any(row["type"] in ("virtual", "shadow") for row in tables):
                raise ExportError("TABLAS_VIRTUALES_NO_RECUPERABLES")
            return {
                "sqlite_integrity_verified": True,
                "owner_verified": True,
                "cloud_restore_verified": False,
                "tables": sum(row["type"] == "table" for row in tables),
            }
        finally:
            database.close()
    except (sqlite3.Error, UnicodeError, ValueError):
        raise ExportError("DUMP_SQL_RECHAZADO") from None


@dataclass(frozen=True)
class Settings:
    account_id: str
    database_id: str
    staging: Path
    token: str = field(repr=False)

    @classmethod
    def from_values(cls, values):
        try:
            account, database, token, staging = (
                values[key]
                for key in (
                    "CF_ACCOUNT_ID",
                    "CF_D1_DATABASE_ID",
                    "CF_PLATFORM_API_TOKEN",
                    "CF_D1_EXPORT_STAGING_PATH",
                )
            )
            if not re.fullmatch(r"[a-f0-9]{32}", account) or str(uuid.UUID(database)) != database:
                raise ValueError
            if not re.fullmatch(r"[!-~]{20,4096}", token) or any(
                word in token.upper() for word in ("CHANGE_ME", "PENDIENTE", "REPLACE_ME")
            ):
                raise ValueError
            path = Path(staging)
            if (
                not path.is_absolute()
                or ".." in path.parts
                or path.resolve(strict=True) != path.absolute()
            ):
                raise ValueError
            return cls(account, database, path, token)
        except (ValueError, OSError, KeyError):
            raise ExportError("CONFIGURACION_D1_RECHAZADA") from None

    @property
    def api_path(self):
        return f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/d1/database/{self.database_id}"


def load_module(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def private_access(path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or (not directory and info.st_nlink != 1):
        raise ExportError("STAGING_TIPO_RECHAZADO")
    if os.name == "nt":
        access = load_module(
            "d1_export_access", Path(__file__).with_name("backup_tools_access_background.py")
        )
        access.check_access(path, private=True)
    elif info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != (
        0o700 if directory else 0o600
    ):
        raise ExportError("STAGING_PERMISOS_RECHAZADOS")


def identity(path):
    info = path.lstat()
    return {"device": info.st_dev, "inode": info.st_ino}


def stage_marker(settings):
    return {
        "owner": OWNER,
        "account_id": settings.account_id,
        "database_id": settings.database_id,
        "resource": RESOURCE,
        "directory": identity(settings.staging),
    }


def read_private(path, maximum):
    private_access(path)
    expected = identity(path)
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if {"device": info.st_dev, "inode": info.st_ino} != expected or info.st_size > maximum:
            raise ExportError("STAGING_ARCHIVO_RECHAZADO")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ExportError("STAGING_TAMANO_RECHAZADO")
    return data


class Staging:
    """Sólo utiliza una carpeta privada ya acreditada; no crea ni adopta directorios."""

    def __init__(self, settings):
        self.settings = settings
        self.path = settings.staging
        self.directory = identity(self.path)
        self.partial_identity = None
        self.check()

    def check(self, *, additional=()):
        for path in (*reversed(self.path.parents), self.path):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ExportError("STAGING_ENLACE_RECHAZADO")
        private_access(self.path, directory=True)
        if identity(self.path) != self.directory:
            raise ExportError("STAGING_IDENTIDAD_CAMBIADA")
        marker = json.loads(read_private(self.path / STAGE_MARKER, 8192))
        if marker != stage_marker(self.settings):
            raise ExportError("STAGING_PROPIEDAD_NO_ACREDITADA")
        if set(path.name for path in self.path.iterdir()) != {STAGE_MARKER, *additional}:
            raise ExportError("STAGING_NO_ESTA_VACIO")

    def open_partial(self):
        self.check()
        descriptor = os.open(
            self.path / PARTIAL_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        info = os.fstat(descriptor)
        self.partial_identity = {"device": info.st_dev, "inode": info.st_ino}
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "wb")

    def remove_partial(self):
        path = self.path / PARTIAL_NAME
        if self.partial_identity is not None and path.exists():
            info = path.lstat()
            if (
                identity(path) != self.partial_identity
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
            ):
                raise ExportError("PARCIAL_CAMBIADO_NO_SE_ELIMINA")
            path.unlink()
        self.partial_identity = None

    def publish(self, result):
        self.check(additional=(PARTIAL_NAME,))
        partial = self.path / PARTIAL_NAME
        private_access(partial)
        if identity(partial) != self.partial_identity:
            raise ExportError("PARCIAL_CAMBIADO_NO_SE_PUBLICA")
        os.link(partial, self.path / OUTPUT_NAME)
        partial.unlink()
        self.partial_identity = None
        private_access(self.path / OUTPUT_NAME)
        receipt = self.path / RECEIPT_NAME
        descriptor = os.open(
            receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(result, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        private_access(receipt)
        if os.name != "nt":
            descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def remaining(deadline, clock):
    value = deadline - clock()
    if value <= 0:
        raise ExportError("PLAZO_EXPORTACION_AGOTADO")
    return value


def response_length(response, maximum):
    value = response.headers.get("content-length")
    if value is not None and (not re.fullmatch(r"[0-9]{1,12}", value) or int(value) > maximum):
        raise ExportError("TAMANO_HTTP_RECHAZADO")
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ExportError("CODIFICACION_HTTP_RECHAZADA")
    return None if value is None else int(value)


def check_http_status(status, operation):
    if status != 200:
        error = TransientExportError if status in RETRY_HTTP_STATUSES else ExportError
        raise error(f"HTTP_{operation}_{status}")


async def exchange(
    operation, transport_code, deadline, clock, sleeper, *, delays=RETRY_DELAYS, cleanup=None
):
    for delay in (*delays, None):
        try:
            return await operation()
        except (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError):
            failure = TransientExportError(transport_code)
        except TransientExportError as error:
            failure = error
        if delay is None:
            raise failure from None
        if cleanup is not None:
            cleanup()
        if remaining(deadline, clock) <= delay:
            raise ExportError("PLAZO_EXPORTACION_AGOTADO")
        await sleeper(delay)


async def post_json(client, url, body, deadline, clock):
    async with client.stream(
        "POST", url, json=body, timeout=min(10, remaining(deadline, clock))
    ) as response:
        check_http_status(response.status_code, "API")
        expected = response_length(response, MAX_JSON)
        data = bytearray()
        async for block in response.aiter_raw(chunk_size=65536):
            remaining(deadline, clock)
            data.extend(block)
            if len(data) > MAX_JSON:
                raise ExportError("RESPUESTA_API_EXCESIVA")
        if expected is not None and len(data) != expected:
            raise ExportError("RESPUESTA_API_TRUNCADA")
        remaining(deadline, clock)
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise ExportError("RESPUESTA_API_JSON") from None


async def preflight(client, settings, deadline, clock, sleeper):
    results = []
    for sql in (
        "SELECT id,project,resource,schema_version FROM _agency_resource_owner ORDER BY id;",
        "PRAGMA table_list;",
    ):
        query = partial(
            post_json, client, settings.api_path + "/query", {"sql": sql}, deadline, clock
        )
        value = envelope(await exchange(query, "TRANSPORTE_API_D1", deadline, clock, sleeper))
        if (
            not isinstance(value, list)
            or len(value) != 1
            or value[0].get("success") is not True
            or not isinstance(value[0].get("results"), list)
        ):
            raise ExportError("PREFLIGHT_D1_RECHAZADO")
        results.append(value[0]["results"])
        if len(results) == 1:
            check_owner(results[0])
    check_owner(results[0])
    tables = results[1]
    if not tables or any(row.get("type") not in ("table", "view") for row in tables):
        raise ExportError("TABLAS_VIRTUALES_NO_RECUPERABLES")
    if not any(
        row.get("name") == "_agency_resource_owner" and row.get("type") == "table" for row in tables
    ):
        raise ExportError("MARCADOR_D1_NO_ES_TABLA")


async def poll_export(client, settings, deadline, clock, sleeper):
    bookmark = None
    for count in range(1, 361):
        body = {"output_format": "polling", "dump_options": {"compatibility": "d1"}}
        if bookmark is not None:
            body["current_bookmark"] = bookmark
        request = partial(post_json, client, settings.api_path + "/export", body, deadline, clock)
        # Una respuesta perdida al iniciar puede corresponder a un export ya activo.
        document = await exchange(
            request,
            "TRANSPORTE_API_D1",
            deadline,
            clock,
            sleeper,
            delays=RETRY_DELAYS if bookmark is not None else (),
        )
        state, value = parse_poll(document)
        if state == "complete":
            returned = document["result"].get("at_bookmark", bookmark)
            if bookmark is not None and returned != bookmark:
                raise ExportError("EXPORTACION_REINICIADA")
            return value, count
        if bookmark is not None and value != bookmark:
            raise ExportError("BOOKMARK_CAMBIADO")
        bookmark = value
        await sleeper(min(0.5, remaining(deadline, clock)))
    raise ExportError("LIMITE_DE_POLLING_AGOTADO")


async def download(client_factory, signed_url, staging, deadline, clock, tls_context):
    validate_signed_url(signed_url)
    options = {
        "verify": tls_context,
        "trust_env": False,
        "follow_redirects": False,
        "headers": {"Accept-Encoding": "identity"},
        "cookies": {},
        "auth": None,
    }
    size, digest = 0, hashlib.sha256()
    async with client_factory(**options) as client:
        with staging.open_partial() as stream:
            async with client.stream(
                "GET", signed_url, timeout=min(10, remaining(deadline, clock))
            ) as response:
                check_http_status(response.status_code, "DESCARGA")
                expected = response_length(response, MAX_SQL)
                async for block in response.aiter_raw(chunk_size=65536):
                    remaining(deadline, clock)
                    size += len(block)
                    if size > MAX_SQL:
                        raise ExportError("SQL_EXCEDE_LIMITE")
                    stream.write(block)
                    digest.update(block)
                if not size or expected is not None and size != expected:
                    raise ExportError("DESCARGA_SQL_TRUNCADA")
                remaining(deadline, clock)
            stream.flush()
            os.fsync(stream.fileno())
    content = read_private(staging.path / PARTIAL_NAME, MAX_SQL)
    if hashlib.sha256(content).hexdigest() != digest.hexdigest():
        raise ExportError("PARCIAL_SQL_ALTERADO")
    return content, digest.hexdigest()


async def _export_database(settings, *, client_factory, clock, sleeper):
    staging = Staging(settings)
    started = clock()
    started_utc = datetime.now(UTC).isoformat()
    deadline = started + MAX_SECONDS
    try:
        async with asyncio.timeout(MAX_SECONDS):
            tls_context = ssl.create_default_context()
            async with client_factory(
                verify=tls_context,
                trust_env=False,
                follow_redirects=False,
                headers={
                    "Authorization": "Bearer " + settings.token,
                    "Accept-Encoding": "identity",
                },
            ) as api:
                await preflight(api, settings, deadline, clock, sleeper)
                signed_url, polls = await poll_export(api, settings, deadline, clock, sleeper)
            download_sql = partial(
                download, client_factory, signed_url, staging, deadline, clock, tls_context
            )
            content, checksum = await exchange(
                download_sql,
                "TRANSPORTE_DESCARGA_D1",
                deadline,
                clock,
                sleeper,
                cleanup=staging.remove_partial,
            )
            validation = validate_dump(
                content, deadline=time.monotonic() + min(10, remaining(deadline, clock))
            )
            remaining(deadline, clock)
            result = {
                "success": True,
                "resource": RESOURCE,
                "database_id": settings.database_id,
                "bytes": len(content),
                "sha256": checksum,
                "polls": polls,
                "elapsed_seconds": round(clock() - started, 3),
                "operation_started_at_utc": started_utc,
                "completed_at_utc": datetime.now(UTC).isoformat(),
                "output_path": str(staging.path / OUTPUT_NAME),
                "receipt_path": str(staging.path / RECEIPT_NAME),
                **validation,
            }
            staging.publish(result)
            return result
    except ExportError:
        raise
    except TimeoutError:
        raise ExportError("PLAZO_EXPORTACION_AGOTADO") from None
    except Exception:
        raise ExportError("EXPORTACION_D1_RECHAZADA") from None
    finally:
        staging.remove_partial()


async def export_database(
    settings, *, client_factory=httpx.AsyncClient, clock=time.monotonic, sleeper=asyncio.sleep
):
    with quiet_http_logs():
        try:
            return await _export_database(
                settings, client_factory=client_factory, clock=clock, sleeper=sleeper
            )
        except ExportError:
            raise
        except Exception:
            raise ExportError("EXPORTACION_D1_RECHAZADA") from None


def plan_export(settings):
    Staging(settings)
    return {
        "mode": "plan",
        "network_used": False,
        "resource": RESOURCE,
        "database_id": settings.database_id,
        "staging": str(settings.staging),
        "output_path": str(settings.staging / OUTPUT_NAME),
        "max_sql_bytes": MAX_SQL,
        "max_seconds": MAX_SECONDS,
        "blocks_database_queries": True,
        "cloud_preflight_verified": False,
        "cloud_restore_verified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=PROJECT / ".env")
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    try:
        environment = load_module("d1_export_environment", PROJECT / "scripts/ci/check_env.py")
        values, errors = environment.parse_env(arguments.vault.read_text(encoding="utf-8-sig"))
        if errors:
            raise ExportError("BOVEDA_RECHAZADA")
        settings = Settings.from_values(values)
        if arguments.staging is not None and arguments.staging != settings.staging:
            raise ExportError("STAGING_NO_RESPALDADO")
        result = (
            asyncio.run(export_database(settings)) if arguments.apply else plan_export(settings)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"success": False, "error": "EXPORTACION_CANCELADA"}))
        return 130
    except Exception as error:
        print(json.dumps({"success": False, "error": public_error_code(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
