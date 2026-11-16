"""Búsqueda CLIP de lectura sobre la biblioteca B-roll aprobada."""

import os
import re
import unicodedata

from common import UpstreamError, read_secret, request_json

HOST = "192.168.0.85"
PORT = 2283
ORIGIN = f"http://{HOST}:{PORT}"
UUID_V4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
MAX_QUERY = 512
MAX_RESULTS = 20


def search(arguments: dict) -> dict:
    """Devuelve referencias opacas en el orden de relevancia de Immich."""
    if not isinstance(arguments, dict) or set(arguments) - {"query", "size"}:
        raise ValueError("IMMICH_ARGUMENTS")
    query = arguments.get("query")
    size = arguments.get("size", 8)
    if (
        not isinstance(query, str)
        or not 0 < len(query) <= MAX_QUERY
        or not query.strip()
        or any(unicodedata.category(character).startswith("C") for character in query)
        or type(size) is not int
        or not 1 <= size <= MAX_RESULTS
    ):
        raise ValueError("IMMICH_ARGUMENTS")

    if os.environ.get("IMMICH_INTERNAL_URL") not in {ORIGIN, ORIGIN + "/"}:
        raise ValueError("IMMICH_ORIGIN")
    library = os.environ.get("IMMICH_BROLL_LIBRARY_ID", "")
    if not UUID_V4.fullmatch(library):
        raise ValueError("IMMICH_LIBRARY")
    key = read_secret("IMMICH_API_KEY_MCP_FILE")
    try:
        response = request_json(
            HOST,
            PORT,
            "/api/search/smart",
            body={"query": query.strip(), "size": size, "libraryId": library},
            header=("x-api-key", key),
            timeout=20,
            limit=1024**2,
        )
    except UpstreamError as error:
        code = {401: "IMMICH_UNAUTHORIZED", 403: "IMMICH_FORBIDDEN"}.get(
            error.status, "IMMICH_UPSTREAM"
        )
        raise ValueError(code) from None

    assets = response.get("assets") if isinstance(response, dict) else None
    items = assets.get("items") if isinstance(assets, dict) else None
    count = assets.get("count") if isinstance(assets, dict) else None
    if not isinstance(items, list) or type(count) is not int or count != len(items) or count > size:
        raise ValueError("IMMICH_RESPONSE")

    matches = []
    seen = set()
    for rank, item in enumerate(items, 1):
        asset_id = item.get("id") if isinstance(item, dict) else None
        # No se reenvían rutas, personas u otros metadatos del objeto original.
        if (
            not isinstance(asset_id, str)
            or not UUID_V4.fullmatch(asset_id)
            or item.get("libraryId") != library
            or asset_id in seen
        ):
            raise ValueError("IMMICH_RESPONSE")
        seen.add(asset_id)
        matches.append(
            {"asset_id": asset_id, "rank": rank, "reference": f"immich://asset/{asset_id}"}
        )
    return {"matches": matches}
