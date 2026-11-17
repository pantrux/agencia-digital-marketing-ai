"""Entrega a Mateo la orden y el brief confirmado en Studio."""

import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import sqlite3
import stat
import time
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path

from production_handoff import canonical, eligible, load_profile, publish, read_regular, validate
from production_inbox import UUID, matches
from production_sofia import JOURNAL, LIMIT

ENDPOINT = "https://studio.e-scale.cl/api/internal/production/dispatch"
SOFIA = Path("/var/lib/agency-production-sofia")
BRIEF = {"objective": 800, "audience": 480, "tone": 240}
ANGLE = {"angulo": 140, "gancho": 200, "copy": 700, "cta": 200}
NATIVE_PATH = "/v1/mateo/strategy"
NATIVE_SOCKET = Path("/run/user/1000/agency-sofia-owner/mateo.sock")
PROTOCOL = "studio-sofia-owner-unix-v1"
ORDER_ERRORS = (OSError, ValueError, TypeError, KeyError, http.client.HTTPException)


def text_fields(obj, limits, optional=()):
    return all(
        type(obj[k]) is str and len(obj[k]) <= limit and (k in optional or obj[k].strip())
        for k, limit in limits.items()
    )


def confirmed(payload, order):
    if type(payload) is not dict or payload.keys() != {"brief"}:
        raise ValueError("Consulta de brief inválida")
    brief = payload["brief"]
    if brief is None:
        return None
    if (
        type(brief) is not dict
        or brief.keys() - {"ad_cta"} != set(BRIEF) | {"order_id", "user_id", "confirmed_at"}
        or brief["order_id"] != order["order_id"]
        or brief["user_id"] != order["user_id"]
        or type(brief["confirmed_at"]) is not int
        or brief["confirmed_at"] < 0
        or not text_fields(brief, BRIEF, ("tone",))
        or ("ad_cta" in brief and not text_fields(brief, {"ad_cta": 70}))
    ):
        raise ValueError("Brief confirmado inválido")
    return brief


def strategy_reply(text):
    if not 0 < len(text) <= 8192:
        raise ValueError("Respuesta de Mateo inválida")
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text.removeprefix("```json\n").removesuffix("\n```")
    data = json.loads(text)
    if type(data) is not dict or data.keys() != {"angulos"}:
        raise ValueError("Estrategia de Mateo inválida")
    angles = data["angulos"]
    if (
        type(angles) is not list
        or len(angles) != 3
        or any(
            type(row) is not dict or row.keys() != ANGLE.keys() or not text_fields(row, ANGLE)
            for row in angles
        )
        or len({r["angulo"].strip().casefold() for r in angles}) != 3
    ):
        raise ValueError("Ángulos de Mateo inválidos")
    return angles


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args):
        return None


def dispatch_request(payload, token):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        ENDPOINT,
        data=canonical(payload),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Studio Production)",
            "Authorization": "Bearer " + token,
        },
    )
    with opener.open(request, timeout=10) as response:
        raw = response.read(LIMIT + 1)
        if response.status != 200 or len(raw) > LIMIT:
            raise ValueError("Respuesta del despacho de Studio inválida")
    return json.loads(raw)


def lookup(order, token):
    return confirmed(
        dispatch_request({"action": "brief", "order_id": order["order_id"]}, token), order
    )


def signed(key, stamp, action, raw):
    direction = "request" if action == "POST" else "response"
    data = "\n".join(
        (PROTOCOL, direction, stamp, action, NATIVE_PATH, hashlib.sha256(raw).hexdigest())
    )
    return "v1=" + hmac.digest(key, data.encode("ascii"), "sha256").hex()


def verify_native(raw, stamp, signature, key, turn, now):
    if len(raw) > LIMIT or not re.fullmatch(r"[0-9]{10}", stamp) or abs(now - int(stamp)) > 60:
        raise ValueError("Respuesta nativa vencida")
    if not hmac.compare_digest(signature, signed(key, stamp, "200", raw)):
        raise ValueError("Firma nativa inválida")
    doc = json.loads(raw)
    fields = ("actor_id", "order_id", "request_id")
    if (
        type(doc) is not dict
        or doc.keys() != set(fields) | {"text"}
        or any(doc[name] != turn[name] for name in fields)
    ):
        raise ValueError("Respuesta nativa de otra orden")
    if type(doc["text"]) is not str:
        raise ValueError("Respuesta nativa inválida")
    return doc["text"]


