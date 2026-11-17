"""Prepara una copia local de revisión de las skills públicas fijadas."""

import argparse
import hashlib
import io
import json
import re
import stat
import sys
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MANIFEST = Path(__file__).with_name("manifest.json")
MAX_ARCHIVE = 16 * 1024**2
MAX_FILE = 1024**2
MAX_TOTAL = 8 * 1024**2
MAX_FILES = 350
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
REPO = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,49}/[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}")
COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
PART = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,79}")
DEVICE = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.IGNORECASE)
SUFFIXES = {".geojson", ".html", ".json", ".md", ".mjs", ".py", ".svg", ".ts", ".tsx", ".yaml"}
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
SOURCE_FIELDS = {"id", "requested", "repository", "commit", "license_evidence", "skills", "files"}


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    """Conserva el host y la revisión exactos de la solicitud."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _match(pattern: re.Pattern, value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _path_is_safe(path: object) -> bool:
    if not isinstance(path, str) or not 0 < len(path) <= 240:
        return False
    parts = path.split("/")
    if parts[:2] == [".agents", "skills"]:
        parts = parts[1:]
    if any(not PART.fullmatch(part) or part.endswith(".") or DEVICE.match(part) for part in parts):
        return False
    return path == "LICENSE" or Path(path).suffix.lower() in SUFFIXES


def _is_skill(path: object, files: dict) -> bool:
    return _path_is_safe(path) and Path(path).name == "SKILL.md" and path in files


def validate(manifest: dict) -> list[dict]:
    """Valida todas las rutas, orígenes y límites antes de hacer una consulta."""
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema", "sources"}
        or type(manifest["schema"]) is not int
        or manifest["schema"] != 1
    ):
        raise ValueError("MANIFIESTO_INVALIDO")
    sources = manifest["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 12:
        raise ValueError("FUENTES_INVALIDAS")
    total_files = 0
    total_size = 0
    ids = set()
    for source in sources:
        if (
            not isinstance(source, dict)
            or set(source) != SOURCE_FIELDS
            or not _match(ID, source["id"])
            or source["id"] in ids
            or not _match(REPO, source["requested"])
            or not _match(REPO, source["repository"])
            or not _match(COMMIT, source["commit"])
            or not isinstance(source["license_evidence"], str)
            or not 0 < len(source["license_evidence"]) <= 120
        ):
            raise ValueError("FUENTE_INVALIDA")
        ids.add(source["id"])
        files, skills = source["files"], source["skills"]
        if (
            not isinstance(files, dict)
            or not 1 <= len(files) <= MAX_FILES
            or not isinstance(skills, list)
            or not 1 <= len(skills) <= len(files)
            or not all(_is_skill(path, files) for path in skills)
            or len(set(skills)) != len(skills)
        ):
            raise ValueError("CATALOGO_INVALIDO")
        seen = set()
        for path, pin in files.items():
            if (
                not _path_is_safe(path)
                or path.casefold() in seen
                or not isinstance(pin, dict)
                or set(pin) != {"size", "sha256"}
                or type(pin["size"]) is not int
                or not 0 < pin["size"] <= MAX_FILE
                or not _match(SHA256, pin["sha256"])
            ):
                raise ValueError("PIN_INVALIDO")
            seen.add(path.casefold())
            total_size += pin["size"]
            total_files += 1
        directories = {}
        for path in files:
            parts = path.split("/")
            for depth in range(1, len(parts)):
                parent = "/".join(parts[:depth])
                key = parent.casefold()
                if key in seen or directories.setdefault(key, parent) != parent:
                    raise ValueError("RUTAS_EN_CONFLICTO")
    if total_files > MAX_FILES or total_size > MAX_TOTAL:
        raise ValueError("LOTE_EXCESIVO")
    return sources


def fetch_https(url: str, maximum: int) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "AgencySkillsReview/1"})
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 200 or response.geturl() != url:
                raise ValueError("RESPUESTA_HTTPS_INVALIDA")
            payload = response.read(maximum + 1)
    except (TimeoutError, urllib.error.URLError):
        raise ValueError("DESCARGA_HTTPS_FALLIDA") from None
    if len(payload) > maximum:
        raise ValueError("DESCARGA_EXCESIVA")
    return payload


def _verify(content: bytes, pin: dict, name: str) -> bytes:
    if len(content) != pin["size"] or hashlib.sha256(content).hexdigest() != pin["sha256"]:
        raise ValueError(f"ARCHIVO_ALTERADO {name}")
    return content


def _from_archive(source: dict, fetch) -> dict[str, bytes]:
    repo, commit = source["repository"], source["commit"]
    data = fetch(f"https://codeload.github.com/{repo}/zip/{commit}", MAX_ARCHIVE)
    prefix = f"{repo.split('/')[1]}-{commit}/"
    expected = {prefix + path for path in source["files"]}
    contents = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                name = entry.filename
                if name not in expected:
                    continue
                path = name.removeprefix(prefix)
                pin = source["files"][path]
                # Codeload representa archivos sin bit ejecutable con modo DOS.
                unix_type = stat.S_IFMT(entry.external_attr >> 16)
                if (
                    not (unix_type == stat.S_IFREG or unix_type == 0 and entry.create_system == 0)
                    or entry.external_attr & REPARSE_POINT
                    or entry.is_dir()
                    or entry.file_size != pin["size"]
                    or path in contents
                ):
                    raise ValueError(f"ENTRADA_ZIP_INVALIDA {path}")
                with archive.open(entry) as stream:
                    contents[path] = _verify(stream.read(pin["size"] + 1), pin, path)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
        raise ValueError("ZIP_INVALIDO") from None
    if len(contents) != len(expected):
        raise ValueError("ZIP_INCOMPLETO")
    return contents


def _from_raw(source: dict, fetch) -> dict[str, bytes]:
    origin = f"https://raw.githubusercontent.com/{source['repository']}/{source['commit']}/"
    return {
        name: _verify(fetch(origin + name, pin["size"]), pin, name)
        for name, pin in source["files"].items()
    }


def _physical_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise ValueError("DIRECTORIO_DE_REVISION_INVALIDO") from None
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
        raise ValueError("DIRECTORIO_DE_REVISION_INVALIDO")


def _new_location(output: Path) -> Path:
    if ".." in output.parts or output.name in {"", "."}:
        raise ValueError("DIRECTORIO_DE_REVISION_INVALIDO")
    output = output.absolute()
    for parent in reversed(output.parents):
        _physical_dir(parent)
    try:
        output.lstat()
    except FileNotFoundError:
        return output
    raise ValueError("DIRECTORIO_DE_REVISION_EXISTENTE")


def stage(manifest: dict, output: Path, *, transport: str, fetch=fetch_https) -> dict:
    sources = validate(manifest)
    target = _new_location(output)
    download = {"raw": _from_raw, "zip": _from_archive}.get(transport)
    if download is None:
        raise ValueError("TRANSPORTE_INVALIDO")

    with ThreadPoolExecutor(max_workers=6) as pool:
        contents = list(pool.map(lambda source: download(source, fetch), sources))
    # Ningún byte externo llega al disco hasta comprobar el lote completo.
    target = _new_location(target)
    target.mkdir(mode=0o700)
    for source, files in zip(sources, contents, strict=True):
        for name, content in files.items():
            parts = Path(name).parts
            parent = target
            for part in (source["id"], *parts[:-1]):
                parent /= part
                parent.mkdir(mode=0o700, exist_ok=True)
                _physical_dir(parent)
            destination = parent / parts[-1]
            with destination.open("xb") as stream:
                stream.write(content)
    receipt = {
        "schema": 1,
        "transport": transport,
        "manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "files": sum(map(len, contents)),
        "skills": sum(len(source["skills"]) for source in sources),
        "sources": {source["id"]: source["commit"] for source in sources},
        "result": "revision_local",
    }
    with (target / "receipt.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="directorio nuevo para revisión")
    parser.add_argument("--transport", choices=("raw", "zip"), default="raw")
    args = parser.parse_args()
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        result = stage(manifest, args.output, transport=args.transport)
    except (ValueError, OSError) as error:
        print(f"Revisión fallida: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
