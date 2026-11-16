import hashlib
import importlib.util
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "sre" / "audit_studio_kit_retention.py"
SPEC = importlib.util.spec_from_file_location("audit_studio_kit_retention", SOURCE)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


@pytest.fixture
def kit():
    return {
        "sha256": "a" * 64,
        "bytes": 42,
        "r2_etag": "1" * 32,
        "expires_at": 100,
    }


@pytest.mark.parametrize("cutoff", [None, 101])
def test_conserva_kit_vigente_o_de_retencion_continua(kit, cutoff):
    kit["expires_at"] = cutoff

    def r2(*_):
        pytest.fail("Un kit vigente no debe consultar el retiro")

    assert audit.retire_r2(kit, "/kits/piloto.zip", 100, r2) == "vigente"


@pytest.mark.parametrize("first_put", [200, 412])
def test_retiro_condicionado_y_reintento_idempotente(kit, first_put):
    headers = {
        "content-length": "42",
        "etag": '"' + kit["r2_etag"] + '"',
        "x-amz-meta-sha256": kit["sha256"],
    }
    mutations = []

    def r2(method, path, extra, body):
        assert path == "/kits/piloto.zip"
        if method == "PUT":
            assert extra["If-Match"] == '"' + kit["r2_etag"] + '"'
            assert body == b""
            mutations.append(method)
            headers.clear()
            headers.update({k.lower(): v for k, v in extra.items() if k != "If-Match"})
            headers["content-length"] = "0"
            return first_put, {}, b""
        assert method == "HEAD"
        return 200, headers, b""

    assert audit.retire_r2(kit, "/kits/piloto.zip", 100, r2) == "retirado"
    assert audit.retire_r2(kit, "/kits/piloto.zip", 101, r2) == "retirado"
    assert mutations == ["PUT"]


def test_retiro_preserva_una_version_distinta(kit):
    def r2(method, _path, _headers, _body):
        assert method == "HEAD"
        return 200, {"content-length": "42", "etag": '"diferente"'}, b""

    assert audit.retire_r2(kit, "/kits/piloto.zip", 100, r2) == "distinto"


