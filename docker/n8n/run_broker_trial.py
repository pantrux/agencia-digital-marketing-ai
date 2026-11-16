"""Supervisa un launcher y un runner Python dentro del contenedor sin red."""

import asyncio
import hashlib
import importlib.util
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

SETUP = Path("/run/n8n-broker")
SECRETS = Path("/run/n8n-broker-secrets")
BROKER_PORT = 5683
EXPECTED_EVENTS = (
    "launcher_registered",
    "launcher_deferred",
    "python_registered",
    "python_task_accepted",
    "python_task_done",
)


def broker_child():
    spec = importlib.util.spec_from_file_location("agency_broker_fixture", SETUP / "fixture.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    auth = fixture.read_token(SECRETS / "auth")
    grants = tuple(fixture.read_token(SECRETS / name) for name in ("launcher", "python"))
    assert len({auth, *grants}) == 3
    # La prueba en la VM sólo usa los valores ya respaldados por el operador.
    issued = iter(grants)
    fixture.secrets = types.SimpleNamespace(token_urlsafe=lambda _size: next(issued))
    return asyncio.run(fixture.serve(auth, BROKER_PORT, 45))


def ready(stream):
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        assert selector.select(timeout=10)
    raw = stream.readline(4097)
    assert 0 < len(raw) <= 4096
    return json.loads(raw)


def stop(proc, *, seconds):
    if proc is None:
        return None
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
            return False
    return True


def listener(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def group_gone(proc):
    if proc is None:
        return True
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return True
    return False


def trial(out):
    assert os.geteuid() == os.getegid() == 1000
    assert sys.version_info[:3] == (3, 13, 18)
    assert not any(listener(p) for p in (5680, 5682, BROKER_PORT))
    broker = launcher = None
    started = time.monotonic()
    common = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
    with tempfile.TemporaryFile() as broker_log, tempfile.TemporaryFile() as launcher_log:
        try:
            out["step"] = "broker"
            broker = subprocess.Popen(
                [sys.executable, "-I", "-B", __file__, "--broker-child"],
                env=common,
                stdout=subprocess.PIPE,
                stderr=broker_log,
                start_new_session=True,
            )
            assert ready(broker.stdout) == {"event": "ready", "port": BROKER_PORT}
            out["step"] = "launcher"
            auth = (SECRETS / "auth").read_text(encoding="ascii").strip()
            env = {
                **common,
                "N8N_RUNNERS_AUTH_TOKEN": auth,
                "N8N_RUNNERS_TASK_BROKER_URI": f"http://127.0.0.1:{BROKER_PORT}",
                "N8N_RUNNERS_CONFIG_PATH": str(SETUP / "runners.json"),
                "N8N_RUNNERS_LAUNCHER_BROKER_READINESS_POLL_INTERVAL_MS": "100",
                "N8N_RUNNERS_LAUNCHER_HEALTH_CHECK_PORT": "5680",
                "N8N_RUNNERS_LAUNCHER_GRACEFUL_SHUTDOWN_TIMEOUT": "3",
                "N8N_RUNNERS_LAUNCHER_LOG_LEVEL": "info",
                "N8N_RUNNERS_TASK_TIMEOUT": "15",
                "N8N_RUNNERS_MAX_CONCURRENCY": "1",
            }
            launcher = subprocess.Popen(
                [str(SETUP / "task-runner-launcher"), "python"],
                env=env,
                stdout=launcher_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            out["step"] = "task"
            output, _ = broker.communicate(timeout=50)
            assert len(output) <= 16384
            lines = output.splitlines()
            assert len(lines) == 1
            result = json.loads(lines[0])
            out["broker"] = result
            assert broker.returncode == 0 and result["passed"] and result["event"] == "result"
            assert result["grants_issued"] == result["grants_consumed"] == 2
            assert all(event in result["events"] for event in EXPECTED_EVENTS)
            assert [event for event in result["events"] if event in EXPECTED_EVENTS] == list(
                EXPECTED_EVENTS
            )
            assert launcher.poll() is None
        finally:
            out["cleanup"] = {
                "launcher_graceful": stop(launcher, seconds=8),
                "broker_graceful": stop(broker, seconds=5),
            }
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not (group_gone(launcher) and group_gone(broker)):
                time.sleep(0.1)
            out["cleanup"]["groups_gone"] = group_gone(launcher) and group_gone(broker)
            out["cleanup"]["listeners_closed"] = not any(
                listener(p) for p in (5680, 5682, BROKER_PORT)
            )
            for key, log in (("broker_log", broker_log), ("launcher_log", launcher_log)):
                log.seek(0)
                raw = log.read(1024**2)
                out[key] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                if not out.get("broker", {}).get("passed"):
                    known = (
                        "permission denied",
                        "not found",
                        "connection refused",
                        "invalid",
                        "failed to",
                        "unauthorized",
                        "task runner",
                        "broker",
                    )
                    out[key]["categories"] = [
                        label for label in known if label.encode() in raw.lower()
                    ]
            out["seconds"] = round(time.monotonic() - started, 3)
    assert all(out["cleanup"].values())


def main():
    if sys.argv[1:] == ["--broker-child"]:
        return broker_child()
    out = {"success": False, "production_approved": False}
    try:
        trial(out)
        out["success"] = True
    except Exception as error:
        out["error"] = type(error).__name__
        frame = error.__traceback__
        while frame.tb_next:
            frame = frame.tb_next
        out["line"] = frame.tb_lineno
    print(json.dumps(out, sort_keys=True))
    return 0 if out["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
