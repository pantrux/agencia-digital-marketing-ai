"""Punto de entrada stdio para un servidor MCP propio de la agencia."""

import argparse
import base64
import json
import re
from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

SAFE_ERROR = re.compile(r"[A-Z][A-Z0-9_]{2,80}")
SERVERS = ("mcp-immich-broll", "mcp-media-inspector", "mcp-remotion-gpu", "mcp-kitsu-review")


class RenderProps(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: Annotated[str, Field(min_length=1, max_length=120)]
    duration_seconds: Annotated[float, Field(ge=0.1, le=600)]
    subtitle: Annotated[str, Field(max_length=240)] = ""
    background: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] = "#102032"
    accent: Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")] = "#8fe3c5"
    video_path: Annotated[str, Field(max_length=500)] | None = None
    audio_path: Annotated[str, Field(max_length=500)] | None = None


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: str
    job_id: str
    plan: Literal["Free", "Pro", "Ultra", "Ilimitado"]
    props: RenderProps
    composition: Literal["AgencyVertical"] = "AgencyVertical"
    format: Literal["mp4", "prores4444"] = "mp4"
    resolution: Literal["1080p", "4k"] = "1080p"


class StillsRequest(RenderRequest):
    frames: (
        Annotated[list[Annotated[int, Field(ge=0)]], Field(min_length=1, max_length=8)] | None
    ) = None


def invoke(tool: Callable[..., dict], *arguments: object) -> dict:
    try:
        return tool(*arguments)
    except ValueError as error:
        code = str(error)
        if SAFE_ERROR.fullmatch(code) is None:
            code = "OPERACION_NO_DISPONIBLE"
        raise ToolError(code) from None
    except OSError:
        raise ToolError("OPERACION_NO_DISPONIBLE") from None


def build(name: str) -> MCPServer:
    server = MCPServer(name, version="0.1.0", log_level="WARNING")
    if name == "mcp-immich-broll":
        from immich_broll import search

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
        def buscar_broll(
            consulta: Annotated[str, Field(min_length=1, max_length=512)],
            cantidad: Annotated[int, Field(ge=1, le=20)] = 8,
        ) -> dict[str, Any]:
            """Busca B-roll mediante CLIP y devuelve identificadores de la biblioteca aprobada."""
            return invoke(search, {"query": consulta, "size": cantidad})
    elif name == "mcp-media-inspector":
        from media_inspector import inspect_media, ruler_image, transcribe

        @server.tool(annotations=ToolAnnotations(idempotentHint=False, openWorldHint=False))
        def inspeccionar_medio(
            proyecto: str,
            trabajo: str,
            ruta: str,
            muestras: Annotated[int, Field(ge=1, le=12)] = 6,
        ) -> CallToolResult:
            """Inspecciona un vídeo del proyecto y publica una tira con escala de 0 a 1920."""
            result = invoke(inspect_media, proyecto, trabajo, ruta, muestras)
            try:
                preview = ruler_image(result)
            except (ValueError, OSError):
                result["preview_inline"] = False
                images = []
            else:
                result["preview_inline"] = True
                images = [
                    ImageContent(
                        type="image",
                        data=base64.b64encode(preview).decode("ascii"),
                        mime_type="image/jpeg",
                    )
                ]
            return CallToolResult(
                structured_content=result,
                content=[
                    TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
                    *images,
                ],
            )

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
        def transcribir_audio(
            proyecto: str, ruta: str, idioma: str | None = None
        ) -> dict[str, Any]:
            """Transcribe el WAV o MP3 del proyecto con tiempos por palabra."""
            return invoke(transcribe, proyecto, ruta, idioma)
    elif name == "mcp-remotion-gpu":
        from remotion_gpu import render, stills

        @server.tool(annotations=ToolAnnotations(idempotentHint=False, openWorldHint=False))
        def generar_stills(solicitud: StillsRequest) -> dict[str, Any]:
            """Genera los PNG de revisión en el trabajo y devuelve sus huellas."""
            return invoke(stills, solicitud.model_dump(exclude_none=True))

        @server.tool(annotations=ToolAnnotations(idempotentHint=False, openWorldHint=False))
        def renderizar_master(solicitud: RenderRequest) -> dict[str, Any]:
            """Rinde el maestro en GPU y verifica sus formatos, audio y huella."""
            return invoke(render, solicitud.model_dump(exclude_none=True))
    elif name == "mcp-kitsu-review":
        from kitsu_review import attach_preview, create_deliverable, create_preview, query_review

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
        def consultar_revision(
            proyecto: str,
            tarea: str,
            revision: Annotated[int, Field(ge=1)] | None = None,
        ) -> dict[str, Any]:
            """Consulta el estado y las anotaciones por fotograma de una revisión propia."""
            return invoke(query_review, proyecto, tarea, revision)

        @server.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
        def crear_entregable(
            proyecto: str, nombre: Annotated[str, Field(min_length=1, max_length=120)]
        ) -> dict[str, Any]:
            """Crea o recupera el entregable y la tarea del proyecto habilitado."""
            return invoke(create_deliverable, proyecto, nombre)

        @server.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
        def preparar_revision(
            proyecto: str, tarea: str, revision: Annotated[int, Field(ge=1)]
        ) -> dict[str, Any]:
            """Prepara la revisión solicitada de una tarea del proyecto."""
            return invoke(create_preview, proyecto, tarea, revision)

        @server.tool(annotations=ToolAnnotations(idempotentHint=True, openWorldHint=False))
        def adjuntar_revision(
            proyecto: str,
            tarea: str,
            revision: Annotated[int, Field(ge=1)],
            ruta: Annotated[str, Field(min_length=1, max_length=500)],
            sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        ) -> dict[str, Any]:
            """Adjunta a la revisión un MP4 verificado del proyecto."""
            return invoke(attach_preview, proyecto, tarea, revision, ruta, sha256)
    else:
        raise ValueError("SERVIDOR_MCP_DESCONOCIDO")
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("nombre", choices=SERVERS)
    build(parser.parse_args().nombre).run(transport="stdio")


if __name__ == "__main__":
    main()
