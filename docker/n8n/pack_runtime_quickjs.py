"""Integra las salidas QuickJS verificadas en un archivo de ejecución pnpm."""

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

SERVER = "ef8a66323e7c4d45d522d715bcd63ea335d2f75d45c68d9f9ccaa5b7502f5ea8"
RESULTS = "05acec0c24c34c6001244aff9df28239b9376e1a78ff28ff28b5652fe4d16f2e"
SOURCE = "066d2e04ea3c5da743853e27a047febb932cc89d77ea3f5c08c798b151187c7d"
VERSION = "0.32.0"
ORIGINAL = {
    "debug-asyncify": "212444e94f065d449d2a9d64c6f4e3e9b3be4257e2175944f1abdf7feae9befe",
    "debug-sync": "f7647394d273bde2df910f5a1dac2e7aa6ac9bd0adedd338ce6d70d67ea62624",
    "release-asyncify": "29666726763413c733a52e865e9de660a091a9c42dbdb892f04f7b8d0403efad",
    "release-sync": "8459053841d6b053207b70287b5a6d762fd5608a3908b40956c5aa3178dcf9c8",
}
LEAVES = (
    "emscripten-module.browser.mjs",
    "emscripten-module.cjs",
    "emscripten-module.cloudflare.cjs",
    "emscripten-module.mjs",
    "emscripten-module.wasm",
)
LIMIT = 8 * 1024**2


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read_pinned(path, expected):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= LIMIT, path.name)
    raw = path.read_bytes()
    require(len(raw) <= LIMIT and sha(raw) == expected, path.name)
    return raw


def read_small_members(raw, mode, expected):
    with tarfile.open(fileobj=io.BytesIO(raw), mode=mode) as archive:
        result = {}
        for row in archive:
            if row.name not in expected and mode == "r:gz":
                continue
            require(row.isfile() and 0 < row.size <= LIMIT, f"Tipo o tamaño inesperado: {row.name}")
            require(
                row.name in expected and row.name not in result, f"Miembro inesperado: {row.name}"
            )
            result[row.name] = archive.extractfile(row).read()
    require(result.keys() == expected, "Cobertura de archivos incompleta")
    return result


def replacements(results, originals):
    leaves = {
        variant: LEAVES + (("emscripten-module.wasm.map",) if variant.startswith("debug-") else ())
        for variant in ORIGINAL
    }
    expected = {f"{variant}/{leaf}" for variant, names in leaves.items() for leaf in names}
    files = read_small_members(
        read_pinned(results, RESULTS), "r:", expected | {"quickjs-build.json"}
    )
    receipt = json.loads(files.pop("quickjs-build.json"))
    require(
        receipt["source_sha256"] == SOURCE and receipt["outputs"].keys() == leaves.keys(),
        "Recibo QuickJS",
    )
    result = {}
    for variant, names in leaves.items():
        package = f"quickjs-wasmfile-{variant}"
        original = originals / "@jitl" / package / f"{VERSION}.tgz"
        preimages = read_small_members(
            read_pinned(original, ORIGINAL[variant]),
            "r:gz",
            {f"package/dist/{leaf}" for leaf in names},
        )
        require(receipt["outputs"][variant].keys() == set(names), f"Cobertura de {variant}")
        for leaf in names:
            raw = files[f"{variant}/{leaf}"]
            before = sha(preimages[f"package/dist/{leaf}"])
            after = sha(raw)
            require(receipt["outputs"][variant][leaf] == {"bytes": len(raw), "sha256": after}, leaf)
            changed = leaf in {"emscripten-module.mjs", "emscripten-module.wasm"}
            require((before != after) == changed, f"Cambio inesperado: {variant}/{leaf}")
            path = f"node_modules/.pnpm/@jitl+{package}@{VERSION}/node_modules/@jitl/{package}/dist/{leaf}"
            result[path] = (before, raw, after)
    return result


def overlay(source, output, rows):
    checked, changed = set(), {}
    members = 0
    for member in source:
        members += 1
        path = member.name
        candidate = path
        if not candidate.startswith("node_modules/"):
            candidate = candidate.partition("/")[2]
        replacement = rows.get(candidate)
        stream = source.extractfile(member) if member.isfile() else None
        if replacement:
            before, raw, after = replacement
            require(candidate not in checked and stream is not None and member.size <= LIMIT, path)
            require(sha(stream.read()) == before, f"Preimagen incorrecta: {path}")
            checked.add(candidate)
            if before != after:
                require(not any(key.startswith("GNU.sparse") for key in member.pax_headers), path)
                member.size = len(raw)
                member.pax_headers.pop("size", None)
                changed[path] = {"before": before, "after": after, "bytes": len(raw)}
            stream = io.BytesIO(raw)
        output.addfile(member, stream)
    require(checked == rows.keys() and len(changed) == 8, "Preimágenes de ejecución incompletas")
    return {"members": members, "files_verified": len(checked), "files_changed": changed}


def publish(runtime, expected, output, rows):
    require(runtime.is_file() and not runtime.is_symlink(), "Entrada de ejecución inválida")
    with runtime.open("rb") as source:
        require(
            hashlib.file_digest(source, "sha256").hexdigest() == expected,
            "SHA del árbol de ejecución",
        )
        source.seek(0)
        with tempfile.TemporaryFile(dir=output.parent) as temporary:
            with gzip.GzipFile(
                filename="", fileobj=temporary, mode="wb", mtime=0, compresslevel=3
            ) as compressed:
                with (
                    tarfile.open(fileobj=source, mode="r|*") as archive,
                    tarfile.open(fileobj=compressed, mode="w|") as destination,
                ):
                    receipt = overlay(archive, destination, rows)
            temporary.seek(0)
            output_hash = hashlib.file_digest(temporary, "sha256").hexdigest()
            temporary.seek(0)
            destination = output.open("xb")
            try:
                with destination:
                    shutil.copyfileobj(temporary, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
            except BaseException:
                output.unlink()
                raise
    return {**receipt, "output_sha256": output_hash, "output_bytes": output.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "results", "originals", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--runtime-sha256", default=SERVER)
    args = parser.parse_args()
    require(
        len(args.runtime_sha256) == 64
        and all(c in "0123456789abcdef" for c in args.runtime_sha256),
        "SHA de entrada inválido",
    )
    require(
        not args.output.exists() and args.output.suffixes[-2:] == [".tar", ".gz"],
        "Salida nueva requerida",
    )
    rows = replacements(args.results, args.originals)
    receipt = publish(args.runtime, args.runtime_sha256, args.output, rows)
    print(
        json.dumps(
            {**receipt, "runtime_sha256": args.runtime_sha256, "results_sha256": RESULTS},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
