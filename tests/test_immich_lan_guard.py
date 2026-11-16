import importlib.util
import json
import shlex
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "docker" / "immich-server" / "lan_guard.py"
SPEC = importlib.util.spec_from_file_location("immich_lan_guard", SOURCE)
guard = importlib.util.module_from_spec(SPEC)
if sys.platform == "win32":
    with patch.dict(sys.modules, {"fcntl": types.ModuleType("fcntl")}):
        SPEC.loader.exec_module(guard)
else:
    SPEC.loader.exec_module(guard)


def state(family):
    obs_target = "AGENCY-OBS-OUT-V1" if family == 4 else "DROP"
    other = ("-j", "RETURN")
    neighbors = [
        ("-i", bridge, "-m", "comment", "--comment", owner, "-j", "DROP")
        for owner, (bridge, _) in guard.NEIGHBORS.items()
        if owner != "agencia-digital-marketing-ai:moneyprinter-functional:v1"
    ]
    return guard.parse_snapshot(
        (
            "*filter\n:INPUT ACCEPT [0:0]\n:FORWARD DROP [0:0]\n:DOCKER-USER - [0:0]\n"
            f"-A FORWARD -i {guard.OBS} -j {obs_target}\n"
            f"-A FORWARD -o {guard.OBS} -j {obs_target}\n"
            "-A FORWARD -j DOCKER-USER\n"
            f"-A INPUT -i {guard.OBS} -j DROP\n"
            + "".join(
                f"-A {chain} {' '.join(row)}\n"
                for chain in guard.CHAINS
                for row in [*neighbors, other]
            )
            + "COMMIT\n"
        ).encode()
    )


def model_restore(before, payload):
    policies, chains = before
    result = {chain: list(entries) for chain, entries in chains.items()}
    for statement in payload.decode().splitlines()[1:-1]:
        action, chain, *tokens = shlex.split(statement)
        if action == "-D":
            result[chain].remove(tuple(tokens))
        else:
            index, *rule = tokens
            result[chain].insert(int(index) - 1, tuple(rule))
    return policies, result


@pytest.mark.parametrize("family", (4, 6))
def test_insert_after_neighbors_and_remove_exactly(family):
    initial = state(family)
    initial[0]["DOCKER-BRIDGE"] = "-"
    initial[1]["DOCKER-BRIDGE"] = [("-o", guard.BR_LAN, "-j", "DOCKER")]
    expected = guard.desired_snapshot(initial, family)
    payload = guard.delta(initial, expected)
    assert b"FORWARD" not in payload
    assert model_restore(initial, payload) == expected
    assert expected[0] == initial[0]
    for chain in guard.CHAINS:
        unowned = initial[1][chain]
        assert expected[1][chain] == [*unowned[:-1], *guard.rules(family)[chain], unowned[-1]]
    assert guard.desired_snapshot(expected, family) == expected
    assert guard.desired_snapshot(expected, family, remove=True) == initial
    assert model_restore(expected, guard.delta(expected, initial)) == initial


def test_reject_foreign_bridge_or_missing_observability_hook():
    initial = state(4)
    initial[1]["FORWARD"].append(("-o", guard.BR_LAN, "-j", "ACCEPT"))
    with pytest.raises(ValueError, match="FIREWALL_BRIDGE_AJENO"):
        guard.desired_snapshot(initial, 4)
    initial = state(6)
    initial[1]["FORWARD"].pop(0)
    with pytest.raises(ValueError, match="FIREWALL_FORWARD"):
        guard.desired_snapshot(initial, 6)


def test_reject_foreign_rows_between_managed_prefixes():
    initial = state(4)
    initial[1]["DOCKER-USER"].insert(1, ("-j", "RETURN"))
    with pytest.raises(ValueError, match="FIREWALL_VECINO_ORDEN"):
        guard.desired_snapshot(initial, 4)


def test_ipv6_commit_failure_restores_ipv4_exactly(monkeypatch):
    before = {family: state(family) for family in (4, 6)}
    simulated = before.copy()

    def restore(family, payload, test=False):
        after = model_restore(simulated[family], payload)
        if not test:
            if family == 6:
                raise ValueError("OPERACION_RECHAZADA")
            simulated[family] = after

    monkeypatch.setattr(guard, "snapshot", simulated.__getitem__)
    monkeypatch.setattr(guard, "restore", restore)
    with pytest.raises(ValueError, match="OPERACION_RECHAZADA"):
        guard.firewall("apply")
    assert simulated == before


