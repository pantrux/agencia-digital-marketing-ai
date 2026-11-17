"""Gestiona entregables Edit y revisiones MP4 de proyectos propios en Zou."""

import hashlib
import http.client
import json
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

from common import PROJECT, UpstreamError, authorize_project, read_secret
from media_inspector import source_path

HOST = "192.168.0.85"
PORT = 8081
MEDIA_ROOT = Path("/mnt/producciones")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
SHA = re.compile(r"[0-9a-f]{64}")
MAX_RESPONSE = 2 * 1024**2
# El kit de producción admite 3 GiB. Reservar 1 MiB adicional para el multipart.
MAX_MEDIA = 3 * 1024**3
MAX_REQUEST = MAX_MEDIA + 1024**2
CHUNK = 256 * 1024
MAX_ROWS = 1000
PREVIEW_STATUSES = {"processing", "ready", "broken", "missing"}


def _pattern(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _require(condition, code="KITSU_RESPUESTA_INVALIDA"):
    if not condition:
        raise ValueError(code)


def _revision(value):
    return type(value) is int and 1 <= value <= 2_147_483_647


def _no_constant(_):
    raise ValueError("KITSU_JSON_INVALIDO")


def _unique_pairs(pairs):
    result = dict(pairs)
    _require(len(result) == len(pairs))
    return result


def _decode(data):
    return json.loads(data, object_pairs_hook=_unique_pairs, parse_constant=_no_constant)


def _request(token, route, *, body=None, multipart=None):
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Authorization": "Bearer " + token,
    }
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    if multipart is not None:
        payload, size, boundary = multipart
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        headers["Content-Length"] = str(size)
    method = "POST" if payload is not None else "GET"
    client = http.client.HTTPConnection(HOST, PORT, timeout=68 if multipart else 8)
    try:
        client.request(method, "/api" + route, body=payload, headers=headers)
        with client.getresponse() as response:
            if response.status != (201 if method == "POST" else 200):
                code = {
                    401: "KITSU_NO_AUTENTICADO",
                    403: "KITSU_NO_AUTORIZADO",
                    404: "KITSU_NO_ENCONTRADO",
                }.get(response.status, "KITSU_HTTP")
                raise UpstreamError(response.status, code)
            kind = response.getheader("Content-Type", "").split(";", 1)[0]
            _require(kind == "application/json")
            _require(response.getheader("Content-Encoding") in (None, "identity"))
            raw = response.read(MAX_RESPONSE + 1)
        _require(len(raw) <= MAX_RESPONSE)
        return _decode(raw.decode("utf-8"))
    except UpstreamError:
        raise
    except (OSError, http.client.HTTPException):
        raise UpstreamError(None, "KITSU_TRANSPORTE") from None
    except (ValueError, RecursionError):
        raise UpstreamError(None, "KITSU_RESPUESTA_INVALIDA") from None
    finally:
        client.close()


def _rows(value):
    _require(isinstance(value, list) and len(value) <= MAX_ROWS)
    _require(all(isinstance(row, dict) for row in value))
    return value


def _object(value):
    _require(isinstance(value, dict))
    return value


def _one(rows, match, code):
    selected = [row for row in rows if match(row)]
    _require(len(selected) <= 1, code)
    return selected[0] if selected else None


def _project_slug(project_id):
    _require(_pattern(project_id, UUID), "KITSU_ARGUMENTOS")
    raw = os.environ.get("KITSU_REVIEW_PROJECTS", "")
    try:
        _require(len(raw) <= 16 * 1024)
        projects = _decode(raw)
        _require(isinstance(projects, dict) and projects)
        _require(
            all(_pattern(key, UUID) and _pattern(value, PROJECT) for key, value in projects.items())
        )
        _require(len(set(projects.values())) == len(projects))
    except (ValueError, RecursionError):
        raise ValueError("KITSU_CONFIG_PROYECTOS") from None
    slug = projects.get(project_id)
    authorize_project(slug)
    return slug


