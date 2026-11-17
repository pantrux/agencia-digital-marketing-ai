import hashlib
import importlib
import io
import os
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path, PurePosixPath

import pytest

MODULES = Path(__file__).resolve().parents[1] / "openclaw-agency-setup/mcp_servers"
PROJECT, TASK, EDIT, STATUS, BOT, COMMENT, PREVIEW, TYPE, OTHER = (
    f"00000000-0000-4000-8000-{number:012d}" for number in range(1, 10)
)
MP4 = b"\0\0\0\x18ftypisom" + b"x" * 30
DIGEST = hashlib.sha256(MP4).hexdigest()
SOURCE = "proyectos/fixture/06_renders/job/render/master.mp4"


class Zou:
    def __init__(self):
        self.calls = []
        self.task = {"id": TASK, "entity_id": EDIT, "project_id": PROJECT, "task_status_id": STATUS}
        self.edit = {
            "id": EDIT,
            "project_id": PROJECT,
            "created_by": BOT,
            "name": "Entrega 1",
            "parent_id": None,
        }
        self.comments = []
        self.previews = []
        self.edits = []
        self.edit_tasks = []
        self.role = "user"
        self.project_role = "manager"
        self.automations = []
        self.uploaded = b""
        self.interrupt = None

    def __call__(self, token, route, *, body=None, multipart=None):
        assert token == "opaque-fixture"
        write = body is not None or multipart is not None
        self.calls.append(("POST" if write else "GET", route))
        if not write:
            return deepcopy(
                {
                    "/auth/authenticated": {
                        "authenticated": True,
                        "user": {
                            "id": BOT,
                            "role": self.role,
                            "is_bot": True,
                        },
                    },
                    f"/data/projects/{PROJECT}": {"id": PROJECT},
                    f"/data/projects/{PROJECT}/team": [
                        {"id": BOT, "project_role": self.project_role}
                    ],
                    f"/data/projects/{PROJECT}/settings/status-automations": self.automations,
                    f"/data/tasks/{TASK}": self.task,
                    f"/data/edits/{EDIT}": self.edit,
                    f"/data/tasks/{TASK}/comments": self.comments,
                    f"/data/tasks/{TASK}/previews": self.previews,
                    f"/data/projects/{PROJECT}/edits": self.edits,
                    f"/data/edits/{EDIT}/tasks": self.edit_tasks,
                    f"/data/projects/{PROJECT}/task-types": [
                        {"id": TYPE, "for_entity": "Edit", "archived": False}
                    ],
                    f"/data/projects/{PROJECT}/settings/task-status": [
                        {"id": STATUS, "archived": False}
                    ],
                }[route]
            )
        if route.endswith("/comment"):
            assert body == {"task_status_id": STATUS, "comment": "agency-mcp-preview-v1"}
            if self.interrupt == "no_commit":
                self.fail()
            self.comments.append(
                {
                    "id": COMMENT,
                    "object_id": TASK,
                    "text": body["comment"],
                    "person_id": BOT,
                    "previews": [],
                }
            )
            if self.interrupt == "commit":
                self.fail()
        elif route.endswith("/add-preview"):
            assert body == {"revision": 1}
            preview = {
                "id": PREVIEW,
                "task_id": TASK,
                "revision": 1,
                "person_id": BOT,
                "status": "processing",
                "original_name": None,
                "extension": "mp4",
                "annotations": None,
            }
            self.previews.append(preview)
            self.comments[0]["previews"] = [preview]
        elif route in {
            f"/pictures/preview-files/{PREVIEW}?normalize={value}" for value in ("true", "false")
        }:
            payload, size, boundary = multipart
            assert 1 <= len(boundary) <= 70
            self.uploaded = payload.read()
            assert len(self.uploaded) == size
            assert self.uploaded.endswith(f"\r\n--{boundary}--\r\n".encode())
            self.previews[0].update(original_name=f"agency-{DIGEST}", status="ready")
        elif route.endswith("/edits"):
            assert body == {"name": "Entrega 1"}
            self.edits.append(self.edit)
        else:
            assert route == "/data/tasks"
            assert body == {
                "name": "Entrega 1",
                "entity_id": EDIT,
                "task_type_id": TYPE,
                "task_status_id": STATUS,
                "assignees": [],
            }
            self.edit_tasks.append({**self.task, **body})
        return {"discarded": "redact-this-response"}

    def posts(self):
        return [path for method, path in self.calls if method == "POST"]


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.syspath_prepend(str(MODULES))
    module = importlib.import_module("kitsu_review")
    zou = Zou()

    def fail():
        raise module.UpstreamError(None, "KITSU_TRANSPORTE")

    zou.fail = fail
    state = {}

    def intent(fd, key, fingerprint):
        if key in state:
            if state[key] != fingerprint:
                raise ValueError("KITSU_INTENCION_DISTINTA")
            return False
        state[key] = fingerprint
        return True

    def read_intent(fd, key, fingerprint):
        if key not in state:
            return False
        if state[key] != fingerprint:
            raise ValueError("KITSU_INTENCION_DISTINTA")
        return True

    def secret(name):
        assert name == "KITSU_API_TOKEN_FILE"
        return "opaque-fixture"

    monkeypatch.setenv("AGENCY_MEDIA_PROJECTS", "fixture")
    monkeypatch.setenv("KITSU_REVIEW_PROJECTS", f'{{"{PROJECT}":"fixture"}}')
    monkeypatch.setenv("KITSU_REVIEW_BOT_ID", BOT)
    monkeypatch.setenv("KITSU_REVIEW_TASK_TYPE_ID", TYPE)
    monkeypatch.setenv("KITSU_REVIEW_TASK_STATUS_ID", STATUS)
    monkeypatch.setattr(module, "read_secret", secret)
    monkeypatch.setattr(module, "_request", zou)
    monkeypatch.setattr(module, "_write_state", lambda: nullcontext(None))
    monkeypatch.setattr(module, "_record_intent", intent)
    monkeypatch.setattr(module, "_read_intent", read_intent)
    monkeypatch.setattr(module, "_spool", lambda _: io.BytesIO())
    return module, zou, state


