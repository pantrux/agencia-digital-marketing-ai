"""Verifica el modelo SupoClip persistente en el Docker local de la VM."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path

PAIR = ("worker", "model")
ORIGINAL = ("postgres", "valkey", "migrate", "backend", "frontend", "proxy")
ROLES = (*ORIGINAL, *PAIR)
TRIAL_OWNER = "agencia-digital-marketing-ai:supoclip-trial:v1"
MODEL_OWNER = "agencia-digital-marketing-ai:supoclip-local-model:v1"
MODEL_IMAGE = "sha256:cb57106522a97db2a0c9f851e939ace2594d9a8e709b333d8d6f1ac36cef03c0"
MODEL_CONFIG = "sha256:3f3601c42910ca6e8aa1f4f4cfa1d68f8118a9f43baec878b6cdfe464601734d"
CID = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
PROJECT = re.compile(r"[a-z][a-z0-9-]{5,36}")
BACKEND_PYTHON = ("/usr/bin/python3.12", "-I", "-S", "-B", "-c")
MODEL_PORT = 18963
WEIGHTS_BYTES = 1117320736
WEIGHTS_SHA = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"

# Projection excludes environment, logs, and bind-mounted secret contents.
INSPECT = (
    '{"id":{{json .Id}},"image":{{json .Image}},"labels":{{json .Config.Labels}},'
    '"user":{{json .Config.User}},"entrypoint":{{json .Config.Entrypoint}},'
    '"command":{{json .Config.Cmd}},"namespace":{{json .HostConfig.NetworkMode}},'
    '"ports":{{json .HostConfig.PortBindings}},"read_only":{{json .HostConfig.ReadonlyRootfs}},'
    '"cap_drop":{{json .HostConfig.CapDrop}},"security":{{json .HostConfig.SecurityOpt}},'
    '"restart":{{json .HostConfig.RestartPolicy.Name}},"mounts":{{json .Mounts}},'
    '"networks":{{json .NetworkSettings.Networks}},"state":{{json .State.Status}},'
    '"started":{{json .State.StartedAt}},"exit":{{json .State.ExitCode}},'
    '"restarts":{{json .RestartCount}},"health":{{with index .State "Health"}}'
    "{{json .Status}}{{else}}null{{end}}}"
)
PROBE_HEALTH = """
import json,sys,urllib.request
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with client.open(sys.argv[1], timeout=3) as response:
    body = json.load(response)
    assert response.status == 200 and body.get('status') == sys.argv[2]
    if len(sys.argv) == 4:
        assert body.get(sys.argv[3]) == 'connected'
"""
PROBE_REJECTION = """
import errno,socket,sys
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
    connection.settimeout(3)
    assert connection.connect_ex((sys.argv[1], int(sys.argv[2]))) == errno.ECONNREFUSED