def _configured_uuid(key):
    value = os.environ.get(key)
    _require(_pattern(value, UUID), "KITSU_CONFIG_IDENTIDAD")
    return value


def _project_access(token, project_id, *, write=False):
    bot = _configured_uuid("KITSU_REVIEW_BOT_ID")
    identity = _object(_request(token, "/auth/authenticated"))
    user = _object(identity.get("user"))
    _require(
        identity.get("authenticated") is True
        and user.get("id") == bot
        and user.get("is_bot") is True
        and user.get("role") == "user",
        "KITSU_BOT_NO_AUTORIZADO",
    )
    project = _object(_request(token, f"/data/projects/{project_id}"))
    _require(project.get("id") == project_id, "PROYECTO_NO_AUTORIZADO")
    if write:
        team = _rows(_request(token, f"/data/projects/{project_id}/team"))
        member = _one(team, lambda row: row.get("id") == bot, "KITSU_BOT_NO_AUTORIZADO")
        _require(
            member is not None and member.get("project_role") == "manager",
            "KITSU_BOT_NO_AUTORIZADO",
        )
    return bot


def _task_scope(token, project_id, task_id):
    task = _object(_request(token, f"/data/tasks/{task_id}"))
    edit_id = task.get("entity_id")
    _require(
        task.get("id") == task_id
        and task.get("project_id") == project_id
        and _pattern(edit_id, UUID)
        and _pattern(task.get("task_status_id"), UUID),
        "KITSU_TAREA_AJENA",
    )
    edit = _object(_request(token, f"/data/edits/{edit_id}"))
    _require(
        edit.get("id") == edit_id and edit.get("project_id") == project_id, "KITSU_TAREA_AJENA"
    )
    return task


def _previews(token, task_id):
    rows = _rows(_request(token, f"/data/tasks/{task_id}/previews"))
    _require(
        all(
            row.get("task_id") == task_id
            and _pattern(row.get("id"), UUID)
            and type(row.get("revision")) is int
            for row in rows
        )
    )
    _require(len({row["id"] for row in rows}) == len(rows))
    return rows


def _status(preview):
    status = preview.get("status")
    _require(isinstance(status, str) and status in PREVIEW_STATUSES)
    return status


def _annotation_object(row):
    result = {}
    kind = row.get("type")
    if _pattern(kind, re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,31}")):
        result["type"] = kind
    for key in ("left", "top", "width", "height", "scaleX", "scaleY", "angle"):
        value = row.get(key)
        if type(value) in (int, float) and -1_000_000 <= value <= 1_000_000:
            result[key] = value
    message = row.get("text")
    if (
        isinstance(message, str)
        and 0 < len(message) <= 500
        and all(
            not unicodedata.category(char).startswith("C") or char in "\n\t" for char in message
        )
    ):
        result["text"] = message
    return result


def _annotations(value):
    if value is None:
        return []
    result = []
    for row in _rows(value):
        time = row.get("time")
        if _pattern(time, re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,16})?")):
            time = float(time)
        _require(type(time) in (int, float) and 0 <= time <= 7 * 86400)
        objects = _rows(_object(row.get("drawing")).get("objects", []))
        result.append(
            {
                "time_seconds": time,
                "object_count": len(objects),
                "objects": [_annotation_object(obj) for obj in objects],
            }
        )
    return sorted(result, key=lambda item: item["time_seconds"])


def query_review(project_id: str, task_id: str, revision: int | None = None) -> dict:
    """Consulta IDs, estado y marcas de anotación de una tarea Edit propia."""
    _require(
        _pattern(task_id, UUID) and (revision is None or _revision(revision)), "KITSU_ARGUMENTOS"
    )
    _project_slug(project_id)
    token = read_secret("KITSU_API_TOKEN_FILE")
    _project_access(token, project_id)
    task = _task_scope(token, project_id, task_id)
    rows = _previews(token, task_id)
    if revision is not None:
        selected = _one(
            rows, lambda row: row["revision"] == revision, "KITSU_REVISION_EN_CONFLICTO"
        )
        rows = [selected] if selected else []
    previews = []
    for row in rows:
        previews.append(
            {
                "preview_id": row["id"],
                "revision": row["revision"],
                "status": _status(row),
                "annotations": _annotations(row.get("annotations")),
            }
        )
    return {
        "project_id": project_id,
        "task_id": task_id,
        "edit_id": task["entity_id"],
        "previews": previews,
    }


