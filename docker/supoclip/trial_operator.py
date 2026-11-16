"""Stages, creates and starts the isolated seven-container SupoClip trial."""

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

HOST = "192.168.0.85"
PORT = 3080
GPU = "192.168.0.6"
GPU_PORT = "8000"
MIGRATIONS_SHA = "5be6e0901b3b9534950581be148ede142eebabfd193ce2e7c3000a3c9fe18336"
SERVICES = ("postgres", "valkey", "migrate", "backend", "worker", "frontend", "proxy")
IMAGE_VARS = {
    "backend": "BACKEND",
    "frontend": "FRONTEND",
    "postgres": "POSTGRES",
    "valkey": "VALKEY",
    "proxy": "NGINX",
}
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{2,30}")
PROJECT = re.compile(r"[a-z][a-z0-9-]{5,36}")
LABEL = "agency.owner"
OWNER = "agencia-digital-marketing-ai:supoclip-trial:v1"
SECRET_KEYS = {
    "postgres_password",
    "database_password",
    "redis_password",
    "backend_auth",
    "better_auth",
    "speaches",
}


def run(*cmd, env=None, data=None, check=True):
    if cmd[0] == "docker":
        cmd = ("docker", "--context", "default", *cmd[1:])
    return subprocess.run(cmd, env=env, input=data, capture_output=True, check=check, timeout=120)


def get_json(*cmd):
    return json.loads(run(*cmd).stdout)


def read_manifest(path):
    raw = path.read_bytes()
    need(hashlib.sha256(raw).hexdigest() == MIGRATIONS_SHA, "Manifiesto de migraciones distinto")
    return json.loads(raw)


def need(condition, message):
    if not condition:
        raise ValueError(message)


def write_json(path, obj):
    with path.open("x", encoding="utf-8") as out:
        json.dump(obj, out, indent=2, sort_keys=True)
        out.write("\n")


def directory(path, uid):
    path.mkdir(mode=0o700)
    os.chown(path, uid, uid)


def secret(path, value, uid, *, newline=True):
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o400)
        stream.write(value.encode() + (b"\n" if newline else b""))
        stream.flush()
        os.fchown(stream.fileno(), uid, uid)
        os.fsync(stream.fileno())


def stage_secrets(root, pg_admin, app_db, app_role, values):
    need(isinstance(values, dict) and set(values) == SECRET_KEYS, "Campos de secretos inesperados")
    need(
        all(
            isinstance(v, str) and 16 <= len(v) <= 512 and re.fullmatch(r"[!-~]+", v)
            for v in values.values()
        ),
        "Formato de secretos inválido",
    )
    need(len(set(values.values())) == len(values), "Los secretos deben ser distintos")
    bearer = values["speaches"]
    need(
        32 <= len(bearer) <= 256 and re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", bearer),
        "Formato del bearer Speaches inválido",
    )
    for path, uid in (
        ("postgres", 70),
        ("valkey", 1000),
        ("state", 1000),
        ("state/temp", 1000),
        ("state/outputs", 1000),
        ("logs", 0),
        ("logs/api", 1000),
        ("logs/worker", 1000),
        ("secrets", 0),
    ):
        directory(root / path, uid)
    pg_password = values["postgres_password"]
    pgpass = pg_password.replace("\\", "\\\\").replace(":", "\\:")
    db_url = f"{app_role}:{quote(values['database_password'], safe='')}@postgres:5432/{app_db}"
    redis_hash = hashlib.sha256(values["redis_password"].encode()).hexdigest()
    rows = {
        "postgres-password": (pg_password, 70),
        "postgres-pgpass": (
            f"127.0.0.1:5432:postgres:{pg_admin}:{pgpass}\n127.0.0.1:5432:{app_db}:{pg_admin}:{pgpass}",
            70,
        ),
        "database-node": (f"postgresql://{db_url}", 1000),
        "database-url": (f"postgresql+asyncpg://{db_url}", 1000),
        "backend-auth": (values["backend_auth"], 1000),
        "better-auth": (values["better_auth"], 1000),
        "redis-password": (values["redis_password"], 1000),
        "redis-acl": (f"user default reset on #{redis_hash} ~* &* +@all", 1000),
        "speaches": (values["speaches"], 1000),
    }
    for name, (value, uid) in rows.items():
        secret(root / "secrets" / name, value, uid, newline=name != "speaches")


