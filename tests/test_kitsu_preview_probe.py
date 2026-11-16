import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "docker/kitsu-zou/preview_probe.py"
PROJECT = "00000000-0000-4000-8000-000000000001"
EDIT = "00000000-0000-4000-8000-000000000002"
TASK = "00000000-0000-4000-8000-000000000003"
STATUS = "00000000-0000-4000-8000-000000000004"
COMMENT = "00000000-0000-4000-8000-000000000005"
PREVIEW = "00000000-0000-4000-8000-000000000006"
MARKER = "agency-kitsu-preview-20261113-v1"


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location("kitsu_preview_probe", PATH)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_request_closes_standard_http_connection(probe, monkeypatch):
    class Response(io.BytesIO):
        status = 200

        def getheader(self, name, default=None):
            return {"Content-Type": "application/json; charset=utf-8"}.get(name, default)

    class Connection:
        closed = False

        def request(self, method, path, *, body, headers):
            assert (method, path, body) == ("GET", "/api/status", None)
            assert headers == {"Accept-Encoding": "identity"}

        def getresponse(self):
            return Response(b'{"ready":true}')

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(probe.http.client, "HTTPConnection", lambda *args, **kwargs: connection)

    assert probe.API(None).request("GET", "/status", 200, anonymous=True) == (
        b'{"ready":true}',
        "application/json",
        None,
    )
    assert connection.closed


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.comment = {"id": COMMENT, "object_id": TASK, "text": MARKER}
        self.preview = {"id": PREVIEW, "task_id": TASK, "revision": 1, "original_name": None}
        self.rows = {
            f"/data/projects/{PROJECT}": {"id": PROJECT, "name": MARKER},
            f"/data/projects/{PROJECT}/settings/status-automations": [],
            f"/data/edits/{EDIT}": {"id": EDIT, "project_id": PROJECT, "name": MARKER},
            f"/data/tasks/{TASK}": {
                "id": TASK,
                "project_id": PROJECT,
                "entity_id": EDIT,
                "name": MARKER,
                "task_status_id": STATUS,
                "assignees": [],
            },
            f"/data/tasks/{TASK}/comments": [],
            f"/data/tasks/{TASK}/previews": [],
        }

    def json(self, method, path, status, payload=None):
        self.calls.append((method, path, status, payload))
        if method == "GET":
            return self.rows[path]
        return self.preview if path.endswith("/add-preview") else self.comment


def test_preflight_and_creation_have_separate_network_calls(probe):
    client = FakeAPI()
    result = probe.step(client, "preflight", PROJECT, EDIT, TASK, MARKER)
    assert result == {"task_status_id": STATUS, "comments": 0, "previews": 0}
    assert all(method == "GET" for method, *_ in client.calls)
    result = probe.step(client, "comment", PROJECT, EDIT, TASK, MARKER)
    assert result["comment_id"] == COMMENT
    assert client.calls[-1] == (
        "POST",
        f"/actions/tasks/{TASK}/comment",
        201,
        {"task_status_id": STATUS, "comment": MARKER},
    )
    client.rows[f"/data/tasks/{TASK}/comments"] = [client.comment]
    result = probe.step(client, "preview", PROJECT, EDIT, TASK, MARKER, comment_id=COMMENT)
    assert result["preview_id"] == PREVIEW
    assert client.calls[-1] == (
        "POST",
        f"/actions/tasks/{TASK}/comments/{COMMENT}/add-preview",
        201,
        {"revision": 1},
    )


def test_no_mutation_if_task_project_or_previous_resource_differs(probe):
    client = FakeAPI()
    client.rows[f"/data/tasks/{TASK}"]["project_id"] = EDIT
    with pytest.raises(probe.Failure, match="^TAREA_AJENA$"):
        probe.step(client, "comment", PROJECT, EDIT, TASK, MARKER)
    client.rows[f"/data/tasks/{TASK}"]["project_id"] = PROJECT
    client.rows[f"/data/tasks/{TASK}/comments"] = [{**client.comment, "text": "other"}]
    with pytest.raises(probe.Failure, match="^COMENTARIO_DISTINTO$"):
        probe.step(client, "preview", PROJECT, EDIT, TASK, MARKER, comment_id=COMMENT)
    assert all(method == "GET" for method, *_ in client.calls)


