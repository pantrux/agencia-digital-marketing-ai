"""Ledger SQLite local privado; no sirve HTTP, descarga objetos ni publica assets."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass

from sync_storage import AssetJob, Publication, RootProof, StorageError, validate_job

if sys.platform.startswith("linux"):
    import fcntl
else:
    fcntl = None

OWNER = "agencia-digital-marketing-ai:r2-sync-ledger:v1"
DB_NAME = "sync-ledger.sqlite3"
LOCK_NAME = ".sync-ledger.lock"
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_WAIT_SECONDS = 5.0
BUSY_SECONDS = 0.5
PAGE_SIZE = 4096
APPLICATION_ID = 0x4153594E
LOCK_BYTES = (OWNER + "\n").encode("ascii")
LOCAL_FILESYSTEMS = frozenset(("ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs", "overlay"))
SHA = re.compile(r"[0-9a-f]{64}")
META_SQL = "CREATE TABLE ledger_meta (id INTEGER PRIMARY KEY CHECK(id=1), schema_version INTEGER NOT NULL, owner TEXT NOT NULL)"
JOBS_SQL = "CREATE TABLE jobs (job_id TEXT PRIMARY KEY NOT NULL, fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64), state TEXT NOT NULL CHECK(state IN ('pending','completed')), receipt TEXT, CHECK((state='pending' AND receipt IS NULL) OR (state='completed' AND receipt IS NOT NULL)))"


class LedgerError(ValueError):
    """Código fijo sin SQL, payloads, credenciales o mensajes externos."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_operation(value, fingerprint, max_object_bytes, wait_seconds) -> AssetJob:
    if (
        type(wait_seconds) not in (int, float)
        or not math.isfinite(wait_seconds)
        or not 0 <= wait_seconds <= MAX_WAIT_SECONDS
        or type(fingerprint) is not str
        or SHA.fullmatch(fingerprint) is None
    ):
        raise LedgerError("OPERACION_INVALIDA")
    try:
        job = validate_job(value, max_object_bytes)
    except StorageError:
        raise LedgerError("JOB_INVALIDO") from None
    canonical = json.dumps(
        asdict(job), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(canonical) > 4096 or hashlib.sha256(canonical).hexdigest() != fingerprint:
        raise LedgerError("FINGERPRINT_NO_COINCIDE")
    return job


def _receipt_from_dict(value, job: AssetJob) -> Publication:
    fields = tuple(Publication.__dataclass_fields__)
    if type(value) is not dict or set(value) != set(fields):
        raise LedgerError("RECIBO_INVALIDO")
    data = dict(value)
    expected = {
        "job_id": job.job_id,
        "asset_id": job.asset_id,
        "relative_path": job.relative_path,
        "bytes": job.bytes,
        "sha256": job.sha256,
    }
    if (
        any(
            type(data[key]) is not type(wanted) or data[key] != wanted
            for key, wanted in expected.items()
        )
        or type(data["device"]) is not int
        or not 0 <= data["device"] < 2**64
        or type(data["inode"]) is not int
        or not 0 < data["inode"] < 2**64
        or type(data["created"]) is not bool
        or type(data["idempotent"]) is not bool
        or data["created"] == data["idempotent"]
    ):
        raise LedgerError("RECIBO_NO_CORRELACIONADO")
    return Publication(**data)


def validate_publication(value, job: AssetJob) -> Publication:
    if type(value) is not Publication:
        raise LedgerError("RECIBO_INVALIDO")
    return _receipt_from_dict(asdict(value), job)


def _receipt_json(receipt: Publication) -> str:
    result = json.dumps(asdict(receipt), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(result.encode("utf-8")) > 2048:
        raise LedgerError("RECIBO_EXCESIVO")
    return result


def _parse_receipt(value, job: AssetJob) -> Publication:
    if type(value) is not str or len(value.encode("utf-8")) > 2048:
        raise LedgerError("RECIBO_INVALIDO")

    def unique(pairs):
        data = {}
        for key, item in pairs:
            if key in data:
                raise LedgerError("RECIBO_AMBIGUO")
            data[key] = item
        return data

    try:
        result = json.loads(value, object_pairs_hook=unique, parse_constant=lambda _item: None)
    except (ValueError, UnicodeError):
        raise LedgerError("RECIBO_INVALIDO") from None
    return _receipt_from_dict(result, job)


def _identity(info):
    return info.st_dev, info.st_ino


def _fd(stack, descriptor):
    stack.callback(os.close, descriptor)
    return descriptor


def _regular(info, device, limit):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_dev != device
        or info.st_size > limit
    ):
        raise LedgerError("ARCHIVO_ESTADO_INSEGURO")


def _mount_kind(raw, path, device):
    if len(raw) > 2 * 1024 * 1024:
        raise LedgerError("MOUNTS_EXCESIVOS")
    matches = []
    for line in raw.decode("utf-8", errors="strict").splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        if not separator or len(fields) < 6 or len(after.split()) < 3:
            raise LedgerError("MOUNTINFO_INVALIDO")
        mountpoint = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        if fields[2] == device and (
            path == mountpoint or path.startswith(mountpoint.rstrip("/") + "/")
        ):
            matches.append((len(mountpoint), after.split()[0]))
    if not matches:
        raise LedgerError("ESTADO_REQUIERE_FILESYSTEM_LOCAL")
    longest = max(length for length, _kind in matches)
    kinds = {kind for length, kind in matches if length == longest}
    if len(kinds) != 1 or not kinds <= LOCAL_FILESYSTEMS:
        raise LedgerError("ESTADO_REQUIERE_FILESYSTEM_LOCAL")
    return kinds.pop()


def _local_mount(path, device):
    with open("/proc/self/mountinfo", "rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    return _mount_kind(raw, path, f"{os.major(device)}:{os.minor(device)}")


class _Root:
    def __init__(self, ledger, stack):
        self.ledger = ledger
        self.stack = stack
        self.bindings = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self.fd = _fd(stack, os.open("/", flags))
        for component in ledger.root.split("/")[1:]:
            parent = self.fd
            self.fd = _fd(stack, os.open(component, flags, dir_fd=parent))
            self.bindings.append((parent, component, self.fd))
        self.validate()
        _local_mount(ledger.root, ledger.proof.device)

    def validate(self):
        for parent, component, held in self.bindings:
            linked = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(linked.st_mode) or _identity(linked) != _identity(os.fstat(held)):
                raise LedgerError("PADRE_ESTADO_CAMBIADO")
        info = os.fstat(self.fd)
        proof = self.ledger.proof
        if (
            _identity(info) != (proof.device, proof.inode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise LedgerError("RAIZ_ESTADO_NO_ACREDITADA")
        with ExitStack() as marker_stack:
            descriptor = _fd(
                marker_stack,
                os.open(
                    proof.marker_name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=self.fd,
                ),
            )
            before = os.fstat(descriptor)
            _regular(before, proof.device, 65536)
            data = os.read(descriptor, 65537)
            after = os.fstat(descriptor)
            linked = os.stat(proof.marker_name, dir_fd=self.fd, follow_symlinks=False)
            _regular(linked, proof.device, 65536)
            if (
                _identity(linked) != _identity(before)
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or len(data) != before.st_size
                or hashlib.sha256(data).hexdigest() != proof.marker_sha256
            ):
                raise LedgerError("MARCADOR_ESTADO_CAMBIADO")


class _Locked:
    def __init__(self, ledger, root, stack, *, create, wait_seconds, clock, sleep):
        self.ledger, self.root, self.stack = ledger, root, stack
        self.db_fd = None
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            self.lock_fd = _fd(stack, os.open(LOCK_NAME, flags, dir_fd=root.fd))
        except FileNotFoundError:
            if not create:
                raise LedgerError("FALTA_LOCK_PROPIO") from None
            self.lock_fd = _fd(
                stack, os.open(LOCK_NAME, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root.fd)
            )
            offset = 0
            while offset < len(LOCK_BYTES):
                written = os.write(self.lock_fd, LOCK_BYTES[offset:])
                if written <= 0:
                    raise LedgerError("LOCK_NO_INICIALIZADO") from None
                offset += written
            os.fsync(self.lock_fd)
            os.fsync(root.fd)
        self.lock_identity = _identity(os.fstat(self.lock_fd))
        self.validate()
        self._acquire(wait_seconds, clock, sleep)

    def _acquire(self, wait_seconds, clock, sleep):
        start = previous = clock()
        if type(start) not in (int, float) or not math.isfinite(start):
            raise LedgerError("RELOJ_INVALIDO")
        # También termina con un reloj/sleeper inyectado que nunca avanza.
        attempts = math.ceil(wait_seconds / 0.01) + 2
        for attempt in range(attempts):
            now = clock()
            if type(now) not in (int, float) or not math.isfinite(now) or now < previous:
                raise LedgerError("RELOJ_INVALIDO")
            previous = now
            if attempt and now >= start + wait_seconds:
                break
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                remaining = start + wait_seconds - now
                if remaining <= 0:
                    break
                sleep(min(0.01, remaining))
        raise LedgerError("LOCK_TIMEOUT")

    def validate(self):
        self.root.validate()
        for descriptor, name, identity, limit in (
            (self.lock_fd, LOCK_NAME, self.lock_identity, len(LOCK_BYTES)),
            (self.db_fd, DB_NAME, None, self.ledger.max_db_bytes),
        ):
            if descriptor is None:
                continue
            held = os.fstat(descriptor)
            linked = os.stat(name, dir_fd=self.root.fd, follow_symlinks=False)
            _regular(held, self.ledger.proof.device, limit)
            _regular(linked, self.ledger.proof.device, limit)
            if _identity(linked) != _identity(held) or (
                identity is not None and _identity(held) != identity
            ):
                raise LedgerError("ARCHIVO_ESTADO_CAMBIADO")
        os.lseek(self.lock_fd, 0, os.SEEK_SET)
        if os.read(self.lock_fd, len(LOCK_BYTES) + 1) != LOCK_BYTES:
            raise LedgerError("LOCK_AJENO")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                info = os.stat(DB_NAME + suffix, dir_fd=self.root.fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if suffix != "-journal":
                raise LedgerError("SIDECAR_NO_PERMITIDO")
            _regular(info, self.ledger.proof.device, self.ledger.max_db_bytes + 65536)

    def open_db_fd(self, *, create):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if create:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        self.db_fd = _fd(self.stack, os.open(DB_NAME, flags, 0o600, dir_fd=self.root.fd))
        self.validate()


@dataclass(frozen=True)
class Reservation:
    state: str
    is_new: bool
    receipt: Publication | None


class Ledger:
    def __init__(self, root, proof: RootProof, *, max_db_bytes=MAX_DB_BYTES):
        if (
            type(proof) is not RootProof
            or type(max_db_bytes) is not int
            or not 4 * PAGE_SIZE <= max_db_bytes <= MAX_DB_BYTES
        ):
            raise LedgerError("CONFIGURACION_INVALIDA")
        path = os.fspath(root)
        if (
            type(path) is not str
            or not path.startswith("/")
            or path.startswith("//")
            or "\x00" in path
            or any(part in ("", ".", "..") for part in path.split("/")[1:])
        ):
            raise LedgerError("RUTA_ESTADO_INVALIDA")
        if fcntl is None:
            raise LedgerError("REQUIERE_LINUX")
        self.root, self.proof, self.max_db_bytes = path, proof, max_db_bytes
        try:
            with ExitStack() as stack:
                _Root(self, stack)
        except (OSError, UnicodeError):
            raise LedgerError("RAIZ_ESTADO_NO_ACREDITADA") from None

    @contextmanager
    def _locked(self, *, create=False, wait_seconds=0.0, clock=time.monotonic, sleep=time.sleep):
        with ExitStack() as stack:
            try:
                root = _Root(self, stack)
                if create and os.path.lexists(os.path.join(self.root, DB_NAME)):
                    raise LedgerError("DB_YA_EXISTE")
                state = _Locked(
                    self,
                    root,
                    stack,
                    create=create,
                    wait_seconds=wait_seconds,
                    clock=clock,
                    sleep=sleep,
                )
                state.validate()
            except (OSError, UnicodeError):
                raise LedgerError("ESTADO_NO_ACREDITADO") from None
            yield state

    def _connect(self, state, *, initialize=False):
        state.validate()
        # SQLite abre su ruta propia: no se falsea el directorio del journal con /proc/fd.
        from pathlib import Path

        connection = sqlite3.connect(
            Path(self.root, DB_NAME).as_uri() + "?mode=rw",
            uri=True,
            timeout=BUSY_SECONDS,
            isolation_level=None,
        )
        try:
            connection.enable_load_extension(False)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute(f"PRAGMA busy_timeout={int(BUSY_SECONDS * 1000)}")
            if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                raise LedgerError("JOURNAL_NO_PERMITIDO")
            connection.execute("PRAGMA synchronous=FULL")
            if connection.execute("PRAGMA synchronous").fetchone() != (2,) or connection.execute(
                "PRAGMA trusted_schema"
            ).fetchone() != (0,):
                raise LedgerError("PRAGMAS_NO_ACREDITADOS")
            if initialize:
                connection.execute(f"PRAGMA page_size={PAGE_SIZE}")
            if connection.execute("PRAGMA page_size").fetchone() != (PAGE_SIZE,):
                raise LedgerError("PAGINA_NO_PERMITIDA")
            pages = self.max_db_bytes // PAGE_SIZE
            if connection.execute(f"PRAGMA max_page_count={pages}").fetchone() != (pages,):
                raise LedgerError("LIMITE_DB_NO_ACREDITADO")
            state.validate()
            return connection
        except BaseException:
            connection.close()
            raise

    def check(self):
        """Comprueba un estado existente sin reservar trabajos ni crear archivos.

        LOCK_TIMEOUT indica una operación activa; el servidor debe distinguirlo
        de corrupción/configuración y no reiniciar un trabajo sano por estar ocupado.
        """
        try:
            with self._locked() as state:
                state.open_db_fd(create=False)
                connection = self._connect(state)
                try:
                    self._schema(connection)
                    state.validate()
                finally:
                    connection.close()
        except (OSError, sqlite3.Error, UnicodeError):
            raise LedgerError("COMPROBACION_LEDGER_FALLIDA") from None

    def initialize(self):
        try:
            with self._locked(create=True) as state:
                state.open_db_fd(create=True)
                connection = self._connect(state, initialize=True)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(META_SQL)
                    connection.execute(JOBS_SQL)
                    connection.execute("INSERT INTO ledger_meta VALUES (1,1,?)", (OWNER,))
                    connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    connection.execute("PRAGMA user_version=1")
                    state.validate()
                    connection.execute("COMMIT")
                    self._schema(connection)
                    state.validate()
                    os.fsync(state.root.fd)
                finally:
                    connection.close()
        except (OSError, sqlite3.Error):
            raise LedgerError("INICIALIZACION_FALLIDA") from None

    def _schema(self, connection):
        schema = dict(
            connection.execute("SELECT name,sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'")
        )
        if (
            schema != {"ledger_meta": META_SQL, "jobs": JOBS_SQL}
            or connection.execute("SELECT id,schema_version,owner FROM ledger_meta").fetchall()
            != [(1, 1, OWNER)]
            or connection.execute("PRAGMA application_id").fetchone() != (APPLICATION_ID,)
            or connection.execute("PRAGMA user_version").fetchone() != (1,)
            or connection.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]
        ):
            raise LedgerError("ESQUEMA_U_OWNER_AJENO")

    @contextmanager
    def operation(
        self,
        value,
        fingerprint,
        *,
        max_object_bytes,
        wait_seconds=0.0,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        job = validate_operation(value, fingerprint, max_object_bytes, wait_seconds)
        try:
            with self._locked(wait_seconds=wait_seconds, clock=clock, sleep=sleep) as state:
                state.open_db_fd(create=False)
                connection = self._connect(state)
                operation = None
                try:
                    self._schema(connection)
                    operation = _Operation(state, connection, job, fingerprint)
                    yield operation
                    state.validate()
                finally:
                    if operation is not None:
                        operation.active = False
                    connection.close()
        except (OSError, sqlite3.Error, UnicodeError):
            raise LedgerError("OPERACION_LEDGER_FALLIDA") from None


class _Operation:
    def __init__(self, state, connection, job, fingerprint):
        self.state, self.connection, self.job, self.fingerprint = (
            state,
            connection,
            job,
            fingerprint,
        )
        self.active = True
        self.reservation = None
        self.recovery = False
        self.reconciled = None

    def _check(self):
        if not self.active:
            raise LedgerError("OPERACION_CERRADA")
        self.state.validate()

    @contextmanager
    def _transaction(self):
        self._check()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._check()
            self.connection.execute("COMMIT")
            self._check()
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def reserve(self) -> Reservation:
        self._check()
        if self.reservation is not None:
            return self.reservation
        with self._transaction():
            row = self.connection.execute(
                "SELECT fingerprint,state,receipt FROM jobs WHERE job_id=?", (self.job.job_id,)
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO jobs VALUES (?,?,'pending',NULL)",
                    (self.job.job_id, self.fingerprint),
                )
                result = Reservation("pending", True, None)
            else:
                digest, status, receipt = row
                if digest != self.fingerprint:
                    raise LedgerError("JOB_FINGERPRINT_CONFLICTO")
                if status == "pending" and receipt is None:
                    self.recovery = True
                    result = Reservation("pending", False, None)
                elif status == "completed":
                    result = Reservation("completed", False, _parse_receipt(receipt, self.job))
                else:
                    raise LedgerError("REGISTRO_INVALIDO")
        # Sólo se devuelve después del COMMIT FULL; no se mantiene transacción en descarga.
        self.reservation = result
        return result

    def reconcile(self, publication) -> Publication:
        self._check()
        receipt = validate_publication(publication, self.job)
        if self.reservation is None or self.reservation.state != "pending" or not self.recovery:
            raise LedgerError("RECONCILIACION_NO_PROCEDE")
        self.reconciled = receipt
        return receipt

    def complete(self, publication) -> Publication:
        self._check()
        receipt = validate_publication(publication, self.job)
        if self.reservation is None:
            raise LedgerError("FALTA_RESERVA")
        if self.reservation.state == "completed":
            if receipt != self.reservation.receipt:
                raise LedgerError("COMPLETED_INMUTABLE")
            return self.reservation.receipt
        if self.recovery and receipt != self.reconciled:
            raise LedgerError("FALTA_RECONCILIACION")
        serialized = _receipt_json(receipt)
        with self._transaction():
            changed = self.connection.execute(
                "UPDATE jobs SET state='completed',receipt=? WHERE job_id=? AND fingerprint=? AND state='pending' AND receipt IS NULL",
                (serialized, self.job.job_id, self.fingerprint),
            ).rowcount
            if changed != 1:
                raise LedgerError("TRANSICION_CONFLICTIVA")
        self.reservation = Reservation("completed", self.reservation.is_new, receipt)
        return receipt
