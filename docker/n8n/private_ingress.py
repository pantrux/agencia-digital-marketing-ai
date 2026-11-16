"""Controla sólo las reglas IPv4 de entrada LAN de n8n en vm-media-01."""

import argparse
import ipaddress
import json
import re
import shlex
import subprocess

HOST = "192.168.0.85"
CLIENTS = ("192.168.0.6/32", "192.168.0.7/32")
BRIDGE = "br-ag-n8n-in"
OWNER = "agencia-digital-marketing-ai:n8n-private:v1"
OBS = "br-aobsoutv1"
OBS_GUARD = "AGENCY-OBS-OUT-V1"
DOCKER_USER = ("-j", "DOCKER-USER")
OBS_FORWARD = (("-i", OBS, "-j", OBS_GUARD), ("-o", OBS, "-j", OBS_GUARD))
OBS_INPUT = ("-i", OBS, "-j", "DROP")
NEIGHBORS = {
    "agencia-digital-marketing-ai:moneyprinter-lan:v1": ("br-mpt-lan", "8501"),
    "agencia-digital-marketing-ai:moneyprinter-functional:v1": ("br-mpt-fn", "8501"),
    "agency-supoclip-trial-nov13": ("sco24754668cc", "3080"),
}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def run(*args, data=None):
    return subprocess.run(args, input=data, capture_output=True, check=True, timeout=20).stdout


def shape(row):
    fields, invert, tokens = [], False, iter(row)
    for option in tokens:
        if option == "!":
            invert = True
            continue
        value = next(tokens)
        if option in ("-s", "-d", "--ctorigdst"):
            value = str(ipaddress.IPv4Network(value))
        if option == "--ctstate":
            value = ",".join(sorted(value.split(",")))
        fields.append((option, value, invert))
        invert = False
    return sorted(fields)


def rules(interface):
    mark = ("-m", "comment", "--comment", OWNER)
    tcp = ("-p", "tcp", "-m", "tcp", "--dport", "5678")
    local = ("-d", HOST + "/32", *tcp)
    incoming = ("-o", BRIDGE, *tcp)
    forwarded = (
        "-m",
        "conntrack",
        "--ctstate",
        "NEW,ESTABLISHED",
        "--ctdir",
        "ORIGINAL",
        "--ctorigdst",
        HOST,
        "--ctorigdstport",
        "5678",
    )
    return {
        "INPUT": [
            ("-i", BRIDGE, *mark, "-j", "DROP"),
            *(("-i", interface, "-s", source, *local, *mark, "-j", "ACCEPT") for source in CLIENTS),
            ("!", "-i", "lo", *local, *mark, "-j", "DROP"),
        ],
        "DOCKER-USER": [
            *(
                ("-i", interface, "-s", source, *incoming, *forwarded, *mark, "-j", "RETURN")
                for source in CLIENTS
            ),
            (*incoming, *mark, "-j", "DROP"),
        ],
    }


def snapshot():
    rows = {}
    for line in run("/usr/sbin/iptables-save", "-t", "filter").decode().splitlines():
        if line.startswith(":"):
            rows[line[1:].split()[0]] = []
        elif line.startswith("-A "):
            _, chain, *row = shlex.split(line)
            rows[chain].append(tuple(row))
    need({"INPUT", "FORWARD", "DOCKER-USER"} <= rows.keys(), "FIREWALL_CADENAS")
    return rows


def preceding(rows, start):
    offset = start
    for row in rows[start:]:
        owner = next((item for item in NEIGHBORS if item in row), None)
        if owner is None:
            break
        bridge, port = NEIGHBORS[owner]
        fields = shape(row)
        need(
            any((flag, bridge, False) in fields for flag in ("-i", "-o"))
            or ("--dport", port, False) in fields
            or ("--ctorigdstport", port, False) in fields,
            "FIREWALL_VECINO_REGLA",
        )
        offset += 1
    need(not any(set(row) & NEIGHBORS.keys() for row in rows[offset:]), "FIREWALL_VECINO_ORDEN")
    return offset


def plan(rows, interface):
    desired = rules(interface)
    observed = {
        chain: [row for row in entries if OWNER not in row] for chain, entries in rows.items()
    }
    has_obs = (
        any(row in observed["FORWARD"] for row in OBS_FORWARD) or OBS_INPUT in observed["INPUT"]
    )
    expected_forward = [*OBS_FORWARD, DOCKER_USER] if has_obs else [DOCKER_USER]
    need(observed["FORWARD"][: len(expected_forward)] == expected_forward, "FIREWALL_FORWARD")
    need(not has_obs or observed["INPUT"][:1] == [OBS_INPUT], "FIREWALL_OBSERVABILIDAD")
    payload = ["*filter"]
    for chain, entries in rows.items():
        owned = [row for row in entries if OWNER in row]
        allowed = [shape(row) for row in desired.get(chain, ())]
        actual = [shape(row) for row in owned]
        need(
            all(row in allowed for row in actual) and all(actual.count(row) == 1 for row in actual),
            "FIREWALL_REGLAS_PROPIAS",
        )
        payload.extend(" ".join(("-D", chain, *row)) for row in owned)
    for chain, entries in desired.items():
        offset = preceding(observed[chain], int(chain == "INPUT" and has_obs))
        observed[chain][offset:offset] = entries
        payload.extend(" ".join(("-I", chain, str(offset + 1), *row)) for row in reversed(entries))
    return observed, ("\n".join([*payload, "COMMIT", ""])).encode()


def same_rules(left, right):
    for chain in ("INPUT", "DOCKER-USER"):
        a, b = left[chain], right[chain]
        if len(a) != len(b):
            return False
        for x, y in zip(a, b, strict=True):
            if x == y:
                continue
            if OWNER not in x or OWNER not in y or shape(x) != shape(y):
                return False
    return all(left[chain] == right[chain] for chain in left.keys() - {"INPUT", "DOCKER-USER"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply"))
    parser.add_argument("--interface", required=True)
    args = parser.parse_args()
    need(re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", args.interface), "INTERFAZ_LAN")
    addresses = json.loads(run("/usr/sbin/ip", "-j", "-4", "addr", "show", "dev", args.interface))
    need(
        run("/bin/hostname").strip() == b"vm-media-01"
        and any(
            address.get("local") == HOST
            for row in addresses
            for address in row.get("addr_info", [])
        ),
        "HOST_LAN",
    )
    initial = snapshot()
    expected, delta = plan(initial, args.interface)
    if args.action == "apply" and not same_rules(initial, expected):
        command = ("/usr/sbin/iptables-restore", "--wait", "5", "--noflush")
        run(*command, "--test", data=delta)
        need(snapshot() == initial, "FIREWALL_CAMBIO_CONCURRENTE")
        run(*command, data=delta)
    need(same_rules(snapshot(), expected), "FIREWALL_ORDEN_O_AUSENCIA")
    print(
        json.dumps(
            {"ipv4": HOST, "port": 5678, "clients": CLIENTS, "success": True}, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
