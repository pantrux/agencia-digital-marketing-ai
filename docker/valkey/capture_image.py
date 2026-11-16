"""Control de Root: plan local por defecto; acciones separadas y respaldadas."""

import argparse
import ast
import base64
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shlex
import stat
import struct
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
PREFIX = "MEDIA_VALKEY_OCI_CAPTURE_V1_"
CONNECTOR_SHA = "9053e827d2888c194e1794657191c402a1307b272b5e37e4750ff99b44b7ad4a"
CANDIDATE_SHA = "74decf8f583aaf3ba0ca6f4c2aa708c6147c863fb1f5ab2f75fc25ac5e813096"
LAYOUT_SHA = "3c986485b5871df1a15e6fe0c692f50f236e506f744f66eb52e80b0f497f17b9"
ACL_SOURCE_SHA256 = "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b"
PARSER_SHA = "5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda"
UPDATER_SHA = "0fc75c6d082e9e25fa6a5b5146560d7373b3dfab2d701e351749dafb2183b46c"
DESTINATION = ".tools/cache/phase4-built-images/valkey-de6fbe9626fea9f6-v1.tar"
OWNER = "agencia-digital-marketing-ai:valkey-oci-capture:v1"
NAMESPACE = "/var/lib/media-platform/valkey-oci-capture-de6fbe9626fea9f6-v1"
MAX_ARCHIVE = 256 * 1024 * 1024
COMMENTS = (
    "# Para qué: conservar la evidencia de captura OCI de Valkey por etapas.",
    "# Dónde: controlador de Root y recibos propios de la captura Valkey v1.",
    "# Riesgo al rotar: una identidad o hash diferente bloquea la siguiente etapa; no adoptar archivos existentes.",
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def connector_from_source(raw):
    if sha(raw) != CONNECTOR_SHA:
        raise ValueError("El conector SSH no coincide con su fuente fijada.")
    tree = ast.parse(raw.decode("utf-8"))
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "connect_ssh"]
    if len(selected) != 1:
        raise ValueError("No existe una única función SSH fijada.")
    namespace = {"base64": base64, "hashlib": hashlib, "io": io, "importlib": importlib}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "connector-fijado", "exec"), namespace)
    return namespace["connect_ssh"]


def proposal():
    candidate = (PROJECT / ".tools/evidence/phase4-valkey-alpine-candidate.json").read_bytes()
    if sha(candidate) != CANDIDATE_SHA:
        raise ValueError("El candidato fue alterado.")
    supports = {
        "docker/build_component_v2.py": CONNECTOR_SHA,
        "docker/host_layout.py": LAYOUT_SHA,
        "sre/backup_tools_access.py": ACL_SOURCE_SHA256,
        "scripts/ci/check_env.py": PARSER_SHA,
        ".tools/vault_updates.py": UPDATER_SHA,
    }
    for path, expected in supports.items():
        if sha((PROJECT / path).read_bytes()) != expected:
            raise ValueError("Un soporte del plan fue alterado.")
    return {
        "schema": 1,
        "candidate": json.loads(candidate),
        "binary_accepted": False,
        "namespace": NAMESPACE,
        "owner": OWNER,
        "destination": str(PROJECT / DESTINATION),
        "max_archive_bytes": 256 * 1024 * 1024,
        "source_sha256": sha(Path(__file__).read_bytes()),
        "remote_sha256": sha(Path(__file__).with_name("capture_image_remote.py").read_bytes()),
        "supports": supports,
        "required_fields": [
            PREFIX + key
            for key in (
                "ROOT",
                "OWNER",
                "IMAGE",
                "MANIFEST_DIGEST",
                "CONFIG_DIGEST",
                "LOCAL_ARCHIVE_PATH",
                "PROFILE_SHA256",
                "CONTROLLER_SHA256",
                "REMOTE_SHA256",
            )
        ],
    }


def load_pinned(path, checksum):
    raw = path.read_bytes()
    if sha(raw) != checksum:
        raise ValueError("La fuente auxiliar no coincide con su pin.")
    namespace = {"__name__": "valkey_pinned", "__file__": str(path)}
    exec(compile(raw, str(path), "exec"), namespace)
    return namespace


def stamp(info):
    # Sólo campos comunes entre lstat y fstat; ctime se compara dentro de cada API.
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_nlink,
        info.st_mtime_ns,
    )


def same_api_stamp(info):
    return (*stamp(info), info.st_ctime_ns)


