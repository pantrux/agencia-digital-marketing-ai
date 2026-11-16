"""Opera el ensayo acotado de migración y autenticación de Immich."""

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

PROJECT = "agency-media-immich-trial"
OWNER = "agencia-digital-marketing-ai:immich-server-trial:v1"
WORK = Path("/opt/media-platform/immich-server-trial-v1")
STATE = Path("/var/lib/media-platform/immich-server-trial-v1")
MEDIA = STATE / "media"
NETWORKS = ("agency-immich-trial-ml-v1", "agency-immich-trial-egress-v1")
BRIDGE = "br-imml-v1"
IPT = "/usr/sbin/iptables"
IP6T = "/usr/sbin/ip6tables"
SERVICES = ("immich-valkey", "immich-ml-auth", "immich-server")
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
COMPOSE = [
    "compose",
    "--project-name",
    PROJECT,
    "--env-file",
    "/dev/null",
    "-f",
    str(WORK / "compose.yml"),
]
ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "DOCKER_CONFIG": str(WORK / "docker-client"),
    "MEDIA_POSTGRES_IMMICH_BACKUP_NETWORK": "agency-media-immich-db-private-v1",
    "MEDIA_IMMICH_TRIAL_ML_NETWORK": NETWORKS[0],
    "MEDIA_IMMICH_TRIAL_ML_EGRESS_NETWORK": NETWORKS[1],
    "MEDIA_IMMICH_TRIAL_MEDIA_PATH": str(MEDIA),
    "MEDIA_IMMICH_TRIAL_CONFIG_FILE": str(WORK / "functional.json"),
    "MEDIA_IMMICH_TRIAL_DB_PASSWORD_FILE": str(WORK / "secrets/db"),
    "MEDIA_IMMICH_TRIAL_REDIS_PASSWORD_FILE": str(WORK / "secrets/redis"),
    "MEDIA_IMMICH_TRIAL_REDIS_ACL_FILE": str(WORK / "secrets/redis.acl"),
    "MEDIA_IMMICH_TRIAL_GPU_BEARER_FILE": str(WORK / "secrets/gpu"),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def source(entry):
    raw = base64.b64decode(entry["raw"], validate=True)
    assert sha(raw) == entry["sha256"]
    return raw


def run(args, seconds=15, *, data=None, check=True):
    proc = subprocess.run(
        args, env=ENV, input=data, capture_output=True, timeout=seconds, check=False
    )
    assert len(proc.stdout) + len(proc.stderr) <= 1024**2
    if check and proc.returncode:
        raise RuntimeError(f"COMMAND_{Path(args[0]).name}_{proc.returncode}")
    return proc


def docker(args, seconds=15, **kwargs):
    return run(DOCKER + args, seconds, **kwargs)


def json_docker(args):
    return json.loads(docker(args).stdout)


def iptables_rules():
    match = ["-m", "comment", "--comment", OWNER]
    established = ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"]
    return [
        (
            IPT,
            "DOCKER-USER",
            [
                "-i",
                BRIDGE,
                "!",
                "-o",
                BRIDGE,
                "-p",
                "tcp",
                "-d",
                "192.168.0.6/32",
                "--dport",
                "3003",
                *match,
                "-j",
                "RETURN",
            ],
        ),
        (IPT, "DOCKER-USER", ["-i", BRIDGE, "!", "-o", BRIDGE, *match, "-j", "DROP"]),
        (
            IPT,
            "DOCKER-USER",
            ["!", "-i", BRIDGE, "-o", BRIDGE, *established, *match, "-j", "RETURN"],
        ),
        (IPT, "DOCKER-USER", ["!", "-i", BRIDGE, "-o", BRIDGE, *match, "-j", "DROP"]),
        (IPT, "INPUT", ["-i", BRIDGE, *established, *match, "-j", "RETURN"]),
        (IPT, "INPUT", ["-i", BRIDGE, *match, "-j", "DROP"]),
        (IP6T, "DOCKER-USER", ["-i", BRIDGE, *match, "-j", "DROP"]),
        (IP6T, "DOCKER-USER", ["-o", BRIDGE, *match, "-j", "DROP"]),
        (IP6T, "INPUT", ["-i", BRIDGE, *match, "-j", "DROP"]),
    ]


