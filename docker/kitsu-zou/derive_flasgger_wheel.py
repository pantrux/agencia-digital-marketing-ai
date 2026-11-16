"""Derivación de recursos como datos; no importa ni instala Flasgger."""

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import struct
import sys
import types
import zipfile
import zlib
from pathlib import Path

OLD_DIST = "flasgger-0.9.7.1.dist-info"
NEW_DIST = "flasgger-0.9.7.1+agency1.dist-info"
JQUERY = {"flasgger/ui3/static/lib/jquery.min.js", "flasgger/ui3/static/lib/jquery.min.map"}
TEMPLATE = "flasgger/ui3/templates/flasgger/"
WORKSPACE = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("flasgger-ui-derivative-inputs.json")
OWNER = "agencia-digital-marketing-ai:flasgger-ui-derivative:v1"
ROOT_RELATIVE = ".tools/kitsu-zou-flasgger-ui-v1"
MARKER = ".agency-flasgger-ui-owner.json"
OUTPUT = "flasgger-0.9.7.1+agency1-py2.py3-none-any.whl"
INPUT_SHA = "ee71b3e30ed15c59274d72a27277ae258378a7f28dc50ac17a4526d343c4618f"


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def strict_json(raw):
    def unique(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "JSON_DUPLICATE")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("JSON_CONSTANT")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)


def plain(path, directory=False):
    path = Path(path).absolute()
    for parent in [*reversed(path.parents), path]:
        info = parent.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "FILE_LINK",
        )
    require(
        stat.S_ISDIR(info.st_mode)
        if directory
        else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
        "FILE_TYPE",
    )
    return info


def read_file(path, maximum):
    before = plain(path)
    require(before.st_size <= maximum, "FILE_SIZE")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        after, named = os.fstat(fd), plain(path)

        # ctime sólo se coteja dentro de la misma API; Windows usa relojes distintos.
        def signature(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_nlink,
                info.st_mode,
            )

        require(
            signature(before) == signature(named) == signature(opened) == signature(after)
            and before.st_ctime_ns == named.st_ctime_ns
            and opened.st_ctime_ns == after.st_ctime_ns
            and len(raw) == opened.st_size <= maximum,
            "FILE_CHANGED",
        )
        return raw
    finally:
        os.close(fd)


def write_new(path, raw, check_private):
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        check_private(path, private=True)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)


def safe_name(name, dist):
    require(
        isinstance(name, str)
        and name.isascii()
        and 0 < len(name) <= 240
        and re.fullmatch(r"[A-Za-z0-9_+./-]+", name)
        and all(p not in {"", ".", ".."} and not p.endswith((" ", ".")) for p in name.split("/"))
        and (name.startswith("flasgger/") or name.startswith(dist + "/")),
        "ZIP_PATH",
    )
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        prefix + str(i) for prefix in ("COM", "LPT") for i in range(1, 10)
    }
    require(all(p.split(".")[0].upper() not in reserved for p in name.split("/")), "ZIP_PATH")


def inspect_wheel(raw, dist):
    require(22 <= len(raw) <= 8 * 1024**2, "ZIP_SIZE")
    require(raw[-22:-18] == b"PK\x05\x06" and raw[-2:] == b"\x00\x00", "ZIP_TRAILING")
    members, folded, spans = {}, set(), []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            require(0 < len(infos) <= 1000, "ZIP_COUNT")
            require(sum(i.file_size for i in infos) <= 64 * 1024**2, "ZIP_SIZE")
            for info in infos:
                name = info.filename
                safe_name(name, dist)
                require(name.casefold() not in folded, "ZIP_DUPLICATE")
                folded.add(name.casefold())
                require(
                    not info.is_dir()
                    and stat.S_IFMT(info.external_attr >> 16) in {0, stat.S_IFREG}
                    and not info.flag_bits & (1 | 8)
                    and info.file_size <= 16 * 1024**2
                    and info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    and not name.endswith(("/RECORD.jws", "/RECORD.p7s")),
                    "ZIP_TYPE",
                )
                start = info.header_offset
                require(0 <= start <= len(raw) - 30, "ZIP_LOCAL")
                fields = struct.unpack_from("<4s5H3I2H", raw, start)
                size, extra = fields[-2:]
                body = start + 30 + size + extra
                encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
                require(
                    fields[0] == b"PK\x03\x04"
                    and raw[start + 30 : start + 30 + size].decode(encoding) == name
                    and fields[2] == info.flag_bits
                    and fields[3] == info.compress_type
                    and fields[6:9] == (info.CRC, info.compress_size, info.file_size)
                    and body + info.compress_size <= archive.start_dir,
                    "ZIP_LOCAL",
                )
                spans.append((start, body + info.compress_size))
                members[name] = archive.read(info)
            spans.sort()
            require(
                spans[0][0] == 0
                and all(a[1] == b[0] for a, b in zip(spans, spans[1:], strict=False))
                and spans[-1][1] == archive.start_dir,
                "ZIP_OVERLAP",
            )
    except (zipfile.BadZipFile, UnicodeError, struct.error, RuntimeError):
        raise ValueError("ZIP_INVALID") from None
    record = dist + "/RECORD"
    require(record in members and dist + "/METADATA" in members, "WHEEL_REQUIRED")
    seen = set()
    for row in csv.reader(io.StringIO(members[record].decode("utf-8"), newline="")):
        require(len(row) == 3 and row[0] not in seen and row[0] in members, "RECORD_PATH")
        name, checksum, size = row
        seen.add(name)
        expected = base64.urlsafe_b64encode(hashlib.sha256(members[name]).digest()).rstrip(b"=")
        require(
            (checksum == size == "")
            if name == record
            else checksum == "sha256=" + expected.decode() and size == str(len(members[name])),
            "RECORD_HASH",
        )
    require(seen == set(members), "RECORD_COVERAGE")
    return members


