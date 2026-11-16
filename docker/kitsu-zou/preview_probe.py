"""Sonda de una sola revisión de vídeo para una tarea sintética en Kitsu LAN."""

import argparse
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
from contextlib import closing
from pathlib import Path

HOST, PORT = "192.168.0.85", 8081
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")
MARKER = re.compile(r"agency-kitsu-preview-[0-9]{8}-v[1-9][0-9]{0,2}")
SHA256 = re.compile(r"[a-f0-9]{64}")
LIMIT = 1024 * 1024
ZOU_IMAGE = "sha256:c906cfe1ea433f5f9215e55365a789fbd5c790d0563c07f425ca891137f299af"
PREVIEW_DIR = "/var/lib/zou/previews"
ZOU_STATE = "/var/lib/media-platform/kitsu-base-v1/zou"
HOST_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "HOME": "/nonexistent"}


class Failure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Failure(code)


def uuid(value):
    return isinstance(value, str) and UUID.fullmatch(value)


class API:
    def __init__(self, token):
        self.token = token

    def request(
        self,
        method,
        path,
        status,
        *,
        data=None,
        content_type=None,
        anonymous=False,
        byte_range=False,
    ):
        headers = {"Accept-Encoding": "identity"}
        if not anonymous:
            headers["Authorization"] = "Bearer " + self.token
        if content_type:
            headers["Content-Type"] = content_type
        if byte_range:
            headers["Range"] = "bytes=0-31"
        with closing(
            http.client.HTTPConnection(HOST, PORT, timeout=68 if data else 8)
        ) as connection:
            connection.request(method, "/api" + path, body=data, headers=headers)
            with connection.getresponse() as response:
                require(response.status == status, f"HTTP_{method}_{response.status}")
                require(
                    response.getheader("Content-Encoding") in (None, "identity"), "HTTP_ENCODING"
                )
                raw = response.read(LIMIT + 1)
                require(len(raw) <= LIMIT, "HTTP_COTA")
                return (
                    raw,
                    response.getheader("Content-Type", "").split(";", 1)[0],
                    response.getheader("Content-Range"),
                )

    def json(self, method, path, status, payload=None):
        raw, kind, _ = self.request(
            method,
            path,
            status,
            data=json.dumps(payload).encode() if payload is not None else None,
            content_type="application/json" if payload is not None else None,
        )
        require(kind == "application/json", "HTTP_JSON")
        return json.loads(raw)


def owned_task(client, project_id, edit_id, task_id, marker):
    require(
        all(uuid(item) for item in (project_id, edit_id, task_id))
        and len({project_id, edit_id, task_id}) == 3
        and MARKER.fullmatch(marker),
        "TAREA_PARAMETROS",
    )
    project = client.json("GET", f"/data/projects/{project_id}", 200)
    require(project["id"] == project_id and project["name"] == marker, "PROYECTO_AJENO")
    require(
        client.json("GET", f"/data/projects/{project_id}/settings/status-automations", 200) == [],
        "PROYECTO_AUTOMATIZADO",
    )
    edit = client.json("GET", f"/data/edits/{edit_id}", 200)
    require(
        edit["id"] == edit_id and edit["project_id"] == project_id and edit["name"] == marker,
        "EDICION_AJENA",
    )
    task = client.json("GET", f"/data/tasks/{task_id}", 200)
    require(
        task["id"] == task_id
        and task["project_id"] == project_id
        and task["entity_id"] == edit_id
        and task["name"] == marker
        and uuid(task["task_status_id"])
        and task["assignees"] == [],
        "TAREA_AJENA",
    )
    comments = client.json("GET", f"/data/tasks/{task_id}/comments", 200)
    previews = client.json("GET", f"/data/tasks/{task_id}/previews", 200)
    require(isinstance(comments, list) and isinstance(previews, list), "TAREA_LISTAS")
    return task["task_status_id"], comments, previews


def matching_comment(row, comment_id, task_id, marker):
    return (
        row["id"] == comment_id
        and row.get("object_id", task_id) == task_id
        and row["text"] == marker
    )


def matching_preview(row, preview_id, task_id):
    return row["id"] == preview_id and row["task_id"] == task_id and row["revision"] == 1