def firewall(*, install=False):
    rules = iptables_rules()
    for binary in (IPT, IP6T):
        assert (
            run([binary, "-w", "3", "-S", "FORWARD"]).stdout.splitlines()[1]
            == b"-A FORWARD -j DOCKER-USER"
        )
        for chain in ("DOCKER-USER", "INPUT"):
            expected = [args for program, kind, args in rules if (program, kind) == (binary, chain)]
            rows = run([binary, "-w", "3", "-S", chain]).stdout.splitlines()[1:]
            keep = [r for r in rows if OWNER.encode() not in r]
            assert all(b"br-mpt-fn" in r and b"moneyprinter-functional:v1" in r for r in keep)
            if install:
                assert rows == keep
                for args in expected:
                    run([binary, "-w", "3", "-A", chain, *args])
            else:
                assert rows[: len(keep)] == keep and len(rows) == len(keep) + len(expected)
                for args in expected:
                    run([binary, "-w", "3", "-C", chain, *args])


def networks(ids):
    assert set(ids) == set(NETWORKS)
    for name, net_id in ids.items():
        row = json_docker(["network", "inspect", net_id])[0]
        assert row["Name"] == name and row["Id"] == net_id and row["Driver"] == "bridge"
        assert row["Labels"]["agency.owner"] == OWNER and not row["EnableIPv6"]
        assert row["Internal"] == (name == NETWORKS[0])
        if name == NETWORKS[1]:
            assert row["Options"]["com.docker.network.bridge.name"] == BRIDGE


