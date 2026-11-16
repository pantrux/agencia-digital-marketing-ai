"""Versiones canónicas de módulos Go; no implementa resolución MVS ni consultas."""

import re

PATTERN = re.compile(
    r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(\+incompatible)?"
)
RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])", re.IGNORECASE)


def version(value):
    if not isinstance(value, str) or len(value) > 160:
        raise ValueError("Versión ausente o excesiva.")
    match = PATTERN.fullmatch(value)
    if not match:
        raise ValueError("Se requiere una versión de módulo canónica.")
    major, minor, patch, pre, build = match.groups()
    prerelease = tuple(pre.split(".")) if pre else ()
    if any(p.isdigit() and len(p) > 1 and p.startswith("0") for p in prerelease):
        raise ValueError("Identificador numérico prerelease no canónico.")
    return int(major), int(minor), int(patch), prerelease, build


def compare(left, right):
    a, b = version(left), version(right)
    if a[:3] != b[:3]:
        return (a[:3] > b[:3]) - (a[:3] < b[:3])
    x, y = a[3], b[3]
    if not x or not y:
        return (not x) - (not y)
    for first, second in zip(x, y, strict=False):
        if first == second:
            continue
        if first.isdigit() and second.isdigit():
            return (int(first) > int(second)) - (int(first) < int(second))
        if first.isdigit() != second.isdigit():
            return -1 if first.isdigit() else 1
        return (first > second) - (first < second)
    return (len(x) > len(y)) - (len(x) < len(y))


def validate_path(path):
    parts = path.split("/") if isinstance(path, str) else []
    if (
        not parts
        or len(path) > 512
        or not path.isascii()
        or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", parts[0])
        or "." not in parts[0]
    ):
        raise ValueError("Ruta de módulo no válida.")
    for part in parts:
        if (
            not re.fullmatch(r"[A-Za-z0-9._~-]+", part)
            or part in (".", "..")
            or part.startswith(".")
            or part.endswith(".")
            or RESERVED.fullmatch(part.split(".")[0])
            or re.search(r"~[0-9]+$", part.split(".")[0])
        ):
            raise ValueError("Componente de módulo no seguro.")
    return parts


def validate_module(path, release):
    parts = validate_path(path)
    parsed = version(release)
    major = parsed[0]
    if path.startswith("gopkg.in/"):
        suffix = re.search(r"\.v([0-9]+)(?:-unstable)?$", path)
        if not suffix or (len(suffix[1]) > 1 and suffix[1].startswith("0")):
            raise ValueError("gopkg.in requiere sufijo major canónico.")
        wanted = int(suffix[1])
        if wanted == 1 and release.startswith("v0.0.0-"):
            return
        if major != wanted:
            raise ValueError("Major incompatible con gopkg.in.")
    elif re.fullmatch(r"v[0-9]+", parts[-1]):
        suffix = parts[-1][1:]
        if suffix.startswith("0") or int(suffix) < 2 or major != int(suffix):
            raise ValueError("Major incompatible con el sufijo de ruta.")
    elif major > 1 and parsed[4] != "+incompatible":
        raise ValueError("Major requiere sufijo o +incompatible.")


def escape(value):
    if not isinstance(value, str) or not value.isascii() or "!" in value:
        raise ValueError("Identidad no escapable.")
    return "".join("!" + char.lower() if char.isupper() else char for char in value)