def _open_absolute_dir(stack, path):
    _require(
        path.is_absolute() and path.anchor == "/" and ".." not in path.parts,
        "KITSU_DIRECTORIO_INVALIDO",
    )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    stack.callback(os.close, fd)
    for part in path.parts[1:]:
        fd = os.open(part, flags, dir_fd=fd)
        stack.callback(os.close, fd)
    return fd


def _private_file(info):
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600
    )


@contextmanager
def _write_state():
    """Serializa los POST de una instalación y exige un directorio local privado."""
    _require(os.name == "posix", "KITSU_ESTADO_NO_COMPATIBLE")
    import fcntl

    with ExitStack() as stack:
        try:
            root = _open_absolute_dir(stack, Path(os.environ["KITSU_REVIEW_STATE_ROOT"]))
            info = os.fstat(root)
            _require(
                info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "KITSU_ESTADO_INVALIDO",
            )
            lock = os.open(
                ".writer.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=root,
            )
            stack.callback(os.close, lock)
            _require(_private_file(os.fstat(lock)), "KITSU_ESTADO_INVALIDO")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("KITSU_ESTADO_OCUPADO") from None
        except (OSError, KeyError):
            raise ValueError("KITSU_ESTADO_INVALIDO") from None
        yield root


def _read_intent(root, key, fingerprint):
    raw = (fingerprint + "\n").encode("ascii")
    try:
        fd = os.open(key + ".intent", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            _require(_private_file(info) and info.st_size == len(raw), "KITSU_ESTADO_INVALIDO")
            _require(stream.read(len(raw) + 1) == raw, "KITSU_INTENCION_DISTINTA")
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise ValueError("KITSU_ESTADO_INVALIDO") from None


def _record_intent(root, key, fingerprint):
    """Reserva durable antes del POST, sin reemplazar una intención previa."""
    name = key + ".intent"
    raw = (fingerprint + "\n").encode("ascii")
    try:
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600, dir_fd=root)
        except FileExistsError:
            _require(_read_intent(root, key, fingerprint), "KITSU_ESTADO_INVALIDO")
            return False
        with os.fdopen(fd, "wb") as stream:
            _require(_private_file(os.fstat(stream.fileno())), "KITSU_ESTADO_INVALIDO")
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(root)
        return True
    except OSError:
        raise ValueError("KITSU_ESTADO_INVALIDO") from None


def _digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _reconcile(root, key, parameters, lookup: Callable, send: Callable):
    """Reintenta sólo lecturas cuando el primer POST pudo haber llegado a Zou."""
    previous = lookup()
    if previous is not None:
        return previous
    _require(_record_intent(root, _digest(key), _digest(parameters)), "KITSU_ESCRITURA_PENDIENTE")
    try:
        send()
    except UpstreamError as error:
        if error.status in (401, 403):
            raise
    try:
        result = lookup()
    except UpstreamError:
        raise ValueError("KITSU_ESCRITURA_PENDIENTE") from None
    _require(result is not None, "KITSU_ESCRITURA_PENDIENTE")
    return result


def _own_comment(token, task_id, bot, revision):
    marker = f"agency-mcp-preview-v{revision}"
    comments = _rows(_request(token, f"/data/tasks/{task_id}/comments"))
    row = _one(comments, lambda item: item.get("text") == marker, "KITSU_REVISION_EN_CONFLICTO")
    if row is not None:
        _require(
            row.get("object_id") == task_id
            and row.get("person_id") == bot
            and row.get("editor_id") in (None, bot)
            and _pattern(row.get("id"), UUID),
            "KITSU_REVISION_EN_CONFLICTO",
        )
    return row


