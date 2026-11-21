import hashlib
import hmac
import importlib.util
import json
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "openclaw-agency-setup/sofia-broker/sofia_broker.py"
spec = importlib.util.spec_from_file_location("agency_sofia_broker", MODULE)
broker = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = broker
spec.loader.exec_module(broker)

SECRET = b"only-a-synthetic-key-for-local-sofia-contract-tests"
NOW = 1_794_000_000
PATH = "/v1/sofia/chat"
REQUEST = {
    "agent_id": "agency-account",
    "actor_id": "a" * 64,
    "order_id": "11111111-1111-4111-8111-111111111111",
    "billing_mode": "sandbox",
    "plan_id": "free",
    "request_id": "22222222-2222-4222-8222-222222222222",
    "message": "Edición de vídeo",
}
SECOND = "33333333-3333-4333-8333-333333333333"


def wire(raw, direction="request", status="POST", timestamp=NOW):
    body_hash = hashlib.sha256(raw).hexdigest()
    message = f"studio-sofia-v1\n{direction}\n{timestamp}\n{status}\n{PATH}\n{body_hash}"
    return "v1=" + hmac.new(SECRET, message.encode("ascii"), hashlib.sha256).hexdigest()


def envelope(**fields):
    return json.dumps(dict(REQUEST, **fields), ensure_ascii=False, separators=(",", ":")).encode()


def process(instance, raw=None, timestamp=NOW):
    if raw is None:
        raw = envelope()
    return instance.process(raw, str(timestamp), wire(raw, timestamp=timestamp))


@pytest.fixture
def service(tmp_path):
    calls = []

    def run(turn):
        calls.append(turn)
        return "Un resumen de edición"

    state = tmp_path / "ledger.db"
    return (
        broker.Sofia(SECRET, broker.Ledger(state), run, REQUEST["actor_id"], clock=lambda: NOW),
        calls,
        state,
    )


def assert_status(status, callback):
    with pytest.raises(broker.Rejected) as exc:
        callback()
    assert exc.value.status == status


def test_worker_wire_and_durable_idempotent_replay(service):
    instance, calls, state = service
    raw, headers = process(instance)
    assert headers == {"timestamp": str(NOW), "signature": wire(raw, "response", "200")}
    assert json.loads(raw) == {
        "actor_id": REQUEST["actor_id"],
        "order_id": REQUEST["order_id"],
        "request_id": REQUEST["request_id"],
        "text": "Un resumen de edición",
    }
    restarted = broker.Sofia(
        SECRET, broker.Ledger(state), instance.run, instance.owner, clock=lambda: NOW + 1
    )
    replay, replay_headers = process(restarted)
    assert replay == raw
    assert replay_headers["signature"] == wire(raw, "response", "200", NOW + 1)
    assert len(calls) == 1
    assert calls[0].message == REQUEST["message"]
    assert calls[0].key == (REQUEST["actor_id"], REQUEST["order_id"], REQUEST["request_id"])
    assert REQUEST["message"].encode() not in state.read_bytes()


def test_same_request_id_with_different_body_conflicts(service):
    instance, calls, _ = service
    process(instance)
    assert_status(409, lambda: process(instance, envelope(message="Otra cosa")))
    assert len(calls) == 1


def test_tamper_stale_and_noncanonical_signature_stop_before_inference(service):
    instance, calls, _ = service
    assert_status(
        401, lambda: instance.process(envelope(message="alterado"), str(NOW), wire(envelope()))
    )
    assert_status(401, lambda: process(instance, timestamp=NOW - 301))
    assert_status(401, lambda: process(instance, timestamp=NOW + 301))
    assert_status(401, lambda: instance.process(envelope(), f"0{NOW}", wire(envelope())))
    assert not calls


@pytest.mark.parametrize(
    "raw",
    [
        envelope(agent_id="agency-editor"),
        envelope(session_id="main"),
        envelope(message="é" * 1025),
        envelope(message="hola\x00"),
        envelope(message="  "),
        envelope(message="un texto")[:-1] + b',"message":"duplicado"}',
        b"\xff",
        b"[" * 12000,
        b"{}" + b" " * 12288,
    ],
)
def test_closed_request_surface(service, raw):
    instance, calls, _ = service
    assert_status(413 if len(raw) > 12288 else 422, lambda: process(instance, raw))
    assert not calls


def test_worker_signature_requires_known_environment_and_plan(service):
    instance, calls, _ = service
    for changes in [{"plan_id": "desconocido"}, {"billing_mode": "desconocido"}]:
        raw = envelope(**changes)
        assert_status(403, lambda raw=raw: process(instance, raw))
    assert not calls
    process(instance)


def test_firma_autorizada_conserva_cuenta_plan_y_entorno(service):
    instance, calls, _ = service
    raw = envelope(actor_id="b" * 64, plan_id="ilimitado", billing_mode="production")
    process(instance, raw)
    assert calls[0].actor == "b" * 64
    assert calls[0].plan_id == "ilimitado"
    assert calls[0].billing_mode == "production"


def test_overlap_is_atomic_across_connections(service):
    instance, calls, state = service
    entered, release = threading.Event(), threading.Event()

    def run(turn):
        entered.set()
        assert release.wait(5)
        return "listo"

    primary = broker.Sofia(SECRET, broker.Ledger(state), run, instance.owner, clock=lambda: NOW)
    errors = []

    def first():
        try:
            process(primary)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert entered.wait(5)
        assert_status(429, lambda: process(instance))
        assert_status(429, lambda: process(instance, envelope(request_id=SECOND)))
        assert_status(429, lambda: process(instance, envelope(order_id=SECOND)))
        assert_status(429, lambda: process(instance, envelope(actor_id="b" * 64)))
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert not calls


