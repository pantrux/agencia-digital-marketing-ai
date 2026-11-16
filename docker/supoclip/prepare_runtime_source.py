"""Proyecta las fuentes de SupoClip para el runtime privado, sin ejecutar la aplicación."""

import argparse
import ast
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import types
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
CODE = ROOT / "docker/supoclip"
EVIDENCE = ROOT / ".tools/evidence"
PARENT = ROOT / ".tools/backup-source-build"
DEST = PARENT / "supoclip-runtime-source-v1"
OWNER = "agencia-digital-marketing-ai:supoclip-runtime-source:v1"
COMMIT = "f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38"
TAR_PIN = "be5429721b58f1b477226dd186e7fcbec280802ec8a768bf5e3ce99e9f079605"
FONT_PIN = "9d6a7aa596af4970737699f823229cb83bf4f8f0a542c4d2f69d8c0017676629"
SPEACHES_MANIFEST = "055ecf483837a45c431f6f51bca7fbcacf8a90e1efc54f56fa6f943111e9e044"
SPEACHES_PATCHER = "ecbd3863332353aafaee9f046811c5407ae2a7fc87ff54900596017421cc9873"
ACL_PIN = "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b"
PARENT_PIN = "78925844fd312b9ce3be220ee9d2fe41442908101af8c14416895811110bcff6"
ASS = "/opt/supoclip-ffmpeg-ass/bin/ffmpeg"
ASS_PATCHES = (
    ("backend/src/media/ffmpeg.py", "burn_ass_subtitles_ffmpeg", 1),
    ("backend/src/clip_editor.py", "overlay_custom_captions", 1),
    ("backend/src/media/captions.py", "emoji_rendering_supported", 1),
    ("backend/src/media/reframing.py", "render_reframed_clip_ffmpeg", 6),
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def checked(path, pin, maximum):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise ValueError("La fuente fijada no es un archivo regular acotado.")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) != info.st_size or digest(raw) != pin:
        raise ValueError(f"El SHA o tamaño de {path.name} no coincide.")
    return raw


def replace_once(raw, before, after):
    if raw.count(before) != 1:
        raise ValueError("El reemplazo requiere una preimagen única.")
    return raw.replace(before, after, 1)


def patch_ass(raw, function, count=1):
    functions = [
        node
        for node in ast.parse(raw).body
        if isinstance(node, ast.FunctionDef) and node.name == function
    ]
    if len(functions) != 1:
        raise ValueError("La función que usa subtítulos no es única.")
    locations = [
        node.elts[0]
        for node in ast.walk(functions[0])
        if isinstance(node, ast.List)
        and node.elts
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "ffmpeg"
        and (
            function != "overlay_custom_captions"
            or any(isinstance(arg, ast.Constant) and arg.value == "-vf" for arg in node.elts)
        )
    ]
    if len(locations) != count:
        raise ValueError("Cambió el número de argv de FFmpeg.")
    replacement = json.dumps(ASS)
    if function == "render_reframed_clip_ffmpeg":
        replacement = f'({replacement} if subs else "ffmpeg")'
    lines = raw.splitlines(keepends=True)
    for node in sorted(locations, key=lambda item: (item.lineno, item.col_offset), reverse=True):
        i, start, end = node.lineno - 1, node.col_offset, node.end_col_offset
        if node.lineno != node.end_lineno or lines[i][start:end] != b'"ffmpeg"':
            raise ValueError("Cambió la expresión que inicia FFmpeg.")
        lines[i] = lines[i][:start] + replacement.encode() + lines[i][end:]
    result = b"".join(lines)
    ast.parse(result)
    return result


