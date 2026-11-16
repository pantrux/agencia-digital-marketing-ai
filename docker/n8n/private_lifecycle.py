"""Arranque y parada de los CID acreditados de n8n privado."""

import argparse
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORK = Path("/opt/media-platform/n8n-private-v1")
PROJECT = "agency-n8n-private"
OWNER = "agencia-digital-marketing-ai:n8n-private:v1"
IMAGES = {
    "n8n": "sha256:085d127780cfffdc1725b6e20b6dc37b0ecc325f38c63aead09f736f9f576bcc",
    "javascript": "sha256:8741b3b1c0be32cf84af05f0ad89405bffe7702f04a7cb39ccbaae1a5efd3492",
    "python": "sha256:96f303b7f4b9f47a4c24b1760479a1e117522f6bab91b4089fc2ed6437b47fd8",
    "postgres": "sha256:400e83e5e7aef5630f51e818b190327062420590c3cff4228c543921f64922de",
}
DOCKER = ("/usr/bin/docker", "--host", "unix:///var/run/docker.sock")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
DB_NETWORK = "agency-n8n-postgres"
PORTS = {"5678/tcp": [{"HostIp": "192.168.0.85", "HostPort": "5678"}]}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def run(*args, timeout=20):
    return subprocess.run(
        args,
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=timeout,
    ).stdout


def docker(*args, timeout=20):
    return run(*DOCKER, *args, timeout=timeout)


def deployment():
    path = WORK / "deployment.json"
    info = path.lstat()
    need(
        path.resolve(strict=True) == path
        and stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 0, 0o600)
        and 0 < info.st_size <= 8192
        and os.geteuid() == 0,
        "RECIBO_PRIVADO",
    )
    record = json.loads(path.read_bytes())
    need(
        set(record)
        == {
            "interface",
            "containers",
            "database_network",
            "database_client_ip",
            "database_postgres_ip",
        },
        "RECIBO_CAMPOS",
    )
    ids = record["containers"]
    need(
        set(ids) == set(IMAGES)
        and len(set(ids.values())) == len(ids)
        and all(
            re.fullmatch(r"[a-f0-9]{64}", value)
            for value in [*ids.values(), record["database_network"]]
        ),
        "RECIBO_IDENTIDADES",
    )
    need(re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", record["interface"]), "RECIBO_INTERFAZ")
    need(
        all(
            str(ipaddress.IPv4Address(record[key])) == record[key]
            for key in ("database_client_ip", "database_postgres_ip")
        )
        and record["database_client_ip"] != record["database_postgres_ip"],
        "RECIBO_IPS",
    )
    return record


def inspect(cid):
    [container] = json.loads(docker("inspect", "--type", "container", cid))
    host = container["HostConfig"]
    config = container["Config"]
    return {
        "id": container["Id"],
        "image": container["Image"],
        "labels": config["Labels"] or {},
        "state": container["State"],
        "restart": host["RestartPolicy"]["Name"],
        "read_only": host["ReadonlyRootfs"],
        "user": config["User"],
        "init": host.get("Init"),
        "networks": container["NetworkSettings"]["Networks"],
        "ports": host["PortBindings"],
        "memory": host["Memory"],
    }


def containers(record):
    rows = {role: inspect(cid) for role, cid in record["containers"].items()}
    for role, item in rows.items():
        need(
            item["id"] == record["containers"][role] and item["image"] == IMAGES[role],
            "CONTENEDOR_IDENTIDAD_" + role,
        )
        if role == "postgres":
            need(
                item["user"] == "70:70"
                and not item["ports"]
                and item["labels"].get("com.docker.compose.project") == "agency-media"
                and item["labels"].get("com.docker.compose.service") == "postgres-apps",
                "POSTGRES_IDENTIDAD",
            )
            continue
        expected_mem = {"n8n": 1280, "javascript": 640, "python": 512}[role] * 1024**2
        network_names = (
            {PROJECT + "_broker", PROJECT + "_ingress", DB_NETWORK}
            if role == "n8n"
            else {PROJECT + "_broker"}
        )
        need(
            item["labels"].get("agency.owner") == OWNER
            and item["labels"].get("com.docker.compose.project") == PROJECT
            and item["labels"].get("com.docker.compose.service") == role
            and item["user"] == "1000:1000"
            and item["restart"] == "no"
            and item["read_only"]
            and item["init"]
            and item["memory"] == expected_mem
            and set(item["networks"]) == network_names
            and (item["ports"] == PORTS if role == "n8n" else not item["ports"]),
            "CONTENEDOR_CONTRATO_" + role,
        )
    return rows


def database_network(record, rows):
    [network] = json.loads(docker("network", "inspect", DB_NETWORK))
    need(
        network["Id"] == record["database_network"]
        and network["Internal"]
        and network["Driver"] == "bridge"
        and not network["EnableIPv6"],
        "POSTGRES_RED",
    )
    active = network.get("Containers") or {}
    expected = {rows["postgres"]["id"], rows["n8n"]["id"]}
    required = expected if rows["n8n"]["state"]["Running"] else {rows["postgres"]["id"]}
    need(required <= set(active) <= expected, "POSTGRES_RED_MIEMBROS")
    endpoint = rows["postgres"]["networks"][DB_NETWORK]
    need(
        endpoint["NetworkID"] == network["Id"]
        and endpoint["IPAddress"] == record["database_postgres_ip"]
        and "postgres-n8n" in (endpoint.get("Aliases") or []),
        "POSTGRES_RED_ALIAS",
    )
    endpoint = rows["n8n"]["networks"][DB_NETWORK]
    need(
        (endpoint.get("IPAMConfig") or {}).get("IPv4Address") == record["database_client_ip"],
        "POSTGRES_RED_IP_CLIENTE",
    )


def health(cid, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = inspect(cid)["state"]
        need(state["Running"], "CONTENEDOR_DETENIDO")
        if state.get("Health", {}).get("Status") == "healthy":
            return
        time.sleep(2)
    raise ValueError("SALUD_TIMEOUT")


def ingress(action, record):
    run(
        sys.executable,
        "-I",
        "-S",
        "-B",
        str(WORK / "private_ingress.py"),
        action,
        "--interface",
        record["interface"],
    )


def stop(rows):
    limits = {"n8n": 90, "javascript": 65, "python": 65}

    def one(role):
        cid = rows[role]["id"]
        if inspect(cid)["state"]["Running"]:
            docker("stop", "--time", str(limits[role]), cid, timeout=limits[role] + 10)
        state = inspect(cid)["state"]
        need(
            not state["Running"]
            and state["Pid"] == 0
            and not state["OOMKilled"]
            and state["ExitCode"] == 0,
            "CIERRE_" + role,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(one, limits))


def start(record, rows):
    postgres = rows["postgres"]["id"]
    health(postgres, 45)
    database_network(record, rows)
    ingress("apply", record)
    ingress("check", record)
    try:
        server = rows["n8n"]["id"]
        docker("start", server)
        ingress("check", record)
        health(server, 120)
        database_network(record, containers(record))
        runners = [rows[role]["id"] for role in ("javascript", "python")]
        docker("start", *runners)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda cid: health(cid, 60), runners))
        ingress("check", record)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        try:
            stop(rows)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as cleanup:
            error.add_note(f"La limpieza del arranque también falló: {cleanup}")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "check"))
    action = parser.parse_args().action
    record = deployment()
    rows = containers(record)
    if action == "start":
        start(record, rows)
    elif action == "stop":
        stop(rows)
    else:
        database_network(record, rows)
        ingress("check", record)
    print(
        json.dumps(
            {"action": action, "containers": record["containers"], "success": True}, sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
