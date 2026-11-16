"""Proyecta bytes de skills verificados a los cinco workspaces de la agencia."""

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
PUBLIC = BASE / "upstream-skills" / "manifest.json"
LOCAL = BASE / "upstream-skills" / "local-manifest.json"
MAX_DOCUMENT = 8 * 1024**2
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
AD_REFERENCE = "skills/ads/references/meta-decision-system.md"


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROSTER = _module("agency_patch_config", BASE / "patch_config.py").ROSTER
review = _module("agency_review_skills", BASE / "upstream-skills" / "review_skills.py")


def _info(path):
    try:
        found = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(found.st_mode) or getattr(found, "st_file_attributes", 0) & REPARSE:
        raise ValueError("ENLACE_NO_PERMITIDO")
    return found


def _directory_chain(path, *, exists):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("RUTA_NO_CANONICA")
    for node in (*reversed(path.parents), path):
        found = _info(node)
        if found is None:
            if exists:
                raise ValueError("DIRECTORIO_AUSENTE")
            return
        if not stat.S_ISDIR(found.st_mode):
            raise ValueError("DIRECTORIO_INVALIDO")


def _read(path, limit):
    _directory_chain(path.parent, exists=True)
    before = _info(path)
    if before is None or not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= limit:
        raise ValueError("ARCHIVO_INVALIDO")
    with path.open("rb") as stream:
        if not os.path.samestat(before, os.fstat(stream.fileno())):
            raise ValueError("ARCHIVO_CAMBIO")
        payload = stream.read(limit + 1)
    after = _info(path)
    if (
        after is None
        or not os.path.samestat(before, after)
        or len(payload) != before.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ValueError("ARCHIVO_CAMBIO")
    return payload


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("CLAVE_DUPLICADA")
        result[key] = value
    return result


def _document(path):
    value = json.loads(_read(path, MAX_DOCUMENT), object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise ValueError("DOCUMENTO_INVALIDO")
    return value


def _pinned(root, relative, pin):
    try:
        raw = _read(root / relative, pin["size"])
    except ValueError:
        raise ValueError(f"HUELLA_SKILL_INVALIDA {relative}") from None
    if len(raw) != pin["size"] or hashlib.sha256(raw).hexdigest() != pin["sha256"]:
        raise ValueError(f"HUELLA_SKILL_INVALIDA {relative}")
    return raw


def project_public(root, manifest):
    """Copia referencias de revisión conservando las rutas entre skills."""
    required = {skill for _, _, skills in ROSTER.values() for skill in skills}
    result = {}
    for source in manifest["sources"]:
        source_root = root / source["id"]
        pins = source["files"]
        approved = {name: _pinned(source_root, name, pin) for name, pin in pins.items()}
        for entry in source["skills"]:
            directory = Path(entry).parent
            skill = directory.name
            if skill not in required or skill in result:
                raise ValueError("CATALOGO_PUBLICO_INCONSISTENTE")
            prefix = directory.as_posix() + "/"
            components = {
                f"{skill}/{name.removeprefix(prefix)}": raw
                for name, raw in approved.items()
                if name.startswith(prefix)
            }
            if "LICENSE" in approved:
                components[f"{skill}/LICENSE"] = approved["LICENSE"]
            if source["id"] == "marketing" and skill == "ad-creative":
                components[AD_REFERENCE.removeprefix("skills/")] = approved[AD_REFERENCE]
            result[skill] = components
    return result


def project_local(local_root):
    pins = _document(LOCAL)
    if pins.get("schema") != 1 or set(pins) != {"schema", "external", "internal"}:
        raise ValueError("MANIFIESTO_LOCAL_INVALIDO")
    result = {}
    for scope, root in (("external", local_root), ("internal", BASE / "skills")):
        for skill, entries in pins[scope].items():
            if not review.ID.fullmatch(skill) or skill in result or "SKILL.md" not in entries:
                raise ValueError("SKILL_LOCAL_INVALIDA")
            files = {}
            for name, pin in entries.items():
                safe_shell = name.startswith("scripts/") and name.endswith((".ps1", ".sh"))
                if (
                    not review._path_is_safe(name if not safe_shell else name + ".md")
                    or set(pin) != {"size", "sha256"}
                    or type(pin["size"]) is not int
                    or not 0 < pin["size"] <= review.MAX_FILE
                    or not review.SHA256.fullmatch(pin["sha256"])
                ):
                    raise ValueError("PIN_LOCAL_INVALIDO")
                relative = f"{skill}/{name}"
                files[relative] = _pinned(root, relative, pin)
            result[skill] = files
    return result


def _workspace(value):
    if not isinstance(value, str):
        raise ValueError("WORKSPACE_INVALIDO")
    expanded = Path(value).expanduser()
    if not expanded.is_absolute() or ".." in expanded.parts:
        raise ValueError("WORKSPACE_INVALIDO")
    return expanded


def _overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def workspaces(config, root):
    """Acepta únicamente el patrón de directorios configurado para la agencia."""
    _directory_chain(root, exists=True)
    agents = config.get("agents", {})
    entries = agents.get("entries", {})
    if (
        not isinstance(entries, dict)
        or "list" in agents
        or not all(isinstance(row, dict) for row in entries.values())
    ):
        raise ValueError("AGENTES_INVALIDOS")
    defaults = agents.get("defaults", {})
    other_workspaces = [
        _workspace(agents_row["workspace"]).resolve(strict=False)
        for agents_row in (defaults, *(row for name, row in entries.items() if name not in ROSTER))
        if "workspace" in agents_row
    ]
    agent_dirs = [
        _workspace(row["agentDir"]).resolve(strict=False)
        for row in entries.values()
        if "agentDir" in row
    ]
    result = {}
    for agent in ROSTER:
        target = root / f"workspace-{agent}"
        configured = entries.get(agent, {}).get("workspace", str(target))
        if _workspace(configured) != target:
            raise ValueError("WORKSPACE_FUERA_DE_ALCANCE")
        if any(_overlaps(target, other) for other in (*other_workspaces, *agent_dirs)):
            raise ValueError("WORKSPACE_SOLAPADO")
        _directory_chain(target, exists=False)
        result[agent] = target
    return result


def publish(files, *, apply):
    pending = {}
    for path, data in files.items():
        _directory_chain(path.parent, exists=False)
        existing = _info(path)
        if existing is None:
            pending[path] = data
        elif existing.st_nlink != 1 or _read(path, MAX_DOCUMENT) != data:
            raise ValueError(f"ARCHIVO_EN_CONFLICTO {path.name}")
    if apply:
        for path, data in pending.items():
            _directory_chain(path.parent, exists=False)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _directory_chain(path.parent, exists=True)
            # Un enlace duro publica el archivo completo y rechaza cualquier colisión.
            fd, temporary = tempfile.mkstemp(prefix=".agency-skill-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, path)
            finally:
                Path(temporary).unlink()
    return {"new": len(pending), "identical": len(files) - len(pending)}


def synchronize(config, workspace_root, review_root, local_root, *, apply):
    roots = workspaces(config, workspace_root)
    for target in roots.values():
        if any(_overlaps(target, source) for source in (review_root, local_root, BASE)):
            raise ValueError("ORIGEN_SOLAPADO")
    manifest = _document(PUBLIC)
    review.validate(manifest)
    public = project_public(review_root, manifest)
    local = project_local(local_root)
    if public.keys() & local.keys():
        raise ValueError("CATALOGO_DUPLICADO")
    skill_data = public | local
    required = {skill for _, _, skills in ROSTER.values() for skill in skills}
    if required != skill_data.keys():
        raise ValueError("CATALOGO_INCOMPLETO")
    files = {}
    for agent, root in roots.items():
        for skill in ROSTER[agent][2]:
            for relative, data in skill_data[skill].items():
                target = root / "skills" / relative
                if target in files and files[target] != data:
                    raise ValueError("SKILLS_EN_CONFLICTO")
                files[target] = data
    return {
        "mode": "apply" if apply else "dry-run",
        "agents": len(roots),
        "skills": len(required),
        **publish(files, apply=apply),
    }


def _merge(original, patch):
    if not isinstance(patch, dict):
        return patch
    result = dict(original) if isinstance(original, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge(result.get(key), value)
    return result


def _strip_cli_metadata(config):
    metadata = config.get("meta")
    if isinstance(metadata, dict):
        remainder = {
            key: value
            for key, value in metadata.items()
            if key not in {"lastTouchedAt", "lastTouchedVersion"}
        }
        if remainder:
            config["meta"] = remainder
        else:
            config.pop("meta")
    return config


def _checked_patch(original, change):
    # El generador sólo puede modificar las cinco filas propias y el registro del plugin.
    agents = change.get("agents", {})
    plugins = change.get("plugins", {})
    plugin_name = "agency-audiovisual-mcp"
    if (
        set(change) - {"agents", "plugins"}
        or set(agents) - {"entries"}
        or set(agents.get("entries", {})) - ROSTER.keys()
        or set(plugins) - {"allow", "entries", "load"}
        or set(plugins.get("entries", {})) - {plugin_name}
        or set(plugins.get("load", {})) - {"paths"}
    ):
        raise ValueError("OPENCLAW_PATCH_FUERA_DE_ALCANCE")
    current_plugins = original.get("plugins", {})
    if "allow" in plugins:
        previous = current_plugins.get("allow", [])
        addition = [] if plugin_name in previous else [plugin_name]
        if plugins["allow"] != previous + addition:
            raise ValueError("OPENCLAW_PATCH_FUERA_DE_ALCANCE")
    if "paths" in plugins.get("load", {}):
        previous = current_plugins.get("load", {}).get("paths", [])
        paths = plugins["load"]["paths"]
        if (
            not isinstance(paths, list)
            or paths[: len(previous)] != previous
            or len(paths) > len(previous) + 1
        ):
            raise ValueError("OPENCLAW_PATCH_FUERA_DE_ALCANCE")
    return change


def check_config(before, after, patch):
    original = _document(before)
    change = _checked_patch(original, _document(patch))
    expected = _strip_cli_metadata(_merge(original, change))
    actual = _strip_cli_metadata(_document(after))
    # La comparación de tipos mediante JSON evita igualar booleanos con números.
    if json.dumps(expected, sort_keys=True) != json.dumps(actual, sort_keys=True):
        raise ValueError("OPENCLAW_DIFERENCIA_FUERA_DEL_PATCH")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--review-root", type=Path)
    parser.add_argument("--local-root", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("PREIMAGEN", "PATCH"))
    parser.add_argument("--check-patch", type=Path, metavar="PATCH")
    parser.add_argument("--digest", action="store_true")
    args = parser.parse_args()
    try:
        if args.digest:
            print(hashlib.sha256(_read(args.config, MAX_DOCUMENT)).hexdigest())
            return
        if args.check_patch:
            _checked_patch(_document(args.config), _document(args.check_patch))
            return
        if args.compare:
            check_config(args.compare[0], args.config, args.compare[1])
            print("OPENCLAW_CAMBIOS_COTEJADOS")
            return
        if args.workspace_root is None or args.review_root is None or args.local_root is None:
            raise ValueError("RAICES_REQUERIDAS")
        result = synchronize(
            _document(args.config),
            args.workspace_root,
            args.review_root,
            args.local_root,
            apply=args.apply,
        )
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        # El contenido de la configuración puede incluir valores confidenciales.
        message = str(error) if type(error) is ValueError else "OPERACION_LOCAL_FALLIDA"
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
