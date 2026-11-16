"""Genera el catálogo y la política para las herramientas del gateway."""

import argparse
import asyncio
import json
from pathlib import Path

from serve import SERVERS, build

POLICY = {
    "buscar_broll": ("agency-strategist", "agency-director"),
    "inspeccionar_medio": ("agency-account", "agency-director"),
    "transcribir_audio": ("agency-account", "agency-director"),
    "generar_stills": ("agency-editor", "agency-critic"),
    "renderizar_master": ("agency-editor",),
    "consultar_revision": ("agency-account", "agency-editor", "agency-critic"),
    "crear_entregable": ("agency-account",),
    "preparar_revision": ("agency-editor",),
    "adjuntar_revision": ("agency-editor",),
}


async def catalog() -> list[dict]:
    rows = []
    tools_seen = set()
    for server_id in SERVERS:
        for tool in await build(server_id).list_tools():
            name = server_id.replace("-", "_") + "__" + tool.name
            if tool.name in tools_seen or tool.name not in POLICY:
                raise ValueError("MCP_POLITICA_INVALIDA")
            tools_seen.add(tool.name)
            annotations = tool.annotations
            rows.append(
                {
                    "name": name,
                    "tool": tool.name,
                    "server": server_id,
                    "description": tool.description,
                    "schema": tool.input_schema,
                    "agents": POLICY[tool.name],
                    "read_only": annotations is not None and annotations.read_only_hint is True,
                }
            )
    if tools_seen != POLICY.keys():
        raise ValueError("MCP_POLITICA_INCOMPLETA")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin", type=Path)
    args = parser.parse_args()
    generated = asyncio.run(catalog())
    names = [row["name"] for row in generated]
    manifest = {
        "id": "agency-audiovisual-mcp",
        "name": "MCP audiovisual de la agencia",
        "description": "Herramientas de producción para los roles de la agencia.",
        "contracts": {"tools": names},
        "toolMetadata": {name: {"optional": True} for name in names},
        "configSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["runtimeRoot"],
            "properties": {"runtimeRoot": {"type": "string", "minLength": 2}},
        },
    }
    for filename, body in (
        ("catalog.json", {"version": 1, "tools": generated}),
        ("openclaw.plugin.json", manifest),
    ):
        blob = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
        path = args.plugin / filename
        if path.is_file() and path.read_bytes() == blob.encode("utf-8"):
            continue
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(blob)


if __name__ == "__main__":
    main()