def read_values(*, path=None, acl=None, parser=None):
    path = path or PROJECT / ".env"
    if acl is None:
        access = load_pinned(PROJECT / "sre/backup_tools_access.py", ACL_SOURCE_SHA256)

        def acl(target):
            return access["check_access"](target, private=True)

    if parser is None:
        parser = load_pinned(PROJECT / "scripts/ci/check_env.py", PARSER_SHA)["parse_env"]
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or getattr(before, "st_file_attributes", 0) & 0x400
        or before.st_size > 8 * 1024 * 1024
    ):
        raise ValueError("La bóveda no es un archivo regular privado dentro del límite de 8 MiB.")
    acl(path)
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if stamp(opened) != stamp(before):
            raise ValueError("La identidad de la bóveda cambió.")
        raw = source.read(8 * 1024 * 1024 + 1)
        if len(raw) != opened.st_size or same_api_stamp(
            os.fstat(source.fileno())
        ) != same_api_stamp(opened):
            raise ValueError("La bóveda cambió durante la lectura.")
    values, errors = parser(raw.decode("utf-8-sig"))
    if errors:
        raise ValueError("El análisis literal de la bóveda fue rechazado.")
    acl(path)
    if same_api_stamp(path.lstat()) != same_api_stamp(before):
        raise ValueError("La bóveda cambió después del análisis.")
    return values


def receive_archive(stream, target, size, checksum, *, require_eof=True, validate=None):
    """Conserva el parcial ante errores y jamás sustituye un destino existente."""
    if (
        type(size) is not int
        or not 0 < size <= MAX_ARCHIVE
        or not isinstance(checksum, str)
        or not re.fullmatch(r"[0-9a-f]{64}", checksum)
    ):
        raise ValueError("Tamaño o SHA256 de captura inválido.")
    if os.path.lexists(target):
        raise FileExistsError("El destino ya existe; no se adopta ni reemplaza.")
    partial = target.with_suffix(target.suffix + ".partial")
    descriptor = os.open(
        partial,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as output:
        original = os.fstat(output.fileno())
        sha256, remaining = hashlib.sha256(), size
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise ValueError("La captura llegó truncada.")
            if len(chunk) > remaining:
                raise ValueError("La captura excedió su longitud declarada.")
            output.write(chunk)
            sha256.update(chunk)
            remaining -= len(chunk)
        if (require_eof and stream.read(1)) or sha256.hexdigest() != checksum:
            raise ValueError("La longitud o SHA256 de la captura no coincide.")
        output.flush()
        os.fsync(output.fileno())
        info = os.fstat(output.fileno())
        if info.st_nlink != 1 or (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino):
            raise ValueError("El parcial perdió su identidad única.")
    if validate is not None:
        validate(partial)
    current = partial.lstat()
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino, current.st_size) != (
        info.st_dev,
        info.st_ino,
        size,
    ):
        raise ValueError("El parcial cambió antes de la publicación.")
    os.link(partial, target)
    expected_identity = (info.st_dev, info.st_ino, size)
    published, own_partial = target.lstat(), partial.lstat()
    if (
        (published.st_dev, published.st_ino, published.st_size) != expected_identity
        or (own_partial.st_dev, own_partial.st_ino, own_partial.st_size) != expected_identity
        or not stat.S_ISREG(published.st_mode)
        or published.st_nlink != 2
    ):
        raise ValueError("La publicación no conserva el inodo propio; se preservan ambos nombres.")
    partial.unlink()
    final = target.lstat()
    if final.st_nlink != 1:
        raise ValueError("La captura publicada tiene enlaces adicionales.")
    return {
        "path": str(target),
        "size": size,
        "sha256": checksum,
        "directory": {"device": final.st_dev, "inode": final.st_ino},
        "binary_accepted": False,
    }


def check_values(values, plan, profile_sha):
    expected = {
        "ROOT": plan["namespace"],
        "OWNER": plan["owner"],
        "IMAGE": plan["candidate"]["image"],
        "MANIFEST_DIGEST": plan["candidate"]["platform_digest"],
        "CONFIG_DIGEST": plan["candidate"]["manifest"]["config"]["digest"],
        "LOCAL_ARCHIVE_PATH": plan["destination"],
        "PROFILE_SHA256": profile_sha,
        "CONTROLLER_SHA256": plan["source_sha256"],
        "REMOTE_SHA256": plan["remote_sha256"],
    }
    if any(values.get(PREFIX + key) != value for key, value in expected.items()):
        raise ValueError("Los pins respaldados no coinciden con el plan actual.")


def backed_receipt(values, stage):
    raw = base64.b64decode(values[PREFIX + stage.upper() + "_RECEIPT_B64"], validate=True)
    if len(raw) > 65536 or sha(raw) != values[PREFIX + stage.upper() + "_RECEIPT_SHA256"]:
        raise ValueError("El recibo respaldado no coincide con su SHA físico.")
    return json.loads(raw)