def test_no_mutation_if_project_has_status_automations(probe):
    client = FakeAPI()
    client.rows[f"/data/projects/{PROJECT}/settings/status-automations"] = [{"id": COMMENT}]
    with pytest.raises(probe.Failure, match="^PROYECTO_AUTOMATIZADO$"):
        probe.step(client, "comment", PROJECT, EDIT, TASK, MARKER)
    assert all(method == "GET" for method, *_ in client.calls)


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("source_retained", [False, True])
def test_upload_requires_linked_preview_and_proves_distinct_playback(
    probe, fallback, source_retained
):
    client = FakeAPI()
    source = b"\0\0\0\x20ftyp" + b"source" * 8
    playback = source if fallback else source + b"normalized"
    requests = []
    client.rows[f"/data/tasks/{TASK}/comments"] = [{**client.comment, "previews": [client.preview]}]
    previews = f"/data/tasks/{TASK}/previews"
    client.rows[previews] = [client.preview]

    def request(method, path, status, **kwargs):
        requests.append((method, path, status, kwargs))
        if method == "POST":
            assert source in kwargs["data"] and b'name="file"' in kwargs["data"]
            ready = {
                **client.preview,
                "status": "ready",
                "original_name": MARKER,
                "extension": "mp4",
            }
            client.rows[previews] = [ready]
            return json.dumps(ready).encode(), "application/json", None
        if kwargs.get("anonymous"):
            assert status == 401
            return b"", "application/json", None
        if kwargs.get("byte_range"):
            return playback[:32], "video/mp4", f"bytes 0-31/{len(playback)}"
        if "/source/" in path:
            assert status == (200 if source_retained else 404)
            return (
                (source, "video/mp4", None) if source_retained else (b"", "application/json", None)
            )
        if path.endswith(".png"):
            return b"\x89PNG\r\n\x1a\nthumbnail", "image/png", None
        return playback, "video/mp4", None

    client.request = request
    if fallback:
        with pytest.raises(probe.Failure, match="^PREVIEW_BYTES$"):
            probe.step(
                client,
                "upload",
                PROJECT,
                EDIT,
                TASK,
                MARKER,
                comment_id=COMMENT,
                preview_id=PREVIEW,
                fixture=source,
                source_retained=source_retained,
            )
    else:
        result = probe.step(
            client,
            "upload",
            PROJECT,
            EDIT,
            TASK,
            MARKER,
            comment_id=COMMENT,
            preview_id=PREVIEW,
            fixture=source,
            source_retained=source_retained,
        )
        assert result["fixture_sha256"] == hashlib.sha256(source).hexdigest()
        assert result["source_sha256"] == (result["fixture_sha256"] if source_retained else None)
        assert result["source_http"] == (200 if source_retained else 404)
        assert result["preview_sha256"] == hashlib.sha256(playback).hexdigest()
    assert [method for method, *_ in requests].count("POST") == 1


def test_unlinked_preview_is_rejected_before_upload(probe):
    client = FakeAPI()
    client.rows[f"/data/tasks/{TASK}/comments"] = [{**client.comment, "previews": []}]
    client.rows[f"/data/tasks/{TASK}/previews"] = [client.preview]
    with pytest.raises(probe.Failure, match="^PREVIEW_DISTINTO$"):
        probe.step(
            client,
            "upload",
            PROJECT,
            EDIT,
            TASK,
            MARKER,
            comment_id=COMMENT,
            preview_id=PREVIEW,
            fixture=b"x",
        )
    assert all(method == "GET" for method, *_ in client.calls)