def compose(root, state, *args, check=True):
    env = {**os.environ, **state["env"]}
    return run(
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-p",
        state["project"],
        "--profile",
        "migration",
        "-f",
        str(root / "config/compose.functional.yml"),
        "-f",
        str(root / "config/compose.integration.override.yml"),
        *args,
        env=env,
        check=check,
    )


def firewall(state, mode):
    name = state["project"]
    bridge = state["bridge"]
    marker = ("-m", "comment", "--comment", name)
    published = (
        "-p",
        "tcp",
        "-m",
        "conntrack",
        "--ctstate",
        "NEW",
        "--ctorigdst",
        HOST,
        "--ctorigdstport",
        str(PORT),
    )
    rules = [
        (
            "INPUT",
            "!",
            "-i",
            "lo",
            "-d",
            HOST,
            "-p",
            "tcp",
            "--dport",
            str(PORT),
            "-m",
            "conntrack",
            "--ctstate",
            "NEW",
            *marker,
            "-j",
            "DROP",
        ),
        ("FORWARD", *published, *marker, "-j", "DROP"),
        ("INPUT", "-i", bridge, *marker, "-j", "DROP"),
        (
            "FORWARD",
            "-i",
            bridge,
            "-m",
            "conntrack",
            "--ctstate",
            "NEW,INVALID,UNTRACKED",
            *marker,
            "-j",
            "DROP",
        ),
        (
            "FORWARD",
            "-i",
            bridge,
            "-d",
            GPU,
            "-p",
            "tcp",
            "--dport",
            GPU_PORT,
            "-m",
            "conntrack",
            "--ctstate",
            "NEW,ESTABLISHED",
            *marker,
            "-j",
            "ACCEPT",
        ),
    ]
    if state["lan_source"]:
        source = state["lan_source"]
        rules.extend(
            [
                (
                    "INPUT",
                    "-s",
                    source,
                    "-d",
                    HOST,
                    "-p",
                    "tcp",
                    "--dport",
                    str(PORT),
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "NEW",
                    *marker,
                    "-j",
                    "ACCEPT",
                ),
                ("FORWARD", "-s", source, *published, *marker, "-j", "ACCEPT"),
            ]
        )
    if mode == "check":
        for chain in ("INPUT", "FORWARD"):
            expected = [tuple(rule) for table, *rule in reversed(rules) if table == chain]
            lines = run("iptables", "-w", "5", "-S", chain).stdout.decode().splitlines()
            positions = [
                i
                for i, line in enumerate(lines)
                if f"--comment {name} " in line or f'--comment "{name}" ' in line
            ]
            need(len(positions) == len(expected), "Reglas de aislamiento inesperadas")
            targets = [lines[i].split(" -j ")[-1] for i in positions]
            need(
                targets == [rule[-1] for rule in expected],
                "Orden de reglas de aislamiento distinto",
            )
    for chain, *rule in rules if mode != "remove" else reversed(rules):
        exists = run("iptables", "-w", "5", "-C", chain, *rule, check=False).returncode == 0
        if mode == "apply" and not exists:
            run("iptables", "-w", "5", "-I", chain, "1", *rule)
        elif mode == "remove" and exists:
            run("iptables", "-w", "5", "-D", chain, *rule)
        elif mode == "check":
            need(exists, "Falta una regla de aislamiento del ensayo")


def collect(root, state):
    out = run(
        "docker",
        "ps",
        "-aq",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={state['project']}",
    ).stdout
    if not out.strip():
        return {}
    ids = out.decode().splitlines()
    containers = get_json("docker", "inspect", *ids)
    result = {}
    for item in containers:
        labels = item["Config"]["Labels"]
        name = labels.get("com.docker.compose.service")
        need(
            labels.get(LABEL) == OWNER and name in SERVICES and name not in result,
            "Contenedor no propio o servicio duplicado",
        )
        result[name] = {"id": item["Id"], "image": item["Image"], "state": item["State"]["Status"]}
    return result