def make_request(stage, plan, values):
    return {
        "stage": stage,
        "profile": plan,
        "profile_sha256": sha(canonical(plan)),
        "layout": json.loads(
            base64.b64decode(values["MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64"], validate=True)
        ),
        "layout_source_b64": base64.b64encode(
            (PROJECT / "docker/host_layout.py").read_bytes()
        ).decode(),
        "prepare": backed_receipt(values, "prepare") if stage != "prepare" else None,
        "capture": backed_receipt(values, "capture") if stage == "fetch" else None,
    }


BOOTSTRAP = """import hashlib,json,struct,sys
def exact(n):
 b=bytearray()
 while len(b)<n:
  x=sys.stdin.buffer.read(n-len(b))
  if not x: raise ValueError("Truncado")
  b.extend(x)
 return bytes(b)
def read(limit):
 n=struct.unpack(">I",exact(4))[0]
 if not 0<n<=limit: raise ValueError("Limite")
 return exact(n)
source=read(131072)
if hashlib.sha256(source).hexdigest()!=sys.argv[1]: raise ValueError("Hash")
request=json.loads(read(524288).decode("utf-8"))
ns={"__name__":"valkey_remote","__file__":"capture_image_remote.py"}
exec(compile(source,"capture_image_remote-fijado","exec"),ns)
raise SystemExit(ns["main"](request))
"""


def remote_command(remote_sha):
    if not re.fullmatch(r"[0-9a-f]{64}", remote_sha):
        raise ValueError("Pin remoto inválido.")
    return (
        "sudo -n /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin "
        "/usr/bin/python3 -I -S -B -c " + shlex.quote(BOOTSTRAP) + " " + remote_sha
    )


class ChannelStream:
    def __init__(self, channel, seconds):
        self.channel, self.until = channel, time.monotonic() + seconds
        self.pulse, self.stderr_bytes = 0, 0

    def drain_stderr(self):
        while self.channel.recv_stderr_ready():
            self.stderr_bytes += len(self.channel.recv_stderr(65536))
            if self.stderr_bytes > 65536:
                raise ValueError("El diagnóstico remoto excedió su límite.")

    def read(self, size):
        if type(size) is not int or size <= 0:
            raise ValueError("Lectura debe tener límite positivo.")
        while True:
            if time.monotonic() >= self.until:
                raise TimeoutError("El canal SSH agotó su plazo.")
            self.drain_stderr()
            if self.channel.recv_ready():
                return self.channel.recv(min(size, 65536))
            if self.channel.eof_received or self.channel.closed:
                return b""
            if time.monotonic() >= self.pulse and self.channel.send_ready():
                self.channel.sendall(b".")
                self.pulse = time.monotonic() + 0.5
            time.sleep(0.01)

    def exact(self, size):
        result = bytearray()
        while len(result) < size:
            chunk = self.read(size - len(result))
            if not chunk:
                raise ValueError("El flujo remoto llegó truncado.")
            result.extend(chunk)
        return bytes(result)

    def frame(self):
        size = struct.unpack(">I", self.exact(4))[0]
        if not 0 < size <= 65536:
            raise ValueError("Marco remoto fuera del límite.")
        return json.loads(self.exact(size))

    def finish(self):
        if self.read(1):
            raise ValueError("El remoto añadió bytes fuera del protocolo.")
        while not self.channel.exit_status_ready():
            if time.monotonic() >= self.until:
                raise TimeoutError("No llegó el estado final remoto.")
            self.drain_stderr()
            time.sleep(0.01)
        self.drain_stderr()
        if self.stderr_bytes:
            raise ValueError(
                "El remoto emitió un diagnóstico no previsto; no se acepta la captura."
            )
        if self.channel.recv_exit_status() != 0:
            raise ValueError("El remoto no terminó con éxito.")


def verify_result(remote, result, request):
    value = remote["check_envelope"](result)
    if (
        value.get("success") is not True
        or value.get("stage") != request["stage"]
        or value.get("owner") != OWNER
        or value.get("path") != NAMESPACE
        or value.get("profile_sha256") != request["profile_sha256"]
        or value.get("binary_accepted") is not False
        or set(value.get("directory", {})) != {"device", "inode"}
        or any(type(x) is not int or x <= 0 for x in value.get("directory", {}).values())
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("marker_sha256", "")))
    ):
        raise ValueError("El resultado remoto no acredita esta etapa propia.")
    if request["prepare"] is not None:
        prepared = remote["check_envelope"](request["prepare"])
        if (
            value["directory"] != prepared["directory"]
            or value["marker_sha256"] != prepared["marker_sha256"]
        ):
            raise ValueError("La etapa cambió de directorio o marcador.")
    return value


