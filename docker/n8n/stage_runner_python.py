"""Preparar un contexto offline del payload Python de n8n para Linux amd64."""

import argparse
import io
import json
import re
import tarfile
import tomllib
from pathlib import Path, PurePosixPath

from prepare_runner_urllib3 import INPUTS, INPUTS_SHA, RUNNER, read_pinned, require, sha

PATCHED = {
    "pyproject.toml": (804, "dcdd5d13e227d8e0823c42916e4cb8b5c28c628fb73555e536abe714aacfa22b"),
    "uv.lock": (97322, "d2c2d297c1e8c06b0860ab249eecd56af691724a38ec12c755ee8b8437e6d782"),
}
RUNTIME = {
    "certifi": ("2026.10.23", "-none-any.whl"),
    "sentry-sdk": ("2.35.2", "-none-any.whl"),
    "urllib3": ("2.8.0", "-none-any.whl"),
    "websockets": ("15.0.1", "-cp313-cp313-musllinux_1_2_x86_64.whl"),
}
BUILD = {
    "packaging": (
        "26.3",
        129956,
        "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c",
    ),
    "setuptools": (
        "84.2.1",
        876263,
        "ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2",
    ),
    "wheel": ("0.48.0", 33320, "3217dcc807155e45db462d7ef2431f5ddda0d7273b700d05a67b271ceb1287ab"),
}
LAUNCHER = "docker/images/runners/n8n-task-runners.json"
PYTHON = "/opt/runners/task-runner-python/.venv/bin/python"
BASE = "sha256:d76f585fb0d5107dd47143da5e61554fd9cae656a30f3470f815cb8475769b6a"


def runtime_wheels(raw_lock):
    packages = {row["name"]: row for row in tomllib.loads(raw_lock.decode())["package"]}
    project = packages["task-runner-python"]
    require(
        project["version"] == "0.1.0"
        and project["dependencies"] == [{"name": "urllib3"}, {"name": "websockets"}]
        and project["optional-dependencies"] == {"sentry": [{"name": "sentry-sdk"}]},
        "Dependencias del proyecto distintas del lock fijado.",
    )
    wheels = {}
    for name, (version, suffix) in RUNTIME.items():
        package = packages[name]
        dependencies = [{"name": "certifi"}, {"name": "urllib3"}] if name == "sentry-sdk" else []
        require(
            package["version"] == version and package.get("dependencies", []) == dependencies,
            "Clausura de ejecución distinta del lock fijado.",
        )
        matches = [row for row in package["wheels"] if row["url"].endswith(suffix)]
        require(len(matches) == 1, "El lock no selecciona una única rueda objetivo.")
        row = matches[0]
        require(
            row["url"].startswith("https://files.pythonhosted.org/"), "Origen de rueda no admitido."
        )
        wheels[name] = (
            version,
            row["size"],
            row["hash"].removeprefix("sha256:"),
            row["url"].rsplit("/", 1)[-1],
        )
    return wheels


def runner_sources(release, archive_path):
    pin = release["source_archive"]
    archive = read_pinned(archive_path, pin["bytes"], pin["sha256"])
    prefix = f"n8n-{release['commit']}/"
    selected = {
        "LICENSE.md": "LICENSE.md",
        f"{RUNNER}/README.md": "source/README.md",
        LAUNCHER: "n8n-task-runners.json",
    }
    outputs = {}
    names = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r|gz") as source:
        for member in source:
            name = member.name.removeprefix(prefix)
            if name.startswith(f"{RUNNER}/src/") and not member.isdir():
                relative = name.removeprefix(f"{RUNNER}/")
                require(
                    re.fullmatch(r"src/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py", relative),
                    "Ruta de código Python inesperada.",
                )
                path = f"source/{relative}"
            elif name in selected:
                path = selected[name]
            else:
                continue
            require(
                member.name == prefix + name
                and member.isfile()
                and 0 <= member.size <= 256 * 1024
                and path.casefold() not in names,
                "Miembro enlazado, duplicado o inválido en el TAR fijado.",
            )
            names.add(path.casefold())
            with source.extractfile(member) as stream:
                content = stream.read(member.size + 1)
            require(len(content) == member.size, "Miembro truncado en el TAR fijado.")
            outputs[path] = content
    require(
        len(outputs) == 45
        and set(selected.values()) <= outputs.keys()
        and "source/src/main.py" in outputs,
        "Se esperaban 42 fuentes Python, README, licencia y configuración del launcher.",
    )
    configs = json.loads(outputs["n8n-task-runners.json"])["task-runners"]
    python_configs = [row for row in configs if row["command"] == PYTHON]
    require(
        len(python_configs) == 1
        and python_configs[0]["workdir"] == "/home/runner"
        and python_configs[0]["args"]
        == ["-I", "-B", "-X", "disable_remote_debug", "-m", "src.main"],
        "El contrato original del launcher Python no coincide.",
    )
    return outputs


def stage(archive, patched_dir, wheel_dirs, output):
    raw_inputs = INPUTS.read_bytes()
    require(sha(raw_inputs) == INPUTS_SHA, "Inventario original distinto del pin.")
    files = runner_sources(json.loads(raw_inputs)["release"], archive)
    for name, (size, digest) in PATCHED.items():
        files[f"source/{name}"] = read_pinned(patched_dir / name, size, digest)
    build = {name: (*spec, f"{name}-{spec[0]}-py3-none-any.whl") for name, spec in BUILD.items()}
    runtime = runtime_wheels(files["source/uv.lock"])
    for group, wheels in (("build", build), ("runtime", runtime)):
        lines = []
        for name, (version, size, digest, filename) in wheels.items():
            candidates = [
                directory / filename for directory in wheel_dirs if (directory / filename).is_file()
            ]
            require(len(candidates) == 1, f"Debe haber una única copia local de {filename}.")
            files[f"wheels/{filename}"] = read_pinned(candidates[0], size, digest)
            lines.append(f"{name}=={version} --hash=sha256:{digest}\n")
        files[f"{group}.requirements"] = "".join(lines).encode()
    files["Dockerfile"] = Path(__file__).with_name("Dockerfile.runner-python").read_bytes()
    require(
        f"@{BASE} AS build\n".encode() in files["Dockerfile"].replace(b"\r\n", b"\n"),
        "Base de compilación distinta.",
    )
    manifest = "".join(
        f"{sha(content)}  {path}\n" for path, content in sorted(files.items())
    ).encode()
    files["SHA256SUMS"] = manifest
    output.mkdir(exist_ok=False)
    for name, content in sorted(files.items()):
        destination = output.joinpath(*PurePosixPath(name).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(content)
    return {
        "base_manifest": BASE,
        "context_files": len(files),
        "context_sha256sums": sha(manifest),
        "source_files": 42,
        "runtime_wheels": len(runtime),
        "build_wheels": len(build),
        "docker_executed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--patched-dir", type=Path, required=True)
    parser.add_argument("--wheel-dir", type=Path, required=True, action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            stage(args.archive, args.patched_dir, args.wheel_dir, args.output), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
