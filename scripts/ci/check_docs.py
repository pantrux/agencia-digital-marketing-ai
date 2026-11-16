"""Verifica documentación CI/CD y cambios asociados mediante API de solo lectura."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ".github/workflows/ci-cd-media-platform.yml"
RECIPE = "docs/ci/recipes/developer-push-flow.md"
DIAGRAM = "docs/ci/diagrams/pipeline-overview.mmd"
RUNBOOKS = (
    "rb-01-gpu-bridge-unreachable.md",
    "rb-02-cloudflare-d1-migration-fail.md",
    "rb-03-agent-loop-or-openai-degraded.md",
    "rb-04-r2-nas-sync-backlog.md",
    "rb-05-kitsu-immich-db-recovery.md",
)
REQUIRED_FILES = (
    "docs/ci/README.md",
    RECIPE,
    "docs/ci/recipes/sre-rollback.md",
    DIAGRAM,
    "docs/ci/diagrams/observability-sre-topology.mmd",
    *(f"docs/ci/runbooks/{nombre}" for nombre in RUNBOOKS),
)
JOBS = (
    "gate-1-secrets",
    "gate-2-lint",
    "gate-3-types",
    "gate-4-tests",
    "gate-5-infra",
    "gate-6-security",
    "gate-7-docs",
)
DEPLOYMENT_PREFIXES = (
    ".github/workflows/",
    "scripts/ci/",
    "sre/",
    "docker/",
    "vcenter/",
    "storage/",
    "gpu-bridge-win11/",
    "ingress/",
    "cloudflare-saas/migrations/",
)


def validate_structure(root: Path) -> list[str]:
    """Comprueba archivos, enlaces locales, secciones operativas y siete trabajos."""
    errors = []
    for relative in REQUIRED_FILES:
        target = root / relative
        if not target.is_file():
            errors.append(f"Falta documento obligatorio: {relative}")
    for document in (root / "docs/ci").rglob("*.md"):
        content = document.read_text(encoding="utf-8")
        # Los ejemplos de código no son enlaces de navegación del documento.
        prose = re.sub(r"```[^\n]*\n.*?```", "", content, flags=re.DOTALL)
        for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", prose):
            target = match.group(1).strip().strip("<>")
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme in ("http", "https", "mailto") or target.startswith("#"):
                continue
            if parsed.scheme or parsed.netloc:
                errors.append(f"Enlace local no portable en {document.relative_to(root)}")
                continue
            path = (document.parent / urllib.parse.unquote(parsed.path)).resolve()
            if not path.is_relative_to(root.resolve()) or not path.exists():
                errors.append(f"Enlace local roto en {document.relative_to(root)}: {target}")
    for name in RUNBOOKS:
        path = root / "docs/ci/runbooks" / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
        for required in (
            "Prerrequisitos",
            "Diagnóstico",
            "Mitigación",
            "Verificación",
            "Reversión",
            "Resolución de problemas",
            "Registros",
        ):
            if not any(heading.startswith(required) for heading in headings):
                errors.append(f"Falta sección «{required}» en {name}")
        if "**Responsable:**" not in text or "**Última revisión:**" not in text:
            errors.append(f"Falta responsable o revisión en {name}")
        if "```" not in text or "Resultado esperado" not in text:
            errors.append(f"Faltan comandos o resultados esperados en {name}")
    workflow = root / WORKFLOW
    if not workflow.is_file():
        errors.append("Falta workflow canónico de los siete controles.")
    else:
        text = workflow.read_text(encoding="utf-8")
        names = re.findall(r"^  ([a-z0-9-]+):\s*$", text, flags=re.MULTILINE)
        for name in (*JOBS, "quality-gates"):
            if names.count(name) != 1:
                errors.append(f"El workflow debe declarar una vez el trabajo {name}.")
        final = text.partition("\n  quality-gates:")[2]
        for name in JOBS:
            if not re.search(rf"^      - {re.escape(name)}\s*$", final, re.MULTILINE):
                errors.append(f"El resultado final no exige {name}.")
    diagram = root / DIAGRAM
    if diagram.is_file():
        text = diagram.read_text(encoding="utf-8")
        if not re.match(r"flowchart\s+(TB|LR|TD)\b", text):
            errors.append("El diagrama del pipeline debe ser Mermaid editable.")
        for number in range(1, 8):
            if not re.search(rf"\bG{number}\[", text):
                errors.append(f"El diagrama omite el control {number}.")
        if "quality-gates" not in text:
            errors.append("El diagrama omite el resultado final quality-gates.")
    return errors


def normalize_paths(paths: object) -> list[str]:
    """Rechaza listas truncadas por formato o rutas fuera del repositorio."""
    if not isinstance(paths, list) or len(paths) > 10000:
        raise ValueError("La lista de archivos cambiados es inválida o excede el límite.")
    result = []
    for path in paths:
        if not isinstance(path, str) or not path or "\\" in path:
            raise ValueError("Una ruta de cambios no es válida.")
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or ":" in path or "\x00" in path:
            raise ValueError("Una ruta de cambios sale del repositorio.")
        result.append(str(pure))
    return list(dict.fromkeys(result))


def validate_changes(paths: Sequence[str]) -> list[str]:
    """Exige receta y diagrama en el mismo cambio que altera la entrega o CI."""
    normalized = normalize_paths(list(paths))
    needs_docs = any(
        path.startswith(DEPLOYMENT_PREFIXES)
        or path
        in (
            "config/phase-capabilities.json",
            "cloudflare-saas/wrangler.toml",
            "openclaw-agency-setup/setup.sh",
            "openclaw-agency-setup/patch_config.py",
        )
        for path in normalized
    )
    if not needs_docs:
        return []
    return [
        f"El cambio de CI/despliegue debe actualizar también {path}."
        for path in (RECIPE, DIAGRAM)
        if path not in normalized
    ]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Impide reenviar el token a un destino diferente por redirección HTTP."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_fetcher(environ: Mapping[str, str]) -> Callable[[str], object]:
    """Construye un lector autenticado acotado a la API configurada por GitHub."""
    base = environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("El origen de la API de GitHub no es válido.")
    token = environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("Falta token de solo lectura para comprobar cambios en GitHub.")
    opener = urllib.request.build_opener(NoRedirect())
    deadline = time.monotonic() + 90

    def fetch(path: str) -> object:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not path.startswith("/repos/"):
            raise ValueError("La verificación de cambios excedió su límite o ruta permitida.")
        request = urllib.request.Request(
            base + path,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "verificador-documental-plataforma",
            },
            method="GET",
        )
        try:
            with opener.open(request, timeout=min(10, remaining)) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
            if len(payload) > 8 * 1024 * 1024:
                raise ValueError("La respuesta de cambios excede el límite permitido.")
            return json.loads(payload)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
            # La excepción original podría incluir datos de autenticación o el cuerpo remoto.
            raise ValueError("No se pudo verificar íntegramente la API de cambios.") from error

    return fetch


def file_paths(rows: object) -> list[str]:
    """Incluye nombres anteriores de renombrados para no ocultar cambios de despliegue."""
    if not isinstance(rows, list):
        raise ValueError("La API no entregó una lista válida de archivos.")
    paths = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("filename"), str):
            raise ValueError("La API entregó un archivo sin nombre verificable.")
        paths.append(row["filename"])
        if "previous_filename" in row:
            paths.append(row["previous_filename"])
    return normalize_paths(paths)


def pull_request_identity(metadata: object) -> tuple[int, str, str]:
    """Identifica una instantánea de PR para detectar cambios mientras se pagina."""
    if not isinstance(metadata, dict):
        raise ValueError("No se pudo identificar la versión de la solicitud de cambio.")
    count = metadata.get("changed_files")
    if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 3000:
        raise ValueError("No se puede acreditar el total de archivos de la solicitud.")
    commits = []
    for key in ("head", "base"):
        side = metadata.get(key)
        sha = side.get("sha") if isinstance(side, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise ValueError("La solicitud no identifica sus dos versiones comparables.")
        commits.append(sha)
    return count, commits[0], commits[1]


def commit_tree(fetch: Callable[[str], object], prefix: str, revision: str) -> dict[str, tuple]:
    """Lee e identifica un árbol completo del commit indicado por el evento."""
    commit = fetch(f"{prefix}/commits/{revision}")
    if not isinstance(commit, dict) or commit.get("sha") != revision:
        raise ValueError("La API no identifica la versión indicada por el evento.")
    detail = commit.get("commit")
    tree_reference = detail.get("tree") if isinstance(detail, dict) else None
    tree_sha = tree_reference.get("sha") if isinstance(tree_reference, dict) else None
    if not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", tree_sha):
        raise ValueError("El commit no contiene una referencia de árbol verificable.")
    snapshot = fetch(f"{prefix}/git/trees/{tree_sha}?recursive=1")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("sha") != tree_sha
        or snapshot.get("truncated") is not False
        or not isinstance(snapshot.get("tree"), list)
    ):
        raise ValueError("El árbol del commit está truncado o no acredita integridad completa.")
    identities = []
    all_paths = []
    for entry in snapshot["tree"]:
        if not isinstance(entry, dict) or entry.get("type") not in ("blob", "tree", "commit"):
            raise ValueError("El árbol del commit contiene una entrada no verificable.")
        sha = entry.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise ValueError("El árbol del commit contiene una entrada sin identidad verificable.")
        all_paths.append(entry.get("path"))
        identities.append((entry["type"], entry.get("mode"), sha))
    normalized_all = normalize_paths(all_paths)
    if len(normalized_all) != len(all_paths):
        raise ValueError("El árbol del commit contiene rutas duplicadas o ambiguas.")
    return {
        path: identity
        for path, identity in zip(normalized_all, identities, strict=True)
        if identity[0] != "tree"
    }


def initial_snapshot_paths(fetch: Callable[[str], object], prefix: str, after: str) -> list[str]:
    """Valida un primer push desde el árbol completo de su versión inmutable."""
    files = commit_tree(fetch, prefix, after)
    changed = list(files)
    if any(files.get(required, (None,))[0] != "blob" for required in (WORKFLOW, RECIPE, DIAGRAM)):
        raise ValueError(
            "El primer push debe incluir el workflow, la receta y el diagrama como archivos."
        )
    if validate_changes(changed):
        raise ValueError("El árbol inicial no incluye la documentación de sus cambios de entrega.")
    return changed


def changes_from_github(
    environ: Mapping[str, str], fetch_json: Callable[[str], object] | None = None
) -> list[str]:
    """Obtiene cambios completos para PR/push; rechaza las respuestas no acreditables."""
    event_name = environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "workflow_dispatch":
        return []
    if event_name not in ("pull_request", "push"):
        raise ValueError("Evento de CI sin procedimiento verificable de cambios.")
    if not environ.get("GITHUB_TOKEN"):
        raise ValueError("Falta token de solo lectura para comprobar cambios en GitHub.")
    repository = environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("El repositorio de GitHub no es válido.")
    try:
        event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    except (KeyError, OSError, json.JSONDecodeError) as error:
        raise ValueError("No se pudo leer el evento de CI para verificar cambios.") from error
    if not isinstance(event, dict):
        raise ValueError("El evento de CI no es un objeto válido.")
    fetch = fetch_json or github_fetcher(environ)
    prefix = f"/repos/{repository}"
    if event_name == "pull_request":
        number = event.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError("El evento no identifica una solicitud de cambio válida.")
        identity = pull_request_identity(fetch(f"{prefix}/pulls/{number}"))
        count, head_sha, base_sha = identity
        original = event.get("pull_request")
        if isinstance(original, dict):
            for side, expected in (("head", head_sha), ("base", base_sha)):
                reference = original.get(side)
                if not isinstance(reference, dict) or reference.get("sha") != expected:
                    raise ValueError("La solicitud cambió respecto al evento que ejecuta CI.")
        rows = []
        for page in range(1, math.ceil(count / 100) + 1):
            page_rows = fetch(f"{prefix}/pulls/{number}/files?per_page=100&page={page}")
            expected = min(100, count - len(rows))
            if not isinstance(page_rows, list) or len(page_rows) != expected:
                raise ValueError("La lista paginada de archivos está incompleta o cambió.")
            rows.extend(page_rows)
        filenames = [row.get("filename") for row in rows if isinstance(row, dict)]
        if (
            len(filenames) != count
            or any(not isinstance(name, str) for name in filenames)
            or len(set(filenames)) != count
        ):
            raise ValueError("La lista paginada no acredita archivos únicos y completos.")
        if pull_request_identity(fetch(f"{prefix}/pulls/{number}")) != identity:
            raise ValueError("La solicitud cambió mientras se comprobaba la lista de archivos.")
        return file_paths(rows)
    before, after = event.get("before"), event.get("after")
    if (
        any(
            not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)
            for sha in (before, after)
        )
        or after == "0" * 40
    ):
        raise ValueError("La actualización no proporciona dos versiones comparables.")
    if before == "0" * 40:
        return initial_snapshot_paths(fetch, prefix, after)
    comparison = fetch(f"{prefix}/compare/{before}...{after}?per_page=100&page=1")
    rows = comparison.get("files") if isinstance(comparison, dict) else None
    if not isinstance(rows, list):
        raise ValueError("La comparación no acredita todos los archivos cambiados.")
    # La comparación de GitHub se limita a 300 archivos, incluso si se pagina.
    if len(rows) >= 300:
        previous = commit_tree(fetch, prefix, before)
        current = commit_tree(fetch, prefix, after)
        return normalize_paths(
            sorted(
                path
                for path in previous.keys() | current.keys()
                if previous.get(path) != current.get(path)
            )
        )
    return file_paths(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changed-files", type=Path, help="JSON local con lista de rutas modificadas"
    )
    args = parser.parse_args(argv)
    try:
        errors = validate_structure(ROOT)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            if args.changed_files:
                raise ValueError("En CI los cambios deben obtenerse por la API de GitHub.")
            errors.extend(validate_changes(changes_from_github(os.environ)))
        elif args.changed_files:
            changes = normalize_paths(json.loads(args.changed_files.read_text(encoding="utf-8")))
            errors.extend(validate_changes(changes))
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("Documentación CI/CD coherente; archivos, enlaces y siete controles verificados.")
        if os.environ.get("GITHUB_ACTIONS") != "true" and not args.changed_files:
            print("Validación local de estructura; cambios de PR/push se comprueban en CI por API.")
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
