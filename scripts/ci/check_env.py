"""Valida la bóveda sin ejecutar ni imprimir el contenido de sus variables."""

import argparse
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
PENDING = re.compile(
    r"(?:^|[^a-z0-9])(?:CHANGE_ME|REPLACE_ME|PENDIENTE|GENERAR|COMPLETAR|CONFIGURAR|POR_DEFINIR|TODO)(?:$|[^a-z0-9])|^<.*>$",
    re.IGNORECASE,
)


def parse_env(content: str) -> tuple[dict[str, str], list[str]]:
    """Lee asignaciones literales; nunca usa eval, source ni sustituciones."""
    values, errors = {}, []
    for number, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = KEY.fullmatch(line)
        if not match:
            errors.append(f"Asignación inválida en línea {number}.")
            continue
        key, value = match.groups()
        if key in values:
            errors.append(f"Clave duplicada: {key}.")
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                errors.append(f"Comillas sin cerrar: {key}.")
                continue
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values, errors


def is_pending(value: str) -> bool:
    return not value or bool(PENDING.search(value))


def validate_template(values: dict[str, str]) -> list[str]:
    return [
        f"La plantilla debe usar CHANGE_ME: {key}."
        for key, value in values.items()
        if value != "CHANGE_ME"
    ]


def compare_keys(vault: dict[str, str], template: dict[str, str]) -> list[str]:
    errors = [f"Falta en plantilla: {key}." for key in sorted(vault.keys() - template.keys())]
    errors += [f"Falta en bóveda: {key}." for key in sorted(template.keys() - vault.keys())]
    return errors


def validate_comments(content: str) -> list[str]:
    errors, comments = [], []
    for line in content.splitlines():
        if line.lstrip().startswith("#"):
            comments.append(line.casefold())
        elif KEY.match(line.strip()):
            key = KEY.match(line.strip()).group(1)
            block = "\n".join(comments)
            for label in ("# para qué:", "# dónde:", "# riesgo al rotar:"):
                if label not in block:
                    errors.append(f"Falta comentario {label} en {key}.")
            comments = []
        elif line.strip():
            comments = []
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-only", action="store_true")
    parser.add_argument("--require", nargs="*", default=[])
    args = parser.parse_args()
    template_text = (ROOT / ".env.example").read_text(encoding="utf-8-sig")
    template, errors = parse_env(template_text)
    errors += validate_template(template) + validate_comments(template_text)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        forbidden = [
            path.name
            for path in ROOT.glob(".env*")
            if path.is_file() and path.name != ".env.example"
        ]
        if forbidden:
            errors.append("El checkout CI contiene una bóveda o archivo de entorno privado.")
    if not args.template_only:
        path = ROOT / ".env"
        if not path.is_file():
            errors.append("No existe la bóveda local .env.")
        else:
            vault_text = path.read_text(encoding="utf-8-sig")
            vault, vault_errors = parse_env(vault_text)
            errors += vault_errors + compare_keys(vault, template) + validate_comments(vault_text)
            pending = sorted(key for key, value in vault.items() if is_pending(value))
            print(f"Bóveda: {len(vault)} variables; {len(pending)} reservas pendientes.")
            for key in pending:
                print(f"Pendiente: {key}")
            errors += [
                f"Credencial requerida ausente o pendiente: {key}."
                for key in args.require
                if key not in vault or is_pending(vault[key])
            ]
    for error in errors:
        print(error)
    if not errors:
        print("Gobierno de variables: correcto; valores ocultos.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
