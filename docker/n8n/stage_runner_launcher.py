"""Prepara fuentes del launcher 1.5.1 y un proxy Go de archivos autenticados por h1."""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

from prepare_runner_urllib3 import INPUTS, INPUTS_SHA, ROOT, read_pinned, require, sha

GO_HELPERS = {
    "go_module_semver": "f2d57383174a76998a6e14266cae62d9a2cffa111f5c894eebaf29cf5cba3a1a",
    "restic_archive_inputs": "1c76cb8f1b89341bf0ae3398faaeaa5e04805674584143c809675553b002f0fc",
}
PROXY = "https://proxy.golang.org/"
TEXT = "golang.org/x/text"
MAX_ZIP = 16 * 1024**2
MAX_META = 64 * 1024


def load_zip_reader():
    for name, digest in GO_HELPERS.items():
        path = ROOT / "sre" / f"{name}.py"
        raw = path.read_bytes()
        require(sha(raw) == digest, "El lector de ZIP Go difiere del pin.")
        require(name not in sys.modules, "El lector Go ya se cargó por otra ruta.")
        module = ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        exec(compile(raw, str(path), "exec"), module.__dict__)
    return sys.modules["restic_archive_inputs"]


def source_files(pin, path):
    archive = read_pinned(path, pin["bytes"], pin["sha256"])
    prefix = f"task-runner-launcher-{pin['commit']}/"
    files = {}
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as members:
        for member in members:
            require(
                member.name == prefix[:-1] or member.name.startswith(prefix), "Raíz TAR distinta."
            )
            name = member.name.removeprefix(prefix)
            if name not in {"LICENSE.md", "LICENSE_EE.md", "go.mod", "go.sum"} and not re.fullmatch(
                r"(?:cmd|internal)/(?:[a-z0-9_]+/)*[a-z0-9_]+\.go", name
            ):
                continue
            require(
                member.isfile() and name.casefold() not in seen and 0 < member.size <= MAX_META,
                "Miembro Go inesperado, enlazado o duplicado.",
            )
            seen.add(name.casefold())
            with members.extractfile(member) as stream:
                raw = stream.read(member.size + 1)
            require(len(raw) == member.size, "TAR truncado.")
            raw.decode("utf-8", errors="strict")
            files[name] = raw
    require(
        {"go.mod", "go.sum", "cmd/launcher/main.go", "LICENSE.md", "LICENSE_EE.md"} <= files.keys(),
        "TAR Go incompleto.",
    )
    for file in pin["files"]:
        name = file["path"]
        if name in files:
            require(
                len(files[name]) == file["bytes"] and sha(files[name]) == file["sha256"],
                "Pin de fuente distinto.",
            )
    return files


def patched_module(files, pin):
    mod = files["go.mod"]
    for before, after in (
        (b"go 1.25.11\n", f"go {pin['update_go_directive']}\n".encode()),
        (b"golang.org/x/text v0.39.0 ", f"{TEXT} {pin['update_x_text']} ".encode()),
    ):
        require(mod.count(before) == 1, "Directiva Go original ausente o ambigua.")
        mod = mod.replace(before, after, 1)
    replacement = pin["x_text_sumdb_response"].splitlines()[1:3]
    require(
        [line.split()[:2] for line in replacement]
        == [[TEXT, pin["update_x_text"]], [TEXT, pin["update_x_text"] + "/go.mod"]],
        "Registro SumDB distinto del módulo objetivo.",
    )
    original = files["go.sum"].decode("ascii").splitlines()
    checks = [
        line
        for line in original
        if not line.startswith(f"{TEXT} v0.39.0 ")
        and not line.startswith(f"{TEXT} v0.39.0/go.mod ")
    ]
    require(len(checks) == len(original) - 2, "Sumas antiguas de x/text no únicas.")
    checks.extend(replacement)
    files["go.mod"] = mod
    files["go.sum"] = ("\n".join(sorted(checks)) + "\n").encode()
    return checks