def test_two_acls_and_task_relationship_before_any_mutation(adapter, monkeypatch):
    module, zou, _ = adapter
    with pytest.raises(ValueError, match="PROYECTO_NO_AUTORIZADO"):
        module.query_review(OTHER, TASK)
    monkeypatch.setenv("AGENCY_MEDIA_PROJECTS", "other")
    with pytest.raises(ValueError, match="PROYECTO_NO_AUTORIZADO"):
        module.query_review(PROJECT, TASK)
    assert zou.calls == []
    monkeypatch.setenv("AGENCY_MEDIA_PROJECTS", "fixture")
    zou.task["project_id"] = OTHER
    with pytest.raises(ValueError, match="KITSU_TAREA_AJENA"):
        module.create_preview(PROJECT, TASK, 1)
    assert zou.posts() == []
    assert ("GET", f"/data/edits/{EDIT}") not in zou.calls


def test_zip_reconcilia_desde_comentario_propio_sin_listado_administrativo(adapter, monkeypatch):
    module, zou, _ = adapter
    module.create_preview(PROJECT, TASK, 1)
    zou.previews[0].update(status="ready", original_name=f"agency-{DIGEST}")
    blob = b"PK\x03\x04" + b"archivo" * 12
    digest = hashlib.sha256(blob).hexdigest()
    name = f"kit-studio-{digest}.zip"
    path = f"proyectos/fixture/05_revision/{name}"

    @contextmanager
    def media(_path):
        yield io.BytesIO(blob), len(blob)

    monkeypatch.setattr(module, "_media_file", media)
    monkeypatch.setattr(module, "_attachment_digest", lambda *args: (digest, len(blob)))

    def request(token, route, *, body=None, multipart=None):
        if route.endswith("/add-attachment"):
            zou.calls.append(("POST", route))
            zou.comments[0]["attachment_files"] = [
                {"id": OTHER, "name": name, "size": len(blob), "extension": "zip", "reply_id": None}
            ]
            zou.fail()
        return zou(token, route, body=body, multipart=multipart)

    monkeypatch.setattr(module, "_request", request)
    first = module.attach_delivery_zip(PROJECT, TASK, 1, path, digest)
    assert first == module.attach_delivery_zip(PROJECT, TASK, 1, path, digest)
    assert first["attachment_id"] == OTHER and first["sha256"] == digest
    assert sum(p.endswith("/add-attachment") for p in zou.posts()) == 1
    assert all(not route.endswith("/attachment-files") for _, route in zou.calls)
    monkeypatch.setattr(module, "_attachment_digest", lambda *args: ("0" * 64, len(blob)))
    with pytest.raises(ValueError, match="KITSU_ADJUNTO_HUELLA"):
        module.attach_delivery_zip(PROJECT, TASK, 1, path, digest)


