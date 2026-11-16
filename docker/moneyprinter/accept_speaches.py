"""Plan y núcleo MIT de aceptación parcial; nunca importa MoneyPrinter completo."""

from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import sys
import types
import uuid
import zipfile
from pathlib import Path

OWNER = "agencia-digital-marketing-ai:moneyprinter-speaches-acceptance:v1"
COMMIT = "6992380c5e82f07f3e1fc46b90927b6f1c655407"
MODULE_NAMES = ("speaches_contract", "speaches_client", "agency_speaches")
MAX_BUNDLE = 2 * 1024**2
PINS = {
    "speaches_client.py": "f5421409c749c65768b3f8364ee41896a803f9f36b21e5dc793809b3dc5295c5",
    "speaches_contract.py": "f3c6aaee4efbac0fbf65f983ae64cb0f8f4a86415d94bfde5e690c0a79dc7e02",
    "provider.py": "4053c26cf06765d0262cec4f51cd61645629c28af4c13a38214e31154b37c3d7",
    "subtitle.py": "f36bd2d53ad40b1b6a1fab5a1651ff60b043d36bba47b1037d7ed349d8617ad4",
    "task.py": "bdc1254417ac10db9b9983d2565fd990ed1692cfb387c9dd5e0aeb4cfc4e1c17",
    "original-subtitle.py": "6567613fb3fe5f360861f9a2505e894980b8026014d2e5c07142abc6ddeb709b",
    "original-task.py": "daa859e689e41dd3098842e1d4161b07cb018d765395ec637a991dafa512387c",
    "subtitle_writer.py": "b5c845ac19ed7d284a757b0c211bfe2a588fdc2f6a5f7d23e8bb19144b87b82f",
    "LICENSE": "87dd19b6a0779e14a91c1b06423ef251df37e89666160c031130a26bae68c229",
    "srt.whl": "bee6b3ba4b1f683f3d8199d60b0d22df316220b8851cae8cbab8f2ae18de8d12",
    "probe.wav": "00bf9721fcfbe2dfd69780d912048c969098d693c8ee25216a8b4f49da94fb99",
}
SELECTION = {
    "original-subtitle.py": {"create", "transcribe_audio_bytes"},
    "original-task.py": {"generate_subtitle"},
    "subtitle.py": {"create", "transcribe_audio_bytes"},
    "task.py": {"generate_subtitle"},
    "subtitle_writer.py": {"staged_subtitle_file", "has_subtitle_cue", "write_subtitle_file"},
}
CODE_PATHS = {
    "accept_speaches.py": "docker/moneyprinter/accept_speaches.py",
    "accept_speaches_remote.py": "docker/moneyprinter/accept_speaches_remote.py",
    "test_moneyprinter_speaches_acceptance.py": "tests/test_moneyprinter_speaches_acceptance.py",
}
PATCHER_SHA = "0ce4bbbff7d4d7cb769ce135045acfdefc6e70638a8036d5a54fc81bda8469e1"
MANIFEST_SHA = "3fb00b9534f81116cfdcf1884d3462097feb67bea1e749eb4562b9b8bdefaec4"


def require(condition, code):
    if not condition:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def strict_json(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BUNDLE, "ACCEPTANCE_JSON_LIMIT")

    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "ACCEPTANCE_JSON_DUPLICATE")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("ACCEPTANCE_JSON_NUMBER")

    return json.loads(
        raw.decode("utf-8", "strict"), object_pairs_hook=pairs, parse_constant=invalid
    )


