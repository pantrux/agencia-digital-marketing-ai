import base64
import hashlib
import importlib.util
import json
import sqlite3
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "docker/kitsu-zou/provision_order.py"


def load_provisioner():
    spec = importlib.util.spec_from_file_location("provision_order", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reconcilia_proyecto_y_permisos_sin_repetir_altas():
    module = load_provisioner()
    project = "00000000-0000-4000-8000-000000000002"
    settings = {
        "order": "00000000-0000-4000-8000-000000000001",
        "status": "00000000-0000-4000-8000-000000000003",
        "task_type": "00000000-0000-4000-8000-000000000004",
        "task_status": "00000000-0000-4000-8000-000000000005",
        "bot": "00000000-0000-4000-8000-000000000006",
    }
    writes = []
    rows = {"/task-types": [], "/settings/task-status": [], "/team": []}
    production = {}
    saved = []

    def get(route):
        if "?name=" in route:
            return [production] if production else []
        suffix = route.removeprefix("/data/projects/" + project)
        return rows[suffix] if suffix else production

    def post(route, body):
        writes.append(route)
        if route == "/data/projects":
            production.update(body, id=project)
            return production
        if route.endswith("/task-types"):
            rows["/task-types"].append({"id": body["task_type_id"]})
        elif route.endswith("/task-status"):
            rows["/settings/task-status"].append({"id": body["task_status_id"]})
        else:
            rows["/team"].append({"id": body["person_id"], "project_role": body["role"]})

    assert module.ensure_project(get, post, settings, saved.append) == project
    assert len(writes) == 4
    assert saved == [project]
    assert module.ensure_project(get, post, settings, saved.append, existing=project) == project
    assert len(writes) == 4
    production["fps"] = "25"
    with pytest.raises(ValueError):
        module.ensure_project(get, post, settings, saved.append, existing=project)
    assert len(writes) == 4


def test_selecciona_ordenes_del_piloto_y_planes_pagados_de_ensayo_con_crudo_y_brief():
    module = load_provisioner()
    owner = "00000000-0000-4000-8000-000000000010"
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE orders (
                id TEXT PRIMARY KEY, user_id TEXT, project_slug TEXT,
                kind TEXT, provider_mode TEXT, plan_id TEXT, status TEXT
            );
            CREATE TABLE production_briefs (order_id TEXT, user_id TEXT, confirmed_at INTEGER);
            CREATE TABLE production_dispatch (order_id TEXT, received_at INTEGER);
        """)
        ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 9)]
        for ix, identity in enumerate(ids):
            db.execute(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identity,
                    identity if ix in (4, 5, 6) else owner,
                    "job-" + identity.replace("-", ""),
                    "production",
                    "production" if ix == 5 else "sandbox",
                    "ultra" if ix in (4, 5) else "free",
                    "queued" if ix == 3 else "processing",
                ),
            )
            if ix != 1:
                db.execute(
                    "INSERT INTO production_briefs VALUES (?, ?, 1794665400)",
                    (identity, identity if ix in (4, 5, 6, 7) else owner),
                )
            if ix != 2:
                db.execute("INSERT INTO production_dispatch VALUES (?, 1794665300)", (identity,))
        db.row_factory = sqlite3.Row
        rows = db.execute(module.PENDING_ORDERS, (owner, None, None, -1, "")).fetchall()
        exact = db.execute(module.PENDING_ORDERS, (owner, ids[6], ids[6], -1, "")).fetchall()
        wrong_order = db.execute(module.PENDING_ORDERS, (owner, ids[1], ids[6], -1, "")).fetchall()
        following = db.execute(
            module.PENDING_ORDERS, (owner, ids[6], ids[6], 1794665400, ids[0])
        ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "id": ids[0],
            "user_id": owner,
            "project_slug": "job-" + ids[0].replace("-", ""),
            "confirmed_at": 1794665400,
        },
        {
            "id": ids[4],
            "user_id": ids[4],
            "project_slug": "job-" + ids[4].replace("-", ""),
            "confirmed_at": 1794665400,
        },
    ]
    assert [row["id"] for row in exact] == [ids[0], ids[4], ids[6]]
    assert [row["id"] for row in wrong_order] == [ids[0], ids[4]]
    assert [row["id"] for row in following] == [ids[4], ids[6]]


def test_recorre_siguiente_pagina_y_procesa_tras_un_error(monkeypatch):
    module = load_provisioner()
    owner = "00000000-0000-4000-8000-000000000010"
    ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 34)]
    rows = [{"id": key, "confirmed_at": 1794665400} for key in ids]
    cursors = []

    def query(_values, _sql, params):
        cursors.append(params[-2:])
        return [row for row in rows if (row["confirmed_at"], row["id"]) > tuple(params[-2:])][:32]

    def provision(_vault, _values, identity, _title, *, automatic):
        assert automatic
        if identity == ids[0]:
            raise ValueError("El proyecto de prueba tiene una revisión en conflicto")
        return identity == ids[-1]

    monkeypatch.setattr(module, "query_orders", query)
    monkeypatch.setattr(module, "provision", provision)
    result = module.provision_pending(None, {"SOFIA_OWNER_USER_ID": owner})
    assert result == {"elegibles": 33, "aprovisionadas": 1, "fallidas": 1}
    assert cursors == [[-1, ""], [1794665400, ids[31]]]


@pytest.mark.parametrize(
    ("plan", "requested", "expected"),
    [("free", None, "1080x1920"), ("ilimitado", "4k", "2160x3840")],
)
def test_lee_orden_y_resolucion_en_una_sola_consulta(monkeypatch, plan, requested, expected):
    module = load_provisioner()
    identity = "00000000-0000-4000-8000-000000000001"
    calls = []

    def query(values, statement, params):
        calls.append(statement)
        assert "output_resolution" in statement and params == [identity]
        return [
            {
                "plan_id": plan,
                "user_id": identity,
                "output_resolution": requested,
                "project_slug": "job-" + identity.replace("-", ""),
            }
        ]

    monkeypatch.setattr(module, "query_orders", query)
    assert module.load_order({}, identity)["resolution"] == expected
    assert len(calls) == 1


def test_respalda_identificadores_finales_juntos_y_reutiliza_kit_tras_interrupcion(monkeypatch):
    module = load_provisioner()
    identity, owner, project, status, task, edit, preview, comment = (
        f"00000000-0000-4000-8000-{n:012d}" for n in range(1, 9)
    )
    slug = "job-" + identity.replace("-", "")
    prefix = "STUDIO_KITSU_ORDER_" + identity.replace("-", "") + "_"
    stored = {
        "SOFIA_OWNER_USER_ID": owner,
        "MEDIA_KITSU_PRODUCTION_AUTO_DELIVERY_PROJECT_ID": project,
        "KITSU_API_TOKEN": "token de prueba",
        "KITSU_REVIEW_TASK_TYPE_ID": status,
        "KITSU_REVIEW_TASK_STATUS_ID": status,
        "KITSU_REVIEW_BOT_ID": owner,
    }
    snapshots = []
    assign_attempts = []

    def update(fields):
        snapshots.append(set(fields))
        stored.update(fields)

    def prepare_admin(_ssh, _values, store, key, settings):
        assert settings["resolution"] == "2160x3840"
        store({key + "PROJECT_ID": project})
        return project

    def remote(_ssh, _command, source, payload):
        if source == module.READ_ASSIGNMENTS:
            return {"data": base64.b64encode(b"{}"), "sha256": hashlib.sha256(b"{}").hexdigest()}
        if source == module.KITSU_TASK and "task" not in payload:
            return {"project_id": project, "task_id": task, "edit_id": edit}
        if source == module.KITSU_TASK:
            assert stored[prefix + "TASK_ID"] == payload["task"] == task
            return {
                "project_id": project,
                "task_id": task,
                "revision": 1,
                "preview_id": preview,
                "comment_id": comment,
            }
        assert source == module.WRITE_ASSIGNMENTS
        assigned = json.loads(base64.b64decode(payload["data"]))[identity]
        required = {
            prefix + key for key in ("KIT_ID", "PREVIEW_ID", "COMMENT_ID", "ASSIGNMENTS_SHA256")
        }
        assert snapshots[-1] == required
        assert assigned["id"] == stored[prefix + "KIT_ID"]
        assert payload["sha256"] == stored[prefix + "ASSIGNMENTS_SHA256"]
        assign_attempts.append(assigned)
        if len(assign_attempts) == 1:
            raise OSError("Conexión interrumpida antes de publicar")
        return {"verified": True}

    monkeypatch.setattr(
        module,
        "load_order",
        lambda *args: {
            "user_id": owner,
            "plan_id": "ilimitado",
            "project_slug": slug,
            "resolution": "2160x3840",
        },
    )
    monkeypatch.setattr(module, "connect", lambda *args: nullcontext())
    monkeypatch.setattr(module, "prepare_admin", prepare_admin)
    monkeypatch.setattr(module, "request_kitsu", lambda *args: (200, {"project_status_id": status}))
    monkeypatch.setattr(module, "run_remote", remote)
    vault = SimpleNamespace(update=update)
    with pytest.raises(OSError, match="Conexión interrumpida"):
        module.provision(vault, dict(stored), identity, "Entrega")
    assert module.provision(vault, dict(stored), identity, "Entrega")
    assert assign_attempts[0] == assign_attempts[1]
