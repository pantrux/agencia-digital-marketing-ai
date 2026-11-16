"""Publicación Linux por descriptores; no implementa HTTP, descargas ni ledger."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass

MAX_SAFE_INTEGER = 2**53 - 1
MAX_MARKER_BYTES = 64 * 1024
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
EXTENSION = re.compile(r"[a-z0-9]{1,10}")
SHA256 = re.compile(r"[0-9a-f]{64}")
MARKER = re.compile(r"[A-Za-z0-9._-]{1,128}")
DOS_NAME = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", re.IGNORECASE)
JOB_FIELDS = frozenset(
    ("job_id", "asset_id", "project_slug", "extension", "bytes", "sha256", "object_key")
)


class StorageError(ValueError):
    """Error saneado; published exige reconciliar un destino que nunca se borra."""

    def __init__(self, code: str, *, published: bool = False):
        super().__init__(code)
        self.code = code
        self.published = published


def _positive_safe(value: object) -> bool:
    return type(value) is int and 0 < value <= MAX_SAFE_INTEGER


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class RootProof:
    """Identidad y marcador obtenidos previamente por un operador de confianza."""

    device: int
    inode: int
    marker_name: str
    marker_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.device) is not int
            or self.device < 0
            or type(self.inode) is not int
            or self.inode <= 0
            or not _matches(MARKER, self.marker_name)
            or self.marker_name in (".", "..")
            or self.marker_name.endswith(".")
            or DOS_NAME.fullmatch(self.marker_name.split(".", 1)[0])
            or not _matches(SHA256, self.marker_sha256)
        ):
            raise StorageError("PRUEBA_RAIZ_INVALIDA")


@dataclass(frozen=True)
class AssetJob:
    job_id: str
    asset_id: str
    project_slug: str
    extension: str
    bytes: int
    sha256: str
    object_key: str

    @property
    def filename(self) -> str:
        return f"{self.asset_id}.{self.extension}"

    @property
    def relative_path(self) -> str:
        return f"proyectos/{self.project_slug}/01_raw/{self.filename}"


def validate_job(value: object, max_object_bytes: int) -> AssetJob:
    """Captura inmutable de los siete campos del contrato Edge, sin normalizar."""
    if not _positive_safe(max_object_bytes):
        raise StorageError("LIMITE_OBLIGATORIO_INVALIDO")
    if type(value) is not dict or frozenset(value) != JOB_FIELDS:
        raise StorageError("ESQUEMA_JOB_INVALIDO")
    snapshot = dict(value)
    if (
        not _matches(UUID4, snapshot["job_id"])
        or not _matches(UUID4, snapshot["asset_id"])
        or not _matches(SLUG, snapshot["project_slug"])
        or DOS_NAME.fullmatch(snapshot["project_slug"])
        or not _matches(EXTENSION, snapshot["extension"])
        or not _positive_safe(snapshot["bytes"])
        or snapshot["bytes"] > max_object_bytes
        or not _matches(SHA256, snapshot["sha256"])
        or type(snapshot["object_key"]) is not str
        or snapshot["object_key"]
        != f"ingest/{snapshot['job_id']}/{snapshot['asset_id']}.{snapshot['extension']}"
    ):
        raise StorageError("JOB_INVALIDO")
    return AssetJob(**snapshot)


@dataclass(frozen=True)
class Publication:
    job_id: str
    asset_id: str
    relative_path: str
    bytes: int
    sha256: str
    device: int
    inode: int
    created: bool
    idempotent: bool


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _file_snapshot(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (*_identity(info), info.st_size, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)


def _own_fd(stack: ExitStack, fd: int) -> int:
    stack.callback(os.close, fd)
    return fd


def _regular(info: os.stat_result, *, device: int) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_dev == device


class _Anchors:
    """Todos los padres quedan abiertos y sus enlaces se vuelven a comprobar."""

    def __init__(self, stack: ExitStack, root: str, proof: RootProof):
        self.stack = stack
        self.proof = proof
        self.bindings: list[tuple[int, str, int]] = []
        self.flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self.root_fd = _own_fd(stack, os.open("/", self.flags))
        for component in root.split("/")[1:]:
            self.root_fd = self.open_directory(self.root_fd, component, media_child=False)
        if _identity(os.fstat(self.root_fd)) != (proof.device, proof.inode):
            raise StorageError("IDENTIDAD_RAIZ_DISTINTA")
        self.validate()

    def open_directory(self, parent: int, name: str, *, media_child: bool = True) -> int:
        child = _own_fd(self.stack, os.open(name, self.flags, dir_fd=parent))
        if media_child and os.fstat(child).st_dev != self.proof.device:
            raise StorageError("FILESYSTEM_DISTINTO")
        self.bindings.append((parent, name, child))
        return child

    def validate(self) -> None:
        for parent, name, child in self.bindings:
            linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            held = os.fstat(child)
            if not stat.S_ISDIR(linked.st_mode) or _identity(linked) != _identity(held):
                raise StorageError("PADRE_CAMBIADO")
        if _identity(os.fstat(self.root_fd)) != (self.proof.device, self.proof.inode):
            raise StorageError("IDENTIDAD_RAIZ_DISTINTA")
        self._validate_marker()

    def _validate_marker(self) -> None:
        with ExitStack() as marker_stack:
            fd = _own_fd(
                marker_stack,
                os.open(
                    self.proof.marker_name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=self.root_fd,
                ),
            )
            before = os.fstat(fd)
            if not _regular(before, device=self.proof.device) or before.st_size > MAX_MARKER_BYTES:
                raise StorageError("MARCADOR_INVALIDO")
            digest, size = _hash_fd(fd, MAX_MARKER_BYTES)
            linked = os.stat(self.proof.marker_name, dir_fd=self.root_fd, follow_symlinks=False)
            if (
                _file_snapshot(before) != _file_snapshot(os.fstat(fd))
                or _identity(linked) != _identity(before)
                or not stat.S_ISREG(linked.st_mode)
                or size != before.st_size
                or digest != self.proof.marker_sha256
            ):
                raise StorageError("MARCADOR_CAMBIADO")


def _hash_fd(fd: int, limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(fd, min(1024 * 1024, limit - size + 1))
        if not chunk:
            return digest.hexdigest(), size
        size += len(chunk)
        if size > limit:
            raise StorageError("ARCHIVO_SUPERA_LIMITE")
        digest.update(chunk)


class SyncStorage:
    """Sólo publica en un proyecto ya existente dentro de una raíz acreditada."""

    def __init__(
        self, media_root: str | os.PathLike[str], proof: RootProof, *, max_object_bytes: int
    ):
        if not _positive_safe(max_object_bytes) or type(proof) is not RootProof:
            raise StorageError("CONFIGURACION_INVALIDA")
        path = os.fspath(media_root)
        if (
            type(path) is not str
            or not path.startswith("/")
            or path.startswith("//")
            or any(part in ("", ".", "..") for part in path.split("/")[1:])
            or "\x00" in path
        ):
            raise StorageError("RUTA_RAIZ_INVALIDA")
        if not sys.platform.startswith("linux") or not all(
            hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
        ):
            raise StorageError("REQUIERE_LINUX")
        if (
            os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.link not in os.supports_dir_fd
            or os.link not in os.supports_follow_symlinks
            or not os.path.isdir("/proc/self/fd")
        ):
            raise StorageError("FALTAN_PRIMITIVAS_LINUX")
        self.media_root = path
        self.proof = proof
        self.max_object_bytes = max_object_bytes
        self.expected_project = None
        try:
            with ExitStack() as stack:
                _Anchors(stack, path, proof)
        except OSError:
            raise StorageError("RAIZ_NO_ACREDITADA") from None

    def require_project_inode(self, job: AssetJob, expected: int) -> None:
        with ExitStack() as stack:
            anchors = _Anchors(stack, self.media_root, self.proof)
            projects = anchors.open_directory(anchors.root_fd, "proyectos")
            project = anchors.open_directory(projects, job.project_slug)
            if os.fstat(project).st_ino != expected:
                raise StorageError("IDENTIDAD_PROYECTO_DISTINTA")
            anchors.open_directory(project, "01_raw")
            anchors.validate()
        self.expected_project = (job.project_slug, expected)

    def publish(self, value: object, chunks: Iterable[bytes]) -> Publication:
        """El iterador no puede aportar rutas; una publicación nunca sobrescribe."""
        job = validate_job(value, self.max_object_bytes)
        published = False
        with ExitStack() as stack:
            partial: _Partial | None = None
            try:
                anchors = _Anchors(stack, self.media_root, self.proof)
                parent = anchors.root_fd
                for name in ("proyectos", job.project_slug, "01_raw"):
                    parent = anchors.open_directory(parent, name)
                    if name == job.project_slug and self.expected_project is not None:
                        if self.expected_project != (name, os.fstat(parent).st_ino):
                            raise StorageError("IDENTIDAD_PROYECTO_DISTINTA")
                anchors.validate()
                partial = _Partial(stack, parent, self.proof.device)
                self._write(partial.fd, chunks, job)
                anchors.validate()
                partial.validate()
                try:
                    # AT_SYMLINK_FOLLOW resuelve el FD retenido, nunca el nombre parcial.
                    os.link(
                        f"/proc/self/fd/{partial.fd}",
                        job.filename,
                        dst_dir_fd=parent,
                        follow_symlinks=True,
                    )
                    published = True
                except FileExistsError:
                    pass
                partial.cleanup()
                partial.close_writer()
                os.fsync(parent)
                anchors.validate()
                info = self._verify_destination(stack, parent, job)
                if published and _identity(info) != partial.identity:
                    raise StorageError("DESTINO_CAMBIADO")
                anchors.validate()
                return Publication(
                    job.job_id,
                    job.asset_id,
                    job.relative_path,
                    job.bytes,
                    job.sha256,
                    info.st_dev,
                    info.st_ino,
                    published,
                    not published,
                )
            except Exception as error:
                if partial is not None:
                    try:
                        partial.cleanup()
                    except (StorageError, OSError):
                        raise StorageError(
                            "PARCIAL_IDENTIDAD_CAMBIADA", published=published
                        ) from None
                code = error.code if isinstance(error, StorageError) else "PUBLICACION_FALLIDA"
                raise StorageError(code, published=published) from None
            finally:
                # También se limpia ante KeyboardInterrupt; nunca se busca otro archivo.
                if partial is not None and not partial.cleaned:
                    partial.cleanup_if_owned()

    def _write(self, fd: int, chunks: Iterable[bytes], job: AssetJob) -> None:
        digest = hashlib.sha256()
        size = 0
        for chunk in chunks:
            if type(chunk) is not bytes:
                raise StorageError("CHUNK_INVALIDO")
            if len(chunk) > job.bytes - size or len(chunk) > self.max_object_bytes - size:
                raise StorageError("OBJETO_SUPERA_LIMITE")
            remaining = memoryview(chunk)
            while remaining:
                try:
                    written = os.write(fd, remaining)
                except InterruptedError:
                    continue
                if written <= 0 or written > len(remaining):
                    raise StorageError("ESCRITURA_INCOMPLETA")
                digest.update(remaining[:written])
                size += written
                remaining = remaining[written:]
        if size != job.bytes or digest.hexdigest() != job.sha256:
            raise StorageError("CONTENIDO_NO_COINCIDE")
        os.fsync(fd)

    def _verify_destination(self, stack: ExitStack, raw_fd: int, job: AssetJob) -> os.stat_result:
        before_open = os.stat(job.filename, dir_fd=raw_fd, follow_symlinks=False)
        # NFS puede conservar nlink=2 tras retirar el parcial. La apertura revalida
        # atributos; el FD abierto debe acreditar un único enlace más abajo.
        if not stat.S_ISREG(before_open.st_mode) or before_open.st_dev != self.proof.device:
            raise StorageError("CONFLICTO_DESTINO")
        fd = _own_fd(
            stack,
            os.open(
                job.filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=raw_fd,
            ),
        )
        before = os.fstat(fd)
        if (
            not _regular(before, device=self.proof.device)
            or _identity(before) != _identity(before_open)
            or before.st_size != job.bytes
        ):
            raise StorageError("CONFLICTO_DESTINO")
        digest, size = _hash_fd(fd, job.bytes)
        after = os.fstat(fd)
        linked = os.stat(job.filename, dir_fd=raw_fd, follow_symlinks=False)
        if (
            _file_snapshot(before) != _file_snapshot(after)
            or _identity(linked) != _identity(after)
            or not _regular(linked, device=self.proof.device)
            or size != job.bytes
            or digest != job.sha256
        ):
            raise StorageError("CONFLICTO_DESTINO")
        return after


class _Partial:
    def __init__(self, stack: ExitStack, raw_fd: int, device: int):
        self.raw_fd = raw_fd
        self.device = device
        self.cleaned = False
        for _ in range(8):
            name = f".sync-{secrets.token_hex(16)}.partial"
            try:
                fd = os.open(
                    name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=raw_fd,
                )
            except FileExistsError:
                continue
            try:
                self.stream = os.fdopen(fd, "r+b", buffering=0)
            except BaseException:
                os.close(fd)
                raise
            # El objeto recuerda su cierre; no se vuelve a cerrar un número de FD
            # que el sistema pudiera reutilizar para leer el destino posteriormente.
            stack.callback(self.stream.close)
            self.fd = fd
            self.name = name
            self.identity = _identity(os.fstat(fd))
            self.validate()
            return
        raise StorageError("COLISION_PARCIALES")

    def close_writer(self) -> None:
        if not self.cleaned:
            raise StorageError("PARCIAL_NO_RETIRADO")
        self.stream.close()

    def validate(self) -> None:
        held = os.fstat(self.fd)
        linked = os.stat(self.name, dir_fd=self.raw_fd, follow_symlinks=False)
        if (
            not _regular(held, device=self.device)
            or not _regular(linked, device=self.device)
            or _identity(held) != self.identity
            or _identity(linked) != self.identity
        ):
            raise StorageError("PARCIAL_IDENTIDAD_CAMBIADA")

    def cleanup(self) -> None:
        if self.cleaned:
            return
        linked = os.stat(self.name, dir_fd=self.raw_fd, follow_symlinks=False)
        if not stat.S_ISREG(linked.st_mode) or _identity(linked) != self.identity:
            raise StorageError("PARCIAL_IDENTIDAD_CAMBIADA")
        # El nombre sigue ligado al FD propio; no se recorren ni borran otras rutas.
        os.unlink(self.name, dir_fd=self.raw_fd)
        self.cleaned = True

    def cleanup_if_owned(self) -> None:
        try:
            self.cleanup()
        except (StorageError, OSError):
            # Un nombre sustituido pertenece a otro actor y se conserva para revisión.
            pass
