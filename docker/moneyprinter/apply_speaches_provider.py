"""Overlay cerrado por SHA; plan predeterminado, sin instalar ni ejecutar upstream."""

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path(__file__).with_name("speaches-provider-artifacts.json")
OWNER = "agencia-digital-marketing-ai:moneyprinter-speaches-overlay:v1"
MARKER = ".agency-moneyprinter-overlay.json"
SUBTITLE = "app/services/subtitle.py"
TASK = "app/services/task.py"
UI = "webui/Main.py"
OVERLAY_ROOT = ROOT / ".tools/moneyprinter-speaches-overlay-v1"
UPSTREAM_PATHS = {
    SUBTITLE: ".tools/evidence/moneyprinter-upstream-subtitle-699.py",
    TASK: ".tools/evidence/moneyprinter-upstream-task-699.py",
    UI: ".tools/evidence/moneyprinter-upstream-ui-699.py",
    "app/utils/subtitle_writer.py": ".tools/evidence/moneyprinter-upstream-subtitle-writer-699.py",
    "app/models/const.py": ".tools/evidence/moneyprinter-upstream-const-699.py",
    "config.example.toml": ".tools/evidence/moneyprinter-upstream-config-699.toml",
    "requirements.txt": ".tools/evidence/moneyprinter-upstream-requirements-699.txt",
    "LICENSE": ".tools/evidence/moneyprinter-upstream-license-699.txt",
}
SHARED_PATHS = {
    "speaches_client.py": "docker/supoclip/speaches_client.py",
    "speaches_contract.py": "docker/supoclip/speaches_contract.py",
}

CREATE = """def create(audio_file, subtitle_file="", word_level=False, log_details=True):
    # La selección es explícita también para los llamadores directos de create.
    if agency_speaches.select_provider(config.app.get("subtitle_provider")) != "speaches":
        raise agency_speaches.ProviderError("MPT_SPEACHES_PROVIDER_REQUIRED")
    outcome = agency_speaches.transcribe(
        audio_file, config.app.get("subtitle_speaches_token_file"), word_level=word_level
    )
    if outcome.status == "silence":
        return "silence"
    if not subtitle_file:
        subtitle_file = f"{audio_file}.srt"
    try:
        if not write_subtitle_file(subtitle_file, agency_speaches.render_srt(outcome)):
            raise agency_speaches.ProviderError("MPT_SUBTITLE_WRITE_FAILED")
    except agency_speaches.ProviderError:
        raise
    except Exception:
        raise agency_speaches.ProviderError("MPT_SUBTITLE_WRITE_FAILED") from None
    return None
"""

BYTES = """def transcribe_audio_bytes(audio_bytes: bytes) -> str:
    if type(audio_bytes) is not bytes or not 0 < len(audio_bytes) <= 24 * 1024**2:
        raise agency_speaches.ProviderError("MPT_SPEACHES_INPUT_REJECTED")
    with tempfile.TemporaryDirectory(prefix="moneyprinter-transcript-") as temp_dir:
        audio_file = os.path.join(temp_dir, "reference.wav")
        subtitle_file = os.path.join(temp_dir, "reference.srt")
        fd = os.open(audio_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(audio_bytes)
        if create(audio_file, subtitle_file, log_details=False) == "silence":
            return ""
        subtitle_items = file_to_subtitles(subtitle_file)
        return " ".join(item[2].strip() for item in subtitle_items if item[2].strip())
"""

GENERATE = """def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    if not params.subtitle_enabled:
        return ""
    provider = agency_speaches.select_provider(config.app.get("subtitle_provider"))
    final_subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    if sub_maker is None and provider == "edge":
        logger.warning("No hay tiempos de voz generada; no se cambiará de proveedor.")
        return ""
    is_word_level = getattr(params, "subtitle_display_mode", "sentence") == "word_by_word"
    with staged_subtitle_file(final_subtitle_path) as subtitle_path:
        if provider == "speaches":
            try:
                status = subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path, word_level=is_word_level)
                if status == "silence":
                    logger.info("El audio no contiene palabras reconocidas.")
                    return ""
                if not subtitle.file_to_subtitles(subtitle_path):
                    raise agency_speaches.ProviderError("MPT_SPEACHES_INVALID_RESPONSE")
                os.replace(subtitle_path, final_subtitle_path)
                return final_subtitle_path
            except agency_speaches.ProviderError:
                raise
            except Exception:
                raise agency_speaches.ProviderError("MPT_SUBTITLE_WRITE_FAILED") from None
        voice.create_subtitle(text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path, word_level=is_word_level)
        subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
        if not subtitle_lines:
            return ""
        os.replace(subtitle_path, final_subtitle_path)
        return final_subtitle_path
"""

