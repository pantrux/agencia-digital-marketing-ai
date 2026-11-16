"""Instala o retira el filtro temporal de salida del proxy facial de ensayo."""

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path

CORE_SHA = "32e1553b8009c01ed96e72b79e98a8476bccd6771f58ea8129c3a22aa8493001"
PROJECT = "agency-media-immich-face-accept"
OWNER = "agencia-digital-marketing-ai:immich-face-accept:v1"
BRIDGE, NIC = "br-im-face", "media0"
NETWORK = "agency-immich-face-accept-ml-egress-v1"
SUBNET, GATEWAY = "172.31.241.96/28", "172.31.241.97"
GPU, PORT = "192.168.0.6", "3003"
LAN_OWNER = "agencia-digital-marketing-ai:immich-lan:v1"
LAN_BRIDGES = {"br-immich-lan", "br-immich-eg"}
PROXY = "sha256:077a815fb7f45eedee7fdb799d52519de21266884eb0bcdd6b38d0b78ca0f723"
N8N_INBOX = (
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
    "3160",
    "-m",
    "comment",
    "--comment",
    "agencia-digital-marketing-ai:n8n-production-inbox:v1",
    "-j",
    "ACCEPT",
)


def need(condition, code):
    if not condition:
        raise ValueError(code)


def core_load():
    core_path = Path(__file__).resolve().with_name("lan_guard.py")
    source = core_path.read_bytes()
    need(hashlib.sha256(source).hexdigest() == CORE_SHA, "NUCLEO_SHA")
    spec = importlib.util.spec_from_file_location("face_accept_lan_guard", core_path)
    core = importlib.util.module_from_spec(spec)
    exec(compile(source, str(core_path), "exec"), core.__dict__)
    return core


def rules(family):
    mark = ("-m", "comment", "--comment", OWNER)
    user = [(flag, BRIDGE, *mark, "-j", "DROP") for flag in ("-i", "-o")]
    if family == 4:
        tcp = ("-p", "tcp", "-m", "tcp")
        original = ("-m", "conntrack", "--ctstate", "NEW,ESTABLISHED", "--ctdir", "ORIGINAL")
        reply = ("-m", "conntrack", "--ctstate", "ESTABLISHED", "--ctdir", "REPLY")
        user[:0] = [
            (
                "-i",
                BRIDGE,
                "-o",
                NIC,
                "-d",
                GPU,
                *tcp,
                "--dport",
                PORT,
                *original,
                *mark,
                "-j",
                "RETURN",
            ),
            (
                "-i",
                NIC,
                "-o",
                BRIDGE,
                "-s",
                GPU,
                *tcp,
                "--sport",
                PORT,
                *reply,
                *mark,
                "-j",
                "RETURN",
            ),
        ]
    return {"INPUT": [("-i", BRIDGE, *mark, "-j", "DROP")], "DOCKER-USER": user}


def configure(core):
    original_prefix = core.prefix

    def prefix(rows, family):
        entrada = rows["INPUT"]
        if family == 4 and entrada[:1] == [N8N_INBOX]:
            # Valida el resto con el núcleo fijado y conserva la regla en la tabla real.
            need(entrada.count(N8N_INBOX) == 1, "FIREWALL_N8N_DUPLICADO")
            offsets = original_prefix({**rows, "INPUT": entrada[1:]}, family)
            offsets["INPUT"] += 1
        else:
            offsets = original_prefix(rows, family)
        for chain, start in offsets.items():
            entries = rows[chain]
            offset = start
            for row in entries[start:]:
                if LAN_OWNER not in row:
                    break
                fields = core.shape(row)
                need(
                    ("--comment", LAN_OWNER, False) in fields
                    and (set(row) & LAN_BRIDGES or ("--dport", "2283", False) in fields),
                    "FIREWALL_VECINO_IMMICH",
                )
                offset += 1
            need(not any(LAN_OWNER in row for row in entries[offset:]), "FIREWALL_IMMICH_ORDEN")
            offsets[chain] = offset
        return offsets

    core.OWNER = OWNER
    core.BR_LAN = core.BR_GPU = BRIDGE
    core.rules = rules
    core.prefix = prefix