@pytest.mark.parametrize("port, accepted", (("3160", True), ("3161", False)))
@pytest.mark.parametrize("temporary", (False, True))
def test_preserves_the_exact_production_inbox_before_observability(port, accepted, temporary):
    # Carga dos módulos independientes para aislar los cambios de la guarda temporal.
    spec = importlib.util.spec_from_file_location(
        "immich_face_accept_egress_test", SOURCE.with_name("face_accept_egress.py")
    )
    face = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(face)
    core = importlib.util.module_from_spec(SPEC)
    with patch.dict(sys.modules, {"fcntl": types.ModuleType("fcntl")}):
        SPEC.loader.exec_module(core)
    initial = state(4)
    if temporary:
        for chain, rules in guard.rules(4).items():
            initial[1][chain][-1:-1] = rules
    initial[1]["INPUT"].insert(
        0,
        (
            "-s",
            "172.31.240.2/32",
            "-d",
            "172.31.240.1/32",
            "-i",
            "br-ag-n8n-in",
            "-p",
            "tcp",
            "-m",
            "tcp",
            "--dport",
            port,
            "-m",
            "comment",
            "--comment",
            "agencia-digital-marketing-ai:n8n-production-inbox:v1",
            "-j",
            "ACCEPT",
        ),
    )
    if temporary:
        face.configure(core)
    if not accepted:
        with pytest.raises(ValueError, match="FIREWALL_OBSERVABILIDAD"):
            core.desired_snapshot(initial, 4)
        return
    expected = core.desired_snapshot(initial, 4)
    assert expected[1]["INPUT"][0] == initial[1]["INPUT"][0]
    assert model_restore(initial, core.delta(initial, expected)) == expected
    assert core.desired_snapshot(expected, 4, remove=True) == initial


def test_start_waits_for_microservices_after_api_health(monkeypatch):
    started = "2026-11-13T14:46:02.962518282Z"
    cid = "b" * 64
    log_reads = []
    replies = iter(
        [
            b"Nest application successfully started",
            b"Immich Microservices is running [v3.3.6] [production]",
        ]
    )

    def logs(*args, include_stderr):
        assert include_stderr
        log_reads.append(args)
        return next(replies)

    monkeypatch.setattr(guard, "docker", logs)
    monkeypatch.setattr(guard.time, "sleep", lambda _: None)
    guard.wait_for_microservices(cid, started)
    assert log_reads == [("logs", "--since", started, cid)] * 2


@pytest.mark.parametrize(
    ("status", "changes"),
    [(200, {}), (503, {}), (200, {"cuda_available": False}), (200, {"ready": 1})],
)
def test_gpu_readiness_requires_authenticated_cuda(monkeypatch, status, changes):
    secret = "A" * 48
    data = {"ready": True, "cuda_available": True, "service": "immich-ml", "status": "ok"}
    response = Mock(status=status)
    response.read.return_value = json.dumps({**data, **changes}).encode()
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(guard.http.client, "HTTPConnection", factory)
    monkeypatch.setattr(guard, "gpu_bearer", lambda: secret)

    if status == 200 and not changes:
        guard.check_gpu_ready()
    else:
        with pytest.raises(ValueError, match="GPU_ML_NO_DISPONIBLE"):
            guard.check_gpu_ready()

    factory.assert_called_once_with(
        guard.GPU, int(guard.GPU_PORT), timeout=4, source_address=(guard.HOST, 0)
    )
    connection.request.assert_called_once_with(
        "GET", "/health", headers={"Authorization": "Bearer " + secret}
    )
    connection.close.assert_called_once()


def test_cold_start_leaves_containers_and_firewall_alone_until_gpu_is_ready(monkeypatch):
    states = {name: {"Running": False} for name in guard.IMAGES}
    monkeypatch.setattr(guard, "network_host", Mock())
    monkeypatch.setattr(guard, "check_containers", Mock(return_value=states))
    readiness = Mock(side_effect=ValueError("GPU_ML_NO_DISPONIBLE"))
    monkeypatch.setattr(guard, "check_gpu_ready", readiness)
    for operation in ("firewall", "compose", "stop"):
        monkeypatch.setattr(guard, operation, Mock())

    with pytest.raises(ValueError, match="GPU_ML_NO_DISPONIBLE"):
        guard.start({})

    readiness.assert_called_once_with()
    guard.firewall.assert_not_called()
    guard.compose.assert_not_called()
    guard.stop.assert_not_called()


def test_existing_server_keeps_its_start_contract_without_a_second_gpu_probe(monkeypatch):
    states = {
        name: {"Running": True, "Health": {"Status": "healthy"}, "StartedAt": "original"}
        for name in guard.IMAGES
    }
    monkeypatch.setattr(guard, "check_containers", Mock(return_value=states))
    monkeypatch.setattr(guard, "firewall", Mock(return_value={"4": "filtro"}))
    for operation in ("network_host", "check_gpu_ready", "compose", "wait_for_microservices"):
        monkeypatch.setattr(guard, operation, Mock())

    assert guard.start({"containers": {"immich-server": "servidor"}}) == {"4": "filtro"}
    guard.check_gpu_ready.assert_not_called()
    guard.wait_for_microservices.assert_called_once_with("servidor", "original")