UI_HELPER = """def _render_subtitle_provider(disabled):
    labels = {"speaches": "Transcribir el audio", "edge": "Usar tiempos de la voz generada"}
    try:
        configured = agency_speaches.select_provider(config.app.get("subtitle_provider"))
        selected = stable_selectbox(
            "Obtener subtítulos", options=["speaches", "edge"], default_value=configured,
            key="agency_subtitle_provider", format_func=labels.__getitem__, disabled=disabled,
        )
        agency_speaches.select_provider(selected)
    except agency_speaches.ProviderError:
        st.error("El proveedor de subtítulos configurado no es válido.")
        raise
    _set_runtime_config("app", "subtitle_provider", selected)
    return selected
"""


def require(condition, code):
    if not condition:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(data):
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def strict_json(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "JSON_DUPLICATE")
            result[key] = value
        return result

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("JSON_CONSTANT")),
    )


def replace_nodes(raw, edits):
    lines = raw.decode("utf-8").splitlines(keepends=True)
    for node, replacement in sorted(edits, key=lambda row: row[0].lineno, reverse=True):
        lines[node.lineno - 1 : node.end_lineno] = [replacement + ("\n" if replacement else "")]
    return "".join(lines).encode()


def function(tree, name):
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    require(len(nodes) == 1, "UPSTREAM_FUNCTION")
    return nodes[0]


def patch_application(sources):
    """Transformación pura; el llamador de plan ya acredita los hashes upstream completos."""
    subtitle = sources[SUBTITLE]
    tree = ast.parse(subtitle)
    edits = [
        (function(tree, "create"), CREATE),
        (function(tree, "transcribe_audio_bytes"), BYTES),
        (function(tree, "_ensure_model_loaded"), ""),
    ]
    model_names = {
        "model_size",
        "device",
        "compute_type",
        "initial_prompt",
        "model",
        "_model_init_lock",
    }
    removed, imports = set(), 0
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {
                n.id for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Name)
            }
            if names & model_names:
                require(names <= model_names, "UPSTREAM_MODEL_ASSIGNMENT")
                removed.update(names)
                edits.append((node, ""))
        if isinstance(node, ast.Try) and any(
            isinstance(n, ast.ImportFrom) and n.module == "faster_whisper" for n in ast.walk(node)
        ):
            imports += 1
            edits.append((node, ""))
        if (
            isinstance(node, ast.Import)
            and [n.name for n in node.names] == ["threading"]
            or isinstance(node, ast.ImportFrom)
            and node.module == "timeit"
        ):
            edits.append((node, ""))
    require(removed == model_names and imports == 1, "UPSTREAM_MODEL_SHAPE")
    result = {
        SUBTITLE: b"from app.services import agency_speaches\n" + replace_nodes(subtitle, edits)
    }
    task = sources[TASK]
    tree = ast.parse(task)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "generate_subtitle"
    ]
    require(
        len(calls) == 1
        and len(calls[0].targets) == 1
        and isinstance(calls[0].targets[0], ast.Name)
        and calls[0].targets[0].id == "subtitle_path",
        "UPSTREAM_CALLER",
    )
    call = calls[0]
    source_call = ast.get_source_segment(task.decode(), call)
    indentation = " " * call.col_offset
    replacement = (
        indentation
        + "try:\n"
        + "\n".join(indentation + "    " + line.strip() for line in source_call.splitlines())
        + "\n"
        + indentation
        + "except agency_speaches.ProviderError as error:\n"
        + indentation
        + '    return _mark_task_failed(task_id, "subtitle", error.code)'
    )
    result[TASK] = b"from app.services import agency_speaches\n" + replace_nodes(
        task, [(function(tree, "generate_subtitle"), GENERATE), (call, replacement)]
    )
    ui = sources[UI]
    tree = ast.parse(ui)
    function(tree, "_render_subtitle_settings")
    require(
        not any(
            isinstance(n, ast.FunctionDef) and n.name == "_render_subtitle_provider"
            for n in tree.body
        ),
        "UPSTREAM_UI_ALREADY_PATCHED",
    )
    nodes = [
        n
        for n in ast.walk(function(tree, "_render_subtitle_settings"))
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "subtitle_settings_disabled" for t in n.targets)
    ]
    require(len(nodes) == 1, "UPSTREAM_UI_ANCHOR")
    node = nodes[0]
    original = ast.get_source_segment(ui.decode(), node)
    replacement = (
        " " * node.col_offset
        + original
        + "\n"
        + " " * node.col_offset
        + "_render_subtitle_provider(subtitle_settings_disabled)"
    )
    result[UI] = (
        b"from app.services import agency_speaches\n"
        + UI_HELPER.encode()
        + b"\n\n"
        + replace_nodes(ui, [(node, replacement)])
    )
    for name, raw in result.items():
        parsed = ast.parse(raw, name)
        require(
            not any(
                isinstance(n, ast.ImportFrom)
                and n.module in {"faster_whisper", "torch", "ctranslate2"}
                for n in ast.walk(parsed)
            ),
            "LOCAL_MODEL_IMPORT",
        )
    return result