def read_regular(path, maximum=MAX_BUNDLE):
    path = Path(path).absolute()
    for parent in (*reversed(path.parents), path):
        node = parent.lstat()
        require(
            not stat.S_ISLNK(node.st_mode) and not getattr(node, "st_file_attributes", 0) & 0x400,
            "ACCEPTANCE_LINK",
        )
    require(
        stat.S_ISREG(node.st_mode) and node.st_nlink == 1 and node.st_size <= maximum,
        "ACCEPTANCE_FILE",
    )
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        require(
            (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (node.st_dev, node.st_ino),
            "ACCEPTANCE_FILE_CHANGED",
        )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        after = path.lstat()
        require(
            len(raw) == node.st_size
            and (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            == (node.st_dev, node.st_ino, node.st_size, node.st_mtime_ns),
            "ACCEPTANCE_FILE_CHANGED",
        )
        return raw
    finally:
        os.close(fd)


def fragments(raw, origin, names):
    tree = ast.parse(raw)
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    require(
        len(nodes) == len(names) and {node.name for node in nodes} == names, "ACCEPTANCE_AST_SHAPE"
    )
    lines = raw.splitlines(keepends=True)
    result = []
    for node in nodes:
        first = min([node.lineno, *[row.lineno for row in node.decorator_list]])
        data = b"".join(lines[first - 1 : node.end_lineno])
        result.append(
            {
                "origin": origin,
                "origin_sha256": sha(raw),
                "name": node.name,
                "first_line": first,
                "last_line": node.end_lineno,
                "sha256": sha(data),
                "data": base64.b64encode(data).decode(),
            }
        )
    return result


def encode(raw):
    return {"sha256": sha(raw), "bytes": len(raw), "data": base64.b64encode(raw).decode()}


def decode(row):
    require(
        type(row) is dict and set(row) == {"sha256", "bytes", "data"}, "ACCEPTANCE_INPUT_FIELDS"
    )
    raw = base64.b64decode(row["data"], validate=True)
    require(
        type(row["bytes"]) is int and len(raw) == row["bytes"] and sha(raw) == row["sha256"],
        "ACCEPTANCE_INPUT_PIN",
    )
    return raw


def build_bundle(workspace):
    root = Path(workspace)
    here = root / "docker/moneyprinter"
    patch_raw = read_regular(here / "apply_speaches_provider.py")
    require(sha(patch_raw) == PATCHER_SHA, "ACCEPTANCE_PATCHER_PIN")
    manifest_raw = read_regular(here / "speaches-provider-artifacts.json")
    require(sha(manifest_raw) == MANIFEST_SHA, "ACCEPTANCE_MANIFEST_PIN")
    manifest = strict_json(manifest_raw)
    originals = {}
    for name, row in manifest["upstream"].items():
        raw = read_regular(root / row["cache_path"])
        require(sha(raw) == row["sha256"], "ACCEPTANCE_UPSTREAM_PIN")
        require(
            hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            == row["blob"],
            "ACCEPTANCE_BLOB_PIN",
        )
        originals[name] = raw
    patcher = types.ModuleType("acceptance_frozen_mpt_patcher")
    patcher.__file__ = str(here / "apply_speaches_provider.py")
    exec(compile(patch_raw, patcher.__file__, "exec"), patcher.__dict__)
    patched = patcher.patch_application(originals)
    inputs = {
        "provider.py": read_regular(here / "speaches_provider.py"),
        "speaches_client.py": read_regular(root / "docker/supoclip/speaches_client.py"),
        "speaches_contract.py": read_regular(root / "docker/supoclip/speaches_contract.py"),
        "subtitle.py": patched["app/services/subtitle.py"],
        "task.py": patched["app/services/task.py"],
        "original-subtitle.py": originals["app/services/subtitle.py"],
        "original-task.py": originals["app/services/task.py"],
        "subtitle_writer.py": originals["app/utils/subtitle_writer.py"],
        "LICENSE": originals["LICENSE"],
        "srt.whl": read_regular(
            root / ".tools/supoclip-srt-build-v1/wheels/srt-3.5.3-py3-none-any.whl"
        ),
        "probe.wav": read_regular(root / ".tools/fixtures/whisper-probe.wav"),
    }
    bundle = {
        "schema": 1,
        "owner": OWNER,
        "commit": COMMIT,
        "inputs": {key: encode(raw) for key, raw in inputs.items()},
        "code": {key: encode(read_regular(root / path)) for key, path in CODE_PATHS.items()},
        "fragments": [
            row
            for origin, names in SELECTION.items()
            for row in fragments(inputs[origin], origin, names)
        ],
    }
    validate_bundle(bundle)
    require(len(canonical(bundle)) <= MAX_BUNDLE, "ACCEPTANCE_BUNDLE_LIMIT")
    return bundle


def validate_bundle(bundle):
    require(
        type(bundle) is dict
        and set(bundle) == {"schema", "owner", "commit", "inputs", "code", "fragments"},
        "ACCEPTANCE_BUNDLE_FIELDS",
    )
    require(
        bundle["schema"] == 1 and bundle["owner"] == OWNER and bundle["commit"] == COMMIT,
        "ACCEPTANCE_BUNDLE_OWNER",
    )
    require(
        set(bundle["inputs"]) == set(PINS) and set(bundle["code"]) == set(CODE_PATHS),
        "ACCEPTANCE_INPUT_SET",
    )
    data = {key: decode(row) for key, row in bundle["inputs"].items()}
    require(all(sha(raw) == PINS[key] for key, raw in data.items()), "ACCEPTANCE_FROZEN_INPUT")
    for row in bundle["code"].values():
        decode(row)
    expected = [
        row for origin, names in SELECTION.items() for row in fragments(data[origin], origin, names)
    ]
    require(bundle["fragments"] == expected, "ACCEPTANCE_FRAGMENT_PIN")
    return data


@contextlib.contextmanager
def modules(bundle):
    data = validate_bundle(bundle)
    previous = {name: sys.modules.get(name) for name in MODULE_NAMES}
    loaded = []
    try:
        for name, source in [
            ("speaches_contract", "speaches_contract.py"),
            ("speaches_client", "speaches_client.py"),
            ("agency_speaches", "provider.py"),
        ]:
            module = types.ModuleType(name)
            module.__file__ = "<frozen-MIT/" + source + ">"
            sys.modules[name] = module
            exec(compile(data[source], module.__file__, "exec"), module.__dict__)
            loaded.append(module)
        yield types.SimpleNamespace(contract=loaded[0], client=loaded[1], provider=loaded[2])
    finally:
        for name, before in previous.items():
            if before is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = before


def extracted(bundle, origin, names, scope):
    raw = decode(bundle["inputs"][origin])
    require(sha(raw) == PINS[origin], "ACCEPTANCE_AST_ORIGIN")
    fragments(raw, origin, names)
    nodes = [
        node
        for node in ast.parse(raw).body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), "<MIT-extraido/" + origin + ">", "exec"),
        scope,
    )
    return types.SimpleNamespace(**scope)