def test_preview_replay_projects_ids_and_annotation_only(adapter):
    module, zou, _ = adapter
    first = module.create_preview(PROJECT, TASK, 1)
    assert first == module.create_preview(PROJECT, TASK, 1)
    assert first["preview_id"] == PREVIEW and first["comment_id"] == COMMENT
    assert len(zou.posts()) == 2
    zou.previews[0]["annotations"] = [
        {
            "time": "1.25",
            "drawing": {
                "objects": [
                    {"type": "path", "secret": "private"},
                    {
                        "type": "i-text",
                        "text": "Ajustar el título",
                        "left": 10,
                        "author": "private",
                    },
                ]
            },
        }
    ]
    review = module.query_review(PROJECT, TASK, 1)
    assert review["previews"] == [
        {
            "preview_id": PREVIEW,
            "revision": 1,
            "status": "processing",
            "annotations": [
                {
                    "time_seconds": 1.25,
                    "object_count": 2,
                    "objects": [
                        {"type": "path"},
                        {"type": "i-text", "text": "Ajustar el título", "left": 10},
                    ],
                }
            ],
        }
    ]


@pytest.mark.parametrize("mode", ["commit", "no_commit"])
def test_ambiguous_post_reconciles_commit_and_never_reissues_unknown_write(adapter, mode):
    module, zou, state = adapter
    zou.interrupt = mode
    if mode == "commit":
        assert module.create_preview(PROJECT, TASK, 1)["preview_id"] == PREVIEW
        assert len(zou.posts()) == 2
    else:
        for _ in range(2):
            with pytest.raises(ValueError, match="KITSU_ESCRITURA_PENDIENTE"):
                module.create_preview(PROJECT, TASK, 1)
        assert len(zou.posts()) == len(state) == 1


@pytest.mark.parametrize(
    "role, project_role", [("admin", "manager"), ("manager", "manager"), ("user", "user")]
)
def test_writes_require_non_privileged_global_bot_and_explicit_project_manager(
    adapter,
    role,
    project_role,
):
    module, zou, _ = adapter
    zou.role, zou.project_role = role, project_role
    with pytest.raises(ValueError, match="KITSU_BOT_NO_AUTORIZADO"):
        module.create_preview(PROJECT, TASK, 1)
    assert zou.posts() == []


def test_automation_and_foreign_preview_are_refused_without_post(adapter):
    module, zou, _ = adapter
    zou.automations = [{"archived": False}]
    with pytest.raises(ValueError, match="KITSU_PROYECTO_AUTOMATIZADO"):
        module.create_preview(PROJECT, TASK, 1)
    zou.automations = []
    zou.previews = [{"id": PREVIEW, "task_id": TASK, "revision": 1, "person_id": OTHER}]
    with pytest.raises(ValueError, match="KITSU_REVISION_EN_CONFLICTO"):
        module.create_preview(PROJECT, TASK, 1)
    assert zou.posts() == []


def test_deliverable_reconciles_only_own_edit_and_pinned_task_configuration(adapter):
    module, zou, _ = adapter
    expected = {"project_id": PROJECT, "edit_id": EDIT, "task_id": TASK, "name": "Entrega 1"}
    assert module.create_deliverable(PROJECT, "Entrega 1") == expected
    assert module.create_deliverable(PROJECT, "Entrega 1") == expected
    assert zou.posts() == [f"/data/projects/{PROJECT}/edits", "/data/tasks"]
    zou.edits[0]["created_by"] = OTHER
    with pytest.raises(ValueError, match="KITSU_ENTREGABLE_EN_CONFLICTO"):
        module.create_deliverable(PROJECT, "Entrega 1")
    assert len(zou.posts()) == 2


