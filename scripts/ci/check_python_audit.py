"""Exige cobertura completa del grafo Python fijado antes de aceptar una auditoría."""

import argparse
import json
import re
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

ROOT = Path(__file__).resolve().parents[2]


def normalized_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_report(report, expected):
    """Rechaza ausencias, saltos de paquetes, versiones no fijadas y vulnerabilidades."""
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        return ["El informe no contiene la lista de dependencias Python auditadas."]
    if not expected or not report["dependencies"]:
        return ["El grafo esperado y el informe deben contener dependencias."]
    observed = set()
    errors = []
    for dependency in report["dependencies"]:
        if not isinstance(dependency, dict) or not isinstance(dependency.get("name"), str):
            errors.append("El informe contiene una dependencia sin identidad válida.")
            continue
        name = normalized_name(dependency["name"])
        if name in observed:
            errors.append(f"El informe repite una dependencia: {name}")
        observed.add(name)
        if name not in expected:
            errors.append(f"La dependencia auditada no tiene un pin declarado: {name}")
        elif dependency.get("version") != expected[name]:
            errors.append(f"La versión auditada no coincide con el pin: {name}")
        if "skip_reason" in dependency:
            errors.append(f"La auditoría omitió una dependencia: {name}")
        vulnerabilities = dependency.get("vulns")
        if not isinstance(vulnerabilities, list):
            errors.append(f"Falta un resultado de vulnerabilidades válido: {name}")
        elif vulnerabilities:
            errors.append(f"La dependencia tiene vulnerabilidades conocidas: {name}")
    for name in sorted(set(expected) - observed):
        errors.append(f"La auditoría no cubre una dependencia fijada: {name}")
    return errors


def load_expected_pins(root):
    """Recorre requisitos y restricciones locales con versiones exactas, sin resolver red."""
    root = root.resolve()
    pins = {}
    visited = set()

    def read(path):
        path = path.resolve()
        if not path.is_relative_to(root):
            raise ValueError("Los requisitos incluyen un archivo fuera del proyecto.")
        if path in visited:
            return
        visited.add(path)
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            included = re.fullmatch(r"-(?:r|c)\s+([^\s]+)", line)
            if included:
                read(path.parent / included.group(1))
                continue
            invalid = "Cada dependencia Python debe usar un pin exacto sin opciones externas."
            try:
                requirement = Requirement(line)
            except InvalidRequirement as error:
                raise ValueError(invalid) from error
            pins_for_package = tuple(requirement.specifier)
            if (
                requirement.url
                or requirement.extras
                or len(pins_for_package) != 1
                or pins_for_package[0].operator != "=="
                or "*" in pins_for_package[0].version
            ):
                raise ValueError(invalid)
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            name = normalized_name(requirement.name)
            version = pins_for_package[0].version
            if name in pins and pins[name] != version:
                raise ValueError(f"Los requisitos contienen versiones incompatibles: {name}")
            pins[name] = version

    read(root / "requirements-dev.txt")
    if not pins:
        raise ValueError("Los requisitos no declaran dependencias auditables.")
    return pins


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        expected = load_expected_pins(ROOT)
        report = json.loads(args.report.read_text(encoding="utf-8"))
        errors = validate_report(report, expected)
    except (OSError, ValueError) as error:
        print(f"No se pudo acreditar la auditoría Python: {error}")
        return 1
    for error in errors:
        print(error)
    if errors:
        return 1
    print(
        f"Python: {len(expected)} paquetes y versiones fijados auditados, sin omisiones ni vulnerabilidades conocidas."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