def replay_provider(provider, outcome):
    """Doble explícito de E/S: reutiliza una respuesta real sin repetir inferencia."""
    return types.SimpleNamespace(
        ProviderError=provider.ProviderError,
        select_provider=provider.select_provider,
        render_srt=provider.render_srt,
        transcribe=lambda *_a, **_kw: outcome,
    )


def scopes(bundle, provider, work, token, parse):
    logger = types.SimpleNamespace(info=lambda *_a, **_kw: None, warning=lambda *_a, **_kw: None)
    writer = extracted(
        bundle,
        "subtitle_writer.py",
        SELECTION["subtitle_writer.py"],
        {
            "contextmanager": contextlib.contextmanager,
            "os": os,
            "re": re,
            "tempfile": __import__("tempfile"),
            "logger": logger,
        },
    )
    config = types.SimpleNamespace(
        app={"subtitle_provider": "speaches", "subtitle_speaches_token_file": str(token)}
    )
    subtitle = extracted(
        bundle,
        "subtitle.py",
        {"create"},
        {
            "config": config,
            "agency_speaches": provider,
            "write_subtitle_file": writer.write_subtitle_file,
        },
    )
    subtitle.file_to_subtitles = parse
    task = extracted(
        bundle,
        "task.py",
        {"generate_subtitle"},
        {
            "config": config,
            "agency_speaches": provider,
            "subtitle": subtitle,
            "logger": logger,
            "os": os,
            "path": os.path,
            "utils": types.SimpleNamespace(task_dir=lambda _id: str(work)),
            "staged_subtitle_file": writer.staged_subtitle_file,
            "voice": types.SimpleNamespace(
                create_subtitle=lambda **_kw: (_ for _ in ()).throw(ValueError("NO_EDGE_FALLBACK"))
            ),
        },
    )
    return types.SimpleNamespace(create=subtitle.create, generate_subtitle=task.generate_subtitle)


def srt_module(bundle, work):
    raw = decode(bundle["inputs"]["srt.whl"])
    require(sha(raw) == PINS["srt.whl"], "ACCEPTANCE_SRT_PIN")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) == 17 and "srt.py" in names, "ACCEPTANCE_SRT_WHEEL")
        member = archive.read("srt.py")
    wheel = Path(work) / ("srt-" + uuid.uuid4().hex + ".whl")
    with wheel.open("xb") as stream:
        stream.write(raw)
    wheel.chmod(0o600)
    module = types.ModuleType("srt")
    module.__file__ = str(wheel) + "/srt.py"
    module._agency_previous = sys.modules.get("srt")
    sys.modules["srt"] = module
    try:
        exec(compile(member, module.__file__, "exec"), module.__dict__)
    except BaseException:
        release_srt(module)
        raise
    return module