def replace_once(raw, before, after):
    require(raw.count(before) == 1, "PATCH_NOT_UNIQUE")
    return raw.replace(before, after, 1)


def patch_base(raw):
    expression = rb"(?m)^([ ]*)uiversion = self.config.get\('uiversion', 3\)\n"
    matches = list(re.finditer(expression, raw))
    require(len(matches) == 1, "UI3_GUARD_SOURCE")
    match = matches[0]
    indent = match[1]
    require(len(indent) >= 4 and len(indent) % 4 == 0, "UI3_GUARD_INDENT")
    replacement = (
        match[0]
        + indent
        + b"if type(uiversion) is not int or uiversion != 3:\n"
        + indent
        + b'    raise ValueError("Esta variante privada requiere UI3.")\n'
    )
    return raw[: match.start()] + replacement + raw[match.end() :]


def patch_swagger(raw):
    title = (
        b"    function updateTitle() {\n"
        b"        {% if not flasgger_config.hide_top_bar -%}\n"
        b'        document.querySelectorAll(".topbar-wrapper .link span").forEach(function(element) {\n'
        b"            element.textContent = {{ title | tojson }};\n"
        b"        });\n"
        b"        {%- endif %}\n"
        b"    }\n\n"
    )
    raw = replace_once(raw, b"    // Build a system\n", title + b"    // Build a system\n")
    raw = replace_once(
        raw,
        b"    const ui = SwaggerUIBundle(\n    Object.assign(\n",
        b"    const uiOptions = Object.assign(\n",
    )
    raw = replace_once(
        raw,
        b"    )\n    \n    )\n\n    let auth_config =",
        b"    );\n"
        b"    const priorOnComplete = uiOptions.onComplete;\n"
        b"    uiOptions.onComplete = function() {\n"
        b"        try {\n"
        b'            if (typeof priorOnComplete === "function") {\n'
        b"                return priorOnComplete.apply(this, arguments);\n"
        b"            }\n"
        b"        } finally {\n"
        b"            updateTitle();\n"
        b"        }\n"
        b"    };\n"
        b"    const ui = SwaggerUIBundle(uiOptions);\n\n    let auth_config =",
    )
    return replace_once(
        raw,
        b'    $(".topbar-wrapper .link span").replaceWith("<span>{{title}}</span>");',
        b"    updateTitle();",
    )


