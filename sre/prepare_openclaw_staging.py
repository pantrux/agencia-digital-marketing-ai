"""Prepara el staging de respaldo a partir de cinco agentes ya configurados."""

import argparse
import importlib
import json
import os
import re
import stat
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
backup = importlib.import_module("openclaw_backup")

AGENTS = {
    "agency-account": "openai/gpt-6.1-terra",
    "agency-critic": "openai/gpt-6.1-sol",
    "agency-director": "openai/gpt-6.1-sol",
    "agency-editor": "openai/gpt-6.1-terra",
    "agency-strategist": "openai/gpt-6.1-sol",
}
MAX_CONFIG = 4 * 1024**2
DAY_NOTE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:-[A-Za-z0-9][A-Za-z0-9._-]*)?\.md")


def reject_constant(_value):
    raise ValueError("Constante JSON no válida.")


def load_json(raw):
    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=backup.unique_object, parse_constant=reject_constant
    )
    backup.require(isinstance(value, dict), "Se esperaba un objeto JSON.")
    return value


def config_entries(raw):
    agents = load_json(raw).get("agents")
    backup.require(
        isinstance(agents, dict) and ("entries" in agents) != ("list" in agents),
        "Formato de agentes ambiguo o ausente.",
    )
    if "entries" in agents:
        entries = agents["entries"]
        backup.require(isinstance(entries, dict), "Entradas de agentes no válidas.")
        for agent, row in entries.items():
            backup.require(
                isinstance(row, dict) and row.get("id", agent) == agent,
                "ID de agente inconsistente.",
            )
        return entries
    rows = agents["list"]
    backup.require(isinstance(rows, list), "Lista de agentes no válida.")
    entries = {}
    for row in rows:
        backup.require(
            isinstance(row, dict) and isinstance(row.get("id"), str), "ID de agente ausente."
        )
        agent = row["id"]
        backup.require(agent not in entries, "ID de agente repetido.")
        entries[agent] = row
    return entries


def absolute(value):
    backup.require(isinstance(value, str), "Ruta de origen inválida.")
    path = PurePosixPath(value)
    backup.require(
        path.anchor == "/"
        and len(path.parts) > 1
        and str(path) == value
        and ".." not in path.parts,
        "Se requiere una ruta Linux absoluta canónica.",
    )
    return path


def configured_workspace(value, home):
    if isinstance(value, str) and value.startswith("~/"):
        return absolute(str(home) + value[1:])
    if isinstance(value, str) and value.startswith("/"):
        return absolute(value)
    return None


def overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def validate_workspace(entries, agent, root, home):
    backup.require(
        configured_workspace(entries[agent].get("workspace"), home) == root,
        "La raíz autorizada difiere del workspace explícito del agente.",
    )
    for other, entry in entries.items():
        agent_dir = configured_workspace(entry.get("agentDir"), home)
        backup.require(
            agent_dir is None or not overlaps(root, agent_dir),
            "El workspace se superpone con un directorio de estado de agente.",
        )
        if other != agent:
            other_root = configured_workspace(entry.get("workspace"), home)
            backup.require(
                other_root is None or not overlaps(root, other_root),
                "Los workspaces configurados se superponen.",
            )


def project(entries, inventory, scope, home, read_memory):
    backup.require(
        set(inventory) == {"schema", "workspaces"}
        and type(inventory["schema"]) is int
        and inventory["schema"] == 1,
        "Inventario no válido.",
    )
    workspaces = inventory["workspaces"]
    backup.require(
        isinstance(workspaces, dict) and workspaces.keys() <= AGENTS.keys(),
        "Inventario de espacios fuera de la agencia.",
    )
    files = {}
    for agent, expected in AGENTS.items():
        row = entries.get(agent, {})
        model = row.get("model")
        if isinstance(model, dict):
            model = model.get("primary")
        backup.require(model == expected, "Falta un agente o su modelo primario difiere del plan.")
        files[f"{agent}/config.json"] = backup.canonical({"id": agent, "model": model})
    approved = []
    seen = set(files)
    for agent, row in workspaces.items():
        backup.require(
            isinstance(row, dict)
            and set(row) == {"root", "files"}
            and isinstance(row["files"], dict)
            and row["files"],
            "Espacio no válido.",
        )
        root = absolute(row["root"])
        validate_workspace(entries, agent, root, home)
        for relative, pin in row["files"].items():
            name = f"{agent}/{relative}"
            parts = backup.payload_path(name)
            backup.require(
                relative != "config.json" and backup.valid_pin(pin),
                "La memoria debe estar autorizada con SHA256.",
            )
            backup.require(name.casefold() not in seen, "Colisión entre rutas de memoria.")
            seen.add(name.casefold())
            approved.append((name, root, parts[1:], pin))
    backup.require(len(files) + len(approved) <= backup.MAX_FILES, "Demasiados archivos.")
    total = sum(map(len, files.values()))
    for name, root, parts, pin in approved:
        data = read_memory(root, parts, min(backup.MAX_FILE, backup.MAX_PAYLOAD - total))
        backup.require(backup.digest(data) == pin, "La memoria difiere de los bytes revisados.")
        backup.require("\x00" not in data.decode("utf-8"), "La memoria debe ser texto UTF-8.")
        files[name] = data
        total += len(data)
    manifest = backup.canonical(
        {
            "schema": 1,
            "owner": backup.OWNER,
            "scope": scope,
            "files": [
                {"path": name, "size": len(data), "sha256": backup.digest(data)}
                for name, data in sorted(files.items())
            ],
        }
    )
    for name, row in backup.read_manifest(manifest, scope).items():
        backup.check_payload(row, files[name])
    return manifest, files