def image_ids(item):
    return {item["Id"], (item.get("Descriptor") or {}).get("digest")} - {None}


def pin_migrate(state, container):
    """Upgrade a legacy trial plan while the original migrate CID still exists."""
    pins = state["verified_images"]
    if "migrate" not in pins:
        image = container["image"]
        need(IMAGE.fullmatch(image), "Identidad registrada de migración inválida")
        pins["migrate"] = [image]
        state["env"]["MEDIA_SUPOCLIP_TRIAL_MIGRATE_IMAGE"] = image


def create(args):
    root = args.root
    need(
        root.parent.resolve(strict=True) == root.parent and not root.exists(),
        "Usar una raíz física nueva",
    )
    need(PROJECT.fullmatch(args.project), "Nombre Compose inválido")
    need(args.config.resolve(strict=True) == args.config, "Ruta de configuración no física")
    read_manifest(args.config / "migration-manifest.json")
    need(
        all(IDENTIFIER.fullmatch(s) for s in (args.pg_admin, args.app_db, args.app_role)),
        "Identificador PostgreSQL inválido",
    )
    need(
        args.pg_admin != args.app_role
        and args.app_db not in {"postgres", "template0", "template1"},
        "Roles o base en conflicto",
    )
    need(args.prisma.resolve(strict=True) == args.prisma, "Ruta Prisma no física")
    need(
        (args.prisma / "prisma-source/node_modules/prisma/build/index.js").is_file()
        and (args.prisma / "prisma-bin/schema-engine").is_file(),
        "Cápsula Prisma incompleta",
    )
    if args.lan_source:
        need(
            re.fullmatch(r"192\.168\.0\.[0-9]{1,3}/32", args.lan_source),
            "Origen LAN debe ser una IP privada exacta",
        )
        socket.inet_aton(args.lan_source.split("/")[0])
    env = {}
    verified_images = {}
    for name, suffix in IMAGE_VARS.items():
        reference = getattr(args, name)
        need(IMAGE.fullmatch(reference), f"Imagen de {name} no fijada")
        [image] = get_json("docker", "image", "inspect", reference)
        uid = "70:70" if name == "postgres" else "1000:1000"
        need(
            reference in image_ids(image)
            and image["Os"] == "linux"
            and image["Architecture"] == "amd64",
            f"Imagen distinta para {name}",
        )
        need(
            name in {"valkey", "proxy"} or image["Config"]["User"] == uid,
            f"Usuario de imagen distinto para {name}",
        )
        env[f"MEDIA_SUPOCLIP_TRIAL_{suffix}_IMAGE"] = reference
        verified_images[name] = sorted(image_ids(image))
    env["MEDIA_SUPOCLIP_TRIAL_MIGRATE_IMAGE"] = env["MEDIA_SUPOCLIP_TRIAL_FRONTEND_IMAGE"]
    verified_images["migrate"] = list(verified_images["frontend"])
    need(
        not run(
            "docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={args.project}"
        ).stdout.strip(),
        "El proyecto ya tiene contenedores",
    )
    need(
        not run(
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={args.project}",
        ).stdout.strip(),
        "El proyecto ya tiene redes",
    )
    run("iptables", "-w", "5", "-S", "INPUT")
    run("iptables", "-w", "5", "-S", "FORWARD")
    with socket.socket() as listener:
        listener.bind((HOST, PORT))
    raw = sys.stdin.buffer.read(8193)
    need(len(raw) <= 8192, "DTO de secretos demasiado grande")
    values = json.loads(raw)
    bridge = "sco" + hashlib.sha256(args.project.encode()).hexdigest()[:10]
    speaches = args.project + "-speaches"
    need(
        run("docker", "network", "inspect", speaches, check=False).returncode != 0,
        "La red Speaches ya existe",
    )
    env.update(
        {
            "MEDIA_SUPOCLIP_TRIAL_ROOT": str(root),
            "MEDIA_SUPOCLIP_TRIAL_HOST": HOST,
            "MEDIA_SUPOCLIP_TRIAL_PG_ADMIN": args.pg_admin,
            "MEDIA_SUPOCLIP_TRIAL_PRISMA_SOURCE": str(args.prisma / "prisma-source"),
            "MEDIA_SUPOCLIP_TRIAL_PRISMA_BIN": str(args.prisma / "prisma-bin"),
            "MEDIA_SUPOCLIP_TRIAL_SPEACHES_NETWORK": speaches,
        }
    )
    state = {
        "env": env,
        "project": args.project,
        "bridge": bridge,
        "lan_source": args.lan_source,
        "app_db": args.app_db,
        "app_role": args.app_role,
        "speaches": speaches,
        "verified_images": verified_images,
    }
    directory(root, 0)
    directory(root / "config", 0)
    write_json(root / "plan.json", state)
    stage_secrets(root, args.pg_admin, args.app_db, args.app_role, values)
    for name in (
        "compose.functional.yml",
        "compose.integration.override.yml",
        "launch-trial.sh",
        "nginx.functional.conf",
        "migration-manifest.json",
    ):
        path = root / "config" / name
        path.write_bytes((args.config / name).read_bytes())
        path.chmod(0o444)
    network = (
        run(
            "docker",
            "network",
            "create",
            "--driver",
            "bridge",
            "--ipv6=false",
            "--opt",
            f"com.docker.network.bridge.name={bridge}",
            "--label",
            f"{LABEL}={OWNER}",
            "--label",
            f"agency.trial={args.project}",
            speaches,
        )
        .stdout.decode()
        .strip()
    )
    write_json(root / "speaches-network.json", {"id": network})
    firewall(state, "apply")
    compose(root, state, "config", "--quiet")
    result = compose(root, state, "create", check=False)
    (root / "compose-create.log").write_bytes(result.stdout + result.stderr)
    containers = collect(root, state)
    write_json(root / "containers.json", containers)
    need(
        result.returncode == 0 and set(containers) == set(SERVICES),
        "Creación incompleta. Revisar compose-create.log",
    )
    validate(root, state)
    return containers