def tar_projection(raw, prefix, select):
    files = {}
    folded = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as reader:
        for member in reader:
            if not member.name.startswith(prefix):
                if member.isdir() and member.name == prefix.rstrip("/"):
                    continue
                raise ValueError("La raíz del TAR no coincide.")
            name = member.name[len(prefix) :]
            path = PurePosixPath(name)
            if (
                name != path.as_posix()
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or ":" in name
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("Tipo o ruta inválidos en el TAR fijado.")
            if member.isdir() or not select(name):
                continue
            total += member.size
            if name.casefold() in folded or total > 64 * 1024**2:
                raise ValueError("Miembro duplicado o proyección demasiado grande.")
            body = reader.extractfile(member).read(member.size + 1)
            if len(body) != member.size:
                raise ValueError("Miembro incompleto.")
            files[name] = body
            folded.add(name.casefold())
    return files


def make_tar(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as writer:
        for name, raw in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.mode = 0o444
            member.size = len(raw)
            writer.addfile(member, io.BytesIO(raw))
    return output.getvalue()


def apply_speaches(files):
    path = CODE / "apply_remote_transcription.py"
    tree = ast.parse(checked(path, SPEACHES_PATCHER, 256 * 1024))
    names = {"PatchError", "digest", "patch_source"}
    nodes = [
        item
        for item in tree.body
        if (
            isinstance(item, (ast.FunctionDef, ast.ClassDef))
            and item.name in names
            or isinstance(item, ast.Assign)
            and len(item.targets) == 1
            and isinstance(item.targets[0], ast.Name)
            and item.targets[0].id == "PINS"
        )
    ]
    if len(nodes) != 4:
        raise ValueError("Cambió la clausura del parche Speaches.")
    namespace = {"ast": ast, "hashlib": hashlib}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    manifest = json.loads(checked(CODE / "source-patches.json", SPEACHES_MANIFEST, 256 * 1024))
    for item in manifest["files"]:
        name = item["path"]
        files[name] = namespace["patch_source"](files[name], item)
    for item in manifest["helpers"]:
        name = item["source"]
        files[f"backend/{name}"] = checked(CODE / name, item["sha256"], 256 * 1024)


def frontend_fonts(layout):
    layout = replace_once(
        layout,
        b'import { Geist, Geist_Mono, Syne } from "next/font/google";',
        b'import localFont from "next/font/local";',
    )
    old = (
        b'const geistSans = Geist({\n  variable: "--font-geist-sans",\n  subsets: ["latin"],\n});\n\n'
        b'const geistMono = Geist_Mono({\n  variable: "--font-geist-mono",\n  subsets: ["latin"],\n});\n\n'
        b'const syne = Syne({\n  variable: "--font-syne",\n  subsets: ["latin"],\n'
        b'  weight: ["400", "500", "600", "700", "800"],\n});'
    )
    new = "\n\n".join(
        f'const {const} = localFont({{\n  src: "./fonts/{name}.woff2",\n'
        f'  variable: "--font-{variable}",\n  weight: "{weights}",\n  display: "swap",\n}});'
        for const, name, variable, weights in (
            ("geistSans", "geist", "geist-sans", "100 900"),
            ("geistMono", "geist-mono", "geist-mono", "100 900"),
            ("syne", "syne", "syne", "400 800"),
        )
    ).encode()
    files = {"frontend/src/app/layout.tsx": replace_once(layout, old, new)}
    font_assets = json.loads(
        checked(EVIDENCE / "supoclip-fontsource-assets-v1.json", FONT_PIN, 64 * 1024)
    )
    for item in font_assets:
        name = item["metadata"]["id"]
        asset = item["archive"]
        raw = checked(ROOT / asset["path"], asset["sha256"], asset["bytes"])
        expected = {row["member"].removeprefix("package/"): row for row in item["files"]}
        members = tar_projection(raw, "package/", expected.__contains__)
        for key, row in expected.items():
            data = members[key]
            if digest(data) != row["sha256"] or len(data) != row["bytes"]:
                raise ValueError("La fuente local no coincide con el inventario.")
            filename = f"{name}.woff2" if key.endswith(".woff2") else f"{name}.OFL.txt"
            files[f"frontend/src/app/fonts/{filename}"] = data
    return files


def build():
    raw = checked(ROOT / ".tools/cache/phase4-sources/supoclip.tar.gz", TAR_PIN, 64 * 1024**2)
    layout = "frontend/src/app/layout.tsx"
    files = tar_projection(
        raw,
        f"supoclip-{COMMIT}/",
        lambda name: (
            name in {"LICENSE", layout}
            or name.startswith(("backend/src/", "backend/fonts/", "backend/transitions/"))
        ),
    )
    fonts = frontend_fonts(files.pop(layout))
    apply_speaches(files)
    for name, function, count in ASS_PATCHES:
        files[name] = patch_ass(files[name], function, count)
    name = "backend/src/youtube_proxy.py"
    files[name] = replace_once(
        files[name],
        b'    if shutil.which("deno"):\n'
        b"        # yt-dlp needs a JS runtime for YouTube's player challenge. Prefer the\n"
        b"        # deno binary installed in the image; do not pin a player client.\n"
        b'        params["js_runtimes"] = {"deno": {}}\n',
        b'    params["js_runtimes"] = {"node": {"path": "/usr/bin/node"}}\n'
        b'    params["remote_components"] = []\n',
    )
    for name, raw in files.items():
        if name.endswith(".py"):
            ast.parse(raw)
    groups = {"backend-source.tar": files, "frontend-font-overlay.tar": fonts}
    archives = {name: make_tar(members) for name, members in groups.items()}
    return {
        "owner": OWNER,
        "commit": COMMIT,
        "source_sha256": TAR_PIN,
        "speaches_manifest_sha256": SPEACHES_MANIFEST,
        "fontsource_assets_sha256": FONT_PIN,
        "runtime_approved": False,
        "archives": {
            name: {
                "bytes": len(archives[name]),
                "sha256": digest(archives[name]),
                "members": {
                    key: {"bytes": len(raw), "sha256": digest(raw)}
                    for key, raw in sorted(members.items())
                },
            }
            for name, members in groups.items()
        },
    }, archives


def publish(plan, archives):
    acl_path = ROOT / "sre/backup_tools_access.py"
    acl = types.ModuleType("supoclip_runtime_source_acl")
    exec(compile(checked(acl_path, ACL_PIN, 64 * 1024), str(acl_path), "exec"), acl.__dict__)
    acl.check_access(PARENT, private=True)
    checked(PARENT / ".agency-backup-build-owner", PARENT_PIN, 1024)
    if PARENT.resolve(strict=True) != PARENT:
        raise ValueError("La raíz privada de trabajo no es canónica.")
    DEST.mkdir(mode=0o700)
    acl.check_access(DEST, private=True)
    info = DEST.lstat()
    identity = {"owner": OWNER, "dev": info.st_dev, "ino": info.st_ino}
    outputs = {
        ".agency-supoclip-runtime-source-owner.json": canonical(identity),
        **archives,
        "receipt.json": canonical({**plan, "identity": identity}),
    }
    for name, raw in outputs.items():
        path = DEST / name
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        checked(path, digest(raw), len(raw))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", metavar="SHA256", help="Publica un plan exacto en raíz nueva.")
    args = parser.parse_args()
    plan, archives = build()
    pin = digest(canonical(plan))
    if args.publish:
        if not re.fullmatch(r"[0-9a-f]{64}", args.publish) or args.publish != pin:
            raise ValueError("El SHA del plan no coincide.")
        publish(plan, archives)
    print(json.dumps({"plan_sha256": pin, **plan}, sort_keys=True))


if __name__ == "__main__":
    main()