@pytest.mark.parametrize(
    ("nas_case", "cloud_case"), [("coincide", "coincide"), ("ausente", "distinto")]
)
def test_inventario_coteja_mov_aprobado_de_retencion_continua(tmp_path, kit, nas_case, cloud_case):
    row = {
        **kit,
        "id": "00000000-0000-4000-8000-000000000002",
        "order_id": "00000000-0000-4000-8000-000000000001",
        "provider_mode": "sandbox",
        "plan_id": "ilimitado",
        "expires_at": None,
        "project_slug": "job-prueba",
        "prores_bytes": 14,
        "prores_sha256": hashlib.sha256(b"grafica-prueba").hexdigest(),
        "prores_r2_etag": "2" * 32 + "-3",
    }
    project = tmp_path / "proyectos" / row["project_slug"]
    revision = project / "05_revision"
    revision.mkdir(parents=True)
    name = row["order_id"] + "-v2"
    archive = revision / "kit.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr(
            "04-dictamen-qa.json",
            json.dumps(
                {
                    "order_id": row["order_id"],
                    "job_id": name,
                    "dictamen": {"decision": "aprobado"},
                }
            ),
        )
    row["bytes"] = archive.stat().st_size
    row["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.rename(revision / f"kit-studio-{row['sha256']}.zip")
    movie = project / "06_renders" / (name + "-grafica") / "render" / "master-corrected.mov"
    if nas_case == "coincide":
        movie.parent.mkdir(parents=True)
        movie.write_bytes(b"grafica-prueba")

    def head(path):
        assert path in (
            audit.object_path(row),
            audit.object_path(row).removesuffix(".zip") + ".mov",
        )
        graphic = path.endswith(".mov")
        etag = row["prores_r2_etag"] if cloud_case == "coincide" else "otro"
        return (
            200,
            {
                "content-length": str(row["prores_bytes"] if graphic else row["bytes"]),
                "etag": etag if graphic else row["r2_etag"],
                "x-amz-meta-sha256": row["prores_sha256"] if graphic else row["sha256"],
            },
            b"",
        )

    result = audit.audit_row(row, head, 101, tmp_path.resolve())
    assert result["prores"] == {"r2": cloud_case, "nas": nas_case}
    assert result["vence_utc"] is None and not result["vencido"]

    def head_without_prores(path):
        assert path.endswith(".zip")
        return head(path)

    scheduled = audit.audit_row(
        row, head_without_prores, 101, tmp_path.resolve(), check_prores=False
    )
    assert "prores" not in scheduled and not scheduled["vencido"]


def test_crudo_requiere_clave_y_metadatos_de_ingesta(kit):
    order = "00000000-0000-4000-8000-000000000001"
    asset = "00000000-0000-4000-8000-000000000002"
    row = {
        **kit,
        "order_id": order,
        "id": asset,
        "project_slug": "job-" + order.replace("-", ""),
        "extension": "mp4",
        "object_key": f"ingest/{order}/{asset}.mp4",
    }
    assert audit.raw_object_path(row) == "/studio-media-bucket/" + row["object_key"]
    with pytest.raises(ValueError):
        audit.raw_object_path({**row, "object_key": "ingest/otro.mp4"})
    headers = {
        "content-length": "42",
        "etag": row["r2_etag"],
        "x-amz-meta-agency_sha256": row["sha256"],
        "x-amz-meta-agency_asset_id": asset,
    }
    assert audit.matches_raw_original(row, headers)
    assert not audit.matches_raw_original(row, {**headers, "x-amz-meta-agency_asset_id": order})

    legacy = {**row, "id": "00000000-0000-4000-8000-000000000003"}
    assert audit.raw_leaf(legacy) == f"{asset}.mp4"
    assert not audit.matches_raw_original(legacy, headers)


def test_inventario_de_crudos_aplica_el_plazo_del_plan_a_las_ordenes_terminales():
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE orders (id TEXT,user_id TEXT,project_slug TEXT,plan_id TEXT,status TEXT,
                kind TEXT,provider_mode TEXT,updated_at INTEGER);
            CREATE TABLE upload_sessions (id TEXT,order_id TEXT,user_id TEXT,object_key TEXT,extension TEXT,
                bytes INTEGER,sha256 TEXT,object_etag TEXT,created_at INTEGER,status TEXT);
            CREATE TABLE plan_quotas (plan_id TEXT,retention_days INTEGER);
            CREATE TABLE delivery_kits (order_id TEXT,user_id TEXT,provider_mode TEXT,created_at INTEGER);
            INSERT INTO plan_quotas VALUES ('free',7),('ilimitado',NULL);
            INSERT INTO orders VALUES ('final','user','slug','free','completed','production','sandbox',100),
                ('proceso','user','slug','free','processing','production','sandbox',100),
                ('cola','user','slug','free','queued','production','sandbox',100),
                ('cancelada','user','slug','free','cancelled','production','sandbox',150),
                ('fallida','user','slug','pro','failed','production','sandbox',175),
                ('sin-verificar','user','slug','free','cancelled','production','sandbox',150),
                ('continuo','user','slug','ilimitado','completed','production','sandbox',100),
                ('sin-kit','user','slug','free','completed','production','sandbox',100);
            INSERT INTO plan_quotas VALUES ('pro',30);
            INSERT INTO upload_sessions SELECT id,id,user_id,'x','mp4',4,'sha','etag',10,'completed' FROM orders;
            UPDATE upload_sessions SET status='aborted' WHERE id='sin-verificar';
            INSERT INTO delivery_kits VALUES ('final','user','sandbox',200),('final','user','sandbox',300),
                ('final','user','production',400);
        """)
        rows = {r["id"]: r["expires_at"] for r in db.execute(audit.RAW_QUERY, (-1, "", 32))}
        assert rows == {
            "final": 300 + 7 * 86400,
            "continuo": None,
            "sin-kit": 100 + 7 * 86400,
            "cancelada": 150 + 7 * 86400,
            "fallida": 175 + 30 * 86400,
        }
        assert db.execute(audit.RAW_CURRENT, ("cancelada", "cancelada")).fetchone()
        db.execute("UPDATE orders SET status='processing' WHERE id='cancelada'")
        assert not db.execute(audit.RAW_CURRENT, ("cancelada", "cancelada")).fetchone()


def test_retiro_historico_requiere_descarga_verificada_y_reconoce_el_marcador(kit):
    order = "00000000-0000-4000-8000-000000000001"
    asset = "00000000-0000-4000-8000-000000000002"
    legacy = {
        **kit,
        "order_id": order,
        "id": "00000000-0000-4000-8000-000000000003",
        "project_slug": "job-" + order.replace("-", ""),
        "extension": "mp4",
        "object_key": f"ingest/{order}/{asset}.mp4",
    }
    etag = "b" * 32
    headers = {
        "content-length": str(legacy["bytes"]),
        "x-amz-meta-sha256": legacy["sha256"],
        "etag": f'"{etag}"',
    }
    path = audit.raw_object_path(legacy)
    writes = []

    def r2(method, key, extra, body):
        assert key == path
        if method == "HEAD":
            return 200, headers, b""
        assert method == "PUT" and extra["If-Match"] == f'"{etag}"' and body == b""
        writes.append(method)
        headers.clear()
        headers.update({name.lower(): value for name, value in extra.items()})
        headers["content-length"] = "0"
        return 200, {}, b""

    def corrupt(*_):
        return 200, headers, "c" * 64

    def intact(key, current, size):
        assert (key, current, size) == (path, etag, legacy["bytes"])
        return 200, headers, legacy["sha256"]

    assert audit.retire_r2(legacy, path, 100, r2, raw=True, download=corrupt) == "distinto"
    assert writes == []
    assert audit.retire_r2(legacy, path, 100, r2, raw=True, download=intact) == "retirado"
    assert audit.retire_r2(legacy, path, 100, r2, raw=True) == "retirado"
    assert not audit.legacy_marker({**legacy, "id": order}, headers)
    assert writes == ["PUT"]


@pytest.mark.parametrize(
    "cloud,shared,expected",
    [
        ("retirado", False, "retirado"),
        ("retirado", True, "compartido"),
        ("distinto", False, "pendiente"),
        ("ausente", False, "pendiente"),
    ],
)
def test_coordinacion_exige_marcador_r2_y_respeta_zip_compartido(cloud, shared, expected):
    reads = []
    record = {"compartido": shared}

    def nas(row):
        reads.append(row)
        return "retirado"

    assert audit.retire_kit(record, lambda _: cloud, nas) == {"r2": cloud, "nas": expected}
    assert reads == ([record] if expected == "retirado" else [])


@pytest.mark.skipif(os.name != "posix", reason="El retiro usa descriptores POSIX")
@pytest.mark.parametrize("partial", [False, True])
def test_retiro_en_nas_conserva_vigentes_y_reconcilia_interrupcion(tmp_path, kit, partial):
    kit["order_id"] = "00000000-0000-4000-8000-000000000001"
    kit["id"] = "00000000-0000-4000-8000-000000000002"
    kit["project_slug"] = "job-" + kit["order_id"].replace("-", "")
    original = b"x" * kit["bytes"]
    kit["sha256"] = hashlib.sha256(original).hexdigest()
    revision = tmp_path / "proyectos" / kit["project_slug"] / "05_revision"
    revision.mkdir(parents=True)
    stage = tmp_path / ".studio-kit-retention"
    stage.mkdir(mode=0o700)
    path = revision / ("kit-studio-" + kit["sha256"] + ".zip")
    pending = stage / (kit["id"] + ".zip")
    path.write_bytes(original)

    assert audit.retire_nas(kit, 99, tmp_path) == "vigente"
    assert path.read_bytes() == original
    path.write_bytes(b"z" * kit["bytes"])
    assert audit.retire_nas(kit, 100, tmp_path) == "distinto"
    assert path.read_bytes() == b"z" * kit["bytes"]
    path.write_bytes(original)
    if partial:
        path.rename(pending)
    assert audit.retire_nas(kit, 100, tmp_path) == "retirado"
    assert not path.exists() and not pending.exists()
    assert audit.retire_nas(kit, 101, tmp_path) == "ausente"


@pytest.mark.skipif(os.name != "posix", reason="El retiro usa descriptores POSIX")
@pytest.mark.parametrize("con_cuarentena", [False, True])
def test_crudo_cancelado_sin_proyecto_nas_distingue_ausencia_de_retiro_pendiente(
    tmp_path, kit, con_cuarentena
):
    order = "00000000-0000-4000-8000-000000000001"
    asset = "00000000-0000-4000-8000-000000000002"
    data = b"x" * kit["bytes"]
    kit.update(
        order_id=order,
        id=asset,
        project_slug="job-" + order.replace("-", ""),
        extension="mp4",
        object_key=f"ingest/{order}/{asset}.mp4",
        sha256=hashlib.sha256(data).hexdigest(),
    )
    (tmp_path / "proyectos").mkdir()
    stage = tmp_path / ".studio-kit-retention"
    stage.mkdir(mode=0o700)
    pending = stage / f"crudo-{asset}.mp4"
    if con_cuarentena:
        pending.write_bytes(data)

    status = audit.retire_nas(kit, 100, tmp_path, raw=True)

    assert status == ("distinto" if con_cuarentena else "ausente")
    if con_cuarentena:
        assert pending.read_bytes() == data