def download(curl, url, limit):
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    completed = subprocess.run(
        [
            str(curl),
            "--disable",
            "--noproxy",
            "*",
            "--proto",
            "=https",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "25",
            "--max-filesize",
            str(limit),
            "--write-out",
            "\n%{http_code}",
            url,
        ],
        env=env,
        capture_output=True,
        timeout=30,
        check=True,
    )
    raw, code = completed.stdout.rsplit(b"\n", 1)
    require(code == b"200" and 0 < len(raw) <= limit, "Respuesta de proxy Go no válida.")
    return raw


def write(root, name, raw):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
    return path


def module_h1(raw):
    digest = hashlib.sha256(sha(raw).encode() + b"  go.mod\n").digest()
    return "h1:" + base64.b64encode(digest).decode("ascii")


def collect_module(curl, root, zip_reader, pin, pair, checks):
    module, version = pair
    zip_reader.validate_module(module, version)
    # Los módulos fijados son ASCII sin letras mayúsculas; no se necesita escape Go.
    require(re.fullmatch(r"[a-z0-9._/-]+", module), "Ruta de proxy fuera del cierre fijado.")
    prefix = f"{module}/@v/{version}"
    source = PROXY + prefix
    mod = download(curl, source + ".mod", MAX_META)
    mod.decode("utf-8", errors="strict")
    require(module_h1(mod) == checks["mod"], "h1 de go.mod distinto.")
    if module == TEXT:
        require(sha(mod) == pin["x_text_mod_sha256"], "SHA de go.mod de x/text distinto.")
    info = download(curl, source + ".info", MAX_META)
    require(json.loads(info.decode("utf-8"))["Version"] == version, "Versión .info distinta.")
    write(root, f"proxy/{prefix}.mod", mod)
    write(root, f"proxy/{prefix}.info", info)
    if "zip" in checks:
        raw = download(curl, source + ".zip", MAX_ZIP)
        path = write(root, f"proxy/{prefix}.zip", raw)
        zip_reader.inspect_module_zip(path, module, version, expected_h1=checks["zip"])
    return {"module": module, "version": version, "h1": checks, "origin": source}


def stage(archive, output, curl):
    require(
        curl.is_absolute() and curl.is_file(), "Se requiere la ruta absoluta del curl verificado."
    )
    raw = INPUTS.read_bytes()
    require(sha(raw) == INPUTS_SHA, "Inventario original distinto del pin.")
    pin = json.loads(raw)["launcher_source"]
    files = source_files(pin, archive)
    checks = patched_module(files, pin)
    modules = {}
    for line in checks:
        module, suffix, h1 = line.split()
        version = suffix.removesuffix("/go.mod")
        field = "mod" if suffix.endswith("/go.mod") else "zip"
        rows = modules.setdefault((module, version), {})
        require(field not in rows, "Sumas duplicadas.")
        rows[field] = h1
    require(all("mod" in rows for rows in modules.values()), "Faltan sumas .mod.")
    zip_reader = load_zip_reader()
    output.mkdir(exist_ok=False)
    for name, content in files.items():
        write(output, f"source/{name}", content)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [
            pool.submit(collect_module, curl, output, zip_reader, pin, pair, rows)
            for pair, rows in sorted(modules.items())
        ]
        receipt = [job.result() for job in jobs]
    write(
        output,
        "build-runner-launcher.sh",
        Path(__file__).with_name("build-runner-launcher.sh").read_bytes(),
    )
    write(output, "modules.json", (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
    manifest = "".join(
        f"{sha(path.read_bytes())}  {path.relative_to(output).as_posix()}\n"
        for path in sorted(output.rglob("*"))
        if path.is_file()
    ).encode()
    write(output, "SHA256SUMS", manifest)
    return {
        "source_files": len(files),
        "modules": len(modules),
        "module_zips": sum("zip" in row for row in modules.values()),
        "go_mod_sha256": sha(files["go.mod"]),
        "go_sum_sha256": sha(files["go.sum"]),
        "context_sha256sums": sha(manifest),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--curl", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.archive, args.output, args.curl), sort_keys=True))


if __name__ == "__main__":
    main()
