"""Prepara el contexto Docker CPU de SupoClip desde los tres artefactos verificados."""

import argparse
import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
ENGINE_PREFIX = "llama-b12248/"
ENGINE_NAME = re.compile(r"(?:llama-server|LICENSE|lib[A-Za-z0-9-]+\.so(?:\.[0-9]+)*)")
LIBGOMP = "libgomp.deb"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_file(source, entry):
    path = source / entry["file"]
    if (
        path.resolve(strict=True) != path
        or not path.is_file()
        or path.stat().st_size != entry["bytes"]
        or digest(path) != entry["sha256"]
    ):
        raise ValueError(f"Artefacto distinto: {entry['file']}")
    return path


def copy_engine(source, output):
    with tarfile.open(source) as archive:
        selected = {}
        for member in archive:
            name = member.name.removeprefix(ENGINE_PREFIX)
            if not member.name.startswith(ENGINE_PREFIX) or not ENGINE_NAME.fullmatch(name):
                continue
            if name in selected or not (member.isfile() or member.issym()):
                raise ValueError(f"Miembro de motor inválido: {name}")
            selected[name] = member

        names = set(selected)
        if (
            not {"LICENSE", "llama-server", "libllama-server-impl.so", "libggml-cpu-x64.so"}
            <= names
        ):
            raise ValueError("Cierre del motor incompleto")
        for name, member in selected.items():
            seen = {name}
            while member.issym():
                target = member.linkname
                if PurePosixPath(target).name != target or target not in names or target in seen:
                    raise ValueError(f"Enlace de motor inválido: {name}")
                seen.add(target)
                member = selected[target]

        with (
            output.open("xb") as stream,
            gzip.GzipFile(fileobj=stream, filename="", mode="wb", mtime=0) as compressed,
            tarfile.open(fileobj=compressed, mode="w|") as package,
        ):
            for name, member in sorted(selected.items()):
                item = tarfile.TarInfo(name)
                if member.issym():
                    item.type = tarfile.SYMTYPE
                    item.linkname = member.linkname
                    package.addfile(item)
                else:
                    raw = archive.extractfile(member).read()
                    if name != "LICENSE" and not raw.startswith(b"\x7fELF\x02\x01"):
                        raise ValueError(f"ELF x64 inválido: {name}")
                    if name != "LICENSE" and raw[18:20] != b"\x3e\x00":
                        raise ValueError(f"Arquitectura ELF inválida: {name}")
                    item.size = len(raw)
                    item.mode = 0o555 if name == "llama-server" else 0o444
                    package.addfile(item, io.BytesIO(raw))
    return len(selected)


def prepare(source, output):
    pins = json.loads((HERE / "local-model-inputs.json").read_bytes())
    inputs = {name: verify_file(source, pins[name]) for name in ("engine", "weights", "libgomp")}
    output.mkdir(mode=0o755)
    payload = output / "payload"
    payload.mkdir(mode=0o755)
    engine = payload / "engine.tar.gz"
    members = copy_engine(inputs["engine"], engine)
    (output / "Dockerfile").write_bytes((HERE / "Dockerfile.local-model").read_bytes())
    (payload / "launch.sh").write_bytes((HERE / "launch-local-model.sh").read_bytes())
    (payload / LIBGOMP).write_bytes(inputs["libgomp"].read_bytes())
    sums = "".join(
        f"{digest(file)}  {file.name}\n" for file in sorted(payload.iterdir()) if file.is_file()
    )
    (payload / "INPUTS.sha256").write_bytes(sums.encode("ascii"))
    receipt = {
        "base": pins["base"],
        "weights_sha256": pins["weights"]["sha256"],
        "engine_members": members,
        "payload": {file.name: digest(file) for file in sorted(payload.iterdir())},
        "dockerfile_sha256": digest(output / "Dockerfile"),
        "success": True,
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source.resolve(strict=True), args.output), sort_keys=True))


if __name__ == "__main__":
    main()