def native_call(order, message, owner, key):
    info = NATIVE_SOCKET.lstat()
    if (
        not stat.S_ISSOCK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise ValueError("Socket de Mateo inválido")
    turn = {
        "actor_id": hmac.digest(key, ("studio-mateo-actor-v1\n" + owner).encode(), "sha256").hex(),
        "order_id": order,
        "request_id": str(uuid.uuid4()),
        "billing_mode": "sandbox",
        "plan_id": "free",
        "message": message,
    }
    raw = json.dumps(turn, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(52)
        client.connect(str(NATIVE_SOCKET))
        conn = http.client.HTTPConnection("localhost", timeout=52)
        conn.auto_open = False
        conn.sock = client
        try:
            conn.request(
                "POST",
                NATIVE_PATH,
                body=raw,
                headers={
                    "Content-Type": "application/json",
                    "Connection": "close",
                    "timestamp": timestamp,
                    "signature": signed(key, timestamp, "POST", raw),
                },
            )
            with conn.getresponse() as response:
                body = response.read(LIMIT + 1)
                if response.status != 200:
                    raise ValueError(f"Mateo rechazó el turno ({response.status})")
                return verify_native(
                    body,
                    response.getheader("timestamp", ""),
                    response.getheader("signature", ""),
                    key,
                    turn,
                    int(time.time()),
                )
        finally:
            conn.close()


def run_mateo(receipt, brief, preliminary, target, owner, key):
    order = receipt["order"]
    if (
        preliminary["order_id"] != order["order_id"]
        or preliminary["source_sha256"] != order["media"]["sha256"]
    ):
        raise ValueError("El brief preliminar pertenece a otro medio")
    facts = {
        **{k: brief[k] for k in BRIEF},
        **({"cta_confirmada": brief["ad_cta"]} if "ad_cta" in brief else {}),
        "preliminar": preliminary["brief"],
        "plan": order["plan_id"],
        "duracion_segundos": receipt["duration_seconds"],
    }
    text = native_call(order["order_id"], json.dumps(facts, ensure_ascii=False), owner, key)
    result = {
        "order_id": order["order_id"],
        "source_sha256": order["media"]["sha256"],
        "brief_sha256": hashlib.sha256(canonical(brief)).hexdigest(),
        "angulos": strategy_reply(text),
        "modelo": "openai/gpt-6.1-sol",
        "runtime": "codex",
        "ejecucion": "isolated-agent-runtime",
    }
    publish(target, canonical(result) + b"\n")


def process_pending(profile, token, key, output):
    # El observador de Valeria vigila el directorio de resultados, no sus subdirectorios.
    state = output / "reintentos"
    state.mkdir(mode=0o700, exist_ok=True)
    if state.resolve(strict=True) != state:
        raise ValueError("Directorio de reintentos inválido")
    with closing(sqlite3.connect(state / "mateo.sqlite3")) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS retries (
                order_id TEXT PRIMARY KEY, failures INTEGER NOT NULL, due_at INTEGER NOT NULL
            ) STRICT"""
        )
        for path in sorted(JOURNAL.glob("*.json")):
            target = output / path.name
            preliminary_file = SOFIA / path.name
            if not matches(UUID, path.stem) or target.exists() or not preliminary_file.is_file():
                continue
            previous = db.execute(
                "SELECT failures,due_at FROM retries WHERE order_id=?", (path.stem,)
            ).fetchone()
            if previous and previous[1] > int(time.time()):
                continue
            try:
                receipt = json.loads(read_regular(path, LIMIT))
                order = receipt["order"]
                validate(order, path.stem)
                if not eligible(order, profile) or (brief := lookup(order, token)) is None:
                    continue
                run_mateo(
                    receipt,
                    brief,
                    json.loads(read_regular(preliminary_file, LIMIT)),
                    target,
                    profile["owner"],
                    key,
                )
            except ORDER_ERRORS:
                failures = min(previous[0] + 1, 5) if previous else 1
                due_at = int(time.time()) + min(30 * 2 ** (failures - 1), 300)
                with db:
                    db.execute(
                        """INSERT INTO retries (order_id, failures, due_at) VALUES (?, ?, ?)
                        ON CONFLICT(order_id) DO UPDATE
                        SET failures=excluded.failures, due_at=excluded.due_at""",
                        (path.stem, failures, due_at),
                    )
                raise
            if previous:
                with db:
                    db.execute("DELETE FROM retries WHERE order_id=?", (path.stem,))
            break


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("profile", type=Path)
    cli.add_argument("token", type=Path)
    cli.add_argument("socket_key", type=Path)
    cli.add_argument("out", type=Path)
    opts = cli.parse_args()
    profile = load_profile(opts.profile)
    token = read_regular(opts.token, 64).decode("ascii")
    key = read_regular(opts.socket_key, 64)
    if not re.fullmatch(r"[a-f0-9]{64}", token) or not re.fullmatch(rb"[a-f0-9]{64}", key):
        raise ValueError("Credencial de consulta inválida")
    if any(p.resolve(strict=True) != p for p in (JOURNAL, SOFIA, opts.out)):
        raise ValueError("Directorios de estrategia inválidos")
    process_pending(profile, token, key, opts.out)


if __name__ == "__main__":
    try:
        main()
    except (*ORDER_ERRORS, sqlite3.Error):
        raise SystemExit("Falló el turno de estrategia de Mateo") from None
