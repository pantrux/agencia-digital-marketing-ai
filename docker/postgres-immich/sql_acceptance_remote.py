"""Aceptación SQL propia; contrato puro y etapas explícitas, sin efectos al importar."""

import base64
import hashlib
import json
import os
import re
import stat
import struct
import time
import types
from datetime import UTC, datetime

OWNER = "agencia-digital-marketing-ai:postgres-immich-sql-acceptance:v1"
RUNTIME_OWNER = "agencia-digital-marketing-ai:postgres-immich-runtime:v4.1"
SDK_SOURCE = "a13ab96098b5f739ed88cbacaffc5bed521825aeb033d428bddbf93be57137ed"
SDK_LOCAL = "685bb8a54fc7d65fd25710b5ea317f8eda239b57aa2978a0ff5e33b8aabba608"
ENV_PARSER_SHA = "5b1f728900e7038f83085b4beaf39b8fe5354d724bd06a6e6c828c80b5a32cda"
MARKER = ".sql-acceptance-owner.json"
STAGES = ("prepare", "create", "start1", "verify1", "stop1", "start2", "verify2", "stop2")
BUDGETS = {
    "prepare": 30,
    "create": 15,
    "start1": 90,
    "verify1": 180,
    "stop1": 20,
    "start2": 90,
    "verify2": 60,
    "stop2": 20,
}
SERVER_ARGS = [
    "postgres",
    "-c",
    "shared_preload_libraries=vchord.so",
    "-c",
    "listen_addresses=127.0.0.1",
    "-c",
    "unix_socket_directories=/var/run/postgresql",
    "-c",
    "max_connections=10",
    "-c",
    "shared_buffers=128MB",
    "-c",
    "jit=off",
]
DATABASE_ENV = [
    "POSTGRES_USER=agency_probe",
    "POSTGRES_DB=agency_abi",
    "POSTGRES_PASSWORD_FILE=/run/secrets/db-password",
    "POSTGRES_INITDB_ARGS=--data-checksums --auth-local=scram-sha-256 --auth-host=scram-sha-256",
    "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
]
TMPFS = {
    "/tmp": "rw,nosuid,nodev,noexec,size=67108864,mode=1777",
    "/var/run/postgresql": "rw,nosuid,nodev,noexec,size=8388608,mode=0775,uid=70,gid=70",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def envelope(receipt):
    return {"receipt": receipt, "receipt_sha256": digest(canonical(receipt))}


def sha(value, prefix=False):
    return (
        isinstance(value, str)
        and re.fullmatch(("sha256:" if prefix else "") + "[a-f0-9]{64}", value) is not None
    )


def validate_approval(value, image_id, now=None):
    now = datetime.now(UTC) if now is None else now
    if not isinstance(value, dict):
        raise ValueError("La aprobación debe ser un objeto JSON.")
    image, final = value.get("image", {}), value.get("finalization", {})
    if not isinstance(image, dict) or not isinstance(final, dict):
        raise ValueError("La aprobación requiere objetos imagen y finalización.")
    if (
        value.get("schema_version") != 1
        or value.get("decision") != "approved_for_isolated_sql"
        or value.get("coverage_complete") is not True
        or image.get("id") != image_id
        or not sha(image_id, True)
        or not sha(image.get("config_digest"), True)
    ):
        raise ValueError("No existe aprobación SCA completa para este subject dinámico.")
    if (
        final.get("success") is not True
        or not sha(final.get("receipt_sha256"))
        or final.get("image_id") != image_id
        or final.get("config_digest") != image["config_digest"]
    ):
        raise ValueError("La finalización y aprobación SCA divergen de imagen/config.")
    if (
        type(value.get("unresolved_high_critical")) is not int
        or value["unresolved_high_critical"] != 0
        or value.get("unresolved_advisories") != []
        or value.get("sqlite_version") != "3.54.1"
    ):
        raise ValueError("Quedan avisos sin resolver o SQLite no acreditado.")
    try:
        updated = datetime.fromisoformat(value["db_updated_at_utc"])
        expires = datetime.fromisoformat(value["expires_at_utc"])
        if (
            updated.utcoffset().total_seconds() != 0
            or expires.utcoffset().total_seconds() != 0
            or not updated <= now < expires
            or (expires - updated).total_seconds() > 48 * 3600
        ):
            raise ValueError("Vigencia de DB/aprobación no acreditada.")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Fechas de aprobación inválidas.") from error
    reports = value.get("reports", [])
    if (
        not isinstance(reports, list)
        or len(reports) != 4
        or any(not isinstance(r, dict) for r in reports)
        or {r.get("kind") for r in reports} != {"os", "cargo", "sqlite", "mimalloc"}
    ):
        raise ValueError("Se requieren reportes OS, Cargo, SQLite y mimalloc separados.")
    for report in reports:
        if (
            type(report.get("components")) is not int
            or report["components"] <= 0
            or not sha(report.get("sha256"))
            or not isinstance(report.get("path"), str)
            or not report["path"]
        ):
            raise ValueError("Reporte sin componentes observados o sin hash/ruta.")
    if (
        image.get("user") != "70:70"
        or image.get("entrypoint") != ["docker-entrypoint.sh"]
        or image.get("cmd") != ["postgres"]
        or image.get("labels", {}).get("cl.e-scale.agencia.owner") != RUNTIME_OWNER
        or not isinstance(image.get("env"), list)
    ):
        raise ValueError("La imagen aprobada no acredita UID70/entrypoint/owner.")
    if any(
        not isinstance(row, str)
        or "=" not in row
        or re.search(r"(?:TOKEN|PASSWORD|PRIVATE_KEY)", row.split("=", 1)[0])
        for row in image["env"]
    ):
        raise ValueError("La configuración base no puede incorporar credenciales.")
    return image


def validate_profile(profile):
    if (
        profile.get("schema_version") != 1
        or profile.get("owner") != OWNER
        or profile.get("target")
        != {"hostname": "vm-media-01", "ip": "192.168.0.85", "user": "jandrade", "port": 22}
    ):
        raise ValueError("Perfil SQL fuera del host/rol autorizado.")
    parent, root = profile["parent"]["path"], profile["root"]
    if (
        not re.fullmatch(
            r"/var/lib/media-platform/build/postgres-immich-runtime/[A-Za-z0-9-]{1,64}", parent
        )
        or root != parent + "/sql-acceptance-v1"
        or profile["data"] != root + "/data"
        or profile["secrets"] != root + "/secrets"
    ):
        raise ValueError("Las rutas SQL no pertenecen al namespace propio NVMe.")
    if (
        not re.fullmatch(r"agencia-pg-immich-sql-[a-z0-9-]{1,48}", profile["name"])
        or not sha(profile["password_sha256"])
        or not sha(profile["approval_sha256"])
        or not sha(profile["image"]["id"], True)
    ):
        raise ValueError("Nombre, secreto respaldado o referencia SCA inválidos.")
    if (
        set(profile["sources"]) != {"local", "remote", "sql"}
        or any(not sha(v) for v in profile["sources"].values())
        or profile.get("budgets") != BUDGETS
    ):
        raise ValueError("Fuentes/presupuestos SQL distintos del contrato.")


def create_arguments(profile, profile_sha):
    return [
        "create",
        "--pull=never",
        "--name",
        profile["name"],
        "--label",
        "cl.e-scale.agencia.owner=" + OWNER,
        "--label",
        "cl.e-scale.agencia.profile=" + profile_sha,
        "--user",
        "70:70",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        "2",
        "--memory",
        str(2 * 1024**3),
        "--memory-swap",
        str(2 * 1024**3),
        "--pids-limit",
        "128",
        "--shm-size",
        str(128 * 1024**2),
        "--restart",
        "no",
        "--stop-timeout",
        "5",
        "--mount",
        "type=bind,src=" + profile["data"] + ",dst=/var/lib/postgresql/data",
        "--mount",
        "type=bind,src=" + profile["secrets"] + ",dst=/run/secrets,readonly",
        *[part for path, options in TMPFS.items() for part in ("--tmpfs", path + ":" + options)],
        *[part for row in DATABASE_ENV for part in ("--env", row)],
        profile["image"]["id"],
        *SERVER_ARGS,
    ]


def check_container(value, cid, profile):
    if (
        not sha(cid)
        or value.get("Id") != cid
        or value.get("Image") != profile["image"]["id"]
        or value.get("Name") != "/" + profile["name"]
    ):
        raise ValueError("CID/imagen/nombre no corresponden al recurso SQL propio.")
    config, host = value.get("Config", {}), value.get("HostConfig", {})
    labels = {
        **profile["image"]["labels"],
        "cl.e-scale.agencia.owner": OWNER,
        "cl.e-scale.agencia.profile": digest(canonical(profile)),
    }
    if (
        config.get("Image") != profile["image"]["id"]
        or config.get("User") != "70:70"
        or config.get("Entrypoint") != ["docker-entrypoint.sh"]
        or config.get("Cmd") != SERVER_ARGS
        or config.get("Labels") != labels
        or sorted(config.get("Env", [])) != sorted(profile["image"]["env"] + DATABASE_ENV)
    ):
        raise ValueError("La configuración SQL no acredita imagen/env/entrypoint/owner exactos.")
    limits = {
        "NetworkMode": "none",
        "NanoCpus": 2_000_000_000,
        "Memory": 2 * 1024**3,
        "MemorySwap": 2 * 1024**3,
        "PidsLimit": 128,
        "ShmSize": 128 * 1024**2,
        "Privileged": False,
        "ReadonlyRootfs": True,
    }
    if (
        any(host.get(k) != v for k, v in limits.items())
        or host.get("CapAdd")
        or host.get("CapDrop") not in (["ALL"], ["CAP_ALL"])
        or host.get("SecurityOpt") != ["no-new-privileges"]
        or host.get("RestartPolicy", {}).get("Name") != "no"
        or host.get("PortBindings")
        or host.get("PublishAllPorts")
        or host.get("Binds")
        or host.get("VolumesFrom")
        or host.get("Devices")
        or host.get("DeviceRequests")
        or host.get("PidMode")
        or host.get("IpcMode", "private") != "private"
        or host.get("UsernsMode")
        or host.get("Tmpfs") != TMPFS
    ):
        raise ValueError("NoForeign SQL: límites, capacidades, red, rootfs o namespaces distintos.")
    mounts = host.get("Mounts", [])
    expected = {
        (profile["data"], "/var/lib/postgresql/data", False),
        (profile["secrets"], "/run/secrets", True),
    }
    if (
        len(mounts) != 2
        or any(
            m.get("Type") != "bind"
            or m.get("BindOptions", {}).get("Propagation", "rprivate") != "rprivate"
            or m.get("VolumeOptions")
            or m.get("TmpfsOptions")
            for m in mounts
        )
        or {(m.get("Source"), m.get("Target"), m.get("ReadOnly", False)) for m in mounts}
        != expected
    ):
        raise ValueError("NoForeign SQL: binds distintos de los dos recursos propios.")
    actual, destinations = set(), set()
    for mount in value.get("Mounts", []):
        if mount.get("Destination") in destinations:
            raise ValueError("Destino SQL repetido.")
        destinations.add(mount.get("Destination"))
        if mount.get("Type") == "bind":
            actual.add((mount.get("Source"), mount.get("Destination"), not mount.get("RW")))
        elif not (
            mount.get("Type") == "tmpfs"
            and mount.get("Destination") in TMPFS
            and not mount.get("Name")
            and not mount.get("Source")
        ):
            raise ValueError("Volumen anónimo o montaje efectivo adicional.")
    if actual != expected or len(value.get("Mounts", [])) not in (2, 3, 4):
        raise ValueError("Inventario SQL efectivo no coincide con los binds propios.")
    state = value.get("State", {})
    if (
        type(state.get("Pid")) is not int
        or state["Pid"] < 0
        or type(state.get("Running")) is not bool
    ):
        raise ValueError("Estado/PID SQL no acreditados.")
    return {
        "cid": cid,
        "image_id": value["Image"],
        "created": value.get("Created"),
        "running": state["Running"],
        "pid": state["Pid"],
        "started_at": state.get("StartedAt"),
        "no_foreign": True,
        "limits": limits,
    }


def require_backup(mode, actual, backed):
    required = {
        "create": ("prepare",),
        "start1": ("prepare", "create"),
        "verify1": ("prepare", "create", "start1"),
        "stop1": ("prepare", "create"),
        "start2": ("prepare", "create", "start1", "verify1", "stop1"),
        "verify2": ("prepare", "create", "start1", "verify1", "stop1", "start2"),
        "stop2": ("prepare", "create"),
    }[mode]
    for name in required:
        receipt = actual.get(name, {})
        if (
            receipt != backed.get(name)
            or receipt != envelope(receipt.get("receipt", {}))
            or receipt.get("receipt", {}).get("success") is not True
        ):
            raise ValueError("Falta un recibo anterior positivo y respaldado: " + name)


def stop_owned(docker, cid, profile):
    deadline = time.monotonic() + 10

    def remaining(maximum):
        left = min(maximum, deadline - time.monotonic())
        if left <= 0:
            raise TimeoutError("La parada propia agotó su reserva de 10 segundos.")
        return left

    def inspect():
        return check_container(docker.inspect(cid, timeout=remaining(1)), cid, profile)

    before = inspect()
    killed = False
    if before["running"]:
        try:
            docker.command(["stop", "--time", "5", cid], timeout=remaining(6))
        except (TimeoutError, ValueError):
            pass
        if inspect()["running"]:
            docker.command(["kill", cid], timeout=remaining(1))
            killed = True
    after = inspect()
    if after["running"] or after["pid"] != 0:
        raise ValueError("No se acredita parada del CID SQL propio.")
    return {
        "cid": cid,
        "stopped": True,
        "kill_used": killed,
        "before_pid": before["pid"],
        "after_pid": after["pid"],
    }


def validate_summary(data, previous_cluster=None):
    try:
        value = json.loads(data)
    except (ValueError, TypeError) as error:
        raise ValueError("SQL no devolvió un único resultado JSON completo.") from error
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("extensions"), dict)
        or value.get("kind") != "sql_acceptance"
        or value.get("verified") is not True
        or value.get("server_version_num") != 160015
        or value.get("vectors") != 4096
        or value.get("dimension") != 512
        or value.get("queries") != 5
        or value.get("index_verified") is not True
        or value.get("sentinel") != "persistido"
        or not isinstance(value.get("cluster_id"), str)
        or not value["cluster_id"].isdigit()
        or value.get("extensions", {}).get("vector") != "0.8.5"
        or value.get("extensions", {}).get("vchord") != "1.1.1"
        or not {"cube", "earthdistance"} <= set(value["extensions"])
    ):
        raise ValueError("SQL no acredita versiones/dataset/índice/sentinel/cluster completos.")
    if previous_cluster is not None and value["cluster_id"] != previous_cluster:
        raise ValueError("El cluster cambió después de reiniciar.")
    return value


