"""Recibe una orden firmada, publica su directorio en el NAS y devuelve un recibo."""

import base64
import hashlib
import hmac
import json
import os
import stat
import sys
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def read_file(path, expected_uid, allowed_modes, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) not in allowed_modes
            or not 0 < info.st_size <= maximum
        ):
            raise ValueError("Archivo privado inválido")
        raw = stream.read(maximum + 1)
        if len(raw) != info.st_size:
            raise ValueError("Lectura incompleta")
        return raw


def load_profile(path):
    if not (path.is_absolute() and path.resolve(strict=True) == path):
        raise ValueError("Ruta del perfil inválida")
    root = path.parent
    for part in (root, *root.parents):
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Directorio sin custodia de administrador")
    profile = json.loads(read_file(path, 0, {0o400, 0o444}, 8192))
    files = {"sync_storage.py", "sync_protocol.py", "sync_provision.py"}
    if set(profile["sources"]) != files or profile["actor_uid"] != os.getuid():
        raise ValueError("Identidad o fuentes inesperadas")
    for name, digest in profile["sources"].items():
        if hashlib.sha256(read_file(root / name, 0, {0o444, 0o555}, 131072)).hexdigest() != digest:
            raise ValueError("Fuente distinta de la revisada")
    sys.path.insert(0, str(root))
    return profile


def provision(profile, encoded, *, reconcile_legacy_v2=False):
    from sync_protocol import verify_request, verify_source_request
    from sync_provision import ProjectsProof, ensure_project
    from sync_storage import RootProof, SyncStorage

    if not 0 < len(encoded) <= 8192:
        raise ValueError("Tamaño de solicitud inválido")
    envelope = json.loads(encoded)
    if set(envelope) != {"body", "timestamp", "signature"}:
        raise ValueError("Solicitud inválida")
    body = base64.b64decode(envelope["body"], validate=True)
    key = read_file(Path(profile["hmac_file"]), os.getuid(), {0o400, 0o600}, 128)
    maximum = profile["max_object_bytes"]
    verifier = (
        verify_source_request if str(envelope["signature"]).startswith("v2=") else verify_request
    )
    verified = verifier(
        body, envelope["timestamp"], envelope["signature"], key, maximum, lambda: int(time.time())
    )
    media_root = profile["media_root"]
    media_proof = RootProof(**profile["media_proof"])
    projects_proof = ProjectsProof(**profile["projects_proof"])
    filesystem_id = profile.get("filesystem_id")
    if "filesystem_id" in profile:
        if filesystem_id is None or media_proof.device != projects_proof.device:
            raise ValueError("Perfil de volumen inválido")
        # El número actual se confirma al abrir la raíz por descriptor.
        device = os.stat(media_root, follow_symlinks=False).st_dev
        media_proof = replace(media_proof, device=device)
        projects_proof = replace(projects_proof, device=device)
    storage = SyncStorage(media_root, media_proof, max_object_bytes=maximum)
    project = ensure_project(
        storage,
        projects_proof,
        verified,
        filesystem_id=filesystem_id,
        reconcile_legacy_v2=reconcile_legacy_v2,
    )
    reply = {
        "fingerprint": verified.fingerprint,
        "inode": project.inode,
        "created": project.created,
    }
    raw = json.dumps(reply, separators=(",", ":")).encode()
    message = b"studio-nas-provision-v1\n" + envelope["timestamp"].encode("ascii") + b"\n" + raw
    return raw, "v1=" + hmac.digest(key, message, "sha256").hex()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(4)
        super().setup()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = self.headers.get_all("Content-Length", [])
        content = self.headers.get_all("Content-Type", [])
        if self.client_address[0] != "192.168.0.85" or self.path != "/v1/provision":
            self.send_error(404)
            return
        if (
            len(length) != 1
            or not length[0].isascii()
            or not length[0].isdigit()
            or len(length[0]) > 4
            or not 0 < int(length[0]) <= 8192
            or content != ["application/json"]
            or self.headers.get("Transfer-Encoding") is not None
        ):
            self.send_error(400)
            return
        try:
            raw, signature = provision(self.server.profile, self.rfile.read(int(length[0])))
        except (
            OSError,
            ValueError,
            AttributeError,
            KeyError,
            TypeError,
            ImportError,
            RecursionError,
        ):
            self.send_error(422)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Signature", signature)
        self.end_headers()
        self.wfile.write(raw)


def main():
    profile = load_profile(Path(sys.argv[1]))
    if sys.argv[2:] == ["serve"]:
        with HTTPServer(("192.168.0.8", 3148), Handler) as server:
            server.profile = profile
            server.serve_forever()
        return 0
    if sys.argv[2:] not in ([], ["reconcile-v2"]):
        raise ValueError("Modo inválido")
    raw, _ = provision(
        profile,
        sys.stdin.buffer.read(8193),
        reconcile_legacy_v2=sys.argv[2:] == ["reconcile-v2"],
    )
    sys.stdout.buffer.write(raw + b"\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        OSError,
        ValueError,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ImportError,
        RecursionError,
    ):
        sys.exit(1)
