import pytest

from docker.supoclip import health_readonly as health


def fixture(monkeypatch, failure=None):
    rows = {
        role: {
            "id": role,
            "state": "exited" if role == "migrate" else "running",
            "health": "healthy" if role in health.DOCKER_HEALTH else None,
        }
        for role in health.ROLES
    }
    events = []

    def http(cid, url, status, key):
        events.append((cid, url, status, key))
        return failure != key

    def containers(_accepted):
        if failure == "changed" and events:
            return {**rows, "worker": {**rows["worker"], "state": "exited"}}
        return rows

    if failure == "backend_stopped":
        rows["backend"]["state"] = "exited"
    if failure == "frontend_unhealthy":
        rows["frontend"]["health"] = "unhealthy"
    monkeypatch.setattr(health.boot, "inputs", lambda: ({}, {}, "network"))
    monkeypatch.setattr(health.boot, "containers", containers)
    monkeypatch.setattr(health.boot, "network_ok", lambda *_: None)
    monkeypatch.setattr(health.boot, "firewall", lambda: failure != "firewall")
    monkeypatch.setattr(health.boot, "http", http)
    monkeypatch.setattr(health.boot, "local_proxy", lambda: failure != "proxy")
    return events


def test_readonly_checks_database_redis_and_worker_heartbeat_from_backend(monkeypatch):
    events = fixture(monkeypatch)
    result = health.probe()
    assert events == [
        ("backend", "http://127.0.0.1:8000/health/db", "healthy", "database"),
        ("backend", "http://127.0.0.1:8000/health/redis", "healthy", "redis"),
        ("backend", "http://127.0.0.1:8000/health/worker", "healthy", "worker"),
    ]
    assert all(result.values())
    assert set(result) == {
        "containers",
        "docker_health",
        "network",
        "firewall",
        "backend_database",
        "backend_redis",
        "worker_heartbeat",
        "proxy_http",
        "container_state_stable",
    }


@pytest.mark.parametrize(
    ("failure", "check"),
    [
        ("backend_stopped", "CONTENEDOR_ESTADO"),
        ("frontend_unhealthy", "HEALTHCHECK_DOCKER"),
        ("firewall", "FIREWALL_FALTANTE"),
        ("database", "SALUD_BACKEND_DB"),
        ("redis", "SALUD_BACKEND_REDIS"),
        ("worker", "SALUD_WORKER_HEARTBEAT"),
        ("proxy", "SALUD_PROXY"),
        ("changed", "CONTENEDOR_CAMBIO"),
    ],
)
def test_readonly_health_fails_closed(monkeypatch, failure, check):
    fixture(monkeypatch, failure)
    with pytest.raises(ValueError, match=f"^{check}$"):
        health.probe()