def effective_limits(normal, observed):
    pid = observed["pid"]
    if type(pid) is not int or pid <= 0:
        raise ValueError("PID activo SQL no acreditado.")
    root = os.open("/proc/" + str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def read(fd, name, maximum=65536):
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            data = os.read(source, maximum + 1)
            if len(data) > maximum:
                raise ValueError("Metadato kernel excesivo.")
            return data.decode()
        finally:
            os.close(source)

    try:
        rows = read(root, "cgroup").strip().splitlines()
        if len(rows) != 1 or not rows[0].startswith("0::/"):
            raise ValueError("Se requiere cgroup v2.")
        path = rows[0][3:]
        if (
            not re.fullmatch(r"/[A-Za-z0-9_./:@-]+", path)
            or ".." in path.split("/")
            or observed["cid"] not in path
        ):
            raise ValueError("El cgroup no corresponde al CID SQL propio.")
        fd = normal.open_directory("/sys/fs/cgroup" + path)
        try:
            values = {
                name: read(fd, name, 4096).strip()
                for name in ("memory.max", "memory.swap.max", "cpu.max", "pids.max")
            }
        finally:
            os.close(fd)
        quota, period = values["cpu.max"].split()
        if (
            values["memory.max"] != str(2 * 1024**3)
            or values["memory.swap.max"] != "0"
            or values["pids.max"] != "128"
            or not quota.isdigit()
            or not period.isdigit()
            or int(period) <= 0
            or int(quota) != 2 * int(period)
        ):
            raise ValueError("El kernel no acredita 2CPU/2GiB/swap0/pids128.")
        status = read(root, "status")
        uid = re.search(r"^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", status, re.M)
        caps = re.search(r"^CapEff:\s+([a-fA-F0-9]+)\s*$", status, re.M)
        if not uid or set(uid.groups()) != {"70"} or not caps or int(caps[1], 16) != 0:
            raise ValueError("El postmaster no acredita UID70 sin capacidades efectivas.")
        maps = read(root, "maps")
        libraries = [
            "/usr/local/lib/postgresql/vchord.so",
            "/usr/local/lib/postgresql/libsqlite3.so.0",
        ]
        if any(name not in maps or name + " (deleted)" in maps for name in libraries):
            raise ValueError("Las bibliotecas nativas no están mapeadas en el postmaster.")
        return {
            "verified": True,
            "mapped_libraries": libraries,
            "pid": pid,
            "cgroup": path,
            "values": values,
            "uid": 70,
            "effective_capabilities": 0,
        }
    finally:
        os.close(root)


def write_node(fd, name, data, *, uid=0, mode=0o600):
    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
    with os.fdopen(out, "wb") as stream:
        os.fchown(stream.fileno(), uid, uid)
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(fd)


def marker(profile, directory):
    return {
        "owner": OWNER,
        "path": profile["root"],
        "directory": directory,
        "profile_sha256": digest(canonical(profile)),
    }


class Store:
    def __init__(self, normal, profile):
        self.normal, self.profile, self.root = normal, profile, None

    def close(self):
        if self.root is not None:
            os.close(self.root)
            self.root = None

    def open(self):
        self.root = self.normal.open_directory(self.profile["root"])
        self.normal.checked_node(os.fstat(self.root), 0, directory=True)
        actual = self.normal.json_object(self.normal.read_file(self.root, MARKER))
        if actual != marker(
            self.profile, self.normal.identity(os.fstat(self.root))
        ) or self.normal.read_file(self.root, "profile.json", maximum=512 * 1024) != canonical(
            self.profile
        ):
            raise ValueError("El control SQL no acredita profile/path/devino propios.")

    def receipt(self, name):
        try:
            raw = self.normal.read_file(self.root, name + ".json", maximum=512 * 1024)
        except FileNotFoundError:
            return None
        value = self.normal.json_object(raw)
        if (
            raw != canonical(value)
            or value.get("stage") != name
            or value.get("profile_sha256") != digest(canonical(self.profile))
            or value.get("source_sha256") != self.profile["sources"]["remote"]
        ):
            raise ValueError("Recibo SQL de otra etapa/perfil/fuente.")
        return envelope(value)

    def receipts(self):
        return {name: value for name in STAGES if (value := self.receipt(name)) is not None}

    def put(self, mode, fields):
        receipt = {
            "stage": mode,
            "profile_sha256": digest(canonical(self.profile)),
            "source_sha256": self.profile["sources"]["remote"],
            "completed_at_utc": datetime.now(UTC).isoformat(),
            **fields,
        }
        write_node(self.root, mode + ".json", canonical(receipt))
        return envelope(receipt)

    def verify_files(self):
        prepared = self.receipt("prepare")["receipt"]
        for leaf, _path in (("data", self.profile["data"]), ("secrets", self.profile["secrets"])):
            fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.root)
            try:
                info = os.fstat(fd)
                if (
                    info.st_uid != 70
                    or info.st_gid != 70
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or self.normal.identity(info) != prepared["directories"][leaf]
                ):
                    raise ValueError("Un bind SQL cambió de dueño/modo/inodo.")
                if leaf == "secrets":
                    if set(os.listdir(fd)) != set(prepared["secret_files"]):
                        raise ValueError("Inventario de secretos SQL distinto del preparado.")
                    for name, expected in prepared["secret_files"].items():
                        source = os.open(
                            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                        )
                        with os.fdopen(source, "rb") as stream:
                            item = os.fstat(stream.fileno())
                            if (
                                not stat.S_ISREG(item.st_mode)
                                or item.st_uid != 70
                                or item.st_gid != 70
                                or stat.S_IMODE(item.st_mode) != 0o400
                                or item.st_nlink != 1
                                or item.st_size != expected["size"]
                                or item.st_size > 512 * 1024
                            ):
                                raise ValueError(
                                    "Archivo secreto/SQL no tiene metadata propia exacta."
                                )
                            data = stream.read(512 * 1024 + 1)
                            if digest(data) != expected["sha256"] or self.normal.identity(
                                item
                            ) != self.normal.identity(
                                os.stat(name, dir_fd=fd, follow_symlinks=False)
                            ):
                                raise ValueError("Un secreto/SQL cambió después de prepare.")
            finally:
                os.close(fd)