def step(
    client,
    action,
    project_id,
    edit_id,
    task_id,
    marker,
    *,
    comment_id=None,
    preview_id=None,
    fixture=None,
    source_retained=False,
):
    status, comments, previews = owned_task(client, project_id, edit_id, task_id, marker)
    if action == "preflight":
        return {"task_status_id": status, "comments": len(comments), "previews": len(previews)}
    if action == "comment":
        require(comments == previews == [], "TAREA_YA_UTILIZADA")
        row = client.json(
            "POST",
            f"/actions/tasks/{task_id}/comment",
            201,
            {"task_status_id": status, "comment": marker},
        )
        require(
            uuid(row["id"]) and matching_comment(row, row["id"], task_id, marker),
            "COMENTARIO_RESPUESTA",
        )
        return {"comment_id": row["id"]}
    require(
        uuid(comment_id)
        and len(comments) == 1
        and matching_comment(comments[0], comment_id, task_id, marker),
        "COMENTARIO_DISTINTO",
    )
    if action == "preview":
        require(previews == [], "TAREA_YA_UTILIZADA")
        row = client.json(
            "POST",
            f"/actions/tasks/{task_id}/comments/{comment_id}/add-preview",
            201,
            {"revision": 1},
        )
        require(uuid(row["id"]) and matching_preview(row, row["id"], task_id), "PREVIEW_RESPUESTA")
        return {"preview_id": row["id"], "revision": 1}
    require(action == "upload" and uuid(preview_id) and fixture is not None, "PREVIEW_PARAMETROS")
    linked = comments[0]["previews"]
    require(
        len(previews) == len(linked) == 1
        and matching_preview(previews[0], preview_id, task_id)
        and matching_preview(linked[0], preview_id, task_id)
        and not previews[0]["original_name"],
        "PREVIEW_DISTINTO",
    )
    return upload(client, task_id, preview_id, marker, fixture, source_retained=source_retained)


def is_mp4(data):
    return len(data) >= 32 and data[4:8] == b"ftyp"


def load_fixture(path, expected_sha):
    require(SHA256.fullmatch(expected_sha), "FIXTURE_SHA")
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    require(
        len(data) <= LIMIT and is_mp4(data) and hashlib.sha256(data).hexdigest() == expected_sha,
        "FIXTURE_SHA",
    )
    return data


def upload(client, task_id, preview_id, marker, data, *, source_retained):
    boundary = hashlib.sha256(data).hexdigest()
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{marker}.mp4"\r\n'
            "Content-Type: video/mp4\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    raw, kind, _ = client.request(
        "POST",
        f"/pictures/preview-files/{preview_id}?normalize=true",
        201,
        data=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    require(
        kind == "application/json" and matching_preview(json.loads(raw), preview_id, task_id),
        "UPLOAD_RESPUESTA",
    )
    rows = client.json("GET", f"/data/tasks/{task_id}/previews", 200)
    require(
        len(rows) == 1
        and matching_preview(rows[0], preview_id, task_id)
        and rows[0]["status"] == "ready"
        and rows[0]["original_name"] == marker
        and rows[0]["extension"] == "mp4",
        "PREVIEW_NO_LISTO",
    )
    source_path = f"/movies/source/preview-files/{preview_id}.mp4"
    output_path = f"/movies/originals/preview-files/{preview_id}.mp4"
    source_http = 200 if source_retained else 404
    source, source_kind, _ = client.request("GET", source_path, source_http)
    if source_retained:
        require(source == data and source_kind == "video/mp4", "PREVIEW_SOURCE_BYTES")
    output, output_kind, _ = client.request("GET", output_path, 200)
    thumb, thumb_kind, _ = client.request(
        "GET", f"/pictures/thumbnails/preview-files/{preview_id}.png", 200
    )
    partial, _, range_header = client.request("GET", output_path, 206, byte_range=True)
    client.request("GET", output_path, 401, anonymous=True)
    require(
        output != data
        and output_kind == "video/mp4"
        and is_mp4(output)
        and thumb_kind == "image/png"
        and thumb.startswith(b"\x89PNG\r\n\x1a\n")
        and partial == output[:32]
        and range_header == f"bytes 0-31/{len(output)}",
        "PREVIEW_BYTES",
    )
    return {
        "preview_id": preview_id,
        "revision": 1,
        "fixture_sha256": hashlib.sha256(data).hexdigest(),
        "source_http": source_http,
        "source_sha256": hashlib.sha256(source).hexdigest() if source_retained else None,
        "preview_sha256": hashlib.sha256(output).hexdigest(),
        "thumbnail_sha256": hashlib.sha256(thumb).hexdigest(),
        "source_matches_fixture": source_retained,
        "range_http": 206,
        "anonymous_http": 401,
    }


def local_command(*args):
    child = subprocess.run(args, capture_output=True, check=False, env=HOST_ENV, timeout=10)
    require(
        child.returncode == 0 and not child.stderr and 0 < len(child.stdout) <= 16384,
        "MOUNT_OPERADOR",
    )
    return child.stdout


