"""Genera el patch OpenClaw de los cinco agentes y su integración MCP."""

import argparse
import json
from pathlib import Path

PLUGIN = "agency-audiovisual-mcp"
ROSTER = {
    "agency-account": (
        "Sofía 📋",
        "openai/gpt-6.1-terra",
        ("product-marketing", "onboarding", "protocolo-agencia-audiovisual"),
    ),
    "agency-strategist": (
        "Mateo 🧠",
        "openai/gpt-6.1-sol",
        (
            "ig-reel",
            "ig-viral",
            "ig-caption",
            "ig-story",
            "ig-dm",
            "ig-human",
            "ig-carousel",
            "ig-plan",
            "marketing-psychology",
            "cro",
            "ab-testing",
            "ad-creative",
            "copywriting",
            "social",
            "video",
            "meta-ads",
        ),
    ),
    "agency-director": (
        "Valeria 🎬",
        "openai/gpt-6.1-sol",
        (
            "edicion-de-video",
            "remotion-dev",
            "remotion-best-practices",
            "video",
            "protocolo-agencia-audiovisual",
        ),
    ),
    "agency-editor": (
        "Lucas 🎞️",
        "openai/gpt-6.1-terra",
        (
            "remotion-dev",
            "remotion-best-practices",
            "edicion-de-video",
            "ai-image-generation",
            "ai-video-generation",
            "operacion-worker-remotion-gpu",
        ),
    ),
    "agency-critic": (
        "Elena 🔍",
        "openai/gpt-6.1-sol",
        ("rubrica-qa-audiovisual", "edicion-de-video", "ig-human", "ig-audit", "cro"),
    ),
}


def merge_list(previous: list[str], extra: list[str]) -> list[str]:
    if not isinstance(previous, list) or not all(isinstance(v, str) for v in previous):
        raise ValueError("OPENCLAW_LISTA_INVALIDA")
    return list(dict.fromkeys([*previous, *extra]))


def build_patch(cfg: dict, runtime_root: Path, catalog: list[dict], workspace_root: Path) -> dict:
    original = cfg.get("agents", {}).get("entries", {})
    if not isinstance(original, dict):
        raise ValueError("OPENCLAW_AGENTES_INVALIDOS")
    agents = {}
    for agent_id, (name, model, skills) in ROSTER.items():
        stored = original.get(agent_id, {})
        if not isinstance(stored, dict):
            raise ValueError("OPENCLAW_AGENTE_INVALIDO")
        expected_workspace = workspace_root / f"workspace-{agent_id}"
        workspace = stored.get("workspace", str(expected_workspace))
        configured_model = stored.get("model")
        if Path(workspace).expanduser() != expected_workspace:
            raise ValueError("OPENCLAW_WORKSPACE_FUERA_DE_ALCANCE")
        if isinstance(configured_model, dict):
            configured_model = {**configured_model, "primary": model}
        else:
            configured_model = model
        tools = stored.get("tools", {})
        names = [item["name"] for item in catalog if agent_id in item["agents"]]
        if not names:
            raise ValueError("OPENCLAW_CATALOGO_INCOMPLETO")
        policy_field = "allow" if "allow" in tools else "alsoAllow"
        agents[agent_id] = {
            "name": name,
            "workspace": workspace,
            "model": configured_model,
            "models": {model: {"agentRuntime": {"id": "codex"}}},
            "skills": list(skills),
            "tools": {policy_field: merge_list(tools.get(policy_field, []), names)},
        }

    plugins = cfg.get("plugins", {})
    denied = plugins.get("deny", [])
    if PLUGIN in denied:
        raise ValueError("OPENCLAW_PLUGIN_DENEGADO")
    paths = plugins.get("load", {}).get("paths", [])
    entries = {PLUGIN: {"enabled": True, "config": {"runtimeRoot": str(runtime_root)}}}
    change = {
        "load": {"paths": merge_list(paths, [str(runtime_root / "openclaw-plugin")])},
        "entries": entries,
    }
    if plugins.get("allow"):
        change["allow"] = merge_list(plugins["allow"], [PLUGIN])
    return {"agents": {"entries": agents}, "plugins": change}


def check_catalog(catalog: dict) -> list[dict]:
    if not isinstance(catalog, dict) or catalog.get("version") != 1:
        raise ValueError("OPENCLAW_CATALOGO_INVALIDO")
    entries = catalog.get("tools", [])
    suite = {
        "mcp-immich-broll",
        "mcp-kitsu-review",
        "mcp-media-inspector",
        "mcp-remotion-gpu",
    }
    if {e["server"] for e in entries} != suite:
        raise ValueError("OPENCLAW_CATALOGO_INCOMPLETO")
    names = set()
    for item in entries:
        name = item["name"]
        if (
            name in names
            or name != item["server"].replace("-", "_") + "__" + item["tool"]
            or not item["agents"]
            or not set(item["agents"]) <= ROSTER.keys()
        ):
            raise ValueError("OPENCLAW_CATALOGO_INVALIDO")
        names.add(name)
    return entries


def verify_applied(cfg: dict, patch: dict) -> bool:
    """Comprueba el efecto esperado permitiendo claves ajenas conservadas por merge."""
    for key, value in patch.items():
        if isinstance(value, dict):
            if not isinstance(cfg.get(key), dict) or not verify_applied(cfg[key], value):
                return False
        elif cfg.get(key) != value:
            return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-applied", action="store_true")
    args = parser.parse_args()
    try:
        runtime = args.runtime_root.resolve(strict=True)
        root = args.workspace_root or Path.home() / ".openclaw"
        cfg = json.loads(args.config.read_text(encoding="utf-8"))
        catalog = json.loads(
            (runtime / "openclaw-plugin" / "catalog.json").read_text(encoding="utf-8")
        )
        patch = build_patch(cfg, runtime, check_catalog(catalog), root)
        if args.check_applied:
            if not verify_applied(cfg, patch):
                parser.exit(1, "OPENCLAW_PATCH_PENDIENTE\n")
            print("OPENCLAW_PATCH_APLICADO")
        else:
            data = json.dumps(patch, ensure_ascii=False, indent=2) + "\n"
            if args.output:
                with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(data)
            else:
                print(data, end="")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        parser.exit(1, "OPENCLAW_CONFIGURACION_NO_DISPONIBLE\n")


if __name__ == "__main__":
    main()