def test_attach_sends_exact_snapshot_once_and_conflicts_on_altered_digest(adapter, monkeypatch):
    module, zou, state = adapter
    module.create_preview(PROJECT, TASK, 1)

    @contextmanager
    def mp4(path):
        assert path == PurePosixPath(SOURCE)
        yield io.BytesIO(MP4), len(MP4)

    monkeypatch.setattr(module, "_media_file", mp4)
    args = PROJECT, TASK, 1, SOURCE, DIGEST
    first = module.attach_preview(*args)
    assert first == module.attach_preview(*args)
    assert first["status"] == "ready" and first["sha256"] == DIGEST
    assert first["remote_source_hash_verified"] is False
    assert MP4 in zou.uploaded
    assert zou.posts().count(f"/pictures/preview-files/{PREVIEW}?normalize=false") == 1
    with pytest.raises(ValueError, match="KITSU_REVISION_EN_CONFLICTO"):
        module.attach_preview(PROJECT, TASK, 1, SOURCE, "f" * 64)
    state.clear()
    with pytest.raises(ValueError, match="KITSU_SUBIDA_SIN_INTENCION"):
        module.attach_preview(*args)
    assert len(zou.posts()) == 3


def test_foreign_path_and_hash_mismatch_cannot_send_upload(adapter, monkeypatch):
    module, zou, _ = adapter
    with pytest.raises(ValueError, match="RUTA_MEDIO_INVALIDA"):
        module.attach_preview(PROJECT, TASK, 1, SOURCE.replace("/fixture/", "/other/"), DIGEST)
    assert zou.calls == []
    module.create_preview(PROJECT, TASK, 1)

    @contextmanager
    def altered(_):
        yield io.BytesIO(MP4 + b"changed"), len(MP4) + 7

    monkeypatch.setattr(module, "_media_file", altered)
    with pytest.raises(ValueError, match="KITSU_MEDIO_HUELLA"):
        module.attach_preview(PROJECT, TASK, 1, SOURCE, DIGEST)
    assert len(zou.posts()) == 2


def test_preview_de_edicion_conserva_normalizacion(adapter, monkeypatch):
    module, zou, _ = adapter
    module.create_preview(PROJECT, TASK, 1)

    @contextmanager
    def mp4(_):
        yield io.BytesIO(MP4), len(MP4)

    monkeypatch.setattr(module, "_media_file", mp4)
    module.attach_preview(PROJECT, TASK, 1, "proyectos/fixture/04_edicion/corte.mp4", DIGEST)
    assert zou.posts().count(f"/pictures/preview-files/{PREVIEW}?normalize=true") == 1


