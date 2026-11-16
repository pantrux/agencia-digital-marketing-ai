"""Compila las variantes fijadas de QuickJS con el SDK aislado de SQLite V5."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

INPUTS = Path("/inputs")
WORK = Path("/work")
SDK = Path("/opt/wa-sqlite")
EMS = Path("/emsdk/upstream/emscripten")
NODE = Path("/usr/bin/node")
CLOSURE = INPUTS / "closure-compiler"
TS_CONFIG = INPUTS / "tsconfig.jitl.json"
DWARFDUMP = Path("/emsdk/upstream/bin/llvm-dwarfdump")
VARIANTS = ("debug-asyncify", "debug-sync", "release-asyncify", "release-sync")
OUTPUTS = (
    "emscripten-module.wasm",
    "emscripten-module.mjs",
    "emscripten-module.cjs",
    "emscripten-module.browser.mjs",
    "emscripten-module.cloudflare.cjs",
)
GENERATED_SYMBOLS = {
    "symbols.json": "0f6d878321867bb202ff078a25a917b1a5def2a9f6ca0c5b5a54ec3195a8adc5",
    "asyncify-remove.json": "8c03581d167e31b07305386c1370c8ff6030d5f9f38287cf969e08693f992e0f",
    "asyncify-imports.json": "86ca1b247ba47cea6963566882cf1ab32944cd45b905e61a0fb04dbf640d0b9f",
}


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def pin(path, expected):
    if path.resolve(strict=True) != path or not path.is_file() or digest(path) != expected:
        raise ValueError(f"Identidad de archivo inesperada: {path.name}")


def load_sdk_helper(name, expected):
    path = SDK / name
    pin(path, expected)
    namespace = {"__name__": "quickjs_sdk_helper"}
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)
    return namespace


def run():
    runner = load_sdk_helper(
        "run_wa_sqlite_sdk.py", "47a4de9ff6fbc12dadd6308ee221efdba8957483f0d6b1c5816e01b2be20133a"
    )
    runner["check_environment"]()
    runner["inspect_no_git"](EMS)
    original = load_sdk_helper(
        "source-stage.py", "57f04b592c1facb3cfc9babe4911bf33025bc0bfba96136a608fd81bf9708470"
    )
    pin(NODE, "eaad0c458723e978cac0bd4393ca63d8b88255a5b7ee8b77a0731218d8ce53eb")
    pin(CLOSURE, "5b6d5a3ba839479a4478c05e41c22b0f32f0e0cc130194e89de03f2c5672e2fc")
    pin(TS_CONFIG, "3577590a23016e52106f5c4382379d8dfc6613416248f97d929f42e30d0dc65f")
    pin(DWARFDUMP, "541b07042cd8eb52925db909af3d4858678de14e94c4e7ff00bdd56813efa197")
    description = original["INPUTS"]["quickjs"]
    source = original["read"](
        INPUTS / description["leaf"],
        description["sha256"],
        description["bytes"],
        description["bytes"],
    )
    os.umask(0o077)
    workspace = WORK / "workspace"
    workspace.mkdir()
    for name, (data, mode) in original["context"](source, description).items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as out:
            out.write(data)
            os.fchmod(out.fileno(), mode & 0o700)
    tool_path = WORK / "bin"
    for directory in (tool_path, WORK / "cache", WORK / "home", WORK / "tmp"):
        directory.mkdir()
    for name in (
        "basename",
        "cat",
        "cp",
        "dirname",
        "env",
        "grep",
        "mkdir",
        "python3",
        "readlink",
        "sed",
        "uname",
    ):
        binary = Path("/usr/bin") / ("python3.12" if name == "python3" else name)
        if not os.access(binary, os.X_OK):
            raise ValueError(f"Falta herramienta del SDK: {name}")
        (tool_path / name).symlink_to(binary)
    emcc = EMS / "emcc"
    esbuild = workspace / "node_modules/@esbuild/linux-x64/bin/esbuild"
    env = {
        "PATH": str(tool_path),
        "HOME": str(WORK / "home"),
        "TMPDIR": str(WORK / "tmp"),
        "LC_ALL": "C",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "EM_CONFIG": "/emsdk/.emscripten",
        "EM_CACHE": str(WORK / "cache"),
        "EM_CLOSURE_COMPILER": str(CLOSURE),
        "ESBUILD_BINARY_PATH": str(esbuild),
    }
    for command, version in (
        (NODE, "v24.22.3"),
        (emcc, "5.0.1"),
        (CLOSURE, "Version: v20240317"),
        (esbuild, "0.18.20"),
    ):
        reported = subprocess.check_output([command, "--version"], env=env, text=True)
        if version not in reported:
            raise ValueError(f"Versión inesperada: {command.name}")
    subprocess.run(
        [
            esbuild,
            "--bundle",
            "--platform=node",
            "--format=cjs",
            "--packages=external",
            f"--tsconfig={TS_CONFIG}",
            "--outfile=scripts/generate.build.cjs",
            "scripts/generate.ts",
        ],
        cwd=workspace,
        env=env,
        check=True,
    )
    result = {}
    for variant in VARIANTS:
        project = workspace / "packages" / f"variant-quickjs-wasmfile-{variant}"
        subprocess.run(
            [
                "/usr/bin/make",
                "-C",
                project,
                "-j1",
                "SHELL=/bin/sh",
                f"EMCC={emcc}",
                "GENERATE_TS=$(GENERATE_TS_ENV) /usr/bin/node ../../scripts/generate.build.cjs",
                "CFLAGS_ESM=$(CFLAGS_MJS)",
                "BROWSER",
                "MJS",
                "CJS",
                "CLOUDFLARE",
            ],
            env=env,
            check=True,
        )
        for name, expected in GENERATED_SYMBOLS.items():
            pin(project / "build" / "wrapper" / name, expected)
        names = OUTPUTS + (("emscripten-module.wasm.map",) if variant.startswith("debug-") else ())
        result[variant] = {}
        for name in names:
            path = project / "dist" / name
            if path.resolve(strict=True) != path or not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Salida incompleta: {variant}/{name}")
            if name.endswith((".cjs", ".mjs")):
                subprocess.run([NODE, "--check", path], env=env, check=True)
            result[variant][name] = {"bytes": path.stat().st_size, "sha256": digest(path)}
    receipt = {
        "source_sha256": description["sha256"],
        "generator_symbols_sha256": GENERATED_SYMBOLS,
        "outputs": result,
    }
    with (WORK / "quickjs-build.json").open("x", encoding="ascii") as output:
        json.dump(receipt, output, sort_keys=True)
        output.write("\n")


if __name__ == "__main__":
    run()
