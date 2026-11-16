"""Contrato del desafío firmado antes de que el broker empiece a escuchar."""

import importlib.util
import stat
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import UUID

import pytest

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "openclaw-agency-setup/sofia-broker/sofia_startup_probe.py"
)
ORDER = "01234567-89ab-4cde-8123-0123456789ab"
OLD_REQUEST = "fedcba98-7654-4321-8765-fedcba987654"
SOCKET = "/run/user/1000/agency-sofia-owner/sofia-owner.sock"


class FixturePath(PurePosixPath):
    def lstat(self):
        return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=1000)


@pytest.fixture
def probe(monkeypatch):
    spec = importlib.util.spec_from_file_location("sofia_startup_probe_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "Path", FixturePath)
    monkeypatch.setattr(module.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(module.sys, "argv", ["probe.py", "profile.json", "startup-ids.json"])
    documents = {
        "profile.json": {"socketPath": SOCKET, "hmacSecret": "fixture", "ownerActor": "a" * 64},
        "startup-ids.json": {"order_id": ORDER, "request_id": OLD_REQUEST},
    }
    monkeypatch.setattr(module, "read_private", lambda path: documents[path.name])
    return module


def test_each_startup_uses_a_distinct_request_id(probe, monkeypatch):
    received = []

    def ready(_sock, _key, payload):
        received.append(payload.copy())
        return True

    monkeypatch.setattr(probe, "check", ready)
    assert probe.main() == probe.main() == 0
    assert [value["order_id"] for value in received] == [ORDER, ORDER]
    first, second = [value["request_id"] for value in received]
    assert first != second
    assert OLD_REQUEST not in (first, second)
    assert UUID(first).version == UUID(second).version == 4


def test_failed_response_leaves_next_attempt_to_systemd(probe, monkeypatch):
    def unexpected_sleep(_delay):
        pytest.fail("Una respuesta rechazada debe terminar el intento")

    monkeypatch.setattr(probe, "check", lambda *_args: False)
    monkeypatch.setattr(probe.time, "sleep", unexpected_sleep)
    assert probe.main() == 1


def test_waits_for_socket_that_has_not_started_listening(probe, monkeypatch):
    attempts = []
    sleeps = []

    def starting(_sock, _key, payload):
        attempts.append(payload["request_id"])
        if len(attempts) == 1:
            raise ConnectionRefusedError
        return True

    monkeypatch.setattr(probe, "check", starting)
    monkeypatch.setattr(probe.time, "sleep", sleeps.append)
    assert probe.main() == 0
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert len(sleeps) == 1