def derive_members(members, removed):
    expected = {n for n in members if n.startswith("flasgger/ui2/")} | JQUERY
    require(
        len(removed) == len(set(removed)) == 67
        and len(expected) == 67
        and set(removed) == expected
        and expected <= set(members),
        "REMOVAL_SET",
    )
    patches = {
        "flasgger/base.py": patch_base,
        "flasgger/__init__.py": lambda raw: replace_once(
            raw, b"__version__ = '0.9.7.1'", b"__version__ = '0.9.7.1+agency1'"
        ),
        TEMPLATE + "body_scripts.html": lambda raw: replace_once(
            raw, b"<script src=\"{{ jquery_js }}\" type='text/javascript'></script>\n", b""
        ),
        TEMPLATE + "swagger.html": patch_swagger,
        TEMPLATE + "head.html": lambda raw: replace_once(
            raw,
            b'<link href="https://fonts.googleapis.com/css?family=Open+Sans:400,700|Source+Code+Pro:300,600|Titillium+Web:400,600,700" rel="stylesheet">\n',
            b"",
        ),
        OLD_DIST + "/METADATA": lambda raw: replace_once(
            raw, b"Version: 0.9.7.1\r\n", b"Version: 0.9.7.1+agency1\r\n"
        ),
    }
    require(set(patches) <= set(members), "PATCH_MEMBER_MISSING")
    result = {}
    for name, raw in members.items():
        if name in expected or name == OLD_DIST + "/RECORD":
            continue
        target = name.replace(OLD_DIST + "/", NEW_DIST + "/", 1)
        result[target] = patches[name](raw) if name in patches else raw
    return result