def prepare(pg, req, out):
    assert not os.path.lexists(WORK) and not os.path.lexists(STATE)
    assert not docker(
        ["ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT]
    ).stdout.strip()
    for name in NETWORKS:
        assert not docker(["network", "ls", "-q", "--filter", "name=^" + name + "$"]).stdout.strip()
    assert not Path("/sys/class/net", BRIDGE).exists()
    files = req["files"]
    assert set(files) == {"compose.yml", "functional.json", "api_probe.js"}
    db, redis, gpu = (req["secrets"][key] for key in ("db", "redis", "gpu"))
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{48,80}", key) for key in (db, redis))
    assert re.fullmatch(r"[!-~]{16,256}", gpu)
    out["step"] = "staging"
    out["directories"] = {
        str(WORK): pg.mkdir(WORK),
        str(STATE): pg.mkdir(STATE),
        str(MEDIA): pg.mkdir(MEDIA, 1000, 1000),
    }
    pg.mkdir(WORK / "docker-client")
    pg.mkdir(WORK / "secrets")
    pg.write(WORK / ".agency-owner", OWNER.encode(), 0o400)
    for name, entry in files.items():
        pg.write(WORK / name, source(entry), 0o444 if name == "functional.json" else 0o400)
    acl = "user default reset off\nuser immich_trial on #" + sha(redis.encode()) + " ~* &* +@all\n"
    for name, value, uid in (
        ("db", db, 1000),
        ("redis", redis, 1000),
        ("redis.acl", acl, 1000),
        ("gpu", gpu, 65532),
    ):
        pg.write(WORK / "secrets" / name, value.encode(), 0o400, uid, uid)
    out["step"] = "compose"
    docker(COMPOSE + ["config", "--quiet"])
    out["step"] = "firewall"
    firewall(install=True)
    firewall()
    out["step"] = "networks"
    out["networks"] = {}
    for name, options in (
        (NETWORKS[0], ["--internal"]),
        (NETWORKS[1], ["--opt", "com.docker.network.bridge.name=" + BRIDGE]),
    ):
        net = (
            docker(
                [
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    "--ipv6=false",
                    *options,
                    "--label",
                    "agency.owner=" + OWNER,
                    name,
                ]
            )
            .stdout.decode()
            .strip()
        )
        out["networks"][name] = net
    networks(out["networks"])


def check_stage(pg, req):
    for path, uid in ((WORK, 0), (STATE, 0), (MEDIA, 1000)):
        assert pg.identity(path, uid, uid) == req["directories"][str(path)]
    assert pg.read(WORK / ".agency-owner", 0o400)[0] == OWNER.encode()
    for name, expected in req["file_hashes"].items():
        assert (
            sha(pg.read(WORK / name, 0o444 if name == "functional.json" else 0o400)[0]) == expected
        )
    firewall()
    networks(req["networks"])


def inspect(req, svc, cid):
    assert re.fullmatch(r"[a-f0-9]{64}", cid)
    row = json_docker(["container", "inspect", cid])[0]
    cfg, hc = row["Config"], row["HostConfig"]
    assert row["Id"] == cid and row["Image"] == req["images"][svc]
    assert cfg["Labels"]["agency.owner"] == OWNER
    assert cfg["Labels"]["com.docker.compose.project"] == PROJECT
    assert cfg["Labels"]["com.docker.compose.service"] == svc
    assert cfg["User"] == ("65532:65532" if svc == "immich-ml-auth" else "1000:1000")
    assert hc["ReadonlyRootfs"] and hc["CapDrop"] == ["ALL"] and not hc["Privileged"]
    assert not hc["PortBindings"] and not hc["PublishAllPorts"]
    assert hc["SecurityOpt"] == ["no-new-privileges:true"] and hc["RestartPolicy"]["Name"] == "no"
    assert hc["Memory"] == hc["MemorySwap"] and 0 < hc["PidsLimit"] <= 128
    data = ENV["MEDIA_POSTGRES_IMMICH_BACKUP_NETWORK"]
    desired = {
        "immich-server": {data, NETWORKS[0]},
        "immich-valkey": {data},
        "immich-ml-auth": set(NETWORKS),
    }[svc]
    assert set(row["NetworkSettings"]["Networks"]) == desired
    expected_binds = {
        "immich-server": {
            "/data": (str(MEDIA), True),
            "/run/immich/functional.json": (str(WORK / "functional.json"), False),
            "/run/secrets/immich_db_password": (str(WORK / "secrets/db"), False),
            "/run/secrets/immich_redis_password": (str(WORK / "secrets/redis"), False),
        },
        "immich-valkey": {
            "/run/secrets/immich_redis_acl": (str(WORK / "secrets/redis.acl"), False),
            "/run/secrets/immich_redis_password": (str(WORK / "secrets/redis"), False),
        },
        "immich-ml-auth": {"/run/secrets/gpu_bridge_bearer": (str(WORK / "secrets/gpu"), False)},
    }[svc]
    assert {
        m["Destination"]: (m["Source"], m["RW"]) for m in row["Mounts"] if m["Type"] == "bind"
    } == expected_binds
    assert all(m["Type"] in {"bind", "tmpfs"} for m in row["Mounts"])
    return row


def create(req, out):
    result = docker(COMPOSE + ["create", "--pull", "never"], 45, check=False)
    out["create_exit"] = result.returncode
    out["containers"] = {}
    for svc in SERVICES:
        cid = docker(COMPOSE + ["ps", "-a", "-q", svc]).stdout.decode().strip()
        if cid:
            out["containers"][svc] = {"cid": cid}
            row = inspect(req, svc, cid)
            assert row["State"]["Status"] == "created" and row["State"]["Pid"] == 0
            out["containers"][svc]["created"] = row["Created"]
    assert result.returncode == 0 and set(out["containers"]) == set(SERVICES)


def healthy(reader, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        row = reader()
        assert row["State"]["Running"] and row["RestartCount"] == 0
        if row["State"].get("Health", {}).get("Status") == "healthy":
            return
        time.sleep(1)
    raise TimeoutError("HEALTH")


def db_query(pg, req, query):
    cmd = ["exec", "-i", "--user", "70:70", req["db"]["cid"], *pg.PSQL]
    return pg.docker(cmd, 30, data=query.encode()).stdout


def create_role(pg, req):
    status = json.loads(db_query(pg, req, pg.VERIFY))
    assert status == req["initial_postgres"]
    query = """
SELECT (SELECT datdba = (SELECT oid FROM pg_roles WHERE rolname='postgres')
        FROM pg_database WHERE datname='immich')
  AND NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='immich_trial')
  AND (SELECT count(*) FROM pg_available_extensions WHERE name IN ('vector','vchord')
       AND installed_version=default_version) = 2;
"""
    assert db_query(pg, req, query).strip() == b"t"
    password = pg.read(WORK / "secrets/db", 0o400, 1000, 1000)[0]
    salt = bytes.fromhex(req["db_salt_hex"])
    assert len(salt) == 16
    derived = hashlib.pbkdf2_hmac("sha256", password, salt, 4096)
    stored = hashlib.sha256(hmac.digest(derived, b"Client Key", "sha256")).digest()
    server = hmac.digest(derived, b"Server Key", "sha256")
    salt_b64, stored_b64, server_b64 = (
        base64.b64encode(x).decode() for x in (salt, stored, server)
    )
    verifier = f"SCRAM-SHA-256$4096:{salt_b64}${stored_b64}:{server_b64}"
    db_query(
        pg,
        req,
        f"""
BEGIN;
CREATE ROLE immich_trial LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD '{verifier}';
ALTER DATABASE immich OWNER TO immich_trial;
COMMIT;
""",
    )
    query = """
SELECT has_schema_privilege('immich_trial','public','CREATE')
  AND (SELECT rolcanlogin AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
       FROM pg_roles WHERE rolname='immich_trial')
  AND (SELECT pg_get_userbyid(datdba)='immich_trial' FROM pg_database WHERE datname='immich');
"""
    assert db_query(pg, req, query).strip() == b"t"


def migration(pg, req):
    raw = db_query(
        pg,
        req,
        """
SELECT json_build_object(
  'migrations', (SELECT count(*) FROM public.kysely_migrations),
  'latest', (SELECT name FROM public.kysely_migrations ORDER BY timestamp DESC, name DESC LIMIT 1),
  'owned_tables', (SELECT count(*) FROM pg_tables WHERE schemaname='public'
      AND tablename IN ('user','asset','smart_search') AND tableowner='immich_trial'),
  'users', (SELECT count(*) FROM public."user"),
  'assets', (SELECT count(*) FROM public.asset)
);
""",
    )
    result = json.loads(raw)
    assert result["migrations"] > 0 and result["owned_tables"] == 3
    return result


def api(pg, req, parameters):
    code = pg.read(WORK / "api_probe.js", 0o400)[0].decode()
    cid = req["containers"]["immich-server"]["cid"]
    proc = docker(
        ["exec", "-i", "--user", "1000:1000", cid, "node", "-e", code],
        95,
        data=json.dumps(parameters).encode(),
        check=False,
    )
    result = json.loads(proc.stdout)
    assert result["success"] == (proc.returncode == 0) and not proc.stderr
    return result


def stop(pg, req, out):
    out["stopped"] = {}
    pairs = [(svc, req["containers"][svc]["cid"]) for svc in reversed(SERVICES)]
    pairs.append(("postgres-immich", req["db"]["cid"]))
    for svc, cid in pairs:
        try:
            row = pg.inspect(req["db"], cid) if svc == "postgres-immich" else inspect(req, svc, cid)
            if row["State"]["Running"]:
                docker(["stop", "--time", "30", cid], 42)
            row = pg.inspect(req["db"], cid) if svc == "postgres-immich" else inspect(req, svc, cid)
            status = row["State"]
            out["stopped"][svc] = {
                "status": status["Status"],
                "pid": status["Pid"],
                "exit": status["ExitCode"],
            }
        except Exception as error:
            out["stopped"][svc] = {"error": type(error).__name__}
    assert all(x.get("pid") == 0 for x in out["stopped"].values())


def start_login(pg, req, out):
    for svc, item in req["containers"].items():
        row = inspect(req, svc, item["cid"])
        assert row["State"]["Status"] == "created" and row["Created"] == item["created"]
    db = req["db"]["cid"]
    state = pg.inspect(req["db"], db)["State"]
    assert state["Status"] == "exited" and state["Pid"] == 0 and state["ExitCode"] == 0
    try:
        out["step"] = "db_start"
        docker(["start", db], 30)
        healthy(lambda: pg.inspect(req["db"], db), 55)
        out["step"] = "db_role"
        create_role(pg, req)
        for svc in SERVICES:
            out["step"] = svc
            cid = req["containers"][svc]["cid"]
            docker(["start", cid], 35)
            healthy(lambda s=svc, c=cid: inspect(req, s, c), 210 if svc == "immich-server" else 35)
        out["step"] = "migrations"
        out["schema"] = migration(pg, req)
        assert out["schema"]["users"] == out["schema"]["assets"] == 0
        out["step"] = "login"
        cid = req["containers"]["immich-server"]["cid"]
        out["server_started_at"] = inspect(req, "immich-server", cid)["State"]["StartedAt"]
        out["login"] = api(pg, req, {"action": "login", **req["admin"]})
        assert out["login"]["success"]
    except Exception:
        for svc in SERVICES:
            cid = req["containers"][svc]["cid"]
            p = docker(["logs", "--tail", "80", cid], 10, check=False)
            raw = p.stdout + p.stderr
            pg.write(WORK / (svc + "-trial.log"), raw, 0o600)
            safe = raw.lower()
            out.setdefault("diagnostics", {})[svc] = {
                "sha256": sha(raw),
                "categories": [
                    word
                    for word in (
                        "permission denied",
                        "not found",
                        "econnrefused",
                        "readonly",
                        "authentication",
                        "migration",
                        "error",
                    )
                    if word.encode() in safe
                ],
            }
        stop(pg, req, out)
        raise


def verify(pg, req, out):
    item = req["containers"]["immich-server"]
    cid = item["cid"]
    row = inspect(req, "immich-server", cid)
    assert row["Created"] == item["created"] and row["State"]["Running"]
    assert row["State"]["StartedAt"] == req["server_started_at"]
    try:
        out["step"] = "api"
        out["api"] = api(pg, req, {"action": "verify", **req["auth"]})
        assert out["api"]["success"]
        out["step"] = "migrated_backup"
        docker(["stop", "--time", "30", cid], 42)
        out["schema"] = migration(pg, req)
        assert out["schema"]["users"] == 1 and out["schema"]["assets"] == 0
        dest = pg.STATE / "capture-migrated-v1"
        raw = run(
            [
                "/usr/bin/bash",
                str(pg.WORK / "capture-backup.sh"),
                req["db"]["cid"],
                str(pg.DATA),
                str(dest),
            ],
            340,
        ).stdout
        record = json.loads(raw)
        path = dest / "postgres-immich.sql.zst"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            node = os.fstat(stream.fileno())
            assert stat.S_ISREG(node.st_mode) and node.st_nlink == 1
            assert (node.st_uid, node.st_gid, stat.S_IMODE(node.st_mode)) == (0, 0, 0o600)
            assert 0 < node.st_size <= 2 * 1024**3
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
            assert (after.st_size, after.st_mtime_ns) == (node.st_size, node.st_mtime_ns)
        assert record["sha256"] == digest and record["bytes"] == node.st_size
        assert record["cid"] == req["db"]["cid"]
        out["capture"] = {
            **record,
            "file": {"device": node.st_dev, "inode": node.st_ino},
            "directory": pg.identity(dest),
        }
    finally:
        stop(pg, req, out)


def main():
    out = {"success": False, "owner": OWNER, "step": "host", "production_approved": False}
    try:
        req = json.loads(sys.stdin.buffer.read(256 * 1024))
        action = req["action"]
        out["action"] = action
        pg = types.ModuleType("postgres_immich_backup")
        exec(compile(source(req["pg_source"]), "<postgres-immich-backup>", "exec"), pg.__dict__)
        pg.host(req["db"])
        pg.staged(req["db"])
        if action == "prepare":
            prepare(pg, req, out)
        else:
            check_stage(pg, req)
            if action == "create":
                out["step"] = "create"
                create(req, out)
            elif action == "start_login":
                start_login(pg, req, out)
            else:
                assert action == "verify"
                verify(pg, req, out)
        out["success"] = True
    except Exception as error:
        out["error"] = type(error).__name__
        frame = error.__traceback__
        while frame.tb_next:
            frame = frame.tb_next
        out["line"] = frame.tb_lineno
    print(json.dumps(out, sort_keys=True))
    return 0 if out["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