def _owned_preview(token, task_id, bot, revision, comment):
    row = _one(
        _previews(token, task_id),
        lambda item: item["revision"] == revision,
        "KITSU_REVISION_EN_CONFLICTO",
    )
    links = _rows(comment.get("previews")) if comment else []
    if not links and row is None:
        return None
    _require(
        row is not None and len(links) == 1 and row.get("person_id") == bot,
        "KITSU_REVISION_EN_CONFLICTO",
    )
    linked = links[0]
    _require(
        linked.get("id") == row["id"]
        and linked.get("task_id") == task_id
        and type(linked.get("revision")) is int
        and linked["revision"] == revision,
        "KITSU_REVISION_EN_CONFLICTO",
    )
    return row


def _preview_result(project_id, task, revision, comment, preview):
    return {
        "project_id": project_id,
        "task_id": task["id"],
        "edit_id": task["entity_id"],
        "revision": revision,
        "comment_id": comment["id"],
        "preview_id": preview["id"],
        "status": _status(preview),
    }


def _revision_args(project_id, task_id, revision):
    _require(_pattern(task_id, UUID) and _revision(revision), "KITSU_ARGUMENTOS")
    return _project_slug(project_id)


def create_preview(project_id: str, task_id: str, revision: int) -> dict:
    """Reserva una sola versión y reconcilia los dos POST de Zou por lectura."""
    _revision_args(project_id, task_id, revision)
    with _write_state() as root:
        token = read_secret("KITSU_API_TOKEN_FILE")
        bot = _project_access(token, project_id, write=True)
        task = _task_scope(token, project_id, task_id)
        key = (project_id, task_id, revision)

        def lookup_comment():
            comment = _own_comment(token, task_id, bot, revision)
            _owned_preview(token, task_id, bot, revision, comment)
            return comment

        comment = lookup_comment()
        if comment is None:
            automations = _rows(
                _request(token, f"/data/projects/{project_id}/settings/status-automations")
            )
            _require(not automations, "KITSU_PROYECTO_AUTOMATIZADO")
            body = {
                "task_status_id": task["task_status_id"],
                "comment": f"agency-mcp-preview-v{revision}",
            }
            comment = _reconcile(
                root,
                (*key, "comment"),
                (bot, body),
                lookup_comment,
                lambda: _request(token, f"/actions/tasks/{task_id}/comment", body=body),
            )

        def lookup_preview():
            latest = _own_comment(token, task_id, bot, revision)
            _require(
                latest is not None and latest["id"] == comment["id"], "KITSU_REVISION_EN_CONFLICTO"
            )
            return _owned_preview(token, task_id, bot, revision, latest)

        preview = _reconcile(
            root,
            (*key, "preview"),
            (bot, comment["id"]),
            lookup_preview,
            lambda: _request(
                token,
                f"/actions/tasks/{task_id}/comments/{comment['id']}/add-preview",
                body={"revision": revision},
            ),
        )
        return _preview_result(project_id, task, revision, comment, preview)


def _file_identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@contextmanager
def _media_file(path):
    """Abre el medio anclado al NAS sin seguir enlaces ni cruzar dispositivos."""
    with ExitStack() as stack:
        try:
            root = _open_absolute_dir(stack, MEDIA_ROOT)
            device = os.fstat(root).st_dev
            parent = root
            for part in path.parts[:-1]:
                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                stack.callback(os.close, parent)
                _require(os.fstat(parent).st_dev == device, "KITSU_MEDIO_INVALIDO")
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stream = stack.enter_context(os.fdopen(fd, "rb"))
            before = os.fstat(fd)
            _require(
                stat.S_ISREG(before.st_mode)
                and before.st_nlink == 1
                and before.st_dev == device
                and 32 <= before.st_size <= MAX_MEDIA,
                "KITSU_MEDIO_INVALIDO",
            )
            yield stream, before.st_size
            named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            _require(
                _file_identity(before) == _file_identity(os.fstat(fd)) == _file_identity(named),
                "KITSU_MEDIO_CAMBIO",
            )
        except OSError:
            raise ValueError("KITSU_MEDIO_INVALIDO") from None


