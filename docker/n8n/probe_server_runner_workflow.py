"""Verifica los runners externos con un workflow fijo en una instancia n8n de ensayo."""

import argparse
import http.cookiejar
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import uuid4

OWNER_EMAIL = "n8n-runner-probe@example.invalid"
WEBHOOK_BODY_LIMIT = 4096


def need(condition, code):
    if not condition:
        raise ValueError(code)


def fixture(path):
    kinds = (
        (
            "Webhook",
            "webhook",
            2.2,
            {"httpMethod": "POST", "path": path, "responseMode": "responseNode"},
        ),
        (
            "JavaScript",
            "code",
            2,
            {
                "language": "javaScript",
                "mode": "runOnceForAllItems",
                "jsCode": "return $input.all().map(({json}) => ({json:{js:json.body.value * 2}}));",
            },
        ),
        (
            "Python",
            "code",
            2,
            {
                "language": "pythonNative",
                "mode": "runOnceForAllItems",
                "pythonCode": 'return [{"json": {"js": item["json"]["js"], "python": item["json"]["js"] + 1}} for item in _items]',
            },
        ),
        ("Response", "respondToWebhook", 1.5, {"respondWith": "firstIncomingItem"}),
    )
    nodes = [
        {
            "id": str(uuid4()),
            "name": name,
            "type": "n8n-nodes-base." + kind,
            "typeVersion": version,
            "position": [index * 240, 0],
            "parameters": parameters,
        }
        for index, (name, kind, version, parameters) in enumerate(kinds)
    ]
    nodes[0]["webhookId"] = str(uuid4())
    return {
        "name": "Ensayo de runners externos",
        "nodes": nodes,
        "connections": {
            left[0]: {"main": [[{"node": right[0], "type": "main", "index": 0}]]}
            for left, right in zip(kinds, kinds[1:], strict=False)
        },
        "settings": {"executionOrder": "v1"},
    }


class LocalOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(f"REDIRECCION_HTTP_{code}")