"""


def require(value, message):
    if not value:
        raise ValueError(message)


def docker(*args):
    result = subprocess.run(
        ("docker", "--context", "default", *args),
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    require(result.returncode == 0, f"Docker {args[0]} falló con código {result.returncode}")
    return result.stdout


def verify_model(model, worker_id, weights, *, restart_policy="unless-stopped"):
    require(
        model["namespace"] == f"container:{worker_id}"
        and not model["ports"]
        and model["read_only"]
        and model["cap_drop"] == ["ALL"]
        and "no-new-privileges:true" in model["security"]
        and model["user"] == "1000:1000"
        and model["entrypoint"] == ["/usr/local/bin/supoclip-model"]
        and model["command"] == ["serve"]
        and model["restart"] == restart_policy,
        "Aislamiento o arranque del modelo distinto",
    )
    bind = [mount for mount in model["mounts"] if mount["Type"] != "tmpfs"]
    require(
        len(bind) == 1
        and bind[0]["Type"] == "bind"
        and bind[0]["Source"] == weights
        and bind[0]["Destination"] == "/run/supoclip-model/qwen.gguf"
        and bind[0]["RW"] is False,
        "Montaje de pesos distinto",
    )
    require(model["health"] == "healthy", "Modelo sin salud confirmada")


def verify_restart(before, after):
    for field in ("project", "weights"):
        require(before["success"] and before[field] == after[field], "Recibo previo distinto")
    require(set(before["containers"]) == set(ROLES), "Roles previos distintos")
    require(
        before["migrate_finished"] == after["migrate_finished"],
        "El migrador se volvió a ejecutar",
    )
    for role, current in after["containers"].items():
        prior = before["containers"][role]
        require(
            prior["id"] == current["id"] and prior["image"] == current["image"],
            f"CID de {role} distinto",
        )
        require(prior["restarts"] == current["restarts"], f"Reinicio automático de {role}")
        if role in PAIR:
            require(
                datetime.fromisoformat(prior["started"])
                < datetime.fromisoformat(current["started"]),
                f"{role} no reinició",
            )
        else:
            require(prior["started"] == current["started"], f"{role} reinició durante la prueba")


def verify_service_images(baseline, services, worker_image=None):
    for role in ORIGINAL:
        expected, actual = baseline[role], services[role]
        require(
            expected["id"] == actual["id"] and expected["image"] == actual["image"],
            f"Identidad anterior de {role} distinta",
        )
    if worker_image is not None:
        require(IMAGE.fullmatch(worker_image), "Digest esperado del worker inválido")
    expected_worker_image = worker_image or baseline["worker"]["image"]
    require(services["worker"]["image"] == expected_worker_image, "Imagen del worker distinta")


def collect(project):
    ids = docker(
        "ps", "-aq", "--no-trunc", "--filter", f"label=com.docker.compose.project={project}"
    )
    ids = ids.splitlines()
    require(
        len(ids) == len(ROLES) and all(CID.fullmatch(cid) for cid in ids),
        "Conjunto de CID distinto",
    )
    items = docker("inspect", "--type", "container", "--format", INSPECT, *ids)
    services = {}
    for line in items.splitlines():
        item = json.loads(line)
        labels = item["labels"]
        role = labels.get("com.docker.compose.service")
        require(role in ROLES and role not in services, "Rol de Compose inesperado")
        require(
            labels.get("agency.owner") == (MODEL_OWNER if role == "model" else TRIAL_OWNER)
            and labels.get("com.docker.compose.project") == project,
            f"Propietario de {role} distinto",
        )
        services[role] = item
    require(set(services) == set(ROLES), "Servicios incompletos")
    return services


def probe(args):
    require(
        PROJECT.fullmatch(args.project)
        and all(CID.fullmatch(cid) for cid in (args.worker, args.model)),
        "Proyecto o CID inválido",
    )
    require(
        not os.getenv("DOCKER_HOST") and not os.getenv("DOCKER_CONTEXT"), "Docker debe ser local"
    )
    endpoint = docker(
        "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"
    ).strip()
    require(endpoint == "unix:///var/run/docker.sock", "Socket Docker distinto")
    weights = args.weights
    weights_info = weights.stat()
    require(
        weights.resolve(strict=True) == weights
        and weights.is_file()
        and weights_info.st_size == WEIGHTS_BYTES
        and weights_info.st_uid == 0
        and not stat.S_IMODE(weights_info.st_mode) & 0o022,
        "Archivo físico de pesos distinto",
    )
    with weights.open("rb") as stream:
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == WEIGHTS_SHA,
            "SHA de pesos distinto",
        )
    baseline = json.loads(args.trial_receipt.read_bytes())
    services = collect(args.project)
    worker, model = (services[role] for role in PAIR)
    require(worker["id"] == args.worker and model["id"] == args.model, "CID del par distinto")
    verify_service_images(baseline, services, args.worker_image)
    for role, item in services.items():
        if role == "migrate":
            require(item["state"] == "exited" and item["exit"] == 0, "Migración previa distinta")
        else:
            require(item["state"] == "running", f"{role} está detenido")
    verify_model(
        model,
        worker["id"],
        str(weights),
        restart_policy="no" if args.boot_managed else "unless-stopped",
    )
    if args.boot_managed:
        require(worker["restart"] == "no", "Política de reinicio del worker distinta")
    require(not worker["ports"], "El worker tiene puertos publicados")
    netns = "/proc/self/ns/net"
    worker_ns = docker("exec", worker["id"], "/usr/bin/readlink", netns).strip()
    model_ns = docker("exec", model["id"], "/usr/bin/readlink", netns).strip()
    api_ns = docker("exec", services["backend"]["id"], "/usr/bin/readlink", netns).strip()
    require(
        re.fullmatch(r"net:\[\d+\]", worker_ns) and worker_ns == model_ns and worker_ns != api_ns,
        "Namespace compartido de forma inesperada",
    )
    model_image = docker(
        "image", "inspect", "--format", "{{.Id}} {{.Os}}/{{.Architecture}}", model["image"]
    )
    require(
        model_image.strip() in {f"{MODEL_IMAGE} linux/amd64", f"{MODEL_CONFIG} linux/amd64"}
        and model["image"] in {MODEL_IMAGE, MODEL_CONFIG},
        "Imagen o plataforma del modelo distinta",
    )
    os_release = docker("exec", model["id"], "/usr/bin/cat", "/etc/os-release")
    os_info = dict(line.split("=", 1) for line in os_release.splitlines() if "=" in line)
    require(
        os_info.get("ID") == "ubuntu" and os_info.get("VERSION_ID", "").strip('"') == "24.04",
        "SO del modelo distinto",
    )
    worker_os = docker("exec", worker["id"], "/usr/bin/cat", "/etc/os-release")
    require("ID=wolfi" in worker_os.splitlines(), "SO del worker distinto")
    data_network, speaches_network = f"{args.project}_data", f"{args.project}-speaches"
    require(
        set(worker["networks"]) == {data_network, speaches_network}, "Redes del worker distintas"
    )
    members = docker("network", "inspect", "--format", "{{json .Containers}}", speaches_network)
    require(set(json.loads(members) or {}) == {worker["id"]}, "Red Speaches compartida")
    require(data_network in services["backend"]["networks"], "API fuera de la red de datos")
    worker_ip = str(ipaddress.IPv4Address(worker["networks"][data_network]["IPAddress"]))
    model_url = f"http://127.0.0.1:{MODEL_PORT}/health"
    docker("exec", worker["id"], *BACKEND_PYTHON, PROBE_HEALTH, model_url, "ok")
    docker(
        "exec",
        services["backend"]["id"],
        *BACKEND_PYTHON,
        PROBE_REJECTION,
        worker_ip,
        str(MODEL_PORT),
    )
    docker(
        "exec",
        services["backend"]["id"],
        *BACKEND_PYTHON,
        PROBE_HEALTH,
        "http://127.0.0.1:8000/health/worker",
        "healthy",
        "worker",
    )
    after = collect(args.project)
    stable = ("id", "image", "state", "started", "restarts")
    containers = {role: {field: item[field] for field in stable} for role, item in services.items()}
    require(
        containers
        == {role: {field: item[field] for field in stable} for role, item in after.items()},
        "Cambió un contenedor durante la sonda",
    )
    finished = docker(
        "inspect", "--format", "{{.State.FinishedAt}}", services["migrate"]["id"]
    ).strip()
    result = {
        "success": True,
        "project": args.project,
        "weights": str(weights),
        "weights_sha256": WEIGHTS_SHA,
        "model_platform": "linux/amd64",
        "model_os": "ubuntu 24.04",
        "worker_os": "wolfi",
        "migrate_finished": finished,
        "worker_model_namespace": worker_ns,
        "worker_model_health": 200,
        "api_model_connection": "refused",
        "worker_queue_health": 200,
        "containers": containers,
    }
    if args.previous:
        verify_restart(json.loads(args.previous.read_bytes()), result)
        result["same_pair_restart"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("project", "worker", "model"):
        parser.add_argument(f"--{flag}", required=True)
    for flag in ("weights", "trial-receipt"):
        parser.add_argument(f"--{flag}", required=True, type=Path)
    parser.add_argument("--worker-image", help="Digest del worker que reemplazó al del recibo")
    parser.add_argument(
        "--boot-managed", action="store_true", help="Systemd controla los reinicios"
    )
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args), sort_keys=True))
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        detail = str(error) if type(error) is ValueError else type(error).__name__
        print(json.dumps({"success": False, "error": detail}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