@pytest.mark.skipif(os.name != "posix", reason="Requiere flock y openat nativos")
def test_native_private_state_and_nofollow(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(MODULES))
    module = importlib.import_module("kitsu_review")
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("KITSU_REVIEW_STATE_ROOT", str(root))
    with module._write_state() as fd:
        key, fingerprint = "a" * 64, "b" * 64
        assert module._record_intent(fd, key, fingerprint)
        assert not module._record_intent(fd, key, fingerprint)
        with pytest.raises(ValueError, match="KITSU_ESTADO_OCUPADO"), module._write_state():
            pytest.fail("Dos escritores tienen el mismo lock")
        (root / ("c" * 64 + ".intent")).symlink_to(root / (key + ".intent"))
        with pytest.raises(ValueError, match="KITSU_ESTADO_INVALIDO"):
            module._record_intent(fd, "c" * 64, fingerprint)
        with module._spool(fd) as out:
            out.write(b"test")
        assert {p.name for p in root.iterdir()} == {
            ".writer.lock",
            key + ".intent",
            "c" * 64 + ".intent",
        }


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (200, b'{"answer": true}', {"answer": True}),
        (200, b'{"same": 1, "same": 2}', "KITSU_RESPUESTA_INVALIDA"),
        (302, b"private redirect", "KITSU_HTTP"),
        (403, b"private error", "KITSU_NO_AUTORIZADO"),
    ],
)
def test_http_pins_destination_rejects_redirect_and_never_exposes_response(
    monkeypatch,
    status,
    body,
    expected,
):
    monkeypatch.syspath_prepend(str(MODULES))
    module = importlib.import_module("kitsu_review")

    class Response:
        def __enter__(self):
            self.status = status
            return self

        def __exit__(self, *_):
            pass

        def getheader(self, key, default=None):
            return "application/json" if key == "Content-Type" else default

        def read(self, limit):
            assert limit == module.MAX_RESPONSE + 1
            assert status == 200
            return body

    closed = []

    class Connection:
        def __init__(self, host, port, *, timeout):
            assert (host, port, timeout) == ("192.168.0.85", 8081, 8)

        def request(self, method, route, *, body, headers):
            assert (method, route, body) == ("GET", "/api/auth/authenticated", None)
            assert headers["Authorization"] == "Bearer local-fixture"

        def getresponse(self):
            return Response()

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module.http.client, "HTTPConnection", Connection)
    if isinstance(expected, str):
        with pytest.raises(module.UpstreamError, match=f"^{expected}$"):
            module._request("local-fixture", "/auth/authenticated")
    else:
        assert module._request("local-fixture", "/auth/authenticated") == expected
    assert closed == [True]


@pytest.mark.parametrize(
    "payload_size, failure", [(4, None), (9, None), (9, "truncated"), (9, "offset"), (9, "ignored")]
)
def test_attachment_digest_verifies_every_byte(adapter, monkeypatch, payload_size, failure):
    module, _, _ = adapter
    data = b"123456789"[:payload_size]
    calls = []

    class Response(io.BytesIO):
        def getheader(self, name, default=None):
            return self.headers.get(name, default)

    class Connection:
        def __init__(self, _host, _port, *, timeout):
            assert timeout > 0

        def request(self, method, path, *, headers):
            assert method == "GET"
            assert path == f"/api/data/attachment-files/{OTHER}/file/kit.zip"
            assert headers["Accept-Encoding"] == "identity"
            assert headers["Authorization"] == "Bearer opaque-fixture"
            self.requested_range = headers.get("Range")
            calls.append(self.requested_range)

        def getresponse(self):
            start = 4 * (len(calls) - 1)
            chunk = data[start : start + 4]
            span = f"bytes {start}-{start + len(chunk) - 1}/{len(data)}"
            if start == 4 and failure == "offset":
                span = f"bytes 0-3/{len(data)}"
            response = Response(chunk[:-1] if start == 4 and failure == "truncated" else chunk)
            response.status = 206 if self.requested_range and failure != "ignored" else 200
            response.headers = {
                "Content-Type": "application/zip",
                "Content-Length": str(len(chunk)),
                "Content-Range": span,
            }
            return response

        def close(self):
            pass

    monkeypatch.setattr(module, "ATTACHMENT_READ_BYTES", 4, raising=False)
    monkeypatch.setattr(module.http.client, "HTTPConnection", Connection)
    if failure:
        with pytest.raises(ValueError, match="KITSU_ADJUNTO_HUELLA"):
            module._attachment_digest("opaque-fixture", OTHER, "kit.zip", len(data))
    else:
        result = module._attachment_digest("opaque-fixture", OTHER, "kit.zip", len(data))
        assert result == (hashlib.sha256(data).hexdigest(), len(data))
        assert calls == ([None] if payload_size == 4 else ["bytes=0-3", "bytes=4-7", "bytes=8-8"])


def test_multipart_bounds_include_framing(adapter, monkeypatch):
    module, _, _ = adapter

    @contextmanager
    def media(_):
        yield io.BytesIO(MP4), len(MP4)

    monkeypatch.setattr(module, "_media_file", media)
    monkeypatch.setattr(module, "MAX_REQUEST", len(MP4))
    with pytest.raises(ValueError, match="KITSU_MEDIO_TAMANO"):
        module._stage_upload(io.BytesIO(), PurePosixPath(SOURCE), DIGEST)


