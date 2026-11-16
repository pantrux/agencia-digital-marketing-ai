"""Derivar sólo lectura del SQL congelado; importar no lee ni ejecuta recursos."""

import hashlib
import re

ORIGINAL_SHA256 = "3615b4132cdbc7157fc766ad88c326b1832d132ea7a05e1de0c8bce59899885b"
SUFFIX_SHA256 = "51610082ab0efbb1e58b1e2915ced0d8532b24201ff1624bd5446185b095138e"
OLD_PREDICATE = b"OR ids[1] <> q.id"
NEW_PREDICATE = b"OR NOT (q.id = ANY(ids))"


def derive_readonly_sql(raw: bytes) -> bytes:
    """Conservar el sufijo byte a byte salvo la pertenencia de self al top 10."""
    if (
        type(raw) is not bytes
        or len(raw) != 5588
        or hashlib.sha256(raw).hexdigest() != ORIGINAL_SHA256
    ):
        raise ValueError("Se requieren los bytes exactos del SQL original congelado.")
    if raw.count(b"\\endif") != 1:
        raise ValueError("El SQL debe contener un único cierre del bloque DDL.")
    suffix = raw.partition(b"\\endif")[2]
    if (
        hashlib.sha256(suffix).hexdigest() != SUFFIX_SHA256
        or any(line.lstrip().startswith(b"\\") for line in suffix.splitlines())
        or re.search(
            rb"(?im)^\s*(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|TRUNCATE|COPY|GRANT|REVOKE)\b",
            suffix,
        )
        or suffix.count(OLD_PREDICATE) != 1
        or NEW_PREDICATE in suffix
    ):
        raise ValueError("Sufijo no acreditado como la lectura congelada sin metacomandos.")
    return suffix.replace(OLD_PREDICATE, NEW_PREDICATE, 1)