def _spool(root):
    return tempfile.TemporaryFile(mode="w+b", dir=f"/proc/self/fd/{root}")


def _stage_upload(out, source, sha256, *, zip_file=False):
    boundary = sha256
    filename = f"kit-studio-{sha256}.zip" if zip_file else f"agency-{sha256}.mp4"
    content_type = "application/zip" if zip_file else "video/mp4"
    preamble = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
    ).encode("ascii")
    trailer = f"\r\n--{boundary}--\r\n".encode("ascii")
    delimiter = b"\r\n--" + boundary.encode("ascii")
    digest = hashlib.sha256()
    with _media_file(source) as (stream, size):
        length = size + len(preamble) + len(trailer)
        _require(length <= MAX_REQUEST, "KITSU_MEDIO_TAMANO")
        out.write(preamble)
        remaining = size
        block = stream.read(min(CHUNK, remaining))
        valid = block.startswith(b"PK\x03\x04") if zip_file else block[4:8] == b"ftyp"
        _require(len(block) >= 32 and valid, "KITSU_MEDIO_FORMATO")
        tail = b"\r\n"
        while block:
            _require(delimiter not in tail + block, "KITSU_MEDIO_FORMATO")
            tail = (tail + block)[-len(delimiter) :]
            remaining -= len(block)
            digest.update(block)
            out.write(block)
            block = stream.read(min(CHUNK, remaining))
        _require(
            remaining == 0 and not stream.read(1) and digest.hexdigest() == sha256,
            "KITSU_MEDIO_HUELLA",
        )
    out.write(trailer)
    out.seek(0)
    return out, length, boundary


def _attachment_digest(token, attachment_id, filename, expected_bytes):
    client = http.client.HTTPConnection(HOST, PORT, timeout=68)
    try:
        client.request(
            "GET",
            f"/api/data/attachment-files/{attachment_id}/file/{filename}",
            headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
        )
        with client.getresponse() as response:
            _require(
                response.status == 200
                and response.getheader("Content-Type", "").split(";", 1)[0] == "application/zip"
                and response.getheader("Content-Encoding") in (None, "identity")
                and response.getheader("Content-Length") == str(expected_bytes),
                "KITSU_ADJUNTO_HUELLA",
            )
            digest = hashlib.sha256()
            size = 0
            while block := response.read(CHUNK):
                size += len(block)
                _require(size <= expected_bytes, "KITSU_ADJUNTO_HUELLA")
                digest.update(block)
            return digest.hexdigest(), size
    except (OSError, http.client.HTTPException):
        raise UpstreamError(None, "KITSU_TRANSPORTE") from None
    finally:
        client.close()