def validate(root, state, *, containers=None):
    if containers is None:
        containers = json.loads((root / "containers.json").read_bytes())
    pin_migrate(state, containers["migrate"])
    firewall(state, "check")
    [network] = get_json("docker", "network", "inspect", state["speaches"])
    need(
        network["Id"] == json.loads((root / "speaches-network.json").read_bytes())["id"],
        "ID de red Speaches distinto",
    )
    need(
        network["Labels"].get("agency.trial") == state["project"]
        and network["Driver"] == "bridge"
        and not network["EnableIPv6"]
        and network["Options"].get("com.docker.network.bridge.name") == state["bridge"],
        "Red Speaches distinta",
    )
    for suffix, internal in (("app", True), ("data", True), ("ingress", False)):
        [part] = get_json("docker", "network", "inspect", f"{state['project']}_{suffix}")
        need(
            part["Driver"] == "bridge"
            and part["Internal"] == internal
            and part["Labels"].get("com.docker.compose.project") == state["project"],
            "Red Compose distinta",
        )
    current = collect(root, state)
    need(set(current) == set(SERVICES), "Conjunto de servicios distinto")
    for name, entry in current.items():
        need(entry["id"] == containers[name]["id"], "CID del ensayo distinto")
        [item] = get_json("docker", "inspect", entry["id"])
        role = "backend" if name == "worker" else name
        need(
            entry["image"] in state["verified_images"][role],
            f"Imagen del contenedor {name} distinta",
        )
        cfg = item["HostConfig"]
        need(
            cfg["ReadonlyRootfs"] and "ALL" in cfg["CapDrop"], "Aislamiento de contenedor distinto"
        )
        expected_ports = (
            {f"{PORT}/tcp": [{"HostIp": HOST, "HostPort": str(PORT)}]} if name == "proxy" else {}
        )
        need((cfg["PortBindings"] or {}) == expected_ports, "Puertos publicados distintos")
        attached = item["NetworkSettings"]["Networks"]
        if name == "worker":
            need(cfg["Dns"] == ["127.0.0.1"], "DNS del worker no restringido")
        need(
            (state["speaches"] in attached) == (name == "worker"),
            "Red Speaches adjunta a rol incorrecto",
        )
    need(
        set(network.get("Containers") or {}) <= {containers["worker"]["id"]},
        "Red Speaches compartida con otro contenedor",
    )
    return containers