def release_srt(module):
    if module._agency_previous is None:
        sys.modules.pop("srt", None)
    else:
        sys.modules["srt"] = module._agency_previous


def srt_probe(bundle, work):
    module = srt_module(bundle, work)
    try:
        sample = "1\n00:00:01,250 --> 00:00:03,500\nAcción y diseño.\nLínea dos.\n\n2\n00:00:04,000 --> 00:00:05,750\nFin.\n\n"
        rows = list(module.parse(sample))
        require(
            len(rows) == 2
            and rows[0].start.total_seconds() == 1.25
            and rows[1].end.total_seconds() == 5.75
            and module.compose(rows) == sample,
            "ACCEPTANCE_SRT_ROUNDTRIP",
        )
        try:
            list(module.parse("entrada inválida"))
        except module.SRTParseError:
            pass
        else:
            raise ValueError("ACCEPTANCE_SRT_INVALID_ACCEPTED")
        return {
            "success": True,
            "wheel_sha256": PINS["srt.whl"],
            "unicode_multiline_roundtrip": True,
            "invalid_input_rejected": True,
            "loaded_from_exact_wheel_member": True,
            "installed": False,
        }
    finally:
        release_srt(module)


def publish_response(bundle, provider, data, work):
    word = provider.convert_response(data, word_level=True)
    sentence = provider.convert_response(data, word_level=False)
    require(word.status == sentence.status == "transcribed", "ACCEPTANCE_REAL_AUDIO_SILENT")
    srt = srt_module(bundle, work)
    try:

        def parse(path):
            return list(srt.parse(Path(path).read_text(encoding="utf-8")))

        proxy = replay_provider(provider, sentence)
        functions = scopes(bundle, proxy, work, "<sin-nueva-peticion>", parse)
        params = types.SimpleNamespace(subtitle_enabled=True, subtitle_display_mode="sentence")
        output = functions.generate_subtitle(
            "own-acceptance",
            params,
            "NO_REEMPLAZAR_RECONOCIMIENTO",
            None,
            "<respuesta-real-reutilizada>",
        )
        rows = parse(output)
        require(len(rows) == len(sentence.cues), "ACCEPTANCE_SRT_CUE_COUNT")
        for row, cue in zip(rows, sentence.cues, strict=True):
            require(
                row.content == cue.text
                and round(row.start.total_seconds() * 1000) == round(cue.start * 1000)
                and round(row.end.total_seconds() * 1000) == round(cue.end * 1000),
                "ACCEPTANCE_SRT_CONTENT",
            )
        body = read_regular(output)
        return {
            "success": True,
            "word_cues": len(word.cues),
            "sentence_cues": len(sentence.cues),
            "srt_sha256": sha(body),
            "srt_bytes": len(body),
            "application_imported": False,
            "published_from_extracted_functions": True,
            "network_replay_double_explicit": True,
        }
    finally:
        release_srt(srt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle",
        action="store_true",
        help="Emite sólo el paquete público por stdout; no escribe archivos.",
    )
    args = parser.parse_args(argv)
    bundle = build_bundle(Path(__file__).resolve().parents[2])
    raw = canonical(bundle)
    if args.bundle:
        sys.stdout.buffer.write(raw)
    else:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "owner": OWNER,
                    "bundle_sha256": sha(raw),
                    "bundle_bytes": len(raw),
                    "inputs": {
                        key: {k: v for k, v in row.items() if k != "data"}
                        for key, row in bundle["inputs"].items()
                    },
                    "code": {key: row["sha256"] for key, row in bundle["code"].items()},
                    "fragments": [
                        {k: v for k, v in row.items() if k != "data"} for row in bundle["fragments"]
                    ],
                    "http_executed": False,
                    "application_imported": False,
                    "secret_included": False,
                    "scope": "Sólo funciones extraídas y módulos MIT; no App completa ni fragmentación.",
                },
                ensure_ascii=True,
                sort_keys=True,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"success": False, "error": "MPT_ACCEPTANCE_PLAN_REJECTED"}))
        raise SystemExit(1) from None