def source_owner(info, *, private_workspace=False):
    protected_group = private_workspace and info.st_gid == os.getegid() and not info.st_mode & 0o002
    backup.require(
        info.st_uid == os.geteuid() and (not info.st_mode & 0o022 or protected_group),
        "El nodo de origen debe pertenecer al operador y evitar escritura de terceros.",
    )


@contextmanager
def source_directory(path):
    with ExitStack() as stack:
        fd = stack.enter_context(backup.directory("/"))
        for part in path.parts[1:]:
            fd = stack.enter_context(backup.directory(part, fd))
        source_owner(os.fstat(fd))
        yield fd


def require_private_workspace(fd):
    backup.require(stat.S_IMODE(os.fstat(fd).st_mode) == 0o700, "El workspace debe ser privado.")


def discover_memory(entries, home):
    workspaces = {}
    total = 0
    remaining_files = backup.MAX_FILES - len(AGENTS)
    for agent in AGENTS:
        root = configured_workspace(entries[agent].get("workspace"), home)
        if root is None:
            continue
        validate_workspace(entries, agent, root, home)
        pins = {}
        with ExitStack() as opened:
            try:
                workspace = opened.enter_context(source_directory(root))
            except FileNotFoundError:
                continue
            require_private_workspace(workspace)
            candidates = []
            try:
                os.stat("MEMORY.md", dir_fd=workspace, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                candidates.append(("MEMORY.md", workspace, "MEMORY.md"))
            try:
                memory = opened.enter_context(backup.directory("memory", workspace))
            except FileNotFoundError:
                pass
            else:
                source_owner(os.fstat(memory), private_workspace=True)
                with os.scandir(memory) as listing:
                    for note in listing:
                        if DAY_NOTE.fullmatch(note.name):
                            candidates.append((f"memory/{note.name}", memory, note.name))
                            backup.require(
                                len(candidates) <= remaining_files, "Demasiados archivos."
                            )
            backup.require(len(candidates) <= remaining_files, "Demasiados archivos.")
            remaining_files -= len(candidates)
            for relative, parent, name in candidates:
                backup.payload_path(f"{agent}/{relative}")
                raw = source_file(
                    parent,
                    name,
                    min(backup.MAX_FILE, backup.MAX_PAYLOAD - total),
                    private_workspace=True,
                )
                pins[relative] = backup.digest(raw)
                total += len(raw)
        if pins:
            workspaces[agent] = {"root": str(root), "files": pins}
    return {"schema": 1, "workspaces": workspaces}


def source_file(parent, name, maximum, *, private_workspace=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        source_owner(before, private_workspace=private_workspace)
        backup.require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and 0 <= before.st_size <= maximum,
            "Archivo de origen no ordinario o excesivo.",
        )
        data = stream.read(maximum + 1)
        backup.require(
            len(data) == before.st_size
            and backup.signature(before)
            == backup.signature(os.fstat(fd))
            == backup.signature(os.stat(name, dir_fd=parent, follow_symlinks=False)),
            "Cambió el archivo durante la lectura.",
        )
        return data


def read_source(path, maximum):
    with source_directory(path.parent) as parent:
        return source_file(parent, path.name, maximum)


def read_memory(root, parts, maximum, *, private_workspace=False):
    with ExitStack() as stack:
        fd = stack.enter_context(source_directory(root))
        if private_workspace:
            require_private_workspace(fd)
        for part in parts[:-1]:
            fd = stack.enter_context(backup.directory(part, fd))
            source_owner(os.fstat(fd), private_workspace=private_workspace)
        return source_file(fd, parts[-1], maximum, private_workspace=private_workspace)


def read_workspace_memory(root, parts, maximum):
    return read_memory(root, parts, maximum, private_workspace=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=backup.SCOPES)
    parser.add_argument("config", help="Ruta absoluta del openclaw.json en JSON estricto")
    parser.add_argument("inventory", help="Ruta absoluta del inventario de memorias autorizado")
    parser.add_argument("--publish", nargs=3, metavar=("ROOT", "STAGING", "MANIFEST_SHA256"))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        backup.require(sys.platform == "linux", "La operación de archivos requiere Linux.")
        import pwd

        config = absolute(args.config)
        backup.require(config.name == "openclaw.json", "Se esperaba openclaw.json.")
        entries = config_entries(read_source(config, MAX_CONFIG))
        inventory = load_json(read_source(absolute(args.inventory), backup.MAX_MANIFEST))
        home = PurePosixPath(pwd.getpwuid(os.geteuid()).pw_dir)
        manifest, files = project(entries, inventory, args.scope, home, read_memory)
        pin = backup.digest(manifest)
        if args.publish:
            root, name, expected = args.publish
            backup.require(expected == pin, "El SHA256 del manifiesto difiere del plan.")
            destination = absolute(root) / backup.component(name)
            workspaces = (
                configured_workspace(row.get("workspace"), home) for row in entries.values()
            )
            backup.require(
                not destination.is_relative_to(config.parent)
                and not any(
                    path is not None and overlaps(destination, path) for path in workspaces
                ),
                "El staging debe quedar fuera del árbol de la configuración y los workspaces.",
            )
            backup.publish(destination.parent, name, {backup.MANIFEST: manifest, **files})
    except (OSError, ValueError, KeyError, RecursionError):
        print('{"success":false,"error":"OPENCLAW_STAGING_REJECTED"}', file=sys.stderr)
        return 1
    receipt = {
        "success": True,
        "scope": args.scope,
        "published": bool(args.publish),
        "agents": len(AGENTS),
        "files": len(files),
        "manifest_sha256": pin,
    }
    print(backup.canonical(receipt).decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