def http_client(origin):
    parsed = urlsplit(origin)
    need(
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.netloc == f"127.0.0.1:{parsed.port}"
        and not (parsed.path or parsed.query or parsed.fragment),
        "ORIGEN_LOOPBACK_EXACTO",
    )
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), LocalOnly(), urllib.request.HTTPCookieProcessor(jar)
    )

    def request(path, payload=None, timeout=10, *, diagnose_webhook=False):
        raw = None if payload is None else json.dumps(payload).encode()
        target = urllib.request.Request(
            origin + path,
            data=raw,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with opener.open(target, timeout=timeout) as response:
                body = response.read(512 * 1024 + 1)
                need(len(body) <= 512 * 1024, "RESPUESTA_LIMITE")
                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            with error:
                diagnostic = (
                    webhook_diagnostic(error.read(WEBHOOK_BODY_LIMIT + 1))
                    if diagnose_webhook and error.code == 404
                    else None
                )
                return error.code, diagnostic

    return request


def webhook_diagnostic(raw):
    diagnostic = {
        "kind": "http_route_not_found",
        "body_bytes_read": min(len(raw), WEBHOOK_BODY_LIMIT),
    }
    if len(raw) > WEBHOOK_BODY_LIMIT:
        return {**diagnostic, "truncated": True}
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        return diagnostic
    message = body.get("message") if isinstance(body, dict) else None
    if isinstance(message, str) and re.fullmatch(
        r'The requested webhook "[^"\r\n]+" is not registered\.', message
    ):
        diagnostic["kind"] = "webhook_not_registered"
    elif isinstance(message, str) and message.startswith("This webhook is not registered for "):
        diagnostic["kind"] = "webhook_method_not_registered"
    else:
        diagnostic["kind"] = "json_not_found"
    return diagnostic


def publication_summary(request, workflow_path, node_id, version_id):
    status, body = request(workflow_path + "/publication-status", timeout=3)
    summary = {"http_status": status}
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        return summary
    data = body["data"]
    states = {"in_progress", "not_published", "published", "partial", "failed"}
    value = data.get("status")
    summary["status"] = value if isinstance(value, str) and value in states else "unrecognized"
    summary["live_version_matches"] = data.get("liveVersionId") == version_id
    triggers = data.get("triggers", [])
    if isinstance(triggers, list):
        for trigger in triggers[:8]:
            if isinstance(trigger, dict) and trigger.get("nodeId") == node_id:
                value = trigger.get("status")
                summary["trigger_status"] = (
                    value if value in ("activated", "failed") else "unrecognized"
                )
                break
    return summary


def call_webhook(request, route):
    deadline = time.monotonic() + 8
    attempts = 0
    while True:
        attempts += 1
        status, output = request(route, {"value": 21}, timeout=45, diagnose_webhook=True)
        waiting_for_registration = (
            status == 404
            and isinstance(output, dict)
            and output.get("kind") == "webhook_not_registered"
        )
        if not waiting_for_registration or time.monotonic() >= deadline:
            return status, output, attempts
        time.sleep(0.5)


def ready(request, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, body = request("/healthz/readiness", timeout=3)
            if status == 200:
                return body
        except (OSError, TimeoutError):
            pass
        time.sleep(0.5)
    raise TimeoutError("N8N_READINESS")


def object_data(status, body, step):
    need(
        200 <= status < 300 and isinstance(body, dict) and isinstance(body.get("data"), dict),
        f"{step}_HTTP_{status}",
    )
    return body["data"]


def read_owner_password(stream):
    raw = stream.read(259)
    need(0 < len(raw) <= 258, "OWNER_PASSWORD_STDIN")
    raw = raw.removesuffix(b"\r\n") if raw.endswith(b"\r\n") else raw.removesuffix(b"\n")
    try:
        password = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("OWNER_PASSWORD_STDIN") from None
    need(
        not any(char in password for char in "\0\r\n")
        and 8 <= len(password.encode("utf-16-le")) // 2 <= 64
        and re.search(r"[A-Z]", password)
        and re.search(r"[0-9]", password),
        "OWNER_PASSWORD_STDIN",
    )
    return password


def exercise(request, timeout, owner_password, *, existing=False, webhook_prefix="webhook"):
    ready(request, timeout)
    if existing:
        login = {"emailOrLdapLoginId": OWNER_EMAIL, "password": owner_password}
        object_data(*request("/rest/login", login), "OWNER_LOGIN")
    else:
        owner = {
            "email": OWNER_EMAIL,
            "firstName": "Runner",
            "lastName": "Probe",
            "password": owner_password,
        }
        object_data(*request("/rest/owner/setup", owner), "OWNER_SETUP")
    path = "runner-" + secrets.token_hex(10)
    payload = fixture(path)
    trigger = payload["nodes"][0]
    created = object_data(*request("/rest/workflows", payload), "WORKFLOW_CREATE")
    workflow_id = created["id"]
    need(re.fullmatch(r"[A-Za-z0-9_-]+", workflow_id), "WORKFLOW_ID")
    need(
        any(
            node.get("id") == trigger["id"] and node.get("webhookId") == trigger["webhookId"]
            for node in created["nodes"]
        ),
        "WORKFLOW_WEBHOOK_ID",
    )
    workflow_path = "/rest/workflows/" + workflow_id
    activated = object_data(
        *request(
            workflow_path + "/activate",
            {"versionId": created["versionId"], "expectedChecksum": created["checksum"]},
        ),
        "WORKFLOW_ACTIVATE",
    )
    result = {"success": False, "db_ready": True, "workflow_id": workflow_id}
    try:
        need(activated["activeVersionId"] == created["versionId"], "WORKFLOW_VERSION")
        status, output, attempts = call_webhook(request, f"/{webhook_prefix}/{path}")
        result.update(webhook_status=status, webhook_attempts=attempts)
        if status == 404:
            result["webhook_diagnostic"] = output
            try:
                result["publication"] = publication_summary(
                    request, workflow_path, trigger["id"], created["versionId"]
                )
            except (OSError, ValueError):
                result["publication"] = {"available": False}
        if status == 200 and output == {"js": 42, "python": 43}:
            result.update(success=True, output=output)
    finally:
        try:
            status, body = request(
                workflow_path + "/deactivate", {"expectedChecksum": activated["checksum"]}
            )
            deactivated = object_data(status, body, "WORKFLOW_DEACTIVATE")
            result["workflow_deactivated"] = (
                "activeVersionId" in deactivated and deactivated["activeVersionId"] is None
            )
        except (OSError, ValueError):
            result["workflow_deactivated"] = False
        result["success"] = result["success"] and result["workflow_deactivated"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin")
    parser.add_argument("--ready-seconds", type=int, default=60)
    parser.add_argument("--webhook-prefix", default="webhook")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fresh-test-instance",
        action="store_true",
        help="Confirma una instancia de ensayo vacía. Lee por stdin la contraseña de propietario ya respaldada.",
    )
    mode.add_argument(
        "--existing-test-instance",
        action="store_true",
        help="Inicia sesión con el propietario de un ensayo anterior. Lee la contraseña respaldada por stdin.",
    )
    args = parser.parse_args()
    if args.origin is None:
        print(json.dumps(fixture("runner-probe"), sort_keys=True))
        return
    if (
        not (args.fresh_test_instance or args.existing_test_instance)
        or not 1 <= args.ready_seconds <= 180
    ):
        parser.error("Seleccionar el modo de instancia de ensayo en loopback.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.webhook_prefix):
        parser.error("El prefijo del webhook debe ser un segmento de ruta ASCII.")
    if sys.stdin.isatty():
        parser.error("Pasar por stdin la contraseña de propietario ya respaldada.")
    try:
        owner_password = read_owner_password(sys.stdin.buffer)
    except ValueError:
        parser.error(
            "stdin debe contener una contraseña de propietario válida en una sola línea UTF-8."
        )
    result = exercise(
        http_client(args.origin),
        args.ready_seconds,
        owner_password,
        existing=args.existing_test_instance,
        webhook_prefix=args.webhook_prefix,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