def plain(path, *, directory=False):
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        require(
            not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
            "PATH_LINK",
        )
    require(
        stat.S_ISDIR(info.st_mode)
        if directory
        else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
        "PATH_TYPE",
    )
    return info


def safe_read(path, maximum=1024**2):
    before = plain(path)
    require(before.st_size <= maximum, "FILE_SIZE")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        first = os.fstat(fd)
        require((first.st_dev, first.st_ino) == (before.st_dev, before.st_ino), "FILE_CHANGED")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        last = os.fstat(fd)
        require(
            len(raw) == first.st_size <= maximum
            and (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns, first.st_ctime_ns)
            == (last.st_dev, last.st_ino, last.st_size, last.st_mtime_ns, last.st_ctime_ns),
            "FILE_CHANGED",
        )
        return raw
    finally:
        os.close(fd)


def pinned_read(path, expected):
    raw = safe_read(path)
    require(digest(raw) == expected, "SOURCE_PIN")
    return raw


def safe_relative(name):
    require(
        type(name) is str and name and not name.startswith("/") and "\\" not in name, "OUTPUT_PATH"
    )
    require(
        all(
            part not in {"", ".", ".."} and re.fullmatch(r"[A-Za-z0-9_.-]+", part)
            for part in name.split("/")
        ),
        "OUTPUT_PATH",
    )
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        prefix + str(i) for prefix in ("COM", "LPT") for i in range(1, 10)
    }
    require(
        all(
            not part.endswith(".") and part.split(".")[0].upper() not in reserved
            for part in name.split("/")
        ),
        "OUTPUT_PATH",
    )


def output_inventory(outputs):
    for name in outputs:
        safe_relative(name)
    require(len({name.casefold() for name in outputs}) == len(outputs), "OUTPUT_COLLISION")
    return {
        name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in sorted(outputs.items())
    }


def write_new(path, raw, check_private=None):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if check_private:
            check_private(path, private=True)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)


def publish(root, outputs, plan, expected_plan, *, check_private=None):
    """La CLI acredita antes la raíz; aquí no adopta entradas ni sobreescribe bytes."""
    require(expected_plan == digest(canonical(plan)), "PLAN_PIN")
    require(plan["outputs"] == output_inventory(outputs), "OUTPUT_PLAN")
    require(
        {p.name for p in root.iterdir()} <= {MARKER, "overlay", "intent.json", "receipt.json"},
        "ROOT_FOREIGN",
    )
    intent = {"owner": OWNER, "plan_sha256": expected_plan, "outputs": plan["outputs"]}
    intent_path = root / "intent.json"
    if intent_path.exists():
        require(strict_json(safe_read(intent_path)) == intent, "INTENT_CHANGED")
    else:
        require(
            not (root / "overlay").exists() and not (root / "receipt.json").exists(),
            "OUTPUT_WITHOUT_INTENT",
        )
        write_new(intent_path, canonical(intent), check_private)
    overlay = root / "overlay"
    created = not (root / "receipt.json").exists()
    if not overlay.exists():
        overlay.mkdir(mode=0o700)
    plain(overlay, directory=True)
    expected_dirs = {
        "/".join(name.split("/")[:i]) for name in outputs for i in range(1, len(name.split("/")))
    }
    for path in overlay.rglob("*"):
        relative = path.relative_to(overlay).as_posix()
        info = path.lstat()
        require(
            relative in expected_dirs if stat.S_ISDIR(info.st_mode) else relative in outputs,
            "OVERLAY_FOREIGN",
        )
        plain(path, directory=stat.S_ISDIR(info.st_mode))
    for name, raw in sorted(outputs.items()):
        target = overlay.joinpath(*name.split("/"))
        parent = overlay
        for part in name.split("/")[:-1]:
            parent = parent / part
            if not parent.exists():
                parent.mkdir(mode=0o700)
            plain(parent, directory=True)
            if check_private:
                check_private(parent, private=True)
        plain(target.parent, directory=True)
        if target.exists():
            require(safe_read(target) == raw, "OUTPUT_CHANGED")
        else:
            write_new(target, raw, check_private)
    receipt = {
        **intent,
        "schema": 1,
        "files": len(outputs),
        "application_executed": False,
        "sdk_executed": False,
        "runtime_verified": False,
    }
    receipt_path = root / "receipt.json"
    if receipt_path.exists():
        require(strict_json(safe_read(receipt_path)) == receipt, "RECEIPT_CHANGED")
    else:
        write_new(receipt_path, canonical(receipt), check_private)
    return {
        "success": True,
        "created": created,
        "receipt": receipt,
        "receipt_sha256": digest(canonical(receipt)),
    }


