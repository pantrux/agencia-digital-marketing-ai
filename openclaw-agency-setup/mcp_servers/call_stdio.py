"""Invoca una herramienta MCP stdio con configuración privada del servicio."""

import argparse
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import ImageContent
from serve import SAFE_ERROR, SERVERS

ENV_BY_SERVER = {
    "mcp-immich-broll": {
        "IMMICH_INTERNAL_URL",
        "IMMICH_BROLL_LIBRARY_ID",
        "IMMICH_API_KEY_MCP_FILE",
    },
    "mcp-media-inspector": {"AGENCY_MEDIA_PROJECTS", "GPU_BRIDGE_TOKEN_FILE"},
    "mcp-remotion-gpu": {"AGENCY_MEDIA_PROJECTS", "GPU_BRIDGE_TOKEN_FILE"},
    "mcp-kitsu-review": {
        "AGENCY_MEDIA_PROJECTS",
        "KITSU_API_TOKEN_FILE",
        "KITSU_REVIEW_BOT_ID",
        "KITSU_REVIEW_PROJECTS",
        "KITSU_REVIEW_STATE_ROOT",
        "KITSU_REVIEW_TASK_TYPE_ID",
        "KITSU_REVIEW_TASK_STATUS_ID",
    },
}
ROOT = Path(__file__).resolve().parent
MAX_ARGUMENTS = 32768
MAX_RESULT = 4 * 1024**2


def runtime_settings() -> dict[str, str]:
    """Carga valores de configuración y rutas; los adaptadores leen los secretos."""
    path = ROOT.parent / "runtime.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
            or info.st_nlink != 1
            or not 0 < info.st_size <= MAX_ARGUMENTS
        ):
            raise ValueError("MCP_CONFIGURACION_INVALIDA")
        payload = json.load(stream)
    fields = set().union(*ENV_BY_SERVER.values())
    if not isinstance(payload, dict) or set(payload) - fields:
        raise ValueError("MCP_CONFIGURACION_INVALIDA")
    if any(not isinstance(v, str) or len(v) > 8192 or "\x00" in v for v in payload.values()):
        raise ValueError("MCP_CONFIGURACION_INVALIDA")
    return payload


async def call(server: str, tool: str, arguments: dict) -> dict:
    env = {key: value for key, value in runtime_settings().items() if key in ENV_BY_SERVER[server]}
    settings = StdioServerParameters(
        command=sys.executable,
        args=["-E", "-B", str(ROOT / "serve.py"), server],
        cwd=ROOT,
        env=env,
    )
    async with Client(settings, mode="legacy", read_timeout_seconds=1230) as client:
        result = await client.call_tool(tool, arguments)
    if result.is_error:
        message = getattr(result.content[0], "text", "") if result.content else ""
        if SAFE_ERROR.fullmatch(message.strip()) is None:
            message = "MCP_OPERACION_RECHAZADA"
        return {"error": message.strip()}
    if isinstance(result.structured_content, dict):
        output = {"result": result.structured_content}
        if server == "mcp-media-inspector" and tool == "inspeccionar_medio":
            images = [item for item in result.content if isinstance(item, ImageContent)]
            if len(images) == 1 and images[0].mime_type == "image/jpeg":
                output["image"] = {"mimeType": images[0].mime_type, "data": images[0].data}
        return output
    return {"error": "MCP_RESPUESTA_INVALIDA"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", choices=SERVERS)
    parser.add_argument("tool", type=str)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_ARGUMENTS + 1)
        if len(raw) > MAX_ARGUMENTS:
            raise ValueError
        arguments = json.loads(raw)
        if not isinstance(arguments, dict):
            raise ValueError
        result = asyncio.run(call(args.server, args.tool, arguments))
        wire = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(wire) > MAX_RESULT:
            raise ValueError
    except Exception:
        wire = b'{"error":"MCP_OPERACION_NO_DISPONIBLE"}'
    sys.stdout.buffer.write(wire)


if __name__ == "__main__":
    main()