def derive_bytes(raw, removed):
    original = inspect_wheel(raw, OLD_DIST)
    changed = derive_members(original, removed)
    for path in ("flasgger/base.py", "flasgger/__init__.py"):
        ast.parse(changed[path])  # Sólo sintaxis; no importa ni ejecuta el paquete.
    record = NEW_DIST + "/RECORD"
    text = io.StringIO(newline="")
    writer = csv.writer(text, lineterminator="\n")
    for name, body in sorted(changed.items()):
        checksum = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=")
        writer.writerow((name, "sha256=" + checksum.decode(), str(len(body))))
    writer.writerow((record, "", ""))
    changed[record] = text.getvalue().encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, body in sorted(changed.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
    output = stream.getvalue()
    require(inspect_wheel(output, NEW_DIST) == changed, "OUTPUT_INVENTORY")
    proof = {
        "input_sha256": digest(raw),
        "output_sha256": digest(output),
        "output_bytes": len(output),
        "input_members": len(original),
        "output_members": len(changed),
        "removed_count": len(removed),
        "removed": {n: digest(original[n]) for n in sorted(removed)},
        "changed": {},
        "preserved": {},
        "runtime_approved": False,
        "javascript_executed": False,
        "embedded_bundles_fully_identified": False,
    }
    for name, body in original.items():
        if name in removed or name == OLD_DIST + "/RECORD":
            continue
        target = name.replace(OLD_DIST + "/", NEW_DIST + "/", 1)
        lane = "preserved" if body == changed[target] else "changed"
        proof[lane][name] = {"before": digest(body), "after": digest(changed[target])}
    require(
        set(proof["changed"])
        == {
            "flasgger/base.py",
            "flasgger/__init__.py",
            TEMPLATE + "body_scripts.html",
            TEMPLATE + "swagger.html",
            TEMPLATE + "head.html",
            OLD_DIST + "/METADATA",
        },
        "OUTPUT_DELTA",
    )
    return output, proof


def inspect_context():
    manifest_raw = read_file(MANIFEST, 128 * 1024)
    spec = strict_json(manifest_raw)
    require(
        spec["schema_version"] == 1
        and spec["owner"] == OWNER
        and spec["original"]["sha256"] == INPUT_SHA
        and spec["original"]["path"]
        == ".tools/kitsu-zou-build-flasgger-v2/wheels/flasgger-0.9.7.1-py2.py3-none-any.whl"
        and spec["output"]["root_relative"] == ROOT_RELATIVE
        and spec["output"]["marker"] == MARKER
        and spec["output"]["filename"] == OUTPUT
        and spec["output"]["local_version"] == "0.9.7.1+agency1"
        and spec["runtime_approved"] is False
        and spec["embedded_bundles_fully_identified"] is False,
        "MANIFEST_CONTRACT",
    )
    raw = read_file(WORKSPACE / spec["original"]["path"], 8 * 1024**2)
    require(digest(raw) == INPUT_SHA and len(raw) == spec["original"]["bytes"], "INPUT_PIN")
    members = inspect_wheel(raw, OLD_DIST)
    require(
        len(members) == 99
        and {n: {"sha256": digest(b), "bytes": len(b)} for n, b in members.items()}
        == spec["original"]["members"],
        "INPUT_INVENTORY",
    )
    wheel, proof = derive_bytes(raw, spec["removed_members"])
    require(proof["output_members"] == 32 and len(proof["preserved"]) == 25, "OUTPUT_COUNT")
    require(proof["changed"] == spec["patch_members"], "OUTPUT_PATCH_PINS")
    baseline = spec["before_sca_receipt"]
    require(
        baseline["path"] == ".tools/evidence/phase4-kitsu-swagger-embedded-js-sca-v2/receipt.json"
        and baseline["sha256"] == "64b2e90028d91566ea5475f38710c5caa5af314e30a05e6ece344b87c1c1ef7f"
        and digest(read_file(WORKSPACE / baseline["path"], 128 * 1024)) == baseline["sha256"],
        "SCA_HISTORY_PIN",
    )
    plan = {
        "schema_version": 1,
        "kind": "FLASGGER_UI3_DATA_DERIVATION",
        "owner": OWNER,
        "manifest_sha256": digest(manifest_raw),
        "controller_sha256": digest(read_file(Path(__file__), 128 * 1024)),
        "output_root_proposed": str(WORKSPACE / ROOT_RELATIVE),
        "output_filename": OUTPUT,
        "tool": {"python": sys.version.split()[0], "zlib": zlib.ZLIB_RUNTIME_VERSION},
        "wheel": proof,
        "before_sca_receipt": baseline,
        "installed": False,
        "runtime_approved": False,
        "flask_browser_verified": False,
        "sca_approved": False,
    }
    return manifest_raw, spec, wheel, plan


def load_access(spec):
    helper = spec["access_helper"]
    require(
        helper
        == {
            "path": "sre/backup_tools_access.py",
            "sha256": "ae1c6d5e2fbd4550c78d7ea5857b4b4b203a92e2739f9334c831730a1b8cec3b",
        },
        "ACCESS_HELPER_CONTRACT",
    )
    path = WORKSPACE / helper["path"]
    raw = read_file(path, 65536)
    require(digest(raw) == helper["sha256"], "ACCESS_HELPER_PIN")
    module = types.ModuleType("flasgger_derivative_access")
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module.check_access


def publish(spec, wheel, plan, proof_raw):
    proof = strict_json(proof_raw)
    root = WORKSPACE / ROOT_RELATIVE
    info = plain(root, directory=True)
    identity = {
        "schema_version": 1,
        "owner": OWNER,
        "root": str(root),
        "device": info.st_dev,
        "inode": info.st_ino,
    }
    marker = read_file(root / MARKER, 4096)
    require(
        strict_json(marker) == identity and proof == {**identity, "marker_sha256": digest(marker)},
        "ROOT_IDENTITY",
    )
    require({p.name for p in root.iterdir()} == {MARKER}, "ROOT_NOT_EMPTY")
    check = load_access(spec)
    check(root, private=True)
    check(root / MARKER, private=True)
    intent = {
        "schema_version": 1,
        "owner": OWNER,
        "root": identity,
        "plan_sha256": digest(canonical(plan)),
        "wheel_sha256": digest(wheel),
    }
    write_new(root / "derived-intent.json", canonical(intent), check)
    require(
        (plain(root, directory=True).st_dev, plain(root, directory=True).st_ino)
        == (info.st_dev, info.st_ino),
        "ROOT_CHANGED",
    )
    write_new(root / OUTPUT, wheel, check)
    require(digest(read_file(root / OUTPUT, 8 * 1024**2)) == digest(wheel), "OUTPUT_PIN")
    receipt = {
        "success": True,
        "owner": OWNER,
        "plan_sha256": digest(canonical(plan)),
        "root_proof_sha256": digest(proof_raw),
        "wheel_sha256": digest(wheel),
        "wheel_bytes": len(wheel),
        "runtime_approved": False,
        "installed": False,
        "sca_approved": False,
    }
    write_new(root / "derived-receipt.json", canonical(receipt), check)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("plan", "apply"), default="plan")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--root-proof", type=Path)
    parser.add_argument("--root-proof-sha256")
    args = parser.parse_args(argv)
    manifest_raw, spec, wheel, plan = inspect_context()
    if args.operation == "plan":
        result = {"plan": plan, "plan_sha256": digest(canonical(plan))}
    else:
        require(
            args.manifest_sha256 == digest(manifest_raw)
            and args.plan_sha256 == digest(canonical(plan))
            and args.root_proof is not None
            and isinstance(args.root_proof_sha256, str),
            "APPLY_PINS",
        )
        proof_raw = read_file(args.root_proof, 4096)
        require(digest(proof_raw) == args.root_proof_sha256, "ROOT_PROOF_PIN")
        result = publish(spec, wheel, plan, proof_raw)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, csv.Error, zipfile.BadZipFile):
        print(json.dumps({"success": False, "error": "FLASGGER_DERIVATION_REJECTED"}))
        raise SystemExit(1) from None