def prepare(store, request):
    normal, profile = store.normal, store.profile
    password = request.get("password", "")
    if (
        not isinstance(password, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", password)
        or digest(password.encode()) != profile["password_sha256"]
    ):
        raise ValueError("Falta el secreto exacto respaldado antes de preparar.")
    sql = base64.b64decode(request["sql_source_b64"], validate=True)
    if digest(sql) != profile["sources"]["sql"] or len(sql) > 512 * 1024:
        raise ValueError("SQL distinto del hash aprobado.")
    parent = normal.open_directory(profile["parent"]["path"])
    try:
        normal.checked_node(os.fstat(parent), 0, directory=True)
        raw = normal.read_file(parent, ".runtime-image-owner.json")
        actual = normal.json_object(raw)
        if (
            digest(raw) != profile["parent"]["marker_sha256"]
            or actual
            != {
                "owner": RUNTIME_OWNER,
                "path": profile["parent"]["path"],
                "directory": profile["parent"]["directory"],
            }
            or normal.identity(os.fstat(parent)) != profile["parent"]["directory"]
        ):
            raise ValueError("El padre SQL no corresponde al record respaldado.")
        os.mkdir("sql-acceptance-v1", 0o700, dir_fd=parent)
        store.root = os.open(
            "sql-acceptance-v1", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
    finally:
        os.close(parent)
    write_node(
        store.root, MARKER, canonical(marker(profile, normal.identity(os.fstat(store.root))))
    )
    write_node(store.root, "profile.json", canonical(profile))
    directories, hashes = {}, {}
    for name in ("data", "secrets"):
        os.mkdir(name, 0o700, dir_fd=store.root)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store.root)
        try:
            os.fchown(fd, 70, 70)
            os.fchmod(fd, 0o700)
            directories[name] = normal.identity(os.fstat(fd))
            if name == "secrets":
                files = {
                    "db-password": password.encode() + b"\n",
                    "pgpass": b"127.0.0.1:5432:agency_abi:agency_probe:"
                    + password.encode()
                    + b"\n",
                    "acceptance.sql": sql,
                }
                for leaf, data in files.items():
                    write_node(fd, leaf, data, uid=70, mode=0o400)
                    hashes[leaf] = {"size": len(data), "sha256": digest(data)}
        finally:
            os.close(fd)
    return store.put(
        "prepare",
        {
            "success": True,
            "directories": directories,
            "secret_files": hashes,
            "sql_executed": False,
            "container_created": False,
        },
    )


def psql(docker, cid, *, arguments, password=True, timeout=10, sink=None):
    return docker.command(
        [
            "exec",
            "--user",
            "70:70",
            "--env",
            "PGPASSFILE=" + ("/run/secrets/pgpass" if password else "/run/secrets/not-present"),
            cid,
            "/usr/local/bin/psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-w",
            "-h",
            "127.0.0.1",
            "-U",
            "agency_probe",
            "-d",
            "agency_abi",
            "-v",
            "ON_ERROR_STOP=1",
            *arguments,
        ],
        timeout=timeout,
        sink=sink,
    )


def start_server(normal, docker, cid, profile, mode, actual, deadline):
    before = check_container(docker.inspect(cid), cid, profile)
    if before["running"] or before["pid"] != 0:
        raise ValueError("Start exige el CID SQL propio detenido.")
    if mode == "start1" and docker.inspect(cid).get("State", {}).get("Status") != "created":
        raise ValueError("Primer start exige contenedor nunca iniciado.")
    result = docker.command(["start", cid], timeout=10)
    if result["returncode"] != 0 or result["output"].decode().strip() != cid:
        raise ValueError("Docker no confirmó el start del CID propio.")
    ready = False
    while time.monotonic() < deadline - 10:
        observed = check_container(docker.inspect(cid), cid, profile)
        if not observed["running"]:
            raise ValueError("El servidor SQL terminó durante init/readiness.")
        probe = docker.command(
            [
                "exec",
                "--user",
                "70:70",
                cid,
                "/usr/local/bin/pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "agency_probe",
                "-d",
                "agency_abi",
            ],
            timeout=3,
        )
        if probe["returncode"] == 0:
            ready = True
            break
        time.sleep(0.25)
    if not ready:
        raise TimeoutError("SQL no alcanzó readiness dentro del plazo.")
    negative = psql(docker, cid, password=False, arguments=["-c", "SELECT 1"], timeout=5)
    if negative["returncode"] != 2 or b"no password supplied" not in negative["output"]:
        raise ValueError("La prueba sin password no acredita rechazo por SCRAM.")
    positive = psql(
        docker,
        cid,
        arguments=[
            "-c",
            "SELECT json_build_object('cluster_id',(pg_control_system()).system_identifier::text,'server_version_num',current_setting('server_version_num')::int,'checksums',current_setting('data_checksums'),'user',current_user)",
        ],
        timeout=5,
    )
    value = json.loads(positive["output"]) if positive["returncode"] == 0 else {}
    if (
        value.get("server_version_num") != 160015
        or value.get("checksums") != "on"
        or value.get("user") != "agency_probe"
        or not str(value.get("cluster_id", "")).isdigit()
    ):
        raise ValueError("Readiness autenticado/version/checksums no acreditados.")
    if mode == "start2" and value["cluster_id"] != actual["start1"]["receipt"]["cluster_id"]:
        raise ValueError("Start2 no conserva el cluster inicial.")
    after = check_container(docker.inspect(cid), cid, profile)
    return {
        "success": True,
        "cid": cid,
        "container": after,
        "cluster_id": value["cluster_id"],
        "limits": effective_limits(normal, after),
        "negative_auth_verified": True,
        "process": result["process"],
    }


def apply(request):
    profile = request["profile"]
    validate_profile(profile)
    profile_sha = digest(canonical(profile))
    if profile_sha != request["profile_sha256"] or profile["sources"]["remote"] != globals().get(
        "_SQL_SOURCE_SHA256"
    ):
        raise ValueError("Perfil/helper SQL distintos del pin externo.")
    mode = request["mode"]
    if mode not in STAGES:
        raise ValueError("Etapa SQL no admitida.")
    raw = base64.b64decode(request["sdk_source_b64"], validate=True)
    if digest(raw) != SDK_SOURCE:
        raise ValueError("El helper SDK no es la fuente congelada.")
    normal = types.ModuleType("sql_fixed_sdk")
    normal.__dict__["_SOURCE_SHA256"] = SDK_SOURCE
    exec(compile(raw, "<sql-sdk-fijado>", "exec"), normal.__dict__)
    sdk_context = {"plan": profile["sdk_plan"], "layout_source_b64": request["layout_source_b64"]}
    normal.check_request(sdk_context)
    normal.verify_host(sdk_context)
    stop = mode in ("stop1", "stop2")
    if not stop:
        if digest(canonical(request["approval"])) != profile["approval_sha256"]:
            raise ValueError("Aprobación SCA distinta del perfil respaldado.")
        validate_approval(request["approval"], profile["image"]["id"])
    store = Store(normal, profile)
    armed, docker, cid = False, None, None
    deadline = time.monotonic() + BUDGETS[mode]
    try:
        os.umask(0o077)
        if mode == "prepare":
            return prepare(store, request)
        store.open()
        actual = store.receipts()
        require_backup(mode, actual, request["backed"])
        if mode in actual:
            raise ValueError("La etapa SQL ya tiene evidencia; no se permite replay.")
        os.mkdir("." + mode + "-started", 0o700, dir_fd=store.root)
        armed = True
        # La configuración Docker es privada, existente y no incorpora auth nueva.
        old = normal.Store(sdk_context)
        docker = old.docker()
        docker.heartbeat_fd = request.get("heartbeat_fd")
        if stop:
            cid = actual["create"]["receipt"]["cid"]
            if normal.read_file(store.root, "cid.txt", maximum=128).decode() != cid:
                raise ValueError("CID guardado distinto del respaldado.")
            return store.put(mode, {"success": True, **stop_owned(docker, cid, profile)})
        store.verify_files()
        if mode == "create":
            inspected = normal.json_object(
                docker.capture(["image", "inspect", profile["image"]["id"]], timeout=5)
            )
            if not isinstance(inspected, list) or len(inspected) != 1:
                raise ValueError("Imagen SQL no acreditada.")
            image = inspected[0]
            if (
                image.get("Id") != profile["image"]["id"]
                or image.get("Descriptor", {}).get("annotations", {}).get("config.digest")
                != profile["image"]["config_digest"]
                or image.get("Config", {}).get("Env") != profile["image"]["env"]
                or image.get("Config", {}).get("Labels") != profile["image"]["labels"]
            ):
                raise ValueError("La imagen/config no coincide con la aprobación final.")
            result = docker.command(create_arguments(profile, profile_sha), timeout=12)
            cid = result["output"].decode().strip()
            if result["returncode"] != 0 or not sha(cid):
                raise ValueError("Create no devolvió un CID completo.")
            write_node(store.root, "cid.txt", cid.encode())
            observed = check_container(docker.inspect(cid), cid, profile)
            if observed["running"] or observed["pid"] != 0:
                raise ValueError("Create debe permanecer sin iniciar.")
            return store.put(
                mode,
                {
                    "success": True,
                    "cid": cid,
                    "container": observed,
                    "process": result["process"],
                    "started": False,
                },
            )
        cid = actual["create"]["receipt"]["cid"]
        if normal.read_file(store.root, "cid.txt", maximum=128).decode() != cid:
            raise ValueError("CID físico distinto del respaldado.")
        if mode in ("start1", "start2"):
            fields = start_server(normal, docker, cid, profile, mode, actual, deadline)
            store.verify_files()
            return store.put(mode, fields)
        observed = check_container(docker.inspect(cid), cid, profile)
        expected = actual["start1" if mode == "verify1" else "start2"]["receipt"]["container"]
        if not observed["running"] or any(
            observed[k] != expected[k] for k in ("cid", "pid", "started_at", "created")
        ):
            raise ValueError("La instancia SQL cambió desde start respaldado.")
        limits = effective_limits(normal, observed)
        logfd = os.open(
            mode + ".log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=store.root,
        )
        with os.fdopen(logfd, "wb", buffering=0) as sink:
            result = psql(
                docker,
                cid,
                arguments=[
                    "-v",
                    "initialize=" + ("true" if mode == "verify1" else "false"),
                    "-f",
                    "/run/secrets/acceptance.sql",
                ],
                timeout=max(0.1, deadline - time.monotonic() - 10),
                sink=sink,
            )
            sink.flush()
            os.fsync(sink.fileno())
        rawlog = normal.read_file(store.root, mode + ".log", maximum=normal.MAX_OUTPUT)
        if result["returncode"] != 0:
            failure = ValueError("El SQL de aceptación terminó con error.")
            failure.process = result["process"]
            raise failure
        previous = actual["start1"]["receipt"]["cluster_id"]
        summary = validate_summary(rawlog, previous)
        after = check_container(docker.inspect(cid), cid, profile)
        if after != observed:
            raise ValueError("El CID SQL cambió durante la consulta.")
        store.verify_files()
        validate_approval(request["approval"], profile["image"]["id"])
        return store.put(
            mode,
            {
                "success": True,
                "cid": cid,
                "summary": summary,
                "limits": limits,
                "log_sha256": digest(rawlog),
                "log_bytes": len(rawlog),
                "process": result["process"],
            },
        )
    except BaseException as error:
        cleanup = None
        if armed and docker is not None and cid is not None:
            try:
                cleanup = stop_owned(docker, cid, profile)
            except BaseException as failed:
                cleanup = {"stopped": False, "error_type": type(failed).__name__}
        fields = {
            "success": False,
            "cid": cid,
            "error_type": type(error).__name__,
            "detail": str(error)[:300],
            "process": getattr(error, "process", None),
            "cleanup": cleanup,
            "partial_evidence_preserved": True,
        }
        if store.root is not None:
            try:
                return store.put(mode, fields)
            except BaseException:
                fields["receipt_written"] = False
        return envelope(
            {
                "stage": mode,
                "profile_sha256": profile_sha,
                "source_sha256": profile["sources"]["remote"],
                **fields,
            }
        )
    finally:
        store.close()


def main():
    import signal
    import sys

    def timeout(_signum, _frame):
        raise TimeoutError("Etapa SQL agotó su plazo.")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(190)
    try:
        stream = sys.stdin.buffer.raw

        def read_exact(size):
            parts = []
            while size:
                data = stream.read(size)
                if not data:
                    raise ValueError("Petición SQL truncada.")
                parts.append(data)
                size -= len(data)
            return b"".join(parts)

        header = read_exact(4)
        size = struct.unpack(">I", header)[0]
        if not 0 < size <= 512 * 1024:
            raise ValueError("Petición SQL excesiva.")
        request = json.loads(read_exact(size))
        request["heartbeat_fd"] = sys.stdin.fileno()
        mode = request["mode"]
        signal.alarm(max(1, BUDGETS.get(mode, 1) - 10))
        result = apply(request)
        print(json.dumps({"success": result["receipt"]["success"], "result": result}), flush=True)
        return 0 if result["receipt"]["success"] else 1
    except BaseException as error:
        print(
            json.dumps(
                {"success": False, "error_type": type(error).__name__, "values_hidden": True}
            ),
            flush=True,
        )
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