def exchange(stage, request, values, remote):
    source = Path(__file__).with_name("capture_image_remote.py").read_bytes()
    if sha(source) != request["profile"]["remote_sha256"]:
        raise ValueError("La fuente remota cambió después del plan.")
    connect = connector_from_source((PROJECT / "docker/build_component_v2.py").read_bytes())
    client, channel = None, None
    try:
        client = connect(values)
        channel = client.get_transport().open_session(timeout=10)
        channel.settimeout(10)
        channel.exec_command(remote_command(sha(source)))
        for raw in (source, canonical(request)):
            channel.sendall(struct.pack(">I", len(raw)) + raw)
        stream = ChannelStream(channel, 330 if stage == "capture" else 150)
        if stage != "fetch":
            result = stream.frame()
            verify_result(remote, result, request)
            stream.finish()
            return result
        header = stream.frame()
        if header != {"stage": "fetch-header", "capture": request["capture"]}:
            raise ValueError("Cabecera de transferencia no coincide con la captura respaldada.")
        archive = remote["check_envelope"](request["capture"])["archive"]
        tail = []

        def validate(path):
            with path.open("rb") as captured:
                remote["validate_oci"](captured, request["profile"]["candidate"])
            result = stream.frame()
            value = verify_result(remote, result, request)
            if (
                value.get("archive_sha256") != archive["sha256"]
                or value.get("archive_bytes") != archive["size"]
                or value.get("capture_receipt_sha256") != request["capture"]["sha256"]
            ):
                raise ValueError("El cierre remoto no corresponde al TAR transferido.")
            stream.finish()
            tail.append(result)

        published = receive_archive(
            stream,
            Path(request["profile"]["destination"]),
            archive["size"],
            archive["sha256"],
            require_eof=False,
            validate=validate,
        )
        return remote["envelope"]({**tail[0]["value"], "local_archive": published})
    finally:
        if channel is not None:
            channel.close()
        if client is not None:
            client.close()


def write_exclusive(path, raw):
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def operate(stage, plan, values):
    profile_sha = sha(canonical(plan))
    check_values(values, plan, profile_sha)
    remote = load_pinned(Path(__file__).with_name("capture_image_remote.py"), plan["remote_sha256"])
    request = make_request(stage, plan, values)
    remote["check_request"](request)
    evidence = PROJECT / ".tools/evidence" / ("phase4-valkey-oci-capture-v1-" + stage)
    intent = evidence.with_suffix(".intent.json")
    result_path = evidence.with_suffix(".result.json")
    if os.path.lexists(result_path):
        raise FileExistsError("Ya existe un resultado de esta etapa; no se repite.")
    if stage == "fetch":
        destination = Path(plan["destination"])
        for path in (destination, destination.with_suffix(".tar.partial")):
            if os.path.lexists(path):
                raise FileExistsError("El destino o parcial ya existe; no se adopta.")
        for path in (destination.parent, *destination.parent.parents):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("La ruta de captura contiene un enlace o punto de reanálisis.")
    write_exclusive(
        intent, canonical({"stage": stage, "profile_sha256": profile_sha, "owner": OWNER})
    )
    try:
        result = exchange(stage, request, values, remote)
        raw = canonical(result)
        write_exclusive(result_path, raw)
    except Exception as failure:
        write_exclusive(
            result_path,
            canonical(
                {
                    "success": False,
                    "stage": stage,
                    "owner": OWNER,
                    "error_type": type(failure).__name__,
                    "profile_sha256": profile_sha,
                }
            ),
        )
        raise
    updates = {
        PREFIX + stage.upper() + "_RECEIPT_PATH": str(result_path),
        PREFIX + stage.upper() + "_RECEIPT_SHA256": sha(raw),
        PREFIX + stage.upper() + "_RECEIPT_B64": base64.b64encode(raw).decode(),
    }
    if stage == "fetch":
        updates.update(
            {
                PREFIX + "ARCHIVE_SHA256": result["value"]["local_archive"]["sha256"],
                PREFIX + "ARCHIVE_SIZE": str(result["value"]["local_archive"]["size"]),
            }
        )
    updater = load_pinned(PROJECT / ".tools/vault_updates.py", UPDATER_SHA)
    updater["update"](updates, comments={key: COMMENTS for key in updates})
    return {
        "success": True,
        "stage": stage,
        "receipt_path": str(result_path),
        "receipt_sha256": sha(raw),
        "binary_accepted": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", nargs="?", choices=("plan", "prepare", "capture", "fetch"), default="plan"
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--root-update-vault", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = proposal()
        if args.stage == "plan":
            print(canonical({"profile": plan, "profile_sha256": sha(canonical(plan))}).decode())
            return 0
        if (
            not args.execute
            or not args.root_update_vault
            or args.expected_profile_sha256 != sha(canonical(plan))
        ):
            raise ValueError("La operación requiere ejecución explícita y perfil respaldado.")
        result = operate(args.stage, plan, read_values())
        print(canonical(result).decode())
        return 0
    except Exception:
        print(
            canonical(
                {
                    "success": False,
                    "stage": args.stage,
                    "error": "PRECONDICION_O_OPERACION_RECHAZADA",
                }
            ).decode()
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