def healthy(cid):
    for _ in range(40):
        [item] = get_json("docker", "inspect", cid)
        state = item["State"]
        if state.get("Health", {}).get("Status") == "healthy":
            return
        need(state["Status"] == "running", "Servicio detenido antes de estar saludable")
        time.sleep(2)
    raise ValueError("Tiempo agotado esperando salud del servicio")


def sql(cid, pg_admin, db, query):
    return run(
        "docker",
        "exec",
        "-i",
        cid,
        "psql",
        "-X",
        "-qAtw",
        "-h",
        "127.0.0.1",
        "-U",
        pg_admin,
        "-d",
        db,
        "--set=ON_ERROR_STOP=1",
        data=query.encode(),
    )


def api_health(cid, endpoint, key):
    code = "import json,sys,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health/'+sys.argv[1],timeout=3)); assert data.get('status')=='healthy' and data.get(sys.argv[2])=='connected'"
    for _ in range(40):
        if (
            run(
                "docker",
                "exec",
                cid,
                "/usr/bin/python3.12",
                "-I",
                "-S",
                "-B",
                "-c",
                code,
                endpoint,
                key,
                check=False,
            ).returncode
            == 0
        ):
            return
        time.sleep(2)
    raise ValueError(f"Servicio sin salud verificada: {endpoint}")


