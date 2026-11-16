"""Prepare the pinned n8n Python runner's urllib3 lock update from local inputs."""

import argparse
import hashlib
import io
import json
import re
import stat
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INPUTS = Path(__file__).with_name("remediation-inputs.json")
INPUTS_SHA = "3530fd875cdac6344405235b2f452e4718edc0fa42492082f83c4756e006fcc3"
RUNNER = "packages/@n8n/task-runner-python"
OLD = "2.7.0"
NEW = "2.8.0"
PYPI_RELEASE = "https://pypi.org/pypi/urllib3/2.8.0/json"
SDIST = {
    "url": "https://files.pythonhosted.org/packages/e3/05/"
    "b17359e1cefb4f909b5e40b1b90a496d987258916dbbf88e842c729f510e/urllib3-2.8.0.tar.gz",
    "hash": "sha256:63bf2ead4c879426ebf22ef2a781eeb4aa3b4ae798a0435506f8687fd5bb9b63",
    "size": 458972,
    "upload-time": "2026-09-15T19:29:36.253Z",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_pinned(path, size, digest):
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == size,
        "Entrada no regular, enlazada o con tamaño distinto.",
    )
    with path.open("rb") as stream:
        raw = stream.read(size + 1)
    require(len(raw) == size and sha(raw) == digest, "SHA256 de entrada distinto.")
    return raw


def originals(inputs, archive_path):
    release = inputs["release"]
    archive = release["source_archive"]
    raw = read_pinned(archive_path, archive["bytes"], archive["sha256"])
    required = {f"{RUNNER}/{name}" for name in ("pyproject.toml", "uv.lock")}
    pins = {row["path"]: row for row in release["source_files"] if row["path"] in required}
    require(set(pins) == required, "Faltan pins originales del runner.")
    prefix = f"n8n-{release['commit']}/"
    outputs = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as source:
        for member in source:
            path = member.name.removeprefix(prefix)
            if path not in required:
                continue
            name = Path(path).name
            pin = pins[path]
            require(
                member.name == prefix + path
                and name not in outputs
                and member.isfile()
                and member.size == pin["bytes"],
                "Miembro del runner distinto del original fijado.",
            )
            with source.extractfile(member) as stream:
                content = stream.read(member.size + 1)
            require(sha(content) == pin["sha256"], "SHA256 del miembro original distinto.")
            outputs[name] = content
    require(set(outputs) == {"pyproject.toml", "uv.lock"}, "TAR del runner incompleto.")
    return outputs


def upgrade_pins(content):
    for relation in ("==", ">="):
        before = f"{relation}{OLD}".encode()
        after = f"{relation}{NEW}".encode()
        require(content.count(before) == 1, "Declaración original de urllib3 no única.")
        content = content.replace(before, after, 1)
    return content


def table_inline(values):
    return "{ " + ", ".join(f"{key} = {json.dumps(value)}" for key, value in values.items()) + " }"


def prepare(inputs, archive_path, wheel_path):
    upstream = originals(inputs, archive_path)
    candidates = [row for row in inputs["python_candidates"] if row["name"] == "urllib3"]
    require(len(candidates) == 1 and candidates[0]["version"] == NEW, "Pin de urllib3 distinto.")
    wheel = candidates[0]
    wheel_bytes = read_pinned(wheel_path, wheel["bytes"], wheel["sha256"])
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
        metadata = BytesParser().parsebytes(archive.read(f"urllib3-{NEW}.dist-info/METADATA"))
    require(
        metadata["Name"] == "urllib3"
        and metadata["Version"] == NEW
        and metadata["Requires-Python"] == wheel["requires_python"]
        and all("extra == " in row for row in metadata.get_all("Requires-Dist", [])),
        "La rueda cambió la identidad o las dependencias base.",
    )
    wheel_record = {
        "url": wheel["url"],
        "hash": "sha256:" + wheel["sha256"],
        "size": wheel["bytes"],
        "upload-time": "2026-09-15T19:29:34.577Z",
    }
    lock = upgrade_pins(upstream["uv.lock"])
    package = re.compile(rb'(?ms)^\[\[package\]\]\nname = "urllib3"\n.*?(?=^\[\[package\]\]|\Z)')
    require(len(package.findall(lock)) == 1, "Bloque urllib3 original no único.")
    block = (
        f'[[package]]\nname = "urllib3"\nversion = "{NEW}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        f"sdist = {table_inline(SDIST)}\n"
        f"wheels = [\n    {table_inline(wheel_record)},\n]\n\n"
    ).encode()
    result = {
        "pyproject.toml": upgrade_pins(upstream["pyproject.toml"]),
        "uv.lock": package.sub(lambda _: block, lock),
    }
    validate_delta(upstream, result, wheel_record)
    return result, wheel["sha256"]


def validate_delta(upstream, result, wheel):
    old_project = tomllib.loads(upstream["pyproject.toml"].decode())
    old_project["project"]["dependencies"] = [f"urllib3=={NEW}", "websockets>=15.0.1"]
    old_project["tool"]["uv"]["constraint-dependencies"] = [
        f"urllib3>={NEW}",
        "certifi>=2026.10.23",
    ]
    require(
        tomllib.loads(result["pyproject.toml"].decode()) == old_project,
        "Cambio ajeno en el proyecto.",
    )
    old_lock = tomllib.loads(upstream["uv.lock"].decode())
    by_name = {row["name"]: row for row in old_lock["package"]}
    require(by_name["urllib3"]["version"] == OLD, "Versión original distinta.")
    by_name["urllib3"].update(version=NEW, sdist=SDIST, wheels=[wheel])
    constraints = old_lock["manifest"]["constraints"]
    metadata = by_name["task-runner-python"]["metadata"]["requires-dist"]
    for rows, relation in ((constraints, ">="), (metadata, "==")):
        selected = [row for row in rows if row["name"] == "urllib3"]
        require(
            len(selected) == 1 and selected[0]["specifier"] == relation + OLD,
            "Restricción original de urllib3 distinta.",
        )
        selected[0]["specifier"] = relation + NEW
    require(tomllib.loads(result["uv.lock"].decode()) == old_lock, "Cambio ajeno en el lock.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, help="Directorio nuevo. Sin esta opción sólo verifica."
    )
    args = parser.parse_args()
    raw = INPUTS.read_bytes()
    require(sha(raw) == INPUTS_SHA, "Inventario de remediación distinto.")
    inputs = json.loads(raw)
    archive = args.archive or ROOT / inputs["release"]["source_archive"]["path"]
    result, wheel_sha = prepare(inputs, archive, args.wheel)
    receipt = {
        "mode": "prepare" if args.output else "check",
        "commit": inputs["release"]["commit"],
        "urllib3": NEW,
        "wheel_sha256": wheel_sha,
        "files": {name: {"bytes": len(raw), "sha256": sha(raw)} for name, raw in result.items()},
        "lock_delta_verified": True,
        "image_built": False,
    }
    if args.output:
        args.output.mkdir(mode=0o700)
        for name, content in result.items():
            with (args.output / name).open("xb") as stream:
                stream.write(content)
        with (args.output / "receipt.json").open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