@pytest.mark.parametrize("size, zip_file", [(1_110_957_940, False), (3 * 1024**3, True)])
def test_multipart_admite_tamanos_de_produccion_sin_leer_el_cuerpo_entero(
    adapter, monkeypatch, size, zip_file
):
    module, _, _ = adapter

    class InicioDeLectura(Exception):
        pass

    class Medio:
        def read(self, amount):
            assert amount == module.CHUNK
            raise InicioDeLectura

    @contextmanager
    def media(_):
        yield Medio(), size

    monkeypatch.setattr(module, "_media_file", media)
    out = io.BytesIO()
    with pytest.raises(InicioDeLectura):
        module._stage_upload(out, PurePosixPath(SOURCE), DIGEST, zip_file=zip_file)
    kind = b"application/zip" if zip_file else b"video/mp4"
    assert out.getvalue().endswith(b"Content-Type: " + kind + b"\r\n\r\n")


def test_adapta_zip_superior_a_128_mib_por_bloques(adapter, monkeypatch, tmp_path):
    module, _, _ = adapter
    size = 129 * 1024**2
    file = tmp_path / "ensayo.zip"
    with file.open("wb") as stream:
        stream.write(b"PK\x03\x04")
        stream.truncate(size)
    with file.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()

    @contextmanager
    def media(_):
        with file.open("rb") as stream:
            yield stream, size

    class Sumidero:
        count = 0

        def write(self, block):
            assert len(block) <= module.CHUNK
            self.count += len(block)

        def seek(self, offset):
            assert offset == 0

    monkeypatch.setattr(module, "_media_file", media)
    out = Sumidero()
    stream, length, boundary = module._stage_upload(
        out, PurePosixPath("ensayo.zip"), digest, zip_file=True
    )
    assert stream is out and size < length == out.count and boundary == digest


@pytest.mark.skipif(os.name != "posix", reason="Requiere openat y O_NOFOLLOW nativos")
def test_native_nas_limita_el_tamano_del_medio_sin_asignar_el_cuerpo(
    adapter, monkeypatch, tmp_path
):
    module, _, _ = adapter
    monkeypatch.setattr(module, "MEDIA_ROOT", tmp_path)
    source = tmp_path / "archivo.zip"
    limit = 3 * 1024**3
    with source.open("wb") as out:
        out.truncate(limit)
    with module._media_file(PurePosixPath("archivo.zip")) as (_, size):
        assert size == limit
    with source.open("r+b") as out:
        out.truncate(limit + 1)
    with (
        pytest.raises(ValueError, match="KITSU_MEDIO_INVALIDO"),
        module._media_file(PurePosixPath("archivo.zip")),
    ):
        pytest.fail("Se abrió un archivo sobre el límite de medio")


@pytest.mark.skipif(os.name != "posix", reason="Requiere openat y O_NOFOLLOW nativos")
def test_native_nas_rejects_symlink_and_hardlink(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(MODULES))
    module = importlib.import_module("kitsu_review")
    monkeypatch.setattr(module, "MEDIA_ROOT", tmp_path)
    source = tmp_path / SOURCE
    source.parent.mkdir(parents=True)
    real = source.with_name("valid.mp4")
    real.write_bytes(MP4)
    source.symlink_to(real)
    with pytest.raises(ValueError, match="KITSU_MEDIO_INVALIDO"):
        with module._media_file(PurePosixPath(SOURCE)):
            pytest.fail("Se siguió un symlink")
    source.unlink()
    os.link(real, source)
    with pytest.raises(ValueError, match="KITSU_MEDIO_INVALIDO"):
        with module._media_file(PurePosixPath(SOURCE)):
            pytest.fail("Se aceptó un hardlink")
    source.unlink()
    real.rename(source)
    with module._media_file(PurePosixPath(SOURCE)) as (stream, size):
        assert stream.read() == MP4 and size == len(MP4)