def attach_delivery_zip(
    project_id: str, task_id: str, revision: int, path: str, sha256: str
) -> dict:
    """Reconcilia el ZIP en el comentario propio y verifica su descarga."""
    slug = _revision_args(project_id, task_id, revision)
    _require(_pattern(sha256, SHA), "KITSU_ARGUMENTOS")
    name = f"kit-studio-{sha256}.zip"
    _require(path == f"proyectos/{slug}/05_revision/{name}", "RUTA_MEDIO_INVALIDA")
    source = PurePosixPath(path)
    with _write_state() as root:
        token = read_secret("KITSU_API_TOKEN_FILE")
        bot = _project_access(token, project_id, write=True)
        _task_scope(token, project_id, task_id)
        comment = _own_comment(token, task_id, bot, revision)
        preview = _owned_preview(token, task_id, bot, revision, comment)
        _require(preview is not None and _status(preview) == "ready", "KITSU_PREVIEW_FALLIDO")
        key = (project_id, task_id, revision, "kit", sha256)

        def lookup():
            found = _own_comment(token, task_id, bot, revision)
            _require(
                found is not None and found["id"] == comment["id"], "KITSU_REVISION_EN_CONFLICTO"
            )
            row = _one(
                _rows(found.get("attachment_files") or []),
                lambda v: v.get("name") == name,
                "KITSU_ADJUNTO_CONFLICTO",
            )
            if row is None:
                return None
            ident, size = row.get("id"), row.get("size")
            _require(
                _pattern(ident, UUID)
                and type(size) is int
                and 32 <= size <= MAX_MEDIA
                and row.get("extension") == "zip"
                and row.get("reply_id") is None,
                "KITSU_ADJUNTO_CONFLICTO",
            )
            _require(
                _attachment_digest(token, ident, name, size) == (sha256, size),
                "KITSU_ADJUNTO_HUELLA",
            )
            return {"attachment_id": ident, "sha256": sha256, "bytes": size, "name": name}

        with _spool(root) as out:
            payload = _stage_upload(out, source, sha256, zip_file=True)
            row = _reconcile(
                root,
                key,
                (bot, comment["id"], preview["id"]),
                lookup,
                lambda: _request(
                    token,
                    f"/actions/tasks/{task_id}/comments/{comment['id']}/add-attachment",
                    multipart=payload,
                ),
            )
        return {**row, "task_id": task_id, "comment_id": comment["id"], "revision": revision}


def attach_preview(project_id: str, task_id: str, revision: int, path: str, sha256: str) -> dict:
    """Sube un MP4 de revisión del NAS a una versión ya reservada por el bot."""
    slug = _revision_args(project_id, task_id, revision)
    _require(_pattern(sha256, SHA), "KITSU_ARGUMENTOS")
    source = source_path(path, slug, {".mp4"})
    _require(source.parts[2] in {"04_edicion", "06_renders"}, "RUTA_MEDIO_INVALIDA")
    # El puente ya entrega su máster H.264/AAC apto para reproducción. Zou debe
    # preservar esos bytes. Los demás MP4 conservan la normalización de Zou.
    preserve_master = (
        len(source.parts) == 6
        and source.parts[2] == "06_renders"
        and source.parts[4] == "render"
        and source.name in {"master.mp4", "master-corrected.mp4"}
    )
    normalize = "false" if preserve_master else "true"
    with _write_state() as root:
        token = read_secret("KITSU_API_TOKEN_FILE")
        bot = _project_access(token, project_id, write=True)
        task = _task_scope(token, project_id, task_id)
        comment = _own_comment(token, task_id, bot, revision)
        reserved = _owned_preview(token, task_id, bot, revision, comment)
        _require(reserved is not None, "KITSU_REVISION_NO_RESERVADA")
        upload_key = (project_id, task_id, revision, "upload")
        upload_intent = (bot, reserved["id"], sha256)

        def lookup_upload():
            latest = _own_comment(token, task_id, bot, revision)
            _require(
                latest is not None and latest["id"] == comment["id"], "KITSU_REVISION_EN_CONFLICTO"
            )
            actual = _owned_preview(token, task_id, bot, revision, latest)
            _require(
                actual is not None and actual["id"] == reserved["id"], "KITSU_REVISION_EN_CONFLICTO"
            )
            if actual.get("original_name") in (None, ""):
                _require(_status(actual) == "processing", "KITSU_REVISION_EN_CONFLICTO")
                return None
            _require(
                actual.get("original_name") == f"agency-{sha256}"
                and actual.get("extension") == "mp4",
                "KITSU_REVISION_EN_CONFLICTO",
            )
            _require(_status(actual) in {"processing", "ready"}, "KITSU_PREVIEW_FALLIDO")
            _require(
                _read_intent(root, _digest(upload_key), _digest(upload_intent)),
                "KITSU_SUBIDA_SIN_INTENCION",
            )
            return actual

        preview = lookup_upload()
        if preview is None:
            try:
                with _spool(root) as out:
                    multipart = _stage_upload(out, source, sha256)
                    preview = _reconcile(
                        root,
                        upload_key,
                        upload_intent,
                        lookup_upload,
                        lambda: _request(
                            token,
                            f"/pictures/preview-files/{reserved['id']}?normalize={normalize}",
                            multipart=multipart,
                        ),
                    )
            except OSError:
                raise ValueError("KITSU_MEDIO_TEMPORAL") from None
        return {
            **_preview_result(project_id, task, revision, comment, preview),
            "sha256": sha256,
            "remote_source_hash_verified": False,
        }