def storage_probe(cid, image=ZOU_IMAGE):
    require(
        sys.platform == "linux"
        and os.uname().nodename == "vm-media-01"
        and os.geteuid() == 0
        and SHA256.fullmatch(cid)
        and image.startswith("sha256:")
        and SHA256.fullmatch(image[7:]),
        "MOUNT_HOST_CID",
    )
    template = (
        '{"id":{{json .Id}},"image":{{json .Image}},"pid":{{json .State.Pid}},'
        '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
        '"service":{{json (index .Config.Labels "com.docker.compose.service")}},"mounts":{{json .Mounts}}}'
    )
    cmd = (
        "/usr/bin/docker",
        "--host",
        "unix:///run/docker.sock",
        "inspect",
        "--type",
        "container",
        "--format",
        template,
        cid,
    )
    row = json.loads(local_command(*cmd))
    require(
        row["id"] == cid
        and row["image"] == image
        and row["project"] == "agency-media"
        and row["service"] == "zou-api"
        and type(row["pid"]) is int
        and row["pid"] > 0,
        "MOUNT_IDENTIDAD",
    )
    applicable = [
        mount for mount in row["mounts"] if Path(PREVIEW_DIR).is_relative_to(mount["Destination"])
    ]
    selected = max(applicable, key=lambda mount: len(mount["Destination"]), default={})
    require(selected.get("Type") == "bind" and selected.get("RW") is True, "MOUNT_BIND")
    filesystem = json.loads(
        local_command(
            "/usr/bin/findmnt",
            "--json",
            "-N",
            str(row["pid"]),
            "--target",
            PREVIEW_DIR,
            "-o",
            "TARGET,SOURCE,FSTYPE,OPTIONS",
        )
    )["filesystems"]
    require(len(filesystem) == 1 and json.loads(local_command(*cmd)) == row, "MOUNT_CAMBIO")
    info = filesystem[0]
    require(
        info["target"] == selected["Destination"] and "rw" in info["options"].split(","),
        "MOUNT_REAL",
    )
    local = (
        selected["Destination"] == "/var/lib/zou"
        and selected["Source"] == ZOU_STATE
        and info["fstype"] not in ("nfs", "nfs4")
    )
    nas = (
        info["fstype"] == "nfs4"
        and info["source"]
        in {
            "192.168.0.8:/volume2/producciones[/_kitsu/previews]",
            "192.168.0.8:/volume2/producciones/_kitsu/previews",
        }
        and {"vers=4.1", "proto=tcp"} <= set(info["options"].split(","))
        and selected["Source"] == "/mnt/producciones/_kitsu/previews"
        and selected["Destination"] == PREVIEW_DIR
    )
    require(local or nas, "MOUNT_ORIGEN")
    return {
        "preview_mount": "nas_nfs41" if nas else "local",
        "filesystem": info["fstype"],
        "preview_directory": PREVIEW_DIR,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("storage", "preflight", "comment", "preview", "upload"))
    for flag in (
        "cid",
        "project-id",
        "edit-id",
        "task-id",
        "marker",
        "comment-id",
        "preview-id",
        "fixture-sha256",
    ):
        parser.add_argument(f"--{flag}")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument(
        "--image", default=ZOU_IMAGE, help="Digest de la imagen para verificar el montaje"
    )
    parser.add_argument("--source-retained", action="store_true")
    args = parser.parse_args()
    result = {"action": args.action, "success": False}
    try:
        if args.action == "storage":
            require(args.cid is not None, "MOUNT_HOST_CID")
            result.update(storage_probe(args.cid, args.image))
        else:
            require(args.marker is not None, "TAREA_PARAMETROS")
            if args.action == "upload":
                require(args.fixture is not None and args.fixture_sha256 is not None, "FIXTURE_SHA")
                fixture = load_fixture(args.fixture, args.fixture_sha256)
            else:
                fixture = None
            token = sys.stdin.buffer.read(8193).strip()
            require(
                len(token) <= 8192
                and re.fullmatch(rb"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token),
                "JWT_FORMATO",
            )
            result.update(
                step(
                    API(token.decode("ascii")),
                    args.action,
                    args.project_id,
                    args.edit_id,
                    args.task_id,
                    args.marker,
                    comment_id=args.comment_id,
                    preview_id=args.preview_id,
                    fixture=fixture,
                    source_retained=args.source_retained,
                )
            )
        result["success"] = True
    except Failure as error:
        result["error"] = error.args[0]
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        http.client.HTTPException,
        subprocess.SubprocessError,
    ) as error:
        result["error"] = type(error).__name__
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