def inspect_context():
    manifest_raw = safe_read(MANIFEST)
    manifest = strict_json(manifest_raw)
    require(
        manifest["schema"] == 1
        and manifest["commit"] == "6992380c5e82f07f3e1fc46b90927b6f1c655407",
        "MANIFEST_CONTRACT",
    )
    require(
        set(manifest["upstream"]) == set(UPSTREAM_PATHS)
        and set(manifest["shared_mit"]) == set(SHARED_PATHS),
        "INPUT_SET",
    )
    sources = {}
    for name, path in UPSTREAM_PATHS.items():
        row = manifest["upstream"][name]
        require(row["cache_path"] == path, "SOURCE_PATH")
        raw = pinned_read(ROOT / path, row["sha256"])
        require(
            hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            == row["blob"],
            "SOURCE_BLOB",
        )
        sources[name] = raw
    outputs = patch_application(sources)
    old_config = (
        b'# Subtitle provider: "edge" or "whisper". Whisper is loaded only when selected\n'
        b"# explicitly; Edge failures do not trigger an automatic Whisper model download.\n"
        b'# Leave empty to skip generation.\nsubtitle_provider = "edge"'
    )
    require(sources["config.example.toml"].count(old_config) == 1, "CONFIG_ANCHOR")
    outputs["config.example.toml"] = sources["config.example.toml"].replace(
        old_config,
        b'# Seleccion explicita: transcribir audio o usar tiempos de voz generada.\nsubtitle_provider = "speaches"\n# Archivo privado preparado por el operador; nunca se introduce en la UI.\nsubtitle_speaches_token_file = ""',
    )
    for name, path in SHARED_PATHS.items():
        row = manifest["shared_mit"][name]
        require(row["path"] == path, "SHARED_PATH")
        outputs[name] = pinned_read(ROOT / path, row["sha256"])
    outputs["app/services/agency_speaches.py"] = pinned_read(
        Path(__file__).with_name("speaches_provider.py"), manifest["provider_sha256"]
    )
    outputs["LICENSE.agency-speaches.txt"] = sources["LICENSE"]
    access_raw = pinned_read(
        ROOT / "sre/backup_tools_access.py", manifest["permission_verifier_sha256"]
    )
    access = types.ModuleType("mpt_overlay_access")
    access.__file__ = str(ROOT / "sre/backup_tools_access.py")
    exec(compile(access_raw, access.__file__, "exec"), access.__dict__)
    proof_path = ROOT / ".tools/evidence/phase4-moneyprinter-speaches-root-v1.json"
    proof_raw = pinned_read(proof_path, manifest["root_proof_sha256"])
    proof = strict_json(proof_raw)
    info = plain(OVERLAY_ROOT, directory=True)
    marker_raw = safe_read(OVERLAY_ROOT / MARKER)
    identity = {key: proof[key] for key in ("schema", "owner", "root", "device", "inode")}
    require(
        identity["root"] == str(OVERLAY_ROOT)
        and identity["owner"] == OWNER
        and (info.st_dev, info.st_ino) == (identity["device"], identity["inode"])
        and strict_json(marker_raw) == identity
        and digest(marker_raw) == proof["marker_sha256"],
        "ROOT_IDENTITY",
    )
    access.check_access(OVERLAY_ROOT, private=True)
    for path in OVERLAY_ROOT.iterdir():
        plain(path, directory=path.is_dir())
        access.check_access(path, private=True)
    plan = {
        "schema": 1,
        "owner": OWNER,
        "root_identity": identity,
        "root_proof_sha256": digest(proof_raw),
        "manifest_sha256": digest(manifest_raw),
        "patcher_sha256": digest(safe_read(Path(__file__))),
        "outputs": output_inventory(outputs),
        "sources_preserved": True,
        "shared_modules_copied_by_exact_sha": True,
        "python_cpu_graph_complete": False,
        "sdk_executed": False,
        "runtime_verified": False,
    }
    return outputs, plan, access.check_access


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    if args.apply:
        require(args.manifest_sha256 == digest(safe_read(MANIFEST)), "MANIFEST_PIN")
    outputs, plan, check_private = inspect_context()
    if args.apply:
        result = publish(OVERLAY_ROOT, outputs, plan, args.plan_sha256, check_private=check_private)
    else:
        result = {
            "plan": plan,
            "plan_sha256": digest(canonical(plan)),
            "overlay_materialized": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, SyntaxError):
        print(json.dumps({"success": False, "error": "MPT_OVERLAY_REJECTED"}))
        raise SystemExit(1) from None