def test_ambiguous_upstream_failure_stays_fenced_after_restart(service):
    instance, calls, state = service

    def uncertain(turn):
        raise TimeoutError("la excepción original no sale por HTTP")

    failed = broker.Sofia(
        SECRET, broker.Ledger(state), uncertain, instance.owner, clock=lambda: NOW
    )
    assert_status(503, lambda: process(failed))
    restarted = broker.Sofia(
        SECRET, broker.Ledger(state), instance.run, instance.owner, clock=lambda: NOW + 600
    )
    assert_status(429, lambda: process(restarted, timestamp=NOW + 600))
    assert_status(429, lambda: process(restarted, envelope(request_id=SECOND), NOW + 600))
    assert not calls


def test_connect_failure_is_retryable_before_sending_turn(service, monkeypatch):
    instance, calls, state = service

    def offline(connection):
        raise broker.DelegateUnavailable

    monkeypatch.setattr(broker.UnixHTTPConnection, "connect", offline)
    failed = broker.Sofia(
        SECRET,
        broker.Ledger(state),
        broker.OwnerUnix(state, SECRET),
        instance.owner,
        clock=lambda: NOW,
    )
    assert_status(503, lambda: process(failed))
    assert process(instance)[0]
    assert len(calls) == 1


def test_reply_cache_expiry_leaves_idempotency_tombstone(service):
    instance, calls, state = service
    process(instance)
    later = NOW + 3601
    restarted = broker.Sofia(
        SECRET, broker.Ledger(state), instance.run, instance.owner, clock=lambda: later
    )
    assert_status(409, lambda: process(restarted, timestamp=later))
    assert len(calls) == 1


@pytest.mark.parametrize("change", [None, "actor", "stale", "signature", "text", "extra"])
def test_unix_delegate_requires_signed_matching_text(monkeypatch, change):
    local_key = b"independent-key-for-synthetic-unix-broker"
    path = Path("/tmp/fixture/sofia-owner.sock")
    fixed_uri = "/v1/sofia/owner-turn"
    prompt = "/reset\nEdición vertical"
    turn = broker.signed_turn(envelope(message=prompt))

    def signature(body, stamp, direction, action):
        raw = f"studio-sofia-owner-unix-v1\n{direction}\n{stamp}\n{action}\n{fixed_uri}\n"
        raw += hashlib.sha256(body).hexdigest()
        return "v1=" + hmac.new(local_key, raw.encode("ascii"), hashlib.sha256).hexdigest()

    class Response:
        status = 200

        def __init__(self):
            actor = "b" * 64 if change == "actor" else turn.actor
            text = "á" * 4097 if change == "text" else "Señal local"
            data = {
                "actor_id": actor,
                "order_id": turn.order,
                "request_id": turn.request,
                "text": text,
            }
            if change == "extra":
                data["tools"] = []
            self.raw = json.dumps(data).encode()
            stamp = str(NOW - 61 if change == "stale" else NOW)
            mac = (
                "v1=" + "0" * 64
                if change == "signature"
                else signature(self.raw, stamp, "response", "200")
            )
            self.headers = {
                "Content-Type": "application/json",
                "timestamp": stamp,
                "signature": mac,
            }

        def getheader(self, name):
            return self.headers[name]

        def read(self, limit):
            return self.raw[:limit]

        def close(self):
            pass

    class Connection:
        def __init__(self, supplied_path):
            assert supplied_path == path
            self.sock = object()

        def connect(self):
            pass

        def request(self, method, uri, body, headers):
            assert (method, uri) == ("POST", fixed_uri)
            assert list(json.loads(body)) == [
                "actor_id",
                "order_id",
                "request_id",
                "billing_mode",
                "plan_id",
                "message",
            ]
            assert json.loads(body)["message"] == prompt
            assert json.loads(body)["billing_mode"] == "sandbox"
            assert json.loads(body)["plan_id"] == "free"
            assert "Authorization" not in headers
            assert headers["signature"] == signature(body, str(NOW), "request", "POST")

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(broker, "UnixHTTPConnection", Connection)
    delegate = broker.OwnerUnix(path, local_key, clock=lambda: NOW)
    if change is None:
        assert delegate(turn) == "Señal local"
    else:
        assert_status(503, lambda: delegate(turn))


def test_http_only_exact_post_and_one_auth_header(service):
    instance, calls, _ = service
    with broker.SofiaServer(("127.0.0.1", 0), instance) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:

            def request(method, path, headers, duplicate=False):
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                try:
                    conn.putrequest(method, path)
                    for key, value in headers.items():
                        conn.putheader(key, value)
                    if duplicate:
                        conn.putheader("timestamp", str(NOW))
                    conn.endheaders(envelope())
                    response = conn.getresponse()
                    return response.status, response.read()
                finally:
                    conn.close()

            payload_headers = {
                "Content-Length": str(len(envelope())),
                "Content-Type": "application/json",
                "timestamp": str(NOW),
                "signature": wire(envelope()),
            }
            assert request("GET", PATH, payload_headers)[0] == 405
            assert request("POST", PATH + "?x=1", payload_headers)[0] == 404
            assert request("POST", PATH, payload_headers, duplicate=True)[0] == 401
            assert (
                request("POST", PATH, {**payload_headers, "Transfer-Encoding": "chunked"})[0] == 400
            )
            status, raw = request("POST", PATH, payload_headers)
            assert status == 200 and json.loads(raw)["text"].startswith("Un resumen")
        finally:
            server.shutdown()
            thread.join(5)
    assert len(calls) == 1