def preflight(core, network_id, action):
    need(
        sys.platform == "linux" and os.geteuid() == 0 and socket.gethostname() == "vm-media-01",
        "HOST_IDENTIDAD",
    )
    need(re.fullmatch(r"[a-f0-9]{64}", network_id), "RED_ID")
    interfaces = core.json_load(core.run("/usr/sbin/ip", "-j", "-4", "addr", "show", "dev", NIC))
    need(
        any(
            row["ifname"] == NIC
            and any(address.get("local") == "192.168.0.85" for address in row["addr_info"])
            for row in interfaces
        ),
        "HOST_LAN",
    )
    (net,) = core.json_load(core.docker("network", "inspect", NETWORK))
    need(
        net["Id"] == network_id
        and net["Name"] == NETWORK
        and net["Driver"] == "bridge"
        and not net["Internal"]
        and not net["EnableIPv6"]
        and net["Options"].get("com.docker.network.bridge.name") == BRIDGE
        and [(row["Subnet"], row["Gateway"]) for row in net["IPAM"]["Config"]]
        == [(SUBNET, GATEWAY)],
        "RED_CONTRATO",
    )
    output = core.docker(
        "ps",
        "-aq",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
        "--filter",
        "label=com.docker.compose.service=immich-ml-auth",
    )
    candidates = output.decode().split()
    need(
        len(candidates) <= 1 and all(re.fullmatch(r"[a-f0-9]{64}", cid) for cid in candidates),
        "CID_CANDIDATO",
    )
    active = False
    if candidates:
        (proxy,) = core.json_load(core.docker("container", "inspect", candidates[0]))
        labels = proxy["Config"]["Labels"]
        host = proxy["HostConfig"]
        active = proxy["State"]["Running"]
        attached = proxy["NetworkSettings"]["Networks"]
        need(
            proxy["Id"] == candidates[0]
            and proxy["Image"] == PROXY
            and labels.get("agency.owner") == OWNER
            and labels.get("com.docker.compose.project") == PROJECT
            and labels.get("com.docker.compose.service") == "immich-ml-auth"
            and host["ReadonlyRootfs"]
            and not host["Privileged"]
            and not host["PortBindings"]
            and host["RestartPolicy"]["Name"] == "no"
            and set(attached) == {NETWORK, "agency-immich-face-accept-ml-v1"}
            and attached[NETWORK]["NetworkID"] in ({network_id} if active else {network_id, ""}),
            "CID_CONTRATO",
        )
    endpoints = net["Containers"] or {}
    need(
        set(endpoints) == (set(candidates) if active else set())
        and all(
            ipaddress.ip_interface(row["IPv4Address"]).network == ipaddress.ip_network(SUBNET)
            for row in endpoints.values()
        ),
        "RED_MIEMBROS",
    )
    need(action == "check" or not active, "PROXY_ACTIVO")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "check", "remove"))
    parser.add_argument("--network-id", required=True)
    args = parser.parse_args()
    try:
        core = core_load()
        with os.fdopen(
            os.open("/run/agency-immich-lan.lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600),
            "rb+",
        ) as lock:
            node = os.fstat(lock.fileno())
            need(
                stat.S_ISREG(node.st_mode)
                and node.st_uid == node.st_gid == 0
                and node.st_nlink == 1
                and stat.S_IMODE(node.st_mode) == 0o600,
                "LOCK_MODO",
            )
            core.fcntl.flock(lock, core.fcntl.LOCK_EX | core.fcntl.LOCK_NB)
            preflight(core, args.network_id, args.action)
            configure(core)
            result = core.firewall(args.action)
    except (OSError, KeyError, ValueError, TypeError, subprocess.SubprocessError) as error:
        code = str(error) if type(error) is ValueError else "OPERACION_FALLIDA"
        if not re.fullmatch(r"[A-Z0-9_]+", code):
            code = "OPERACION_FALLIDA"
        print(json.dumps({"success": False, "action": args.action, "error": code}))
        return 1
    print(
        json.dumps(
            {"success": True, "action": args.action, "filter_sha256": result}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