def create_deliverable(project_id: str, name: str) -> dict:
    """Crea o recupera un Edit raíz propio con su tarea de revisión configurada."""
    _require(
        isinstance(name, str)
        and 1 <= len(name) <= 120
        and name == name.strip()
        and unicodedata.normalize("NFC", name) == name
        and not any(unicodedata.category(ch).startswith("C") or ch in "/\\" for ch in name),
        "KITSU_ARGUMENTOS",
    )
    _project_slug(project_id)
    task_type = _configured_uuid("KITSU_REVIEW_TASK_TYPE_ID")
    task_status = _configured_uuid("KITSU_REVIEW_TASK_STATUS_ID")
    project_route = f"/data/projects/{project_id}"
    with _write_state() as root:
        token = read_secret("KITSU_API_TOKEN_FILE")
        bot = _project_access(token, project_id, write=True)
        task_types = _rows(_request(token, f"{project_route}/task-types"))
        statuses = _rows(_request(token, f"{project_route}/settings/task-status"))
        configured_type = _one(
            task_types, lambda row: row.get("id") == task_type, "KITSU_CONFIG_TAREA"
        )
        configured_status = _one(
            statuses, lambda row: row.get("id") == task_status, "KITSU_CONFIG_TAREA"
        )
        _require(
            configured_type is not None
            and configured_type.get("for_entity") == "Edit"
            and configured_type.get("archived") is False
            and configured_status is not None
            and configured_status.get("archived") is False,
            "KITSU_CONFIG_TAREA",
        )
        edits_route = f"{project_route}/edits"

        def lookup_edit():
            edits = _rows(_request(token, edits_route))
            edit = _one(edits, lambda row: row.get("name") == name, "KITSU_ENTREGABLE_EN_CONFLICTO")
            if edit is not None:
                _require(
                    edit.get("project_id") == project_id
                    and edit.get("created_by") == bot
                    and "parent_id" in edit
                    and edit["parent_id"] is None
                    and _pattern(edit.get("id"), UUID),
                    "KITSU_ENTREGABLE_EN_CONFLICTO",
                )
            return edit

        edit = _reconcile(
            root,
            (project_id, name, "edit"),
            (bot, name),
            lookup_edit,
            lambda: _request(token, edits_route, body={"name": name}),
        )
        edit_id = edit["id"]
        body = {
            "name": name,
            "entity_id": edit_id,
            "task_type_id": task_type,
            "task_status_id": task_status,
            "assignees": [],
        }

        def lookup_task():
            tasks = _rows(_request(token, f"/data/edits/{edit_id}/tasks"))
            task = _one(
                tasks,
                lambda row: row.get("task_type_id") == task_type and row.get("name") == name,
                "KITSU_ENTREGABLE_EN_CONFLICTO",
            )
            if task is not None:
                _require(
                    task.get("entity_id") == edit_id
                    and task.get("project_id") == project_id
                    and _pattern(task.get("id"), UUID),
                    "KITSU_ENTREGABLE_EN_CONFLICTO",
                )
            return task

        task = _reconcile(
            root,
            (project_id, edit_id, task_type, "task"),
            (bot, body),
            lookup_task,
            lambda: _request(token, "/data/tasks", body=body),
        )
        return {"project_id": project_id, "edit_id": edit_id, "task_id": task["id"], "name": name}
