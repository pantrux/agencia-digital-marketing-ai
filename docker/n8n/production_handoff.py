"""Publica órdenes verificadas en Synology para el consumidor de OpenClaw."""

import argparse
import hmac
import json
import os
import re
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

from production_inbox import (
    MAX_BODY,
    OPTIONAL_ORDER_FIELDS,
    ORDER_FIELDS,
    UUID,
    matches,
    verify_order,
)

DOMAIN = b"studio-production-handoff-v1\n"
MAX_ENVELOPE = MAX_BODY + 256
SPOOL = Path("/mnt/producciones/.agency-production-handoff")
SOURCE = Path("/var/lib/agency-production-inbox/inbox.sqlite3")
RENDER_PLANS = {"free": "Free", "pro": "Pro", "ultra": "Ultra", "ilimitado": "Ilimitado"}
RENDER_SIZES = {"1080p": (1080, 1920), "4k": (2160, 3840)}
QA_ROUNDS = {"free": 1, "pro": 2, "ultra": 3, "ilimitado": 3}


def render_plan(order):
    plan = RENDER_PLANS.get(order["plan_id"])
    if plan is None:
        raise ValueError("Plan de render desconocido")
    return plan


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def validate(order, identity):
    if (
        type(order) is not dict
        or order.keys() - OPTIONAL_ORDER_FIELDS != ORDER_FIELDS
        or order["order_id"] != identity
    ):
        raise ValueError("Orden de producción inválida")
    verify_order(dict(order, attempt=1, lease=identity, lease_expires_at=0))


def seal(order, key):
    validate(order, order["order_id"])
    signature = hmac.digest(key, DOMAIN + canonical(order), "sha256").hex()
    return canonical({"order": order, "signature": signature}) + b"\n"


def verify(raw, key, identity):
    if not 0 < len(raw) <= MAX_ENVELOPE:
        raise ValueError("Tamaño de orden inválido")
    envelope = json.loads(raw)
    if type(envelope) is not dict or envelope.keys() != {"order", "signature"}:
        raise ValueError("Sobre de producción inválido")
    order = envelope["order"]
    validate(order, identity)
    if not hmac.compare_digest(seal(order, key), raw):
        raise ValueError("Firma de producción inválida")
    return order


def read_regular(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as source:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > limit:
            raise ValueError("Archivo de producción inválido")
        return source.read(limit + 1)


def publish(path, raw):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".orden-")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_regular(path, max(len(raw), MAX_ENVELOPE)) != raw:
                raise ValueError("Orden existente con contenido distinto") from None
            return False
        return True
    finally:
        os.unlink(temporary)


def load_profile(profile):
    doc = json.loads(read_regular(profile, 2048))
    if (
        type(doc) is not dict
        or doc.keys() - {"mode"} != {"key", "owner", "fixture"}
        or doc.get("mode", "sandbox") not in ("sandbox", "production")
        or not matches(re.compile(r"[0-9a-f]{64}"), doc["key"])
    ):
        raise ValueError("Perfil de traspaso inválido")
    if not matches(UUID, doc["owner"]) or not matches(UUID, doc["fixture"]):
        raise ValueError("Titular o prueba inválidos")
    return doc


def eligible(order, profile):
    mode = profile.get("mode", "sandbox")
    if mode not in ("sandbox", "production") or order["mode"] != mode:
        return False
    if mode == "sandbox" and order["plan_id"] == "free":
        return order["user_id"] == profile["owner"] or is_test_order(order, profile)
    return order["plan_id"] in QA_ROUNDS


def is_test_order(order, profile):
    return (
        order["mode"] == "sandbox"
        and order["plan_id"] == "free"
        and order["order_id"] == profile["fixture"]
        and order["user_id"] != profile["owner"]
    )


def qa_rounds(order, profile):
    return 2 if is_test_order(order, profile) else QA_ROUNDS[order["plan_id"]]


def revision_leaf(identity, revision):
    return identity + ("" if revision == 1 else f"-v{revision}") + ".json"


def revision_from_stem(stem):
    match = re.fullmatch(f"({UUID.pattern})(?:-v([23]))?", stem)
    return (match[1], int(match[2] or 1)) if match else None


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("profile", type=Path)
    opts = cli.parse_args()
    profile = load_profile(opts.profile)
    if SPOOL.resolve(strict=True) != SPOOL or not SPOOL.is_dir():
        raise ValueError("Directorio de traspaso inválido")
    with closing(sqlite3.connect(SOURCE.as_uri() + "?mode=ro", uri=True)) as db:
        for identity, raw in db.execute(
            "SELECT order_id,payload FROM inbox ORDER BY received_at,order_id"
        ):
            order = json.loads(raw)
            validate(order, identity)
            if eligible(order, profile):
                publish(SPOOL / (identity + ".json"), seal(order, bytes.fromhex(profile["key"])))


if __name__ == "__main__":
    main()
