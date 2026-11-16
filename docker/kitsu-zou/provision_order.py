"""Prepara en Kitsu el proyecto de una orden piloto e instala su asignación."""

import argparse
import base64
import hashlib
import http.client
import importlib.util
import io
import json
import re
import shlex
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unicodedata import normalize

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = "/home/jandrade/.local/lib/agency-audiovisual-mcp-stage-20261114-v1"
ASSIGNMENTS = "/etc/agency-production-handoff/delivery-assignments.json"
AUTOMATIC_FIXTURE_ORDER = "STUDIO_KITSU_AUTO_PROVISION_FIXTURE_ORDER_ID"
AUTOMATIC_FIXTURE_USER = "STUDIO_KITSU_AUTO_PROVISION_FIXTURE_USER_ID"
PAGE_SIZE = 32
ORDER_ERRORS = (OSError, ValueError, KeyError, http.client.HTTPException)
PAID_PLANS = ("pro", "ultra", "ilimitado")
PAID_PLAN_SQL = ",".join(f"'{plan}'" for plan in PAID_PLANS)
PENDING_ORDERS = f"""SELECT o.id,o.user_id,o.project_slug,b.confirmed_at
    FROM orders o JOIN production_briefs b ON b.order_id=o.id AND b.user_id=o.user_id
    JOIN production_dispatch d ON d.order_id=o.id AND d.received_at IS NOT NULL
    WHERE (o.plan_id IN ({PAID_PLAN_SQL}) OR
      (o.plan_id='free' AND (o.user_id=? OR (o.id=? AND o.user_id=?))))
      AND o.kind='production' AND o.provider_mode='sandbox'
      AND o.status='processing'
      AND (b.confirmed_at,o.id) > (?,?)
    ORDER BY b.confirmed_at,o.id LIMIT {PAGE_SIZE}"""


def uuid_value(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Identificador inválido")
    return value


def ensure_project(get, post, settings, save, existing=None):
    slug = "job-" + uuid_value(settings["order"]).replace("-", "")
    resolution = settings.get("resolution", "1080x1920")
    if resolution not in ("1080x1920", "2160x3840"):
        raise ValueError("Resolución de proyecto inválida")
    data = {
        "name": slug,
        "production_type": "short",
        "resolution": resolution,
        "fps": "30",
        "project_status_id": settings["status"],
    }
    matches = [row for row in get("/data/projects?name=" + slug) if row["name"] == slug]
    if len(matches) > 1 or (existing and (not matches or matches[0]["id"] != existing)):
        raise ValueError("Identidad de proyecto en conflicto")
    project = uuid_value((matches[0] if matches else post("/data/projects", data))["id"])
    route = "/data/projects/" + project
    row = get(route)
    if any(str(row[key]) != expected for key, expected in data.items()):
        raise ValueError("El proyecto existente difiere de la orden")
    if not existing:
        save(project)
    for suffix, endpoint, field, expected in (
        ("/task-types", "/settings/task-types", "task_type_id", settings["task_type"]),
        (
            "/settings/task-status",
            "/settings/task-status",
            "task_status_id",
            settings["task_status"],
        ),
    ):
        if not any(row["id"] == expected for row in get(route + suffix)):
            post(route + endpoint, {field: expected})
        if not any(row["id"] == expected for row in get(route + suffix)):
            raise ValueError("La configuración de revisión no quedó asociada")
    team = [{"id": settings["bot"], "project_role": "manager"}]

    def team_ids():
        return [{k: row[k] for k in ("id", "project_role")} for row in get(route + "/team")]

    if not team_ids():
        post(route + "/team", {"person_id": settings["bot"], "role": "manager"})
    if team_ids() != team:
        raise ValueError("El equipo del proyecto no coincide con el piloto")
    return project


def connect(values, role):
    import paramiko

    ip, prefix, fingerprint = {
        "media": ("192.168.0.85", "MEDIA_VM_SSH_", "HOST_FINGERPRINT"),
        "claw": ("192.168.0.7", "OPENCLAW_SSH_", "HOSTKEY_SHA256"),
    }[role]
    kind, encoded, *_ = values[prefix + "HOST_PUBLIC_KEY"].split()
    host = paramiko.Ed25519Key(data=base64.b64decode(encoded, validate=True))
    digest = base64.b64encode(hashlib.sha256(host.asbytes()).digest()).decode().rstrip("=")
    if kind != "ssh-ed25519" or "SHA256:" + digest != values[prefix + fingerprint]:
        raise ValueError("Identidad SSH inválida")
    key_text = base64.b64decode(values[prefix + "PRIVATE_KEY_B64"], validate=True).decode()
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(key_text))
    client = paramiko.SSHClient()
    client.get_host_keys().add(ip, kind, host)
    client.connect(
        ip,
        username=values[prefix + "USER"],
        pkey=key,
        allow_agent=False,
        look_for_keys=False,
        timeout=10,
    )
    return client


