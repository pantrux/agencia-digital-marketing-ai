import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "docker/supoclip/supoclip_boot.py"
SPEC = importlib.util.spec_from_file_location("supoclip_boot", SOURCE)
boot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boot)


def test_firewall_moves_only_own_rules_and_preserves_observability_prefix():
    other_input = ("-s", "192.168.0.10/32", "-j", "ACCEPT")
    other_forward = ("-j", "DOCKER-FORWARD")
    other_user = ("-j", "RETURN")
    current = {
        "INPUT": [boot.INPUT_RULES[0], *boot.OBS_INPUT, other_input, boot.INPUT_RULES[1]],
        "FORWARD": [boot.USER_RULES[1], *boot.OBS_FWD, boot.DOCKER_USER, other_forward],
        "DOCKER-USER": [other_user],
        "AGENCY-OBS-OUT-V1": [("-j", "DROP")],
    }
    expected, payload = boot.firewall_change(current)
    assert expected == {
        "INPUT": [*boot.OBS_INPUT, *boot.INPUT_RULES, other_input],
        "FORWARD": [*boot.OBS_FWD, boot.DOCKER_USER, other_forward],
        "DOCKER-USER": [*boot.USER_RULES, other_user],
        "AGENCY-OBS-OUT-V1": [("-j", "DROP")],
    }
    assert b"-D FORWARD -i sco24754668cc" in payload
    assert b"-I INPUT 2" in payload
    assert b"-I DOCKER-USER 1" in payload
    assert b"br-aobsoutv1" not in payload
    removed, _ = boot.firewall_change(expected, remove=True)
    assert removed["INPUT"] == [*boot.OBS_INPUT, other_input]
    assert removed["DOCKER-USER"] == [other_user]


def test_firewall_rejects_a_rule_that_could_bypass_docker_user():
    rows = {"INPUT": [], "FORWARD": [("-j", "ACCEPT"), boot.DOCKER_USER], "DOCKER-USER": []}
    with pytest.raises(ValueError, match="FIREWALL_ORDEN_FORWARD"):
        boot.firewall_change(rows)


def test_firewall_rejects_an_unknown_project_rule():
    rows = {
        "INPUT": [(*boot.COMMENT, "-j", "ACCEPT")],
        "FORWARD": [boot.DOCKER_USER],
        "DOCKER-USER": [],
    }
    with pytest.raises(ValueError, match="FIREWALL_COLISION"):
        boot.firewall_change(rows)


def test_firewall_preserves_moneyprinter_prefixes_disjoint_from_supoclip():
    owner = "agencia-digital-marketing-ai:moneyprinter-lan:v1"
    mpt_drop = ("-i", "br-mpt-lan", "-m", "comment", "--comment", owner, "-j", "DROP")
    mpt_port = ("--dport", "8501", "-m", "comment", "--comment", owner, "-j", "DROP")
    rows = {
        "INPUT": [*boot.OBS_INPUT, mpt_drop, mpt_port],
        "FORWARD": [*boot.OBS_FWD, boot.DOCKER_USER],
        "DOCKER-USER": [mpt_drop, ("-j", "RETURN")],
    }
    new, _ = boot.firewall_change(rows)
    assert new["INPUT"] == [*boot.OBS_INPUT, mpt_drop, mpt_port, *boot.INPUT_RULES]
    assert new["DOCKER-USER"] == [mpt_drop, *boot.USER_RULES, ("-j", "RETURN")]
    rows["INPUT"][2] = ("--dport", "3080", "-m", "comment", "--comment", owner, "-j", "DROP")
    with pytest.raises(ValueError, match="FIREWALL_MONEYPRINTER_REGLA"):
        boot.firewall_change(rows)


def test_start_protects_before_any_container_and_publishes_proxy_last(monkeypatch):
    events = []
    rows = {role: {"id": role, "state": "exited"} for role in boot.ROLES}

    def docker(*args, **kwargs):
        if args[0] == "start":
            events.append(args)
        namespace = b"net:[2]" if args[1] == "backend" else b"net:[1]"
        return SimpleNamespace(stdout=namespace)

    def firewall(**kwargs):
        events.append(("firewall", kwargs.get("apply", False)))
        return True

    def postgres(_rows, _plan, query):
        return b"27" if "prisma" in query else b"26"

    monkeypatch.setattr(boot, "docker", docker)
    monkeypatch.setattr(boot, "firewall", firewall)
    monkeypatch.setattr(boot, "postgres", postgres)
    monkeypatch.setattr(boot, "wait_healthy", lambda *_: None)
    monkeypatch.setattr(boot, "wait_for", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(boot, "network_ok", lambda *_: None)
    monkeypatch.setattr(boot, "containers", lambda *_: rows)
    monkeypatch.setattr(boot, "http", lambda *_: True)
    monkeypatch.setattr(
        boot,
        "state",
        lambda _cid: {
            "networks": {boot.PROJECT + "_data": {"IPAddress": "172.29.0.5"}},
        },
    )
    boot.start(rows, None, None, None)
    assert events == [
        ("firewall", True),
        ("start", "postgres"),
        ("start", "valkey"),
        ("start", "backend"),
        ("start", "worker"),
        ("start", "model"),
        ("start", "frontend"),
        ("firewall", False),
        ("start", "proxy"),
        ("firewall", False),
    ]


@pytest.mark.parametrize("pending", [False, True])
def test_stop_closes_ingress_before_draining_and_keeps_database_until_last(monkeypatch, pending):
    events = []
    rows = {role: {"id": role, "state": "running"} for role in boot.ROLES}

    def wait_for(check, *_args, **_kwargs):
        events.append("drain")
        if not check():
            raise ValueError("COLA_DRENAJE")

    monkeypatch.setattr(boot, "docker", lambda *args: events.append(args[-1]))
    monkeypatch.setattr(boot, "state", lambda *_: {"state": "exited"})
    monkeypatch.setattr(boot, "wait_for", wait_for)
    monkeypatch.setattr(boot, "postgres", lambda *_: b"1" if pending else b"0")
    result = {}
    boot.stop(rows, None, result)
    pair = ["worker", "model"] if pending else ["model", "worker"]
    assert events == ["proxy", "frontend", "backend", "drain", *pair, "valkey", "postgres"]
    assert result["drained"] == (not pending)
