"""Aprovisiona una orden verificada en un filesystem Linux con rename exclusivo.

El llamador entrega el resultado original de verificar HMAC tras reservar el
trabajo. ProjectsProof procede del operador, nunca de la solicitud. El proceso
que publica en el NAS exige un directorio sin escritores ajenos al servicio.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import subprocess
from contextlib import ExitStack
from dataclasses import asdict, dataclass

from sync_protocol import VerifiedRequest
from sync_storage import (
    AssetJob,
    StorageError,
    SyncStorage,
    _Anchors,
    _file_snapshot,
    _hash_fd,
    _identity,
    _own_fd,
    validate_job,
)

MARKER = ".agency-sync-project.json"
RECONCILE_MARKER = ".agency-sync-project-v2.tmp"
RAW = "01_raw"
OWNER = "agencia-digital-marketing-ai:r2-sync-project:v1"
RENAME_NOREPLACE = 1


@dataclass(frozen=True)
class ProjectsProof:
    device: int
    inode: int
    uid: int
    gid: int
    synology_acl: bool = False

    def __post_init__(self):
        if (
            any(
                type(value) is not int or value < 0
                for value in (self.device, self.inode, self.uid, self.gid)
            )
            or self.inode == 0
            or type(self.synology_acl) is not bool
        ):
            raise StorageError("PRUEBA_PROYECTOS_INVALIDA")


@dataclass(frozen=True)
class ProjectDirectory:
    device: int
    inode: int
    created: bool


def _authorized(verified):
    if (
        type(verified) is not VerifiedRequest
        or type(verified.job) is not AssetJob
        or verified.protocol not in ("v1", "v2")
        or type(verified.job.job_id) is not str
        or verified.job.project_slug != "job-" + verified.job.job_id.replace("-", "")
    ):
        raise StorageError("ORDEN_APROVISIONAMIENTO_INVALIDA")
    return verified.job


def _stat_directory(fd, proof):
    info = os.fstat(fd)
    if (
        info.st_dev != proof.device
        or info.st_uid != proof.uid
        or info.st_gid != proof.gid
        or (not proof.synology_acl and stat.S_IMODE(info.st_mode) & 0o022)
    ):
        raise StorageError("DIRECTORIO_PROYECTO_NO_ACREDITADO")
    if proof.synology_acl:
        _verify_acl(fd)
    return info


def _verify_acl(fd, *, directory=True):
    # DSM proyecta los permisos efectivos del UID en stat; se cotejan las ACE por descriptor.
    try:
        result = subprocess.run(
            ["/usr/syno/bin/synoacltool", "-get", f"/proc/{os.getpid()}/fd/{fd}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise StorageError("ACL_PROYECTO_NO_ACREDITADA") from None
    entries = [
        line.strip() for line in result.stdout.splitlines() if line.lstrip().startswith(b"[")
    ]
    grants = []
    for entry in entries:
        prefix, separator, rule = entry.partition(b"] ")
        if not separator or not prefix[1:].isdigit():
            raise StorageError("ACL_PROYECTO_NO_ACREDITADA")
        grants.append(rule.split(b" (level:", 1)[0])
    flags = b"fd--" if directory else b"----"
    if sorted(grants) != [
        b"user:admin:allow:rwxpdDaARWcCo:" + flags,
        b"user:jandrade:allow:rwxpdDaARWcCo:" + flags,
    ]:
        raise StorageError("ACL_PROYECTO_NO_ACREDITADA")


def _exists(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _marker_body(job, fingerprint, info):
    return json.dumps(
        {
            "schema": 1,
            "owner": OWNER,
            "job_id": job.job_id,
            "fingerprint": fingerprint,
            "device": info.st_dev,
            "inode": info.st_ino,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _write_marker(fd, body, *, name=MARKER):
    with ExitStack() as stack:
        writer = _own_fd(
            stack,
            os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=fd,
            ),
        )
        pending = memoryview(body)
        while pending:
            length = os.write(writer, pending)
            if length <= 0:
                raise StorageError("MARCADOR_PROYECTO_INCOMPLETO")
            pending = pending[length:]
        os.fsync(writer)
    os.fsync(fd)


def _verify_marker(fd, body, proof, *, name=MARKER, partial=False):
    with ExitStack() as stack:
        marker = _own_fd(
            stack,
            os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=fd,
            ),
        )
        info = os.fstat(marker)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_dev != proof.device
            or info.st_uid != proof.uid
            or info.st_gid != proof.gid
            or info.st_nlink != 1
            or (not proof.synology_acl and stat.S_IMODE(info.st_mode) != 0o600)
            or info.st_size > len(body)
            or (not partial and info.st_size != len(body))
        ):
            raise StorageError("MARCADOR_PROYECTO_INVALIDO")
        if proof.synology_acl:
            _verify_acl(marker, directory=False)
        expected = hashlib.sha256(body[: info.st_size]).hexdigest(), info.st_size
        if (
            _hash_fd(marker, len(body)) != expected
            or _file_snapshot(info)
            != _file_snapshot(os.stat(name, dir_fd=fd, follow_symlinks=False))
            or _file_snapshot(info) != _file_snapshot(os.fstat(marker))
        ):
            raise StorageError("MARCADOR_PROYECTO_INVALIDO")


def _discard_reconcile_marker(fd, body, proof):
    if _exists(fd, RECONCILE_MARKER):
        # Una caída puede dejar el prefijo que alcanzó a escribir el conciliador.
        _verify_marker(fd, body, proof, name=RECONCILE_MARKER, partial=True)
        os.unlink(RECONCILE_MARKER, dir_fd=fd)


def _rename_noreplace(parent, source, destination):
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise StorageError("RENOMBRE_EXCLUSIVO_NO_SOPORTADO") from None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        parent, source.encode("ascii"), parent, destination.encode("ascii"), RENAME_NOREPLACE
    ):
        code = ctypes.get_errno()
        if code in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise StorageError("RENOMBRE_EXCLUSIVO_NO_SOPORTADO")
        raise OSError(code, "renameat2")


def _read_tree(
    anchors, parent, name, job, fingerprint, proof, *, staged=False, fresh=False, legacy=None
):
    fd = anchors.open_directory(parent, name)
    info = _stat_directory(fd, proof)
    body = _marker_body(job, fingerprint, info)
    anchors.validate()
    if fresh:
        _write_marker(fd, body)
    previous_body = None
    try:
        _verify_marker(fd, body, proof)
    except StorageError:
        if legacy is None:
            raise
        previous_body = _marker_body(job, legacy, info)
        _verify_marker(fd, previous_body, proof)
    if staged:
        allowed = {MARKER, RAW}
        if previous_body is not None:
            allowed.add(RECONCILE_MARKER)
        with os.scandir(fd) as files:
            if any(item.name not in allowed for item in files):
                raise StorageError("STAGE_PROYECTO_INESPERADO")
        if not _exists(fd, RAW):
            anchors.validate()
            try:
                os.mkdir(RAW, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
    raw = anchors.open_directory(fd, RAW)
    _stat_directory(raw, proof)
    if staged:
        with os.scandir(raw) as files:
            if next(files, None) is not None:
                raise StorageError("STAGE_PROYECTO_INESPERADO")
        os.fsync(raw)
        os.fsync(fd)
    anchors.validate()
    if previous_body is not None:
        _discard_reconcile_marker(fd, body, proof)
        try:
            _write_marker(fd, body, name=RECONCILE_MARKER)
            _verify_marker(fd, body, proof, name=RECONCILE_MARKER)
            anchors.validate()
            _verify_marker(fd, previous_body, proof)
            os.replace(RECONCILE_MARKER, MARKER, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            _discard_reconcile_marker(fd, body, proof)
    _verify_marker(fd, body, proof)
    return info


def _projects(stack, storage, proof, filesystem_id):
    anchors = _Anchors(stack, storage.media_root, storage.proof)
    if filesystem_id is not None and os.fstatvfs(anchors.root_fd).f_fsid != filesystem_id:
        raise StorageError("FILESYSTEM_PROYECTOS_DISTINTO")
    parent = anchors.open_directory(anchors.root_fd, "proyectos")
    if _identity(_stat_directory(parent, proof)) != (proof.device, proof.inode):
        raise StorageError("IDENTIDAD_PROYECTOS_DISTINTA")
    anchors.validate()
    return anchors, parent


def ensure_project(
    storage: SyncStorage,
    proof: ProjectsProof,
    verified: VerifiedRequest,
    *,
    filesystem_id=None,
    reconcile_legacy_v2=False,
):
    """Prepara sólo un slug del portal, con igual inode para los reintentos propios.

    El llamador obtiene `verified` de `verify_request` y reserva antes en el
    ledger. El tipo Python se valida aquí, pero no sustituye a la autenticación.
    Un stage sin marcador válido requiere conciliación y queda preservado.
    """
    job = _authorized(verified)
    if reconcile_legacy_v2 and verified.protocol != "v2":
        raise StorageError("ORDEN_APROVISIONAMIENTO_INVALIDA")
    if type(storage) is not SyncStorage or type(proof) is not ProjectsProof:
        raise StorageError("CONFIGURACION_APROVISIONAMIENTO_INVALIDA")
    if filesystem_id is not None and (
        type(filesystem_id) is not int or not 0 < filesystem_id < 2**64
    ):
        raise StorageError("PRUEBA_FILESYSTEM_INVALIDA")
    job = validate_job(asdict(job), storage.max_object_bytes)
    body = json.dumps(asdict(job), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fingerprint = hashlib.sha256(body).hexdigest()
    if fingerprint != verified.fingerprint:
        raise StorageError("ORDEN_APROVISIONAMIENTO_INVALIDA")
    # Cada material se autentica por separado. En v2 la carpeta pertenece a la orden.
    project_fingerprint = (
        hashlib.sha256(f"studio-nas-project-v2\n{job.job_id}".encode("ascii")).hexdigest()
        if verified.protocol == "v2"
        else fingerprint
    )
    final = job.project_slug
    stage = f".agency-sync-project-{job.job_id}.staging"
    try:
        with ExitStack() as stack:
            anchors, parent = _projects(stack, storage, proof, filesystem_id)
            if _exists(parent, final):
                info = _read_tree(
                    anchors,
                    parent,
                    final,
                    job,
                    project_fingerprint,
                    proof,
                    legacy=fingerprint if reconcile_legacy_v2 else None,
                )
                os.fsync(parent)
                anchors.validate()
                return ProjectDirectory(*_identity(info), created=False)
            anchors.validate()
            if reconcile_legacy_v2:
                if not _exists(parent, stage):
                    raise StorageError("PROYECTO_V2_AUSENTE")
                fresh = False
            else:
                try:
                    os.mkdir(stage, 0o700, dir_fd=parent)
                    fresh = True
                except FileExistsError:
                    fresh = False
            info = _read_tree(
                anchors,
                parent,
                stage,
                job,
                project_fingerprint,
                proof,
                staged=True,
                fresh=fresh,
                legacy=fingerprint if reconcile_legacy_v2 else None,
            )
            os.fsync(parent)
            anchors.validate()
            try:
                _rename_noreplace(parent, stage, final)
            except OSError:
                if not _exists(parent, final):
                    raise
            # Tras un rename, comprobar las nuevas vinculaciones por FDs nuevos;
            # conservar el stage anterior abierto para cotejar el inode esperado.
            after, current_parent = _projects(stack, storage, proof, filesystem_id)
            current = _read_tree(after, current_parent, final, job, project_fingerprint, proof)
            if _identity(current) != _identity(info):
                raise StorageError("DESTINO_PROYECTO_DISTINTO")
            os.fsync(current_parent)
            after.validate()
            return ProjectDirectory(*_identity(current), created=True)
    except OSError:
        raise StorageError("APROVISIONAMIENTO_NO_CONCILIADO") from None