def request_kitsu(ssh, route, token=None, body=None):
    address = ("127.0.0.1", 5000)
    client = http.client.HTTPConnection(*address, timeout=25)
    client.sock = ssh.get_transport().open_channel(
        "direct-tcpip", address, ("127.0.0.1", 0), timeout=10
    )
    client.sock.settimeout(25)
    headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        method = "POST" if body is not None else "GET"
        client.request(
            method,
            route,
            body=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        with client.getresponse() as response:
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError("Respuesta de Kitsu excesiva")
            return response.status, json.loads(raw)
    finally:
        client.close()


def query_orders(values, query, params):
    account, database = values["CF_ACCOUNT_ID"], values["CF_D1_DATABASE_ID"]
    if not re.fullmatch(r"[a-f0-9]{32}", account):
        raise ValueError("Cuenta Cloudflare inválida")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{uuid_value(database)}/query"
    req = urllib.request.Request(
        url,
        data=json.dumps({"sql": query, "params": params}).encode(),
        headers={
            "Authorization": "Bearer " + values["CF_PLATFORM_API_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        page = json.load(response)
    if not page.get("success") or not page["result"][0].get("success"):
        raise ValueError("D1 no pudo consultar las órdenes")
    return page["result"][0]["results"]


def load_order(values, identity):
    rows = query_orders(
        values,
        f"""SELECT user_id,project_slug,plan_id,output_resolution FROM orders WHERE id=? AND kind='production'
        AND provider_mode='sandbox' AND plan_id IN ('free',{PAID_PLAN_SQL})
        AND status IN ('queued','processing')""",
        [identity],
    )
    if len(rows) != 1 or rows[0]["project_slug"] != "job-" + identity.replace("-", ""):
        raise ValueError("La orden no está disponible para aprovisionamiento")
    order = rows[0]
    requested = order.pop("output_resolution")
    resolution = "1080p"
    if order["plan_id"] in {"ultra", "ilimitado"}:
        if requested not in {"1080p", "4k"}:
            raise ValueError("La orden no tiene una resolución de salida válida")
        resolution = requested
    order["resolution"] = {"1080p": "1080x1920", "4k": "2160x3840"}[resolution]
    return order


def run_remote(ssh, command, source, data):
    i, o, _ = ssh.exec_command(command + " -I -B -c " + shlex.quote(source), timeout=65)
    i.write(json.dumps(data))
    i.channel.shutdown_write()
    raw = o.read(65537)
    if o.channel.recv_exit_status() or len(raw) > 65536:
        raise ValueError("Falló el paso de aprovisionamiento en OpenClaw")
    return json.loads(raw)


KITSU_TASK = """
import json,os,sys
from pathlib import Path
x=json.load(sys.stdin)
sys.path.insert(0,str(Path(sys.prefix).parent/'mcp_servers'))
from call_stdio import ENV_BY_SERVER,runtime_settings
from kitsu_review import create_deliverable,create_preview
os.environ.update({k:v for k,v in runtime_settings().items() if k in ENV_BY_SERVER['mcp-kitsu-review']})
os.environ['AGENCY_MEDIA_PROJECTS']=x['slug']
os.environ['KITSU_REVIEW_PROJECTS']=json.dumps({x['project']:x['slug']})
if x.get('task'):
 print(json.dumps(create_preview(x['project'],x['task'],1)))
else:
 print(json.dumps(create_deliverable(x['project'],x['title'])))
"""

READ_ASSIGNMENTS = """
import base64,hashlib,json,sys
from pathlib import Path
p=Path(json.load(sys.stdin)['path'])
b=p.read_bytes()
assert len(b)<=65536
print(json.dumps({'sha256':hashlib.sha256(b).hexdigest(),'data':base64.b64encode(b).decode()}))
"""

WRITE_ASSIGNMENTS = """
import base64,fcntl,hashlib,json,os,stat,sys,tempfile
from pathlib import Path
x=json.load(sys.stdin)
p=Path(x['path']); lock=p.with_name(p.name+'.lock')
fd=os.open(lock,os.O_CREAT|os.O_NOFOLLOW|os.O_RDWR,0o600)
with os.fdopen(fd,'rb') as lock_file:
 fcntl.flock(lock_file,fcntl.LOCK_EX)
 m=p.lstat()
 assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and stat.S_IMODE(m.st_mode)==0o400
 old=p.read_bytes(); new=base64.b64decode(x['data'],validate=True)
 assert len(new)<=65536 and hashlib.sha256(new).hexdigest()==x['sha256']
 if old!=new:
  assert hashlib.sha256(old).hexdigest()==x['previous']
  out,tmp=tempfile.mkstemp(dir=p.parent,prefix='.asignacion-')
  try:
   with os.fdopen(out,'wb') as f:
    os.fchmod(f.fileno(),0o400);f.write(new);f.flush();os.fsync(f.fileno())
   os.replace(tmp,p)
  finally:
   if os.path.exists(tmp):os.unlink(tmp)
print(json.dumps({'verified':p.read_bytes()==new}))
"""


def prepare_admin(ssh, values, store, prefix, settings):
    status, session = request_kitsu(
        ssh,
        "/auth/login",
        body={
            "email": values["KITSU_ADMIN_EMAIL"],
            "password": values["KITSU_ADMIN_PASSWORD"],
        },
    )
    if status != 200:
        raise ValueError("No se pudo iniciar la sesión administrativa")
    access, refresh = session["access_token"], session["refresh_token"]
    try:
        store(
            {
                prefix + "ADMIN_ACCESS": access,
                prefix + "ADMIN_REFRESH": refresh,
                prefix + "ADMIN_REVOKED": "false",
            }
        )
        if (
            session["user"]["role"] != "admin"
            or session["user"]["email"] != values["KITSU_ADMIN_EMAIL"]
        ):
            raise ValueError("Identidad administrativa inesperada")

        def call(route, body=None):
            code, result = request_kitsu(ssh, route, access, body)
            if code != (200 if body is None else 201):
                raise ValueError(f"Kitsu rechazó una operación con HTTP {code}")
            return result

        return ensure_project(
            call,
            call,
            settings,
            lambda pid: store({prefix + "PROJECT_ID": pid}),
            existing=values.get(prefix + "PROJECT_ID"),
        )
    finally:
        result, state = request_kitsu(ssh, "/auth/logout", access)
        active, _ = request_kitsu(ssh, "/auth/authenticated", access)
        renewed, unexpected = request_kitsu(ssh, "/auth/refresh-token", refresh)
        if renewed == 200 and unexpected.get("access_token"):
            store({prefix + "UNEXPECTED_ACCESS": unexpected["access_token"]})
            request_kitsu(ssh, "/auth/logout", unexpected["access_token"])
        if result != 200 or state.get("logout") is not True or active != 401 or renewed != 401:
            raise ValueError("No se pudo verificar la revocación de la sesión administrativa")
        store({prefix + "ADMIN_REVOKED": "true"})


def pending_orders(values):
    scope = [
        uuid_value(values["SOFIA_OWNER_USER_ID"]),
        values.get(AUTOMATIC_FIXTURE_ORDER),
        values.get(AUTOMATIC_FIXTURE_USER),
    ]
    cursor = (-1, "")
    while True:
        rows = query_orders(values, PENDING_ORDERS, [*scope, *cursor])
        yield from rows
        if len(rows) < PAGE_SIZE:
            return
        next_cursor = rows[-1]["confirmed_at"], rows[-1]["id"]
        if next_cursor <= cursor:
            raise ValueError("La consulta de órdenes no avanzó")
        cursor = next_cursor


def provision_pending(vault, values):
    summary = {"elegibles": 0, "aprovisionadas": 0, "fallidas": 0}
    for row in pending_orders(values):
        summary["elegibles"] += 1
        try:
            identity = uuid_value(row["id"])
            confirmed = datetime.fromtimestamp(row["confirmed_at"], UTC)
            title = f"Producción Studio {confirmed:%Y-%m-%d} · {identity[:8]}"
            summary["aprovisionadas"] += provision(vault, values, identity, title, automatic=True)
        except ORDER_ERRORS:
            summary["fallidas"] += 1
    return summary


def authorized(values, identity, user, automatic, plan):
    if plan in PAID_PLANS or user == values["SOFIA_OWNER_USER_ID"]:
        return True
    if automatic:
        return (
            values.get(AUTOMATIC_FIXTURE_ORDER) == identity
            and values.get(AUTOMATIC_FIXTURE_USER) == user
        )
    return values.get("STUDIO_KITSU_SYNTHETIC_PROVISION_ORDER_ID") == identity


def provision(vault, values, identity, title, *, automatic=False):
    title = normalize("NFC", title)
    if (
        not 1 <= len(title) <= 120
        or title != title.strip()
        or re.search(r"[\x00-\x1f\x7f/\\]", title)
    ):
        raise ValueError("Título de entrega inválido")
    order = load_order(values, identity)
    if not authorized(values, identity, order["user_id"], automatic, order["plan_id"]):
        raise ValueError("La orden no pertenece al piloto autorizado")
    prefix = "STUDIO_KITSU_ORDER_" + identity.replace("-", "").upper() + "_"
    previous_kit = values.get(prefix + "KIT_ID")
    with connect(values, "claw") as claw:
        privileged = "sudo -n /usr/bin/python3"
        saved = run_remote(claw, privileged, READ_ASSIGNMENTS, {"path": ASSIGNMENTS})
        assignments = json.loads(base64.b64decode(saved["data"], validate=True))
        existing = assignments.get(identity)
        if existing is not None:
            expected = {
                "id": uuid_value(previous_kit),
                "project": values[prefix + "PROJECT_ID"],
                "task": values[prefix + "TASK_ID"],
                "title": existing["title"] if automatic else title,
            }
            if existing != expected:
                raise ValueError("La asignación previa difiere de la solicitud")
            print("La orden ya tiene su asignación verificada.")
            return False
        with connect(values, "media") as media:
            status, sample = request_kitsu(
                media,
                "/data/projects/"
                + uuid_value(values["MEDIA_KITSU_PRODUCTION_AUTO_DELIVERY_PROJECT_ID"]),
                values["KITSU_API_TOKEN"],
            )
            if status != 200 or sample["project_status_id"] not in values.values():
                raise ValueError("Estado de proyecto sin respaldo")
            settings = {
                "order": identity,
                "resolution": order["resolution"],
                "status": uuid_value(sample["project_status_id"]),
                "task_type": uuid_value(values["KITSU_REVIEW_TASK_TYPE_ID"]),
                "task_status": uuid_value(values["KITSU_REVIEW_TASK_STATUS_ID"]),
                "bot": uuid_value(values["KITSU_REVIEW_BOT_ID"]),
            }
            project = prepare_admin(media, values, vault.update, prefix, settings)
        cmd = RUNTIME + "/venv/bin/python"
        data = {"project": project, "slug": order["project_slug"], "title": title}
        task = run_remote(claw, cmd, KITSU_TASK, data)
        if task["project_id"] != project:
            raise ValueError("La tarea pertenece a otro proyecto")
        task_id = uuid_value(task["task_id"])
        vault.update({prefix + "TASK_ID": task_id, prefix + "EDIT_ID": uuid_value(task["edit_id"])})
        preview = run_remote(claw, cmd, KITSU_TASK, {**data, "task": task_id})
        if (preview["project_id"], preview["task_id"], preview["revision"]) != (
            project,
            task_id,
            1,
        ):
            raise ValueError("La revisión pertenece a otra tarea")
        kit = str(uuid.uuid4()) if previous_kit is None else uuid_value(previous_kit)
        assignments[identity] = {
            "project": project,
            "task": task_id,
            "id": kit,
            "title": title,
        }
        raw = json.dumps(assignments, ensure_ascii=False, sort_keys=True).encode()
        digest = hashlib.sha256(raw).hexdigest()
        vault.update(
            {
                prefix + "KIT_ID": kit,
                prefix + "PREVIEW_ID": uuid_value(preview["preview_id"]),
                prefix + "COMMENT_ID": uuid_value(preview["comment_id"]),
                prefix + "ASSIGNMENTS_SHA256": digest,
            }
        )
        outcome = run_remote(
            claw,
            privileged,
            WRITE_ASSIGNMENTS,
            {
                "path": ASSIGNMENTS,
                "sha256": digest,
                "previous": saved["sha256"],
                "data": base64.b64encode(raw).decode(),
            },
        )
        if not outcome["verified"]:
            raise ValueError("El registro de asignaciones no coincide")
    print("Proyecto, tarea y revisión asignados al publicador de Studio.")
    return True


@contextmanager
def exclusive_run(vault):
    import msvcrt

    directory, acl = vault.private_temp_directory()
    path = directory / "kitsu-aprovisionamiento.lock"
    with path.open("a+b") as handle:
        acl.check_access(path, private=True)
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def read_vault(vault):
    values, errors = vault.parse_env((ROOT / ".env").read_text(encoding="utf-8-sig"))
    if errors:
        raise ValueError("Bóveda inválida")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--pendientes", action="store_true", help="Aprovisiona órdenes listas del piloto."
    )
    mode.add_argument(
        "--listar-pendientes",
        action="store_true",
        help="Consulta sólo el número de órdenes listas.",
    )
    parser.add_argument("order", nargs="?", type=uuid_value)
    parser.add_argument("title", nargs="?")
    args = parser.parse_args()
    if args.pendientes or args.listar_pendientes:
        if args.order or args.title is not None:
            parser.error("El modo automático no recibe orden ni título.")
    elif not args.order or args.title is None:
        parser.error("Indica la orden y el título.")

    spec = importlib.util.spec_from_file_location("vault", ROOT / ".tools/vault_updates.py")
    vault = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vault)
    if args.listar_pendientes:
        print(json.dumps({"elegibles": sum(1 for _ in pending_orders(read_vault(vault)))}))
        return 0
    with exclusive_run(vault):
        values = read_vault(vault)
        if args.order:
            provision(vault, values, args.order, args.title)
            return 0
        report = provision_pending(vault, values)
        print(json.dumps(report))
        return int(report["fallidas"] > 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ORDER_ERRORS as error:
        message = str(error) if type(error) is ValueError else type(error).__name__
        raise SystemExit("Aprovisionamiento detenido. " + message) from None
