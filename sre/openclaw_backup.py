"""Captura y restaura únicamente un staging de agencia acreditado."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

OWNER = "agencia-digital-marketing-ai:openclaw-backup:v1"
AGENTS = frozenset(
    ("agency-account", "agency-critic", "agency-director", "agency-editor", "agency-strategist")
)
SCOPES = ("synthetic", "agency-state")
ARCHIVE = "openclaw.tar.zst"
SYNTHETIC_ARCHIVE = "openclaw.synthetic.tar.zst"
MANIFEST = "manifest.json"
SHA256 = re.compile(r"[a-f0-9]{64}")
COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
RESERVED = re.compile(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", re.IGNORECASE)
MODEL = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MAX_MANIFEST = 64 * 1024
MAX_FILES = 256
MAX_FILE = 4 * 1024**2
MAX_PAYLOAD = 32 * 1024**2
MAX_TAR = MAX_PAYLOAD + MAX_MANIFEST + (MAX_FILES + 2) * 1024
MAX_FRAME = MAX_TAR + 1024**2
BLOCK = tarfile.BLOCKSIZE
TRAILER = bytes(2 * BLOCK)
ZSTD_MAGIC = bytes.fromhex("28b52ffd")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def valid_pin(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def unique_object(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), "El JSON contiene claves repetidas.")
    return result


def component(name):
    require(
        COMPONENT.fullmatch(name)
        and not name.endswith(".")
        and not RESERVED.fullmatch(name.split(".", 1)[0]),
        "Componente de ruta no permitido.",
    )
    return name


def payload_path(name):
    require(isinstance(name, str) and len(name) <= 100, "Ruta de archivo inválida.")
    parts = name.split("/")
    require(len(parts) >= 2 and parts[0] in AGENTS, "Archivo fuera de los cinco agentes.")
    for part in parts:
        component(part)
    require(
        (len(parts) == 2 and parts[1] in {"config.json", "MEMORY.md"})
        or (len(parts) >= 3 and parts[1] == "memory" and parts[-1].endswith(".md")),
        "Se esperaba configuración proyectada o memoria Markdown.",
    )
    return parts


def read_manifest(raw, scope):
    require(
        scope in SCOPES and 0 < len(raw) <= MAX_MANIFEST, "Ámbito o manifiesto fuera de límites."
    )
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    require(
        isinstance(value, dict)
        and set(value) == {"schema", "owner", "scope", "files"}
        and type(value["schema"]) is int
        and value["schema"] == 1
        and value["owner"] == OWNER
        and value["scope"] == scope,
        "Versión, propietario o ámbito del manifiesto no coincide.",
    )
    require(canonical(value) == raw, "El manifiesto debe usar JSON canónico UTF-8.")
    rows = value["files"]
    require(isinstance(rows, list) and 5 <= len(rows) <= MAX_FILES, "Número de archivos inválido.")
    entries, prefixes, total = {}, {}, 0
    for row in rows:
        require(
            isinstance(row, dict)
            and set(row) == {"path", "size", "sha256"}
            and type(row["size"]) is int
            and 0 <= row["size"] <= MAX_FILE
            and valid_pin(row["sha256"]),
            "Entrada del manifiesto inválida.",
        )
        name = row["path"]
        parts = payload_path(name)
        require(name not in entries, "Archivo duplicado en el manifiesto.")
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            label = (prefix, end == len(parts))
            require(
                prefixes.setdefault(prefix.lower(), label) == label,
                "Colisión de ruta o de mayúsculas en el manifiesto.",
            )
        entries[name] = row
        total += row["size"]
    require(total <= MAX_PAYLOAD, "Tamaño total del staging excesivo.")
    require(list(entries) == sorted(entries), "Las rutas deben venir ordenadas.")
    require(
        {f"{agent}/config.json" for agent in AGENTS} <= entries.keys(),
        "Falta la configuración proyectada de algún agente.",
    )
    return entries


def check_payload(row, data):
    require(
        len(data) == row["size"] and digest(data) == row["sha256"],
        "Un archivo difiere del tamaño o SHA256 acreditado.",
    )
    name = row["path"]
    if name.endswith("/config.json"):
        require(len(data) <= 4096, "Configuración proyectada excesiva.")
        config = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
        require(
            isinstance(config, dict)
            and {"id", "model"} <= config.keys() <= {"id", "model", "name"}
            and config["id"] == name.split("/")[0]
            and isinstance(config["model"], str)
            and len(config["model"]) <= 128
            and MODEL.fullmatch(config["model"])
            and (
                "name" not in config
                or (
                    isinstance(config["name"], str)
                    and 0 < len(config["name"]) <= 128
                    and config["name"].isprintable()
                )
            ),
            "La configuración no es una proyección de ese agente.",
        )


def tar_header(name, size):
    member = tarfile.TarInfo(name)
    member.mode = 0o600
    member.size = size
    return member.tobuf(format=tarfile.USTAR_FORMAT)


def pack_tar(manifest, files, scope):
    entries = read_manifest(manifest, scope)
    require(entries.keys() == files.keys(), "El inventario de archivos no es exacto.")
    for name, row in entries.items():
        check_payload(row, files[name])
    parts = []
    for name, data in [(MANIFEST, manifest), *((name, files[name]) for name in entries)]:
        parts.extend((tar_header(name, len(data)), data, bytes(-len(data) % BLOCK)))
    return b"".join([*parts, TRAILER])


def unpack_tar(archive, scope):
    require(len(archive) <= MAX_TAR, "TAR expandido excesivo.")
    try:
        first = tarfile.TarInfo.frombuf(archive[:BLOCK], "ascii", "strict")
    except (tarfile.HeaderError, UnicodeError) as exc:
        raise ValueError("Cabecera inicial TAR inválida.") from exc
    require(0 < first.size <= MAX_MANIFEST, "Tamaño del manifiesto TAR inválido.")
    offset = 0

    def member(name, size):
        nonlocal offset
        require(
            archive[offset : offset + BLOCK] == tar_header(name, size),
            "Cabecera TAR inesperada, no canónica o de un tipo prohibido.",
        )
        start = offset + BLOCK
        end = start + size
        offset = end + (-size % BLOCK)
        require(offset <= len(archive) and not any(archive[end:offset]), "Bloque TAR inválido.")
        return archive[start:end]

    manifest = member(MANIFEST, first.size)
    files = {}
    for name, row in read_manifest(manifest, scope).items():
        data = member(name, row["size"])
        check_payload(row, data)
        files[name] = data
    require(archive[offset:] == TRAILER, "El TAR tiene entradas extra o un cierre inválido.")
    return manifest, files


def private(info, kind, mode):
    require(
        kind(info.st_mode)
        and info.st_uid == os.geteuid()
        and info.st_gid == os.getegid()
        and stat.S_IMODE(info.st_mode) == mode
        and (mode != 0o600 or info.st_nlink == 1),
        "Nodo no ordinario, sin propiedad privada exacta o con enlaces.",
    )


@contextmanager
def directory(name, parent=None):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def private_root(root):
    require(sys.platform == "linux", "La operación de archivos requiere Linux.")
    root = Path(root)
    require(
        root.is_absolute()
        and len(root.parts) > 1
        and not ({"..", ".openclaw"} & set(root.parts))
        and root.anchor == "/",
        "La raíz debe ser absoluta, local y ajena al árbol activo de OpenClaw.",
    )
    with ExitStack() as stack:
        fd = stack.enter_context(directory("/"))
        for part in root.parts[1:]:
            fd = stack.enter_context(directory(part, fd))
        private(os.fstat(fd), stat.S_ISDIR, 0o700)
        yield fd


def signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def read_file(parent, name, maximum):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    private(before, stat.S_ISREG, 0o600)
    require(0 <= before.st_size <= maximum, "Archivo fuera del límite de tamaño.")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        require(signature(os.fstat(fd)) == signature(before), "El nodo cambió antes de leerlo.")
        data = stream.read(maximum + 1)
        require(
            len(data) == before.st_size
            and signature(before)
            == signature(os.fstat(fd))
            == signature(os.stat(name, dir_fd=parent, follow_symlinks=False)),
            "El archivo cambió durante la lectura.",
        )
    return data


def file_tree(names):
    tree = {}
    for path in names:
        *parents, name = path.split("/")
        level = tree
        for parent in parents:
            level = level.setdefault(parent, {})
        level[name] = path
    return tree


def read_tree(fd, tree, limits, result):
    before = os.fstat(fd)
    require(set(os.listdir(fd)) == tree.keys(), "El staging contiene entradas ausentes o extra.")
    for name, child in tree.items():
        if isinstance(child, dict):
            with directory(name, fd) as child_fd:
                private(os.fstat(child_fd), stat.S_ISDIR, 0o700)
                read_tree(child_fd, child, limits, result)
                require(
                    signature(os.fstat(child_fd))
                    == signature(os.stat(name, dir_fd=fd, follow_symlinks=False)),
                    "Cambió la identidad de un directorio del staging.",
                )
        else:
            result[child] = read_file(fd, name, limits[child])
    require(
        set(os.listdir(fd)) == tree.keys() and signature(os.fstat(fd)) == signature(before),
        "El árbol cambió durante la lectura.",
    )


def read_staging(root, staging, pin, scope):
    require(valid_pin(pin), "Se requiere un SHA256 externo completo del manifiesto.")
    try:
        with private_root(root) as root_fd, directory(component(staging), root_fd) as fd:
            private(os.fstat(fd), stat.S_ISDIR, 0o700)
            manifest = read_file(fd, MANIFEST, MAX_MANIFEST)
            require(digest(manifest) == pin, "El SHA256 del manifiesto no coincide.")
            entries = read_manifest(manifest, scope)
            limits = {name: row["size"] for name, row in entries.items()}
            limits[MANIFEST] = len(manifest)
            files = {}
            read_tree(fd, file_tree(limits), limits, files)
            require(files.pop(MANIFEST) == manifest, "El manifiesto cambió durante la lectura.")
            return manifest, files
    except OSError as exc:
        raise ValueError("No se pudo leer el staging ordinario y acreditado.") from exc


def write_tree(fd, tree, files):
    for name, child in tree.items():
        if isinstance(child, dict):
            os.mkdir(name, 0o700, dir_fd=fd)
            with directory(name, fd) as child_fd:
                write_tree(child_fd, child, files)
        else:
            file_fd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            with os.fdopen(file_fd, "wb") as stream:
                stream.write(files[child])
                stream.flush()
                os.fsync(file_fd)
    os.fsync(fd)


def publish(root, destination, files):
    with private_root(root) as fd:
        os.mkdir(component(destination), 0o700, dir_fd=fd)
        with directory(destination, fd) as target:
            private(os.fstat(target), stat.S_ISDIR, 0o700)
            write_tree(target, file_tree(files), files)
        os.fsync(fd)


def zstd(data, *, decompress=False):
    maximum = MAX_TAR if decompress else MAX_FRAME
    args = ["-q", "-M64MB", "-d", "-c"] if decompress else ["-q", "-T1", "--check", "-c"]
    # prlimit impide que un frame amplificado agote el archivo anónimo en memoria.
    with os.fdopen(os.memfd_create("openclaw-zstd", os.MFD_CLOEXEC), "w+b") as output:
        subprocess.run(
            ["/usr/bin/prlimit", f"--fsize={maximum}:{maximum}", "--", "/usr/bin/zstd", *args],
            input=data,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env={"LC_ALL": "C"},
            timeout=30,
            check=True,
        )
        output.seek(0)
        content = output.read(maximum + 1)
    require(0 < len(content) <= maximum, "Salida Zstandard inválida o excesiva.")
    return content


def read_archive(root, source, name, pin):
    require(valid_pin(pin), "Se requiere un SHA256 externo completo del archivo.")
    with private_root(root) as root_fd, directory(component(source), root_fd) as fd:
        private(os.fstat(fd), stat.S_ISDIR, 0o700)
        data = read_file(fd, name, MAX_FRAME)
    require(
        digest(data) == pin and data.startswith(ZSTD_MAGIC), "SHA256 o formato Zstandard inválido."
    )
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "restore"))
    parser.add_argument("root", type=Path)
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("sha256")
    parser.add_argument("scope", choices=SCOPES)
    args = parser.parse_args(argv)
    os.umask(0o077)
    synthetic = args.scope == "synthetic"
    archive_name = SYNTHETIC_ARCHIVE if synthetic else ARCHIVE
    try:
        component(args.destination)
        if args.action == "capture":
            manifest, files = read_staging(args.root, args.source, args.sha256, args.scope)
            tar = pack_tar(manifest, files, args.scope)
            frame = zstd(tar)
            require(
                frame.startswith(ZSTD_MAGIC) and zstd(frame, decompress=True) == tar,
                "Comprobación Zstandard fallida.",
            )
            publish(args.root, args.destination, {archive_name: frame})
        else:
            frame = read_archive(args.root, args.source, archive_name, args.sha256)
            manifest, files = unpack_tar(zstd(frame, decompress=True), args.scope)
            publish(args.root, args.destination, {MANIFEST: manifest, **files})
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        print('{"success":false,"error":"OPENCLAW_BACKUP_REJECTED"}', file=sys.stderr)
        return 1
    receipt = {
        "success": True,
        "action": args.action,
        "scope": args.scope,
        "synthetic": synthetic,
        "source_attestation": "external-staging-manifest"
        if args.action == "capture"
        else "external-archive-sha256",
        "agents": len(AGENTS),
        "files": len(files),
        "manifest_sha256": digest(manifest),
        "archive_name": archive_name,
        "archive_sha256": digest(frame),
        "archive_size": len(frame),
    }
    print(canonical(receipt).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