def start(root, state):
    firewall(state, "apply")
    containers = validate(root, state)
    cid = {name: row["id"] for name, row in containers.items()}
    run("docker", "start", cid["postgres"], cid["valkey"])
    healthy(cid["postgres"])
    healthy(cid["valkey"])
    denied = run("docker", "exec", cid["valkey"], "valkey-cli", "ping", check=False)
    need(b"NOAUTH" in denied.stdout + denied.stderr, "Valkey aceptó acceso sin contraseña")
    pg_admin = state["env"]["MEDIA_SUPOCLIP_TRIAL_PG_ADMIN"]
    role, db = state["app_role"], state["app_db"]
    credentials = urlsplit((root / "secrets/database-node").read_text().strip())
    password = unquote(credentials.password).replace("'", "''")
    existing_role = sql(
        cid["postgres"],
        pg_admin,
        "postgres",
        f"SELECT rolcanlogin AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication) FROM pg_roles WHERE rolname='{role}';\n",
    ).stdout.strip()
    need(existing_role in {b"", b"t"}, "Privilegios del rol de aplicación distintos")
    if not existing_role:
        sql(
            cid["postgres"],
            pg_admin,
            "postgres",
            f"SET standard_conforming_strings=on;\nCREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '{password}';\n",
        )
    existing_owner = sql(
        cid["postgres"],
        pg_admin,
        "postgres",
        f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='{db}';\n",
    ).stdout.strip()
    need(existing_owner in {b"", role.encode()}, "Propietario de la base distinto")
    if not existing_owner:
        sql(cid["postgres"], pg_admin, "postgres", f"CREATE DATABASE {db} OWNER {role};\n")
    sql(cid["postgres"], pg_admin, db, 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";\n')
    [migrate] = get_json("docker", "inspect", cid["migrate"])
    status = migrate["State"]
    if status["Status"] != "exited" or status["ExitCode"] != 0:
        if status["Status"] != "running":
            run("docker", "start", cid["migrate"])
        code = run("docker", "wait", cid["migrate"]).stdout.strip()
        need(code == b"0", "Prisma terminó con error")
    migrations = read_manifest(root / "config/migration-manifest.json")
    expected = {f"{name}|{row['sha256']}" for name, row in migrations["prisma"]["rows"].items()}
    applied = (
        sql(
            cid["postgres"],
            pg_admin,
            db,
            "SELECT migration_name || '|' || checksum FROM _prisma_migrations WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL;\n",
        )
        .stdout.decode()
        .splitlines()
    )
    need(len(applied) == len(expected) and set(applied) == expected, "Migraciones Prisma distintas")
    failed = sql(
        cid["postgres"],
        pg_admin,
        db,
        "SELECT count(*) FROM _prisma_migrations WHERE finished_at IS NULL OR rolled_back_at IS NOT NULL;\n",
    ).stdout.strip()
    need(failed == b"0", "Migraciones Prisma fallidas")
    run("docker", "start", cid["backend"])
    api_health(cid["backend"], "db", "database")
    api_health(cid["backend"], "redis", "redis")
    applied = (
        sql(cid["postgres"], pg_admin, db, "SELECT version FROM schema_migrations;\n")
        .stdout.decode()
        .splitlines()
    )
    need(
        len(applied) == len(migrations["backend"]["rows"])
        and set(applied) == migrations["backend"]["rows"].keys(),
        "Migraciones backend distintas",
    )
    run("docker", "start", cid["worker"])
    api_health(cid["backend"], "worker", "worker")
    run("docker", "start", cid["frontend"])
    healthy(cid["frontend"])
    run("docker", "start", cid["proxy"])
    run(
        "docker",
        "exec",
        cid["proxy"],
        "/usr/bin/nginx",
        "-t",
        "-e",
        "stderr",
        "-c",
        "/etc/nginx/supoclip.conf",
    )
    result = run(
        "curl",
        "--noproxy",
        "*",
        "--fail",
        "--silent",
        "--max-time",
        "5",
        f"http://{HOST}:{PORT}/healthz",
    )
    need(result.stdout == b"ok\n", "Prueba HTTP local del origen LAN fallida")
    receipt = {
        "local_http": 200,
        "lan_external_tested": False,
        "ids": cid,
        "prisma_migrations": len(expected),
        "backend_migrations": len(applied),
    }
    write_json(root / f"start-{time.time_ns()}.json", receipt)
    return receipt


def stop(root, state):
    containers = collect(root, state)
    receipt = root / "containers.json"
    if receipt.exists():
        saved = json.loads(receipt.read_bytes())
        need(
            all(
                name in saved and saved[name]["id"] == row["id"] for name, row in containers.items()
            ),
            "CID ajeno al recibo",
        )
    for name in reversed(SERVICES):
        if name in containers and containers[name]["state"] == "running":
            run("docker", "stop", "--time", "30", containers[name]["id"])
    firewall(state, "remove")
    return {"stopped": True, "preserved_containers": containers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    create_parser = sub.add_parser("create")
    for key in ("root", "config", "prisma"):
        create_parser.add_argument(f"--{key}", type=Path, required=True)
    for key in ("project", "pg-admin", "app-db", "app-role", *IMAGE_VARS):
        create_parser.add_argument(f"--{key}", required=True)
    create_parser.add_argument("--lan-source")
    for mode in ("start", "stop", "verify"):
        sub.add_parser(mode).add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    need(
        os.geteuid() == 0
        and not os.environ.get("DOCKER_HOST")
        and not os.environ.get("DOCKER_CONTEXT"),
        "Requiere root y Docker local",
    )
    need(
        run(
            "docker", "context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}"
        ).stdout.strip()
        == b"unix:///var/run/docker.sock",
        "Socket de Docker no local",
    )
    os.umask(0o077)
    if args.mode == "create":
        result = create(args)
    else:
        need(args.root.resolve(strict=True) == args.root, "Raíz no física")
        state = json.loads((args.root / "plan.json").read_bytes())
        result = {"start": start, "stop": stop, "verify": validate}[args.mode](args.root, state)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        detail = str(error) if type(error) is ValueError else type(error).__name__
        print(f"SupoClip: {detail}", file=sys.stderr)
        sys.exit(1)
