"""Pruebas del delta Alertmanager sobre las tablas DSM simuladas."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_observability_nas_filter_check import baseline, check, repair

SOURCE = Path(__file__).parents[1] / "docker" / "observability" / "nas_alertmanager_filter.py"
SPEC = importlib.util.spec_from_file_location("nas_alertmanager_filter", SOURCE)
am = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(am)
SMTP = "172.217.192.109"


def baseline_with_nat():
    tables = baseline()
    tables["4:nat"] += (
        ("CHAIN", "POSTROUTING", "ACCEPT"),
        ("CHAIN", am.POSTROUTING, "-"),
        ("-A", "POSTROUTING", "-j", am.POSTROUTING),
        ("-A", am.POSTROUTING, "-s", "172.17.0.0/16", "!", "-o", "docker0", "-j", "MASQUERADE"),
    )
    return tables


@pytest.mark.parametrize("value", ["192.168.0.8", "127.0.0.1", "224.0.0.1", "240.0.0.1", "::1"])
def test_rejects_nonpublic_smtp(value):
    with pytest.raises(ValueError):
        am.smtp_address(value)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("", ""),
        ("certificate", "AM_SMTP_CERTIFICADO"),
        ("no-starttls", "AM_SMTP_STARTTLS"),
        ("timeout", "AM_SMTP_TRANSPORTE"),
    ],
)
def test_smtp_connects_to_pinned_ip_and_verifies_gmail_without_auth(monkeypatch, failure, code):
    calls = []

    def connect(destination, timeout):
        assert destination == (SMTP, 587) and timeout == am.SMTP_TIMEOUT
        calls.append("tcp")
        if failure == "timeout":
            raise TimeoutError

    class Client:
        def __init__(self, hostname, port, local_hostname, timeout):
            assert hostname == "smtp.gmail.com" and local_hostname == "localhost"
            self._get_socket(hostname, port, timeout)

        def ehlo(self):
            calls.append("ehlo")
            return 250, b""

        def has_extn(self, extension):
            assert extension == "starttls"
            return failure != "no-starttls"

        def starttls(self, *, context):
            assert context.check_hostname and context.verify_mode == am.ssl.CERT_REQUIRED
            calls.append("starttls")
            if failure == "certificate":
                raise am.ssl.SSLCertVerificationError
            return 220, b""

        def close(self):
            calls.append("close")

    monkeypatch.setattr(am, "SMTP_CA", None)
    monkeypatch.setattr(am.smtplib, "SMTP", Client)
    monkeypatch.setattr(am.socket, "create_connection", connect)
    if code:
        with pytest.raises(ValueError, match=code):
            am.smtp_preflight(SMTP)
    else:
        am.smtp_preflight(SMTP)
        assert calls == ["tcp", "ehlo", "starttls", "ehlo", "close"]
    if failure in {"certificate", "no-starttls"}:
        assert calls[-1] == "close"


def test_exact_extension_preserves_baseline_and_other_docker_chains():
    original = baseline_with_nat()
    reference = repair.hashes(original)
    live = dict(original)
    live["4:filter"] += (("-A", "UNRELATED", "-o", am.BRIDGE, "-j", "RETURN"),)
    ext = am.expected_extension(check, repair, live, reference, SMTP)
    assert set(ext) == set(original)
    specs = am.changes(repair, SMTP)
    assert {
        key: sum(len(group[1]) for group in groups.values()) for key, groups in specs.items()
    } == {
        "4:filter": 8,
        "6:filter": 4,
        "4:nat": 1,
    }
    chain = repair.owned_chain("FORWARD")
    before = repair.chains(live["4:filter"])
    after = repair.chains(ext["4:filter"])
    assert before["FORWARD"] == after["FORWARD"]
    assert after[chain]["rules"] == [
        *repair.TRANSIT,
        *specs["4:filter"][chain][1],
        *repair.own_rules(4, "FORWARD")[2:],
    ]
    assert after["UNRELATED"] == before["UNRELATED"]
    assert {key: am.amend(ext[key], specs[key], False) for key in ext} == live


def test_rejects_postrouting_hook_bypass_and_wrong_pppoe_scope():
    original = baseline_with_nat()
    live = dict(original)
    live["4:nat"] += (("-A", "POSTROUTING", "-j", "RETURN"),)
    with pytest.raises(ValueError, match="AM_POSTROUTING_TOPOLOGIA"):
        am.expected_extension(check, repair, live, repair.hashes(original), SMTP)
    live = dict(original)
    live["4:filter"] = tuple(
        tuple("10.0.0.0/8" if word == "192.168.0.0/24" else word for word in row)
        if row[:2] == ("-A", "PPPOE_FORWARD")
        else row
        for row in live["4:filter"]
    )
    with pytest.raises(ValueError, match="AM_PPPOE_TOPOLOGIA"):
        am.expected_extension(check, repair, live, repair.hashes(original), SMTP)


def test_restore_never_targets_builtin_chains(monkeypatch):
    calls = []

    def run(argv, **args):
        assert "XTABLES_LOCKFILE" not in args["env"]
        calls.append((argv, args["input"].decode()))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(am.subprocess, "run", run)
    specs = am.changes(repair, SMTP)
    for key in am.TABLE_ORDER:
        am.restore(repair, key, specs[key], True, test=True)
        am.restore(repair, key, specs[key], False, test=False)
    assert [argv[1] for argv, _ in calls] == ["ip6tables-restore"] * 2 + ["iptables-restore"] * 4
    assert all("--test" in calls[index][0] for index in (0, 2, 4))
    assert all("--test" not in calls[index][0] for index in (1, 3, 5))
    assert all(" --noflush" not in payload for _, payload in calls)
    assert all("-F " not in payload and "-X " not in payload for _, payload in calls)
    nat_add = calls[2][1]
    assert f"-I DEFAULT_POSTROUTING 1 -s {am.ADDRESS}/32 -d {SMTP}/32 -o eth0 " in nat_add
    assert "-j MASQUERADE" in nat_add
    assert "-j ACCEPT" not in calls[0][1]
    request, response = specs["4:filter"][repair.owned_chain("FORWARD")][1][:2]
    assert "NEW,ESTABLISHED" in request and "ORIGINAL" in request
    assert "NEW,ESTABLISHED" not in response and "REPLY" in response


def test_test_only_and_commit_requires_unchanged_snapshot(monkeypatch):
    tables = baseline_with_nat()
    changes = am.changes(repair, SMTP)
    key = "4:filter"
    calls = []
    monkeypatch.setattr(repair, "snapshot", lambda: tables)
    monkeypatch.setattr(am, "restore", lambda *a, test: calls.append(test))
    am.transition(repair, key, changes[key], True, tables, commit=False)
    assert calls == [True]
    drift = {**tables, "4:nat": (*tables["4:nat"], ("-A", am.POSTROUTING, "-j", "RETURN"))}
    samples = iter([tables, drift])
    monkeypatch.setattr(repair, "snapshot", lambda: next(samples))
    with pytest.raises(ValueError, match="AM_CAMBIO_CONCURRENTE"):
        am.transition(repair, key, changes[key], True, tables)
    assert calls == [True, True]


@pytest.mark.parametrize("applied", [(), ("6:filter",), ("6:filter", "4:nat"), am.TABLE_ORDER])
def test_revert_exact_rows_from_any_recognized_partial_state(monkeypatch, applied):
    tables = baseline_with_nat()
    reference = repair.hashes(tables)
    after = am.expected_extension(check, repair, tables, reference, SMTP)
    current = {key: after[key] if key in applied else rows for key, rows in tables.items()}
    plan = {
        "before": reference,
        "after": repair.hashes(after),
        "reference": reference,
        "smtp_ipv4": SMTP,
    }
    calls = []

    def restore(_repair, key, groups, enable, *, test):
        calls.append((key, enable, test))
        if not test:
            current[key] = am.amend(current[key], groups, enable)

    monkeypatch.setattr(am, "restore", restore)
    monkeypatch.setattr(repair, "snapshot", lambda: dict(current))
    assert am.reverse(check, repair, plan) == reference
    assert current == tables
    assert [key for key, _, test in calls if not test] == list(reversed(applied))
    assert all(not enable for _, enable, _ in calls)
    am.reverse(check, repair, plan)
    assert len(calls) == 2 * len(applied)


def test_check_tolerates_unrelated_drift_and_rejects_moved_owned_row():
    before = baseline_with_nat()
    reference = repair.hashes(before)
    after = am.expected_extension(check, repair, before, reference, SMTP)
    plan = {"reference": reference, "smtp_ipv4": SMTP}
    after["4:filter"] += (("-A", "UNRELATED", "-j", "RETURN"),)
    assert am.verified_extension(check, repair, after, plan) == repair.hashes(after)
    rows = list(after["4:nat"])
    ours = rows.pop(rows.index(next(row for row in rows if row[:2] == ("-A", am.POSTROUTING))))
    after["4:nat"] = (*rows, ours)
    with pytest.raises(ValueError, match="AM_REGLA_PROPIA"):
        am.verified_extension(check, repair, after, plan)


def test_revert_preserves_unrelated_rows_and_refuses_partial_table(monkeypatch):
    before = baseline_with_nat()
    reference = repair.hashes(before)
    after = am.expected_extension(check, repair, before, reference, SMTP)
    plan = {
        "before": reference,
        "after": repair.hashes(after),
        "reference": reference,
        "smtp_ipv4": SMTP,
    }
    edge = ("-A", "UNRELATED", "-j", "RETURN")
    current = {**after, "4:filter": (*after["4:filter"], edge)}
    monkeypatch.setattr(repair, "snapshot", lambda: dict(current))

    def restore(_repair, key, groups, enable, *, test):
        if not test:
            current[key] = am.amend(current[key], groups, enable)

    monkeypatch.setattr(am, "restore", restore)
    am.reverse(check, repair, plan)
    assert current["4:filter"] == (*before["4:filter"], edge)
    suffix = am.changes(repair, SMTP)["4:filter"][repair.owned_chain("FORWARD")][1][-1]
    partial = ("-A", repair.owned_chain("FORWARD"), *suffix)
    current["4:filter"] = tuple(row for row in after["4:filter"] if row != partial)
    unchanged = dict(current)
    with pytest.raises(ValueError, match="AM_REVERSION_ESTADO"):
        am.reverse(check, repair, plan)
    assert current == unchanged


@pytest.mark.parametrize("running", [False, True])
def test_docker_24_endpoint_identity_when_stopped_or_running(monkeypatch, running):
    cid, net_id = "a" * 64, "b" * 64
    net = {
        "Id": net_id,
        "Name": am.NAME,
        "Driver": "bridge",
        "Scope": "local",
        "Internal": False,
        "EnableIPv6": False,
        "Labels": {"agency.owner": am.OWNER},
        "Options": am.OPTIONS,
        "IPAM": {"Config": [{"Subnet": am.SUBNET, "Gateway": am.GATEWAY}]},
        "Containers": {cid: {"IPv4Address": am.ADDRESS + "/29"}} if running else {},
    }
    image = {
        "Id": am.IMAGE,
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {"Labels": {"cl.e-scale.agencia.owner": am.OWNER}},
    }
    row = {
        "Id": cid,
        "Name": "/" + am.NAME,
        "Image": am.IMAGE,
        "Config": {
            "Labels": {"agency.owner": am.OWNER},
            "User": "61211:61211",
            "Cmd": am.COMMAND,
            "Entrypoint": ["/bin/alertmanager"],
        },
        "State": {
            "Running": running,
            "Pid": 204 if running else 0,
            "Status": "running" if running else "created",
            "StartedAt": "time",
        },
        "HostConfig": {
            "PortBindings": {},
            "PublishAllPorts": False,
            "Privileged": False,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges:true"],
            "RestartPolicy": {"Name": "no"},
            "Dns": ["127.0.0.1"],
            "ExtraHosts": [f"{am.SMTP_HOST}:{SMTP}"],
            "NetworkMode": net_id,
        },
        "NetworkSettings": {
            "Networks": {
                am.NAME: {
                    "IPAMConfig": {"IPv4Address": am.ADDRESS},
                    "NetworkID": net_id if running else "",
                    "GlobalIPv6Address": "",
                    "IPAddress": am.ADDRESS if running else "",
                }
            }
        },
    }
    identities = {"container": row, "image": image, "network": net}
    monkeypatch.setattr(am, "docker_inspect", lambda _repair, kind, _id: identities[kind])
    monkeypatch.setattr(am, "route", lambda *_args, **_options: None)
    monkeypatch.setattr(am.Path, "is_dir", lambda _: True)
    monkeypatch.setattr(
        repair, "command", lambda _: f"{SMTP} via 192.168.0.1 dev eth0 src {repair.NAS}".encode()
    )
    assert am.alertmanager_host(repair, cid, net_id, SMTP, stopped=False)["running"] == running
    if running:
        with pytest.raises(ValueError, match="AM_CONTENEDOR_ESTADO"):
            am.alertmanager_host(repair, cid, net_id, SMTP, stopped=True)
    else:
        assert not am.alertmanager_host(repair, cid, net_id, SMTP, stopped=True)["running"]
    row["HostConfig"]["ExtraHosts"] = [f"{am.SMTP_HOST}:8.8.8.8"]
    with pytest.raises(ValueError, match="AM_CONTENEDOR_IDENTIDAD"):
        am.alertmanager_host(repair, cid, net_id, SMTP, stopped=False)
