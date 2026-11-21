import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import uuid
import zipfile
from contextlib import closing
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "docker" / "n8n"


@pytest.fixture
def handoff(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "production_handoff", SCRIPTS / "production_handoff.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setitem(sys.modules, "production_handoff", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def order():
    job, asset, user, upload = (str(uuid.uuid4()) for _ in range(4))
    return {
        "order_id": job,
        "user_id": user,
        "plan_id": "free",
        "upload_id": upload,
        "mode": "sandbox",
        "media": {
            "job_id": job,
            "asset_id": asset,
            "project_slug": "job-" + job.replace("-", ""),
            "extension": "mp4",
            "bytes": 4,
            "sha256": hashlib.sha256(b"dato").hexdigest(),
            "object_key": f"ingest/{job}/{asset}.mp4",
        },
    }


def test_inbox_recibe_una_orden_http_con_32_materiales(handoff, order, monkeypatch, tmp_path):
    import production_inbox as inbox

    checked_assets = []

    def verify_source(media):
        checked_assets.append(media["asset_id"])
        return True

    monkeypatch.setattr(inbox, "source_matches", verify_source)
    extra = []
    for _ in range(31):
        asset = str(uuid.uuid4())
        extra.append(
            dict(
                order["media"], asset_id=asset, object_key=f"ingest/{order['order_id']}/{asset}.mp4"
            )
        )
    request = dict(
        order, additional_media=extra, attempt=1, lease=str(uuid.uuid4()), lease_expires_at=1
    )
    body = json.dumps(request).encode()
    assert 9999 < len(body) <= inbox.MAX_BODY

    database = tmp_path / "inbox.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "CREATE TABLE inbox(order_id TEXT PRIMARY KEY, payload TEXT, received_at INTEGER)"
        )
    with HTTPServer(("127.0.0.1", 0), inbox.Handler) as server:
        server.peer = "127.0.0.1"
        server.token = b"a" * 64
        server.database = database
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        worker.start()
        try:
            headers = {
                "Authorization": "Bearer " + server.token.decode(),
                "Content-Type": "application/json",
            }
            with closing(HTTPConnection(*server.server_address, timeout=4)) as client:
                client.request("POST", "/v1/productions", body=body, headers=headers)
                with client.getresponse() as response:
                    assert response.status == 200
                    assert json.loads(response.read()) == {
                        "accepted": True,
                        "order_id": order["order_id"],
                    }
                client.request(
                    "POST", "/v1/productions", body=b" " * (inbox.MAX_BODY + 1), headers=headers
                )
                with client.getresponse() as response:
                    assert response.status == 400
        finally:
            server.shutdown()
            worker.join(timeout=2)
    assert checked_assets == [item["asset_id"] for item in [order["media"], *extra]]
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT count(*) FROM inbox").fetchone() == (1,)
        stored = db.execute(
            "SELECT payload FROM inbox WHERE order_id=?", (order["order_id"],)
        ).fetchone()
        assert json.loads(stored[0])["additional_media"] == extra


def test_verifica_contrato_firmado_y_separa_ordenes(handoff, order):
    secret = b"a" * 64
    raw = handoff.seal(order, secret)
    assert handoff.verify(raw, secret, order["order_id"]) == order
    with pytest.raises(ValueError):
        handoff.verify(raw, secret, str(uuid.uuid4()))
    altered = json.loads(raw)
    altered["order"]["media"]["bytes"] = 8
    with pytest.raises(ValueError):
        handoff.verify(json.dumps(altered).encode(), secret, order["order_id"])
    with pytest.raises(ValueError):
        handoff.verify(raw, b"b" * 64, order["order_id"])


def test_publica_una_vez_y_conserva_colision(handoff, tmp_path):
    target = tmp_path / "orden.json"
    assert handoff.publish(target, b"uno") is True
    assert handoff.publish(target, b"uno") is False
    with pytest.raises(ValueError):
        handoff.publish(target, b"dos")
    assert [(p.name, p.read_bytes()) for p in tmp_path.iterdir()] == [("orden.json", b"uno")]


def test_revision_de_valeria_admite_ingestas_multiples_grandes(
    handoff, order, monkeypatch, tmp_path
):
    import production_valeria as director

    receipt = tmp_path / f"{order['order_id']}.json"
    receipt.write_text(json.dumps({"order": order, "metadata": "x" * 20000}), encoding="utf-8")
    monkeypatch.setattr(director, "validate", lambda *_: None)
    monkeypatch.setattr(director, "eligible", lambda *_: False)
    assert director.process_pending(receipt, None, None, None, tmp_path, None) is False


def test_publicacion_admite_recibo_de_ingesta_multiple_grande(
    handoff, order, monkeypatch, tmp_path
):
    import production_publish as publisher

    journal = tmp_path / "journal"
    journal.mkdir()
    local = tmp_path / f"{order['order_id']}.json"
    local.write_text(json.dumps({"order_id": order["order_id"], "kitsu": {"task_id": "task"}}))
    (journal / local.name).write_text(json.dumps({"order": order, "metadata": "x" * 20000}))
    monkeypatch.setattr(publisher, "JOURNAL", journal)
    monkeypatch.setattr(publisher, "validate", lambda *_: None)
    monkeypatch.setattr(publisher, "eligible", lambda *_: False)
    with pytest.raises(ValueError, match="La orden no corresponde al piloto"):
        publisher.publish_assigned(
            {}, order["order_id"], {"task": "task"}, local, tmp_path, None, None, pending=False
        )


@pytest.mark.parametrize("ronda_aprobada", (2, None))
def test_revision_itera_sin_publicar_un_rechazo(handoff, monkeypatch, tmp_path, ronda_aprobada):
    import production_revision as revision

    folders = {name: tmp_path / name for name in ("draft", "render", "review")}
    for folder in folders.values():
        folder.mkdir()
    claim = {"id": str(uuid.uuid4()), "lease": str(uuid.uuid4())}
    receipt = {"order": {"plan_id": "ilimitado"}}
    attempts = []
    publication = []
    feedback = {"dictamen": {"decision": "rechazado", "defectos": ["Rótulo sobre el producto"]}}
    draft = {"escaleta": {"estado": "lista_para_stills"}}

    def prepare(current, _, strategy, direction, path, target, *, rejected):
        assert target.stem == current["id"]
        if attempts:
            assert rejected == {**draft["escaleta"], **feedback, "muestras": []}
        else:
            assert rejected is None
        attempts.append(uuid.UUID(current["id"]))
        return draft

    def review(current, *args):
        assert current["id"] == str(attempts[-1])
        assert args[-1] == "gpu-token"
        if len(attempts) == ronda_aprobada:
            return {"status": "aprobado", "render": {}, "review": {}}
        return {"status": "rechazado", "review": feedback}

    def stage(current, *args):
        assert current["id"] == str(attempts[-1])
        assert len(attempts) == ronda_aprobada
        return {"path": "kit"}

    def publish(original, *args, attempt):
        assert original == claim and attempt == ronda_aprobada
        publication.append(attempt)
        return {"id": "kit"}

    def renew(original):
        assert original == claim

    monkeypatch.setattr(revision, "draft_revision", prepare)
    monkeypatch.setattr(revision, "render_and_review", review)
    monkeypatch.setattr(revision, "stage_approved_revision", stage)
    monkeypatch.setattr(revision, "publish_staged_revision", publish)

    result = revision.process_claim(
        claim,
        receipt,
        {},
        {},
        tmp_path,
        folders,
        "dispatch-token",
        "gpu-token",
        None,
        None,
        renew,
        "Reel",
    )
    assert len(attempts) == len(set(attempts)) == (ronda_aprobada or 3)
    assert attempts[0] == uuid.UUID(claim["id"])
    assert all(item.version == 4 for item in attempts)
    if ronda_aprobada is None:
        assert result == {"status": "needs_review", "stage": "quality"}
        assert publication == []
    else:
        assert result == {"status": "completed", "kit": {"id": "kit"}}
        assert publication == [ronda_aprobada]


def test_servicio_de_revision_reclama_y_registra_rechazo(handoff, order, monkeypatch, tmp_path):
    import production_revision as revision

    claim = {"id": str(uuid.uuid4()), "lease": str(uuid.uuid4()), "base_kit_title": "Mi reel"}
    paths = {
        name: tmp_path for name in ("journal", "strategy", "direction", "draft", "render", "review")
    }
    events = []

    def dispatch(payload, token):
        assert token == "token"
        events.append(payload)
        if payload["action"] == "delivery_revision_claim":
            return {"revision": claim}
        if payload["action"] == "delivery_revision_failed":
            return {"recorded": True}
        raise AssertionError("Despacho inesperado")

    monkeypatch.setattr(
        revision,
        "load_claim_context",
        lambda *_: {
            "receipt": {"order": order},
            "strategy": {},
            "direction": {},
            "kit_path": tmp_path,
        },
    )
    monkeypatch.setattr(revision, "eligible", lambda *_: True)
    monkeypatch.setattr(
        revision, "process_claim", lambda *_: {"status": "needs_review", "stage": "quality"}
    )
    result = revision.run_once({}, paths, "token", None, None, None, dispatch)
    assert result == {"status": "needs_review", "stage": "quality"}
    assert events[-1] == {
        "action": "delivery_revision_failed",
        "id": claim["id"],
        "lease": claim["lease"],
        "stage": "quality",
    }


def test_arranque_del_servicio_de_revisiones_sin_ordenes(handoff, monkeypatch, tmp_path, capsys):
    import production_mateo as strategist
    import production_publish as publisher
    import production_revision as revision

    profile = tmp_path / "profile"
    profile.write_text(
        json.dumps({"key": "d" * 64, "owner": str(uuid.uuid4()), "fixture": str(uuid.uuid4())})
    )
    token, gpu = tmp_path / "dispatch", tmp_path / "gpu"
    token.write_text("b" * 64)
    gpu.write_text("c" * 64)
    cli = [profile, token, gpu, tmp_path / "r2", tmp_path / "hmac", tmp_path]
    monkeypatch.setattr(sys, "argv", ["production_revision.py", *map(str, cli)])
    monkeypatch.setattr(publisher, "credentials", lambda *_: ("r2", "acceso", "secreto", b"firma"))

    def dispatch(payload, secret):
        assert payload == {"action": "delivery_revision_claim"}
        assert secret == "b" * 64
        return {"revision": None}

    monkeypatch.setattr(strategist, "dispatch_request", dispatch)
    revision.main()
    assert json.loads(capsys.readouterr().out) == {"status": "idle"}
    assert all((tmp_path / name).is_dir() for name in ("draft", "render", "review"))


def test_revision_releva_el_dictamen_de_una_vista_previa_verificada(
    handoff, order, monkeypatch, tmp_path
):
    import production_elena as reviewer
    import production_lucas as editor
    import production_revision as revision

    order = {**order, "output_edit": "reel"}
    identity = str(uuid.uuid4())
    claim = {"id": identity, "order_id": order["order_id"]}
    brief = {"objective": "Probar un título nuevo"}
    draft = {
        "order_id": order["order_id"],
        "source_sha256": order["media"]["sha256"],
        "brief_sha256": hashlib.sha256(handoff.canonical(brief)).hexdigest(),
        "escaleta": {
            "estado": "lista_para_stills",
            "angulo": "Detalle",
            "render": {
                "project_id": order["media"]["project_slug"],
                "job_id": f"{order['order_id']}-v1",
                "plan": "Free",
                "composition": "AgencyVertical",
                "format": "mp4",
                "resolution": "1080p",
                "props": {"scenes": [], "duration_seconds": 30},
            },
        },
    }
    render = tmp_path / f"{identity}.json"
    review_dir = tmp_path / "review"
    review_dir.mkdir()

    def preview_only(kind, body, *_, **__):
        assert kind == "render" and body["job_id"] == f"{identity}-r-preview"
        return {"sha256": "c" * 64, "path": "vista-previa.mp4"}

    receipt = render.with_suffix(".preview-review.receipt")
    monkeypatch.setattr(revision, "approved_base", lambda *_: {"brief": brief})
    monkeypatch.setattr(editor, "verified_inputs", lambda *_: {})
    monkeypatch.setattr(editor, "submit_with_receipt", preview_only)
    monkeypatch.setattr(editor, "verify_mp4", lambda result, _: result)
    monkeypatch.setattr(
        reviewer,
        "review_video",
        lambda *_: {
            "dictamen": {"decision": "rechazado", "defectos": ["Rótulo sobre el producto"]}
        },
    )
    args = (claim, {"order": order}, {}, tmp_path, draft, render, review_dir / render.name, "gpu")
    result = revision.render_and_review(*args)
    raw = receipt.read_bytes()
    assert result == {"status": "rechazado", "review": json.loads(raw)}
    assert result["review"]["fase"] == "vista_previa"
    monkeypatch.setattr(editor, "run", lambda *_, **__: None)
    receipt.write_bytes(raw.replace(b"rechazado", b"aprobado"))
    with pytest.raises(ValueError, match="dictamen de vista previa"):
        revision.render_and_review(*args)


def test_entrega_rechaza_dictamen_de_otra_escaleta(handoff, order, monkeypatch, tmp_path):
    import production_delivery as delivery

    monkeypatch.setattr(delivery, "MEDIA", tmp_path)
    identity = order["order_id"]
    brief = {"order_id": identity, "user_id": order["user_id"], "objective": "Plantas"}
    digest = hashlib.sha256(handoff.canonical(brief)).hexdigest()
    job = f"{identity}-v1"
    relative = (
        Path("proyectos")
        / order["media"]["project_slug"]
        / "06_renders"
        / job
        / "render"
        / "master.mp4"
    )
    master = tmp_path / relative
    master.parent.mkdir(parents=True)
    master.write_bytes(b"video")
    render = {
        "brief_sha256": digest,
        "source_sha256": order["media"]["sha256"],
        "job_id": job,
        "direction_sha256": "a" * 64,
        "master": {"path": relative.as_posix()},
    }
    review = {"brief_sha256": digest, "direction_sha256": "a" * 64}
    assert delivery.approved_master(order, render, review, brief) == master
    with pytest.raises(ValueError, match="Brief o crudo ajeno"):
        delivery.approved_master(order, render, {**review, "direction_sha256": "b" * 64}, brief)


def test_escaleta_antigua_se_acredita_con_recibo_del_puente(handoff, order, monkeypatch, tmp_path):
    import production_lucas as lucas
    import production_revision as revision

    monkeypatch.setattr(revision, "LUCAS", tmp_path)
    source = {"video": "a" * 64, "audio": "b" * 64, "brief": "c" * 64}
    monkeypatch.setattr(lucas, "verified_inputs", lambda *_: source)
    master = {"path": "master.mp4", "sha256": "d" * 64, "bytes": 5}
    monkeypatch.setattr(lucas, "verify_mp4", lambda *_: master)
    original = {"job_id": order["order_id"] + "-v1", "props": {"title": "Plantas"}}
    direction = {"escaleta": {"render": original}}
    job = order["order_id"] + "-v2"
    request_hash = hashlib.sha256(
        handoff.canonical({"request": {**original, "job_id": job}, "source": source})
    ).hexdigest()
    journal = tmp_path / f"{order['order_id']}-v2.render.receipt"
    journal.write_text(json.dumps({"request_sha256": request_hash, "result": {"test": True}}))
    revision.legacy_direction_proof(
        order, {"order": order}, direction, {"job_id": job, "master": master}, "c" * 64
    )
    direction["escaleta"]["render"]["props"]["title"] = "Otro"
    with pytest.raises(ValueError, match="no corresponde a la escaleta"):
        revision.legacy_direction_proof(
            order, {"order": order}, direction, {"job_id": job, "master": master}, "c" * 64
        )


@pytest.mark.parametrize("plan", ["ultra", "ilimitado"])
def test_4k_viaja_en_la_orden_sellada(handoff, order, plan):
    from production_inbox import render_resolution

    selected = {**order, "plan_id": plan, "output_resolution": "4k"}
    key = b"a" * 64
    assert handoff.verify(handoff.seal(selected, key), key, order["order_id"]) == selected
    assert render_resolution(selected) == "4k"
    with pytest.raises(ValueError):
        handoff.seal({**selected, "plan_id": "free"}, key)


def test_el_formato_prores_se_conserva_en_buzon_y_sobre_firmado(handoff, order, monkeypatch):
    import production_inbox

    selected = {
        **order,
        "plan_id": "ilimitado",
        "output_resolution": "4k",
        "output_format": "prores4444",
    }
    request = dict(selected, attempt=1, lease=order["order_id"], lease_expires_at=0)
    monkeypatch.setattr(production_inbox, "source_matches", lambda _job: True)
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE inbox (order_id TEXT PRIMARY KEY, payload TEXT, received_at INTEGER)"
        )
        production_inbox.receive(db, request)
        production_inbox.receive(db, {**request, "attempt": 2})
        (stored,) = db.execute("SELECT payload FROM inbox").fetchone()
        assert json.loads(stored) == selected
        with pytest.raises(ValueError, match="conflicto"):
            production_inbox.receive(db, {**request, "output_format": "mp4"})
    key = b"a" * 64
    assert handoff.verify(handoff.seal(selected, key), key, order["order_id"]) == selected
    for plan in ("free", "pro", "ultra"):
        with pytest.raises(ValueError, match="Formato fuera del plan"):
            handoff.seal({**selected, "plan_id": plan, "output_resolution": "1080p"}, key)


def test_recibe_y_sella_todos_los_materiales_solo_si_coinciden(handoff, order, monkeypatch):
    import production_inbox

    asset = str(uuid.uuid4())
    second = {
        **order["media"],
        "asset_id": asset,
        "object_key": f"ingest/{order['order_id']}/{asset}.mp4",
    }
    selected = {**order, "additional_media": [second]}
    request = dict(selected, attempt=1, lease=order["order_id"], lease_expires_at=0)
    checked = []
    monkeypatch.setattr(
        production_inbox, "source_matches", lambda media: checked.append(media["asset_id"]) or True
    )
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE inbox (order_id TEXT PRIMARY KEY, payload TEXT, received_at INTEGER)"
        )
        production_inbox.receive(db, request)
        assert checked == [order["media"]["asset_id"], asset]
        assert json.loads(db.execute("SELECT payload FROM inbox").fetchone()[0]) == selected
    key = b"a" * 64
    assert handoff.verify(handoff.seal(selected, key), key, order["order_id"]) == selected
    with pytest.raises(ValueError, match="duplicado"):
        production_inbox.verify_order({**request, "additional_media": [order["media"]]})
    monkeypatch.setattr(
        production_inbox, "source_matches", lambda media: media["asset_id"] != asset
    )
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE TABLE inbox (order_id TEXT PRIMARY KEY, payload TEXT, received_at INTEGER)"
        )
        with pytest.raises(ValueError, match="Crudo"):
            production_inbox.receive(db, request)
        assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
    from production_valeria import source_paths

    with pytest.raises(ValueError, match="varios materiales"):
        source_paths({"order": selected})


def test_preparacion_identifica_cada_video_y_conserva_su_orden(
    handoff, order, monkeypatch, tmp_path
):
    import production_intake

    second = {**order["media"], "asset_id": str(uuid.uuid4())}
    calls = []

    def source(media, audio_name):
        calls.append((media["asset_id"], audio_name))
        return {"media": media, "raw": media["asset_id"], "duration_seconds": 5, "audio": None}

    monkeypatch.setattr(production_intake, "prepare_source", source)
    journal = tmp_path / "recepcion.json"
    production_intake.prepare({**order, "additional_media": [second]}, journal)
    receipt = json.loads(journal.read_text())
    assert receipt["raw"] == order["media"]["asset_id"]
    assert receipt["additional_sources"][0]["media"] == second
    assert calls == [
        (order["media"]["asset_id"], "intake-source.wav"),
        (second["asset_id"], f"intake-source-{second['asset_id']}.wav"),
    ]


@pytest.mark.parametrize(("plan", "rounds"), [("pro", 2), ("ultra", 3), ("ilimitado", 3)])
def test_rondas_autonomas_y_admision_pagada_solo_en_ensayo(handoff, order, plan, rounds):
    profile = {"owner": str(uuid.uuid4()), "fixture": str(uuid.uuid4())}
    selected = {**order, "plan_id": plan}
    assert handoff.eligible(selected, profile)
    assert not handoff.eligible({**selected, "mode": "production"}, profile)
    assert not handoff.eligible(order, profile)
    assert handoff.qa_rounds(selected, profile) == rounds


def test_inspeccion_distingue_silencio_y_rechaza_media_fuera_del_contrato(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_intake import inspect_metadata

    streams = [{"codec_type": "video", "width": 360, "height": 640}]
    data = {"streams": streams, "format": {"duration": "4.000000"}}
    assert inspect_metadata(data) == (4.0, False)
    streams.append({"codec_type": "audio"})
    assert inspect_metadata(data) == (4.0, True)
    data["format"]["duration"] = "NaN"
    with pytest.raises(ValueError):
        inspect_metadata(data)
    data["format"]["duration"] = "601"
    with pytest.raises(ValueError):
        inspect_metadata(data)


def test_brief_exige_el_modelo_y_no_inventa_el_objetivo(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_sofia import brief_reply

    reply = {"enfoque": "Revisar el crudo", "alcance": "Reel", "pendiente": "Confirmar el objetivo"}
    meta = {"agentHarnessId": "codex", "model": "gpt-6.1-terra", "provider": "openai"}
    result = {
        "status": "ok",
        "result": {"meta": {"agentMeta": meta}, "payloads": [{"text": json.dumps(reply)}]},
    }
    assert brief_reply(result) == reply
    meta["model"] = "otro"
    with pytest.raises(ValueError):
        brief_reply(result)


def test_transcripcion_exige_marcas_absolutas_ordenadas(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_sofia import validate_transcript

    result = {
        "language": "es",
        "duration": 130,
        "text": "Hola mundo",
        "words": [
            {"word": "Hola", "start": 0.1, "end": 0.4},
            {"word": "mundo", "start": 121.4, "end": 121.8},
        ],
    }
    assert validate_transcript(result) == result
    result["words"][1]["start"] = 0.2
    with pytest.raises(ValueError):
        validate_transcript(result)


def test_sofia_recibe_el_texto_transcrito_como_contexto_limitado(monkeypatch, tmp_path, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_sofia as sofia

    text = "Las sansevieras necesitan poca agua. " * 1000
    transcript_order = []

    def transcribe(_receipt, _output, source=None):
        transcript_order.append(source["media"]["asset_id"] if source else "principal")
        return {"language": "es", "text": text, "words": ["palabra"]}

    monkeypatch.setattr(sofia, "transcript_for", transcribe)
    observed = {}
    reply = {"enfoque": "Plantas", "alcance": "Reel", "pendiente": "Audiencia"}
    meta = {"agentHarnessId": "codex", "model": "gpt-6.1-terra", "provider": "openai"}

    def run(args, **_kwargs):
        observed["prompt"] = args[args.index("--message") + 1]
        result = {
            "status": "ok",
            "result": {"meta": {"agentMeta": meta}, "payloads": [{"text": json.dumps(reply)}]},
        }
        return type("Run", (), {"stdout": json.dumps(result).encode()})()

    monkeypatch.setattr(sofia.subprocess, "run", run)
    monkeypatch.setattr(sofia, "publish", lambda *_: None)
    sofia.start_brief(
        {"order": order, "duration_seconds": 90, "audio": {}}, tmp_path / "brief.json"
    )
    prompt = observed["prompt"]
    assert "Las sansevieras necesitan poca agua" in prompt
    facts = json.loads(prompt.split("Metadatos verificados: ", 1)[1])
    assert len(facts["transcripcion"]["texto"]) == sofia.MAX_BRIEF_TRANSCRIPT
    assert facts["transcripcion"]["texto_truncado"] is True
    second = {**order["media"], "asset_id": str(uuid.uuid4())}
    sofia.start_brief(
        {
            "order": {**order, "additional_media": [second]},
            "duration_seconds": 90,
            "audio": {},
            "additional_sources": [{"media": second, "duration_seconds": 30, "audio": {}}],
        },
        tmp_path / "brief.json",
    )
    multi = json.loads(observed["prompt"].split("Metadatos verificados: ", 1)[1])
    assert transcript_order == ["principal", second["asset_id"], "principal"]
    assert multi["materiales_adicionales"][0]["id"] == second["asset_id"]
    assert "Las sansevieras" in multi["materiales_adicionales"][0]["transcripcion"]["texto"]
    assert (
        len(multi["transcripcion"]["texto"])
        + len(multi["materiales_adicionales"][0]["transcripcion"]["texto"])
        == sofia.MAX_BRIEF_TRANSCRIPT
    )


def test_escaleta_multiple_conserva_identidad_y_tiempos_de_cada_fuente(handoff, order):
    from production_valeria import VisualResponseFormatError, multi_reel_scenes

    first, second = order["media"]["asset_id"], str(uuid.uuid4())
    sources = {first: 80, second: 30}
    items = [
        {
            "fuente": second,
            "desde": 0,
            "hasta": 10,
            "rotulo": {
                "texto": "Conoce nuestras plantas",
                "desde": 0,
                "hasta": 3,
                "posicion": "superior",
            },
        },
        {"fuente": first, "desde": 15, "hasta": 35},
    ]
    scenes, frames = multi_reel_scenes(items, sources, ad=True)
    assert frames == 900
    assert [(scene["source_id"], scene["source_start_frame"]) for scene in scenes] == [
        (second, 0),
        (first, 450),
    ]
    with pytest.raises(VisualResponseFormatError, match="desconocida"):
        multi_reel_scenes([{**items[0], "fuente": str(uuid.uuid4())}, items[1]], sources, ad=True)
    with pytest.raises(VisualResponseFormatError, match="fuera del material"):
        multi_reel_scenes([items[0], {**items[1], "hasta": 81}], sources, ad=True)
    with pytest.raises(VisualResponseFormatError, match="superponen"):
        multi_reel_scenes(
            [items[0], {**items[1], "fuente": second, "desde": 5, "hasta": 25}], sources, ad=True
        )
    longer = [{**items[0], "hasta": 30}, {**items[1], "desde": 0, "hasta": 80}]
    assert multi_reel_scenes(longer, sources, long_form=True)[1] == 3300
    with pytest.raises(VisualResponseFormatError, match="duración válida"):
        multi_reel_scenes(longer, sources)
    with pytest.raises(VisualResponseFormatError, match="anuncio"):
        multi_reel_scenes(longer, sources, ad=True, long_form=True)


def test_valeria_inspecciona_cada_fuente_en_lotes_separados(handoff, order, monkeypatch):
    import production_valeria as director

    second = {**order["media"], "asset_id": str(uuid.uuid4())}
    selected = {**order, "additional_media": [second]}
    receipt = {
        "order": selected,
        "raw": "primer-video",
        "duration_seconds": 60,
        "audio": None,
        "additional_sources": [
            {"media": second, "raw": "segundo-video", "duration_seconds": 40, "audio": None}
        ],
    }
    calls = []

    def sample(single, *, ad=False, transcript=None):
        calls.append((single["order"]["media"]["asset_id"], single["raw"], ad, transcript))
        return [(1.0, b"imagen")]

    monkeypatch.setattr(director, "sample_images", sample)
    transcripts = {second["asset_id"]: {"words": []}}
    batches = director.sample_source_batches(receipt, ad=True, transcripts=transcripts)
    assert [batch["source_id"] for batch in batches] == [
        order["media"]["asset_id"],
        second["asset_id"],
    ]
    assert calls == [
        (order["media"]["asset_id"], "primer-video", True, None),
        (second["asset_id"], "segundo-video", True, transcripts[second["asset_id"]]),
    ]
    with pytest.raises(ValueError, match="distintos"):
        director.sample_source_batches({**receipt, "additional_sources": []})


def test_analisis_visual_multiple_rechaza_muestras_no_examinadas(handoff, order, monkeypatch):
    import production_valeria as director

    second = str(uuid.uuid4())
    batches = [
        {
            "source_id": order["media"]["asset_id"],
            "duration_seconds": 20,
            "images": [(2.0, b"primera")],
        },
        {"source_id": second, "duration_seconds": 30, "images": [(9.0, b"segunda")]},
    ]
    brief = {"objective": "Mostrar plantas", "audience": "Coleccionistas", "tone": "Cercano"}
    calls = []

    def analyze(request):
        calls.append(request)
        return {
            "resumen": "Plantas en macetas",
            "muestras": [{"segundo": (2.0, 9.0)[len(calls) - 1], "descripcion": "Planta visible"}],
        }

    monkeypatch.setattr(director, "run_visual", analyze)
    result = director.analyze_source_batches(brief, batches, str(uuid.uuid4()))
    assert [item["source_id"] for item in result] == [batch["source_id"] for batch in batches]
    assert calls[0]["idempotencyKey"] != calls[1]["idempotencyKey"]
    assert [len(call["attachments"]) for call in calls] == [1, 1]
    with pytest.raises(director.VisualResponseFormatError, match="no coincide"):
        director.source_observations(
            {"resumen": "Vista", "muestras": [{"segundo": 7.0, "descripcion": "Detalle"}]},
            batches[0],
        )


def test_escaleta_multiple_usa_todas_las_palabras_si_caben_en_el_contexto(
    handoff, order, monkeypatch
):
    import production_valeria as director

    first, second = order["media"]["asset_id"], str(uuid.uuid4())
    batches = [{"source_id": asset, "images": [(2.0, b"muestra")]} for asset in (first, second)]
    analyses = [
        {
            "source_id": asset,
            "duracion_segundos": 24,
            "resumen": "Plantas y maceteros",
            "muestras": [{"segundo": 2, "descripcion": "Planta en primer plano"}],
        }
        for asset in (first, second)
    ]
    transcripts = {
        asset: {
            "words": [
                {"start": i / 2, "end": (i + 1) / 2, "word": f"{label}{i}"} for i in range(40)
            ]
        }
        for asset, label in ((first, "primera"), (second, "segunda"))
    }
    brief = {"objective": "Mostrar plantas", "audience": "Coleccionistas", "tone": "Cercano"}
    request_id = str(uuid.uuid4())

    def plan():
        return director.multiple_plan_request(
            [], brief, batches, analyses, request_id, transcripts=transcripts
        )[0]

    def facts(request):
        message = request["message"]
        return json.loads(message[message.index('{"objetivo"') :])["fuentes"]

    complete = plan()
    first_source, second_source = facts(complete)
    assert first_source["transcripcion_completa"] and second_source["transcripcion_completa"]
    assert first_source["palabras_con_tiempo"][-1] == [19.5, 20, "primera39"]
    assert second_source["palabras_con_tiempo"][-1] == [19.5, 20, "segunda39"]

    limit = len(director.visual_payload(complete)) - 500
    monkeypatch.setattr(director, "MAX_VISUAL_BYTES", limit + 1000)
    bounded = plan()
    assert len(director.visual_payload(bounded)) <= limit
    assert not all(source["transcripcion_completa"] for source in facts(bounded))
    assert [source["id"] for source in facts(bounded)] == [first, second]


def test_correccion_multiple_situa_defectos_y_conserva_tiempos_por_fuente(handoff, order):
    import production_valeria as director

    first, second = order["media"]["asset_id"], str(uuid.uuid4())
    batches = [{"source_id": asset, "images": [(0, b"imagen")]} for asset in (first, second)]
    analyses = [
        {"source_id": asset, "duracion_segundos": 24, "resumen": "Planta", "muestras": []}
        for asset in (first, second)
    ]
    brief = {"objective": "Mostrar plantas", "audience": "Coleccionistas", "tone": "Cercano"}
    angles = [{"angulo": "Variedad"}, {"angulo": "Detalle"}, {"angulo": "Maceteros"}]
    scenes = [
        {
            "source_id": first,
            "source_start_frame": 15,
            "source_end_frame": 465,
            "label": {
                "text": "Sansevierias",
                "start_frame": 0,
                "end_frame": 90,
                "position": "panel",
            },
        },
        {"source_id": second, "source_start_frame": 30, "source_end_frame": 480},
    ]
    defect = {"muestra": 2, "correccion": "Mover el rótulo fuera del producto"}
    prior = {
        "angulo": "Detalle",
        "dictamen": {"defectos": [defect]},
        "render": {"props": {"scenes": scenes}},
        "muestras": [{"segundo": 1.5, "sha256": "a" * 64}, {"segundo": 5, "sha256": "b" * 64}],
    }

    request, _ = director.multiple_plan_request(
        angles, brief, batches, analyses, str(uuid.uuid4()), prior=prior
    )
    message = request["message"]
    previous = json.loads(message[message.index('{"objetivo"') :])["revision_previa"]
    assert previous["angulo"] == 2
    assert previous["defectos"] == [defect]
    assert previous["segundos_por_muestra"] == [1.5, 5]
    assert "sha256" not in previous
    normalized, frames = director.multi_reel_scenes(previous["escenas"], {first: 24, second: 24})
    assert normalized == scenes
    assert frames == 900
    prior["dictamen"].update(
        {
            "decision": "rechazado",
            "puntaje": 75,
            "reglas_duras_incumplidas": ["Audio fuera del intervalo de sonoridad"],
        }
    )
    request, _ = director.multiple_plan_request(
        angles,
        brief,
        batches,
        analyses,
        str(uuid.uuid4()),
        prior=prior,
        adjustment="Usa el ángulo inicial",
        user_revision=True,
    )
    message = request["message"]
    facts = json.loads(message[message.index('{"objetivo"') :])
    assert "Elena rechazó la versión previa" in message
    assert "Cambia el ángulo si el titular lo solicita" in message
    assert facts["ajuste_del_titular"] == "Usa el ángulo inicial"
    assert facts["revision_previa"]["reglas_duras_incumplidas"] == [
        "Audio fuera del intervalo de sonoridad"
    ]
    assert facts["revision_previa"]["puntaje"] == 75


def test_escaleta_multiple_entrega_rutas_y_subtitulos_de_cada_fuente(handoff, order):
    from production_valeria import ad_captions, make_edl

    first, second = order["media"]["asset_id"], str(uuid.uuid4())
    extra = {
        **order["media"],
        "asset_id": second,
        "object_key": f"ingest/{order['order_id']}/{second}.mp4",
    }
    project = order["media"]["project_slug"]
    order = {**order, "output_edit": "reel", "additional_media": [extra]}
    receipt = {
        "order": order,
        "raw": f"proyectos/{project}/01_raw/{first}.mp4",
        "duration_seconds": 60,
        "audio": {"path": f"proyectos/{project}/03_assets/intake-source.wav"},
        "additional_sources": [
            {
                "media": extra,
                "raw": f"proyectos/{project}/01_raw/{second}.mp4",
                "duration_seconds": 30,
                "audio": {"path": f"proyectos/{project}/03_assets/intake-source-{second}.wav"},
            }
        ],
    }
    scenes = [
        {
            "fuente": second,
            "desde": 0,
            "hasta": 10,
            "rotulo": {"texto": "Plantas", "desde": 0, "hasta": 3, "posicion": "superior"},
        },
        {"fuente": first, "desde": 0, "hasta": 20},
    ]
    answer = {
        "angulo": 1,
        "titulo": "Plantas",
        "subtitulo": "Nuestra colección",
        "observacion": "Tomas de ambas fuentes",
        "apto_sin_cambios": True,
        "escenas": scenes,
    }
    edl = make_edl(receipt, [{"angulo": "Colección"}] * 3, answer)
    props = edl["render"]["props"]
    assert [source["id"] for source in props["sources"]] == [first, second]
    assert [scene["source_id"] for scene in props["scenes"]] == [second, first]
    words = {
        second: [{"start": 0.1, "end": 0.6, "word": "Segundo."}],
        first: [{"start": 0.1, "end": 0.6, "word": "Primero."}],
    }
    captions = ad_captions(props["scenes"], words)
    assert [cue["text"] for cue in captions] == ["Segundo.", "Primero."]
    assert captions[1]["start_frame"] >= 300
    receipt["additional_sources"][0]["raw"] = "../ajeno.mp4"
    with pytest.raises(ValueError, match="Rutas"):
        make_edl(receipt, [{"angulo": "Colección"}] * 3, answer)


@pytest.mark.parametrize("edit", ["reel", "full"])
def test_valeria_construye_escaleta_con_varios_videos(handoff, order, monkeypatch, tmp_path, edit):
    import production_valeria as director

    first, second = order["media"]["asset_id"], str(uuid.uuid4())
    extra = {
        **order["media"],
        "asset_id": second,
        "object_key": f"ingest/{order['order_id']}/{second}.mp4",
    }
    project = order["media"]["project_slug"]
    order = {**order, "output_edit": edit, "additional_media": [extra]}
    audio = {"bytes": 12, "sha256": "c" * 64}
    receipt = {
        "order": order,
        "raw": f"proyectos/{project}/01_raw/{first}.mp4",
        "duration_seconds": 60,
        "audio": {**audio, "path": f"proyectos/{project}/03_assets/intake-source.wav"},
        "additional_sources": [
            {
                "media": extra,
                "raw": f"proyectos/{project}/01_raw/{second}.mp4",
                "duration_seconds": 30,
                "audio": {
                    **audio,
                    "path": f"proyectos/{project}/03_assets/intake-source-{second}.wav",
                },
            }
        ],
    }
    brief = {"objective": "Mostrar plantas", "audience": "Personas", "tone": "Cercano"}
    strategy = {
        "order_id": order["order_id"],
        "source_sha256": order["media"]["sha256"],
        "brief_sha256": hashlib.sha256(director.canonical(brief)).hexdigest(),
        "ejecucion": "isolated-agent-runtime",
        "angulos": [
            {"angulo": name, "gancho": "Inicio", "copy": "Mensaje", "cta": "Consultar"}
            for name in ("Uno", "Dos", "Tres")
        ],
    }
    batches = [
        {"source_id": identity, "duration_seconds": 30, "images": [(1.0, b"foto")]}
        for identity in (first, second)
    ]
    analyses = [
        {
            "source_id": batch["source_id"],
            "duracion_segundos": 30,
            "resumen": "Plantas",
            "muestras": [{"segundo": 1.0, "descripcion": "Una maceta"}],
        }
        for batch in batches
    ]
    answer = {
        "angulo": 1,
        "titulo": "Plantas",
        "subtitulo": "Colección",
        "observacion": "Dos videos",
        "apto_sin_cambios": True,
        "escenas": [
            {
                "fuente": second,
                "desde": 0,
                "hasta": 10,
                "rotulo": {"texto": "Plantas", "desde": 0, "hasta": 3, "posicion": "superior"},
            },
            {"fuente": first, "desde": 0, "hasta": 20},
        ],
    }
    if edit == "full":
        answer["escenas"][0]["hasta"] = 30
        answer["escenas"][1]["hasta"] = 60
    monkeypatch.setattr(director, "transcript_for", lambda *_: {"text": "Plantas", "words": []})
    monkeypatch.setattr(director, "sample_source_batches", lambda *_args, **_kwargs: batches)
    monkeypatch.setattr(director, "analyze_source_batches", lambda *_args, **_kwargs: analyses)
    monkeypatch.setattr(director, "run_visual", lambda request: answer)
    monkeypatch.setattr(director, "fingerprint", lambda _path: audio)
    target = tmp_path / "edl.json"
    director.process(receipt, strategy, brief, target, str(uuid.uuid4()))
    edl = json.loads(target.read_bytes())["escaleta"]
    assert [scene["source_id"] for scene in edl["render"]["props"]["scenes"]] == [second, first]
    assert len(edl["render"]["props"]["sources"]) == 2
    assert edl["render"]["props"]["duration_seconds"] == (90 if edit == "full" else 30)

    # Un cambio de montaje solicitado tras la entrega debe reconocerse aunque
    # el título y el subtítulo permanezcan iguales.
    prior = {**edl, "dictamen": {"defectos": []}}
    answer["escenas"].reverse()
    revised_target = tmp_path / "revision.json"
    director.process(
        receipt,
        strategy,
        brief,
        revised_target,
        str(uuid.uuid4()),
        prior=prior,
        adjustment="Invierte el orden de los planos",
        user_revision=True,
    )
    revised = json.loads(revised_target.read_bytes())["escaleta"]
    assert revised["estado"] == "lista_para_stills"
    assert [scene["source_id"] for scene in revised["render"]["props"]["scenes"]] == [first, second]

    unchanged_target = tmp_path / "sin-cambios.json"
    director.process(
        receipt,
        strategy,
        brief,
        unchanged_target,
        str(uuid.uuid4()),
        prior={**revised, "dictamen": {"defectos": []}},
        adjustment="Invierte el orden de los planos",
        user_revision=True,
    )
    assert json.loads(unchanged_target.read_bytes())["escaleta"]["estado"] == "requiere_revision"


def test_mateo_exige_tres_angulos_y_coincidencia_de_titular(monkeypatch, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_mateo import confirmed, strategy_reply

    brief = {
        "order_id": order["order_id"],
        "user_id": order["user_id"],
        "objective": "Presentar el estudio",
        "audience": "Comercios locales",
        "tone": "Cercano",
        "confirmed_at": 1,
    }
    assert confirmed({"brief": brief}, order) == brief
    assert confirmed({"brief": None}, order) is None
    with pytest.raises(ValueError):
        confirmed({"brief": {**brief, "user_id": str(uuid.uuid4())}}, order)
    rows = [
        {"angulo": title, "gancho": "Pregunta", "copy": "Mensaje", "cta": "Escríbenos"}
        for title in ("A", "B", "C")
    ]
    assert strategy_reply(json.dumps({"angulos": rows})) == rows
    rows[2]["angulo"] = "B"
    with pytest.raises(ValueError):
        strategy_reply(json.dumps({"angulos": rows}))


def test_mateo_verifica_firma_y_correlacion_del_socket(monkeypatch, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_mateo import signed, verify_native

    key = b"k" * 64
    stamp = "1794652200"
    turn = {"actor_id": "a" * 64, "order_id": order["order_id"], "request_id": str(uuid.uuid4())}
    raw = json.dumps(dict(turn, text="resultado")).encode()
    signature = signed(key, stamp, "200", raw)
    assert verify_native(raw, stamp, signature, key, turn, int(stamp)) == "resultado"
    with pytest.raises(ValueError):
        verify_native(raw + b" ", stamp, signature, key, turn, int(stamp))
    with pytest.raises(ValueError):
        verify_native(
            raw, stamp, signature, key, dict(turn, request_id=str(uuid.uuid4())), int(stamp)
        )
    with pytest.raises(ValueError):
        verify_native(raw, stamp, signature, key, turn, int(stamp) + 61)


def test_mateo_reintenta_sin_bloquear_las_ordenes_siguientes(monkeypatch, tmp_path, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_mateo as mateo

    journal, sofia, output = (tmp_path / name for name in ("ingesta", "sofia", "mateo"))
    for directory in (journal, sofia, output):
        directory.mkdir()
    first = "00000000-0000-4000-8000-000000000001"
    second = "00000000-0000-4000-8000-000000000002"
    for identity in (first, second):
        media = {**order["media"], "job_id": identity}
        media["project_slug"] = "job-" + identity.replace("-", "")
        media["object_key"] = f"ingest/{identity}/{media['asset_id']}.mp4"
        pending = {**order, "order_id": identity, "media": media}
        (journal / f"{identity}.json").write_text(json.dumps({"order": pending}), encoding="utf-8")
        (sofia / f"{identity}.json").write_text("{}", encoding="utf-8")
    profile, token, key = (tmp_path / name for name in ("perfil", "token", "llave"))
    profile.write_text(json.dumps(dict(owner=order["user_id"], fixture=second, key="a" * 64)))
    token.write_bytes(b"b" * 64)
    key.write_bytes(b"c" * 64)
    monkeypatch.setattr(sys, "argv", ["mateo", *map(str, (profile, token, key, output))])
    monkeypatch.setattr(mateo, "JOURNAL", journal)
    monkeypatch.setattr(mateo, "SOFIA", sofia)
    monkeypatch.setattr(mateo, "lookup", lambda *_: {"brief_confirmado": True})
    now = 1_794_680_000
    monkeypatch.setattr(mateo.time, "time", lambda: now)
    attempts = []

    def strategy(receipt, _brief, _preliminary, target, _owner, _key):
        identity = receipt["order"]["order_id"]
        attempts.append(identity)
        if identity == first and attempts.count(first) < 3:
            raise TimeoutError("Falla simulada del agente")
        target.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(mateo, "run_mateo", strategy)
    with pytest.raises(TimeoutError):
        mateo.main()
    now += 10
    mateo.main()
    assert (output / f"{second}.json").is_file()
    now += 19
    mateo.main()
    assert attempts == [first, second]
    now += 1
    with pytest.raises(TimeoutError):
        mateo.main()
    now += 59
    mateo.main()
    assert attempts == [first, second, first]
    now += 1
    mateo.main()
    assert (output / f"{first}.json").is_file()
    assert attempts == [first, second, first, first]


@pytest.mark.parametrize(
    ("plan_id", "plan"),
    [("free", "Free"), ("pro", "Pro"), ("ultra", "Ultra"), ("ilimitado", "Ilimitado")],
)
def test_escaleta_de_valeria_exige_fuente_confirmada_y_preserva_el_plan(
    monkeypatch, order, plan_id, plan
):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_valeria import make_edl

    job = order["media"]
    order["plan_id"] = plan_id
    raw = f"proyectos/{job['project_slug']}/01_raw/{job['asset_id']}.mp4"
    audio = f"proyectos/{job['project_slug']}/03_assets/intake-source.wav"
    receipt = {"order": order, "raw": raw, "duration_seconds": 4.0, "audio": {"path": audio}}
    angles = [{"angulo": title} for title in ("A", "B", "C")]
    answer = {
        "angulo": 2,
        "titulo": "Revisión de color",
        "subtitulo": "Pide una prueba",
        "observacion": "Barras visibles",
        "apto_sin_cambios": True,
    }
    edl = make_edl(receipt, angles, answer)
    assert edl["angulo"] == "B"
    props = edl["render"]["props"]
    assert props["video_path"] == raw and props["audio_path"] == audio
    assert "subtitle_size" not in props
    expanded = make_edl(receipt, angles, {**answer, "subtitulo_ampliado": True})
    assert expanded["render"]["props"] == {**props, "subtitle_size": "large"}
    assert props["duration_seconds"] == 4.0 and edl["render"]["plan"] == plan
    assert (edl["render"]["format"], edl["render"]["resolution"]) == ("mp4", "1080p")
    if plan_id in ("ultra", "ilimitado"):
        selected = {**receipt, "order": {**order, "output_resolution": "4k"}}
        assert make_edl(selected, angles, answer)["render"]["resolution"] == "4k"
    assert make_edl(receipt, angles, {**answer, "apto_sin_cambios": False})["render"] is None
    with pytest.raises(ValueError):
        make_edl({**receipt, "raw": "../otro.mp4"}, angles, answer)
    with pytest.raises(ValueError):
        make_edl(receipt, angles, {**answer, "titulo": "a" * 121})
    with pytest.raises(ValueError):
        make_edl(receipt, angles, {**answer, "subtitulo_ampliado": "sí"})


def test_valeria_convierte_escenas_a_fotogramas_y_acota_los_rotulos(monkeypatch, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_valeria import make_edl

    order["output_edit"] = "reel"
    job = order["media"]
    root = f"proyectos/{job['project_slug']}"
    receipt = {
        "order": order,
        "raw": f"{root}/01_raw/{job['asset_id']}.mp4",
        "duration_seconds": 75.0,
        "audio": {"path": f"{root}/03_assets/intake-source.wav"},
    }
    angles = [{"angulo": name} for name in ("A", "B", "C")]
    answer = {
        "angulo": 1,
        "titulo": "Catálogo",
        "subtitulo": "Pide asesoría",
        "observacion": "Hay planos de detalle disponibles",
        "apto_sin_cambios": True,
        "escenas": [
            {
                "desde": 0,
                "hasta": 12,
                "rotulo": {
                    "texto": "Catálogo",
                    "desde": 1,
                    "hasta": 4,
                    "posicion": "inferior",
                    "tamano": "grande",
                },
            },
            {"desde": 22, "hasta": 44},
        ],
    }
    props = make_edl(receipt, angles, answer)["render"]["props"]
    assert props["duration_seconds"] == 34
    assert props["scenes"] == [
        {
            "source_start_frame": 0,
            "source_end_frame": 360,
            "label": {
                "text": "Catálogo",
                "start_frame": 30,
                "end_frame": 120,
                "position": "bottom",
                "size": "large",
            },
        },
        {"source_start_frame": 660, "source_end_frame": 1320},
    ]
    for bad in ({"desde": 11, "hasta": 35}, {"desde": 60, "hasta": 82}, {"desde": 20, "hasta": 22}):
        with pytest.raises(ValueError):
            make_edl(receipt, angles, {**answer, "escenas": [answer["escenas"][0], bad]})


def test_valeria_preserva_inicio_y_cierre_dentro_del_presupuesto_visual(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_valeria import plan_request

    words = [
        {"start": index / 4, "end": (index + 1) / 4, "word": "descripción" * 6}
        for index in range(2200)
    ]
    words[0]["word"] = "inicio-del-clip"
    words[-1]["word"] = "cierre-del-clip"
    brief = {"objective": "Presentar tres productos", "audience": "Catálogo", "tone": ""}
    request = plan_request(
        [],
        brief,
        [(index, b"x" * 12000) for index in range(5)],
        550,
        str(uuid.uuid4()),
        reel=True,
        transcript={"words": words},
    )
    assert len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()) <= 109000
    assert "inicio-del-clip" in request["message"] and "cierre-del-clip" in request["message"]


@pytest.mark.parametrize(
    "first_size,qualities,mutate_original",
    [(100, [4], False), (26_000, [4, 8], False), (100, [4], True)],
)
def test_valeria_solo_reduce_jpeg_que_supera_el_limite(
    monkeypatch, tmp_path, order, first_size, qualities, mutate_original
):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_valeria as director

    project = order["media"]["project_slug"]
    asset = order["media"]["asset_id"]
    raw = tmp_path / f"proyectos/{project}/01_raw/{asset}.mp4"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"dato")
    attempts = []

    def run(args, **kwargs):
        from subprocess import CompletedProcess

        quality = int(args[args.index("-q:v") + 1])
        attempts.append(quality)
        size = first_size if quality == 4 else 100
        jpeg = b"\xff\xd8" + b"x" * (size - 4) + b"\xff\xd9"
        if mutate_original and len(attempts) == 1:
            with raw.open("ab") as file:
                file.write(b"archivo cambiado")
        return CompletedProcess(args, 0, stdout=jpeg)

    monkeypatch.setattr(director, "MEDIA", tmp_path)
    if sys.platform == "win32":
        monkeypatch.setattr(os, "O_NOFOLLOW", os.O_BINARY, raising=False)
        monkeypatch.setattr(os, "O_NONBLOCK", 0, raising=False)
        # Valeria se ejecuta en Linux. Windows puede dar distinto ctime para la
        # misma ruta y descriptor. Conserva los otros campos en esta prueba.
        real_identity = director.identity
        monkeypatch.setattr(director, "identity", lambda info: real_identity(info)[:-1])
    monkeypatch.setattr(director.subprocess, "run", run)
    receipt = {
        "order": order,
        "raw": raw.relative_to(tmp_path).as_posix(),
        "audio": None,
        "duration_seconds": 4.0,
    }
    if mutate_original:
        with pytest.raises(ValueError, match="El original cambió durante la inspección"):
            director.sample_images(receipt)
        return
    images = director.sample_images(receipt)
    assert [stamp for stamp, _ in images] == [0.6, 2.0, 3.4]
    assert all(len(image) == 100 for _, image in images)
    assert attempts == qualities * 3


def test_valeria_admite_el_piloto_y_envia_las_muestras_por_stdin(monkeypatch, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_valeria as director

    profile = {"owner": order["user_id"], "fixture": str(uuid.uuid4())}
    assert director.eligible(order, profile)
    assert not director.eligible({**order, "mode": "production"}, profile)
    assert not director.eligible({**order, "user_id": str(uuid.uuid4())}, profile)
    message = "dato privado del brief"
    request = director.visual_request(
        "agency-director", message, [(1, b"imagen")], str(uuid.uuid4())
    )

    def run(argv, *, input, **kwargs):
        assert message not in " ".join(argv) and "aW1hZ2Vu" not in " ".join(argv)
        assert json.loads(input) == request
        assert request["modelRun"] is True and request["deliver"] is False
        result = {
            "meta": {
                "agentMeta": {
                    "provider": "openai",
                    "model": "gpt-6.1-sol",
                    "agentHarnessId": "openclaw",
                }
            },
            "payloads": [{"text": '{"angulo":1}'}],
        }
        return type(
            "Salida", (), {"stdout": json.dumps({"status": "ok", "result": result}).encode()}
        )()

    monkeypatch.setattr(director.subprocess, "run", run)
    assert director.run_visual(request) == {"angulo": 1}


def test_valeria_procesa_la_orden_del_titular_al_confirmar_el_brief(monkeypatch, order, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_valeria as director

    journal, mateo, output = (tmp_path / name for name in ("ingesta", "mateo", "valeria"))
    for directory in (journal, mateo, output):
        directory.mkdir()
    name = f"{order['order_id']}.json"
    receipt = {"order": order}
    strategy = {"angulo": "Demostración"}
    (journal / name).write_text(json.dumps(receipt), encoding="utf-8")
    (mateo / name).write_text(json.dumps(strategy), encoding="utf-8")
    profile = tmp_path / "perfil.json"
    profile.write_text(
        json.dumps({"key": "a" * 64, "owner": order["user_id"], "fixture": str(uuid.uuid4())}),
        encoding="utf-8",
    )
    token = tmp_path / "token"
    token.write_text("b" * 64, encoding="ascii")
    monkeypatch.setattr(director, "JOURNAL", journal)
    monkeypatch.setattr(director, "MATEO", mateo)
    monkeypatch.setattr(sys, "argv", ["valeria", str(profile), str(token), str(output)])
    processed = []
    monkeypatch.setattr(director, "process", lambda *args, **opts: processed.append((args, opts)))

    monkeypatch.setattr(director, "lookup", lambda *_: None)
    director.main()
    assert processed == []

    brief = {"objective": "Probar el relevo"}
    monkeypatch.setattr(director, "lookup", lambda *_: brief)
    director.main()
    assert len(processed) == 1
    arguments, options = processed[0]
    assert arguments[:4] == (receipt, strategy, brief, output / name)
    assert options == {"synthetic": False}


@pytest.mark.parametrize("first", ["forma", "json", "modelo", "pausa"])
def test_valeria_corrige_solo_el_formato_de_la_respuesta(monkeypatch, order, tmp_path, first):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_valeria as director

    job = order["media"]
    brief = {"objective": "Mostrar el producto", "audience": "Tiendas", "tone": "Directo"}
    receipt = {
        "order": order,
        "raw": f"proyectos/{job['project_slug']}/01_raw/{job['asset_id']}.mp4",
        "duration_seconds": 4.0,
        "audio": {
            "path": f"proyectos/{job['project_slug']}/03_assets/intake-source.wav",
            "bytes": 12,
            "sha256": "c" * 64,
        },
    }
    strategy = {
        "order_id": order["order_id"],
        "source_sha256": job["sha256"],
        "brief_sha256": hashlib.sha256(director.canonical(brief)).hexdigest(),
        "ejecucion": "isolated-agent-runtime",
        "angulos": [
            {"angulo": angle, "gancho": "Inicio", "copy": "Mensaje", "cta": "Consultar"}
            for angle in ("Uno", "Dos", "Tres")
        ],
    }
    answer = {
        "angulo": 2,
        "titulo": "Prueba de color",
        "subtitulo": "La muestra",
        "observacion": "Fondo oscuro uniforme",
        "apto_sin_cambios": True,
    }
    samples, requests = [], []

    def sample(source):
        samples.append(source)
        return [(1, b"imagen")]

    def run(_argv, *, input, **_opts):
        requests.append(json.loads(input))
        response = answer
        model = "gpt-6.1-sol"
        if len(requests) == 1:
            if first == "forma":
                response = {**answer, "angulo": "2"}
            elif first == "modelo":
                model = "otro-modelo"
            elif first == "pausa":
                response = {**answer, "apto_sin_cambios": False}
        raw = "No es JSON" if len(requests) == 1 and first == "json" else json.dumps(response)
        result = {
            "meta": {
                "agentMeta": {"provider": "openai", "model": model, "agentHarnessId": "openclaw"}
            },
            "payloads": [{"text": raw}],
        }
        return SimpleNamespace(stdout=json.dumps({"status": "ok", "result": result}).encode())

    monkeypatch.setattr(director, "sample_images", sample)
    monkeypatch.setattr(director.subprocess, "run", run)
    monkeypatch.setattr(director, "fingerprint", lambda _: {"bytes": 12, "sha256": "c" * 64})
    target = tmp_path / "escaleta.json"
    request_id = str(uuid.uuid4())
    if first == "modelo":
        with pytest.raises(ValueError, match="gateway"):
            director.process(receipt, strategy, brief, target, request_id)
        assert len(requests) == 1 and not target.exists()
        return
    director.process(receipt, strategy, brief, target, request_id)
    edl = json.loads(target.read_bytes())["escaleta"]
    assert samples == [receipt] and requests[0]["idempotencyKey"] == request_id
    if first == "pausa":
        assert len(requests) == 1 and edl["estado"] == "requiere_revision"
        return
    assert len(requests) == 2 and edl["estado"] == "lista_para_stills"
    assert requests[1]["idempotencyKey"] != request_id
    assert requests[1]["attachments"] == requests[0]["attachments"]
    assert requests[1]["message"].split("\n", 1)[1] == requests[0]["message"].split("\n", 1)[1]
    assert "formato" in requests[1]["message"].split("\n", 1)[0]


@pytest.mark.parametrize("format_failure", [True, False])
def test_valeria_difiere_una_orden_y_avanza_la_siguiente(
    monkeypatch, order, tmp_path, format_failure
):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_valeria as director

    journal, mateo, output = (tmp_path / name for name in ("ingesta", "mateo", "valeria"))
    for directory in (journal, mateo, output):
        directory.mkdir()
    first = "00000000-0000-4000-8000-000000000001"
    second = "00000000-0000-4000-8000-000000000002"
    for identity in (first, second):
        media = {
            **order["media"],
            "job_id": identity,
            "project_slug": "job-" + identity.replace("-", ""),
            "object_key": f"ingest/{identity}/{order['media']['asset_id']}.mp4",
        }
        pending = {**order, "order_id": identity, "media": media}
        (journal / f"{identity}.json").write_bytes(director.canonical({"order": pending}))
        (mateo / f"{identity}.json").write_bytes(b"{}")
    monkeypatch.setattr(director, "JOURNAL", journal)
    monkeypatch.setattr(director, "MATEO", mateo)
    monkeypatch.setattr(director, "lookup", lambda *_: {"objective": "Presentar el producto"})
    now = 1_794_680_000
    monkeypatch.setattr(director.time, "time", lambda: now)
    attempts = []

    def model(receipt, _strategy, _brief, target, key, **_opts):
        identity = receipt["order"]["order_id"]
        attempts.append((identity, key))
        if identity == first and len(attempts) == 1:
            error = director.VisualResponseFormatError if format_failure else TimeoutError
            raise error("Fallo simulado")
        director.publish(target, b'{"escaleta":{"estado":"lista_para_stills"}}')

    monkeypatch.setattr(director, "process", model)
    profile = {"owner": order["user_id"], "fixture": str(uuid.uuid4())}
    director.run_pending(profile, "b" * 64, output)
    assert (output / f"{second}.json").exists() and not (output / f"{first}.json").exists()
    now += 45
    director.run_pending(profile, "b" * 64, output)
    assert [identity for identity, _ in attempts] == [first, second]
    now += 65
    director.run_pending(profile, "b" * 64, output)
    assert (output / f"{first}.json").exists()
    assert [identity for identity, _ in attempts] == [first, second, first]
    assert (attempts[0][1] != attempts[2][1]) == format_failure


def test_valeria_reintenta_la_revision_manual_desde_la_escaleta_guardada(
    monkeypatch, order, tmp_path
):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_valeria as director

    journal, mateo, output = (tmp_path / name for name in ("ingesta", "mateo", "valeria"))
    for directory in (journal, mateo, output):
        directory.mkdir()
    order["plan_id"] = "ultra"
    identity = order["order_id"]
    leaf = identity + ".json"
    brief = {"objective": "Probar la edición", "confirmed_at": 15}
    saved = {
        "order_id": identity,
        "source_sha256": order["media"]["sha256"],
        "brief_sha256": hashlib.sha256(director.canonical(brief)).hexdigest(),
        "escaleta": {"estado": "requiere_revision", "render": None},
    }
    (journal / leaf).write_bytes(director.canonical({"order": order}))
    (mateo / leaf).write_bytes(b"{}")
    (output / leaf).write_bytes(director.canonical({"escaleta": {"estado": "lista_para_stills"}}))
    (output / (identity + "-v2.json")).write_bytes(director.canonical(saved))
    monkeypatch.setattr(director, "JOURNAL", journal)
    monkeypatch.setattr(director, "MATEO", mateo)
    calls = []
    monkeypatch.setattr(director, "lookup", lambda *_: brief if len(calls) < 2 else None)
    monkeypatch.setattr(director, "process", lambda *_a, **_kw: pytest.fail("Recreó la escaleta"))

    def notify(payload, _token):
        if payload["action"] == "manual_context":
            return {"brief": None, "adjustment": None}
        calls.append(payload)
        if len(calls) == 1:
            raise TimeoutError("Se perdió la comunicación con Studio")
        return {"recorded": True}

    monkeypatch.setattr(director, "dispatch_request", notify)
    profile = {"owner": order["user_id"], "fixture": str(uuid.uuid4())}
    now = 1_794_680_000
    monkeypatch.setattr(director.time, "time", lambda: now)
    director.run_pending(profile, "b" * 64, output)
    director.run_pending(profile, "b" * 64, output)
    assert len(calls) == 1
    now += 110
    director.run_pending(profile, "b" * 64, output)
    director.run_pending(profile, "b" * 64, output)
    assert calls == [calls[0], calls[0]]
    assert calls[0] == {
        "action": "quality_manual_review",
        "order_id": identity,
        "user_id": order["user_id"],
        "upload_id": order["upload_id"],
        "source_sha256": order["media"]["sha256"],
        "direction_sha256": hashlib.sha256(director.canonical(saved)).hexdigest(),
        "revision": 2,
        "brief_confirmed_at": 15,
    }


def test_valeria_reconciliacion_reutiliza_la_escaleta_del_ajuste(monkeypatch, order, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_valeria as director

    journal, mateo, output = (tmp_path / name for name in ("ingesta", "mateo", "valeria"))
    for directory in (journal, mateo, output):
        directory.mkdir()
    order["plan_id"] = "ultra"
    identity = order["order_id"]
    leaf = identity + ".json"
    brief = {
        "order_id": identity,
        "user_id": order["user_id"],
        "objective": "Presentar el producto",
        "audience": "Tiendas",
        "tone": "Directo",
        "confirmed_at": 15,
    }
    original = {
        "order_id": identity,
        "source_sha256": order["media"]["sha256"],
        "brief_sha256": hashlib.sha256(director.canonical(brief)).hexdigest(),
        "escaleta": {"estado": "requiere_revision", "render": None},
    }
    corrected = {**original, "escaleta": {"estado": "lista_para_stills", "render": {"props": {}}}}
    raw = director.canonical(original) + b"\n"
    (journal / leaf).write_bytes(director.canonical({"order": order}))
    (mateo / leaf).write_bytes(b"{}")
    target = output / leaf
    target.write_bytes(raw)
    monkeypatch.setattr(director, "JOURNAL", journal)
    monkeypatch.setattr(director, "MATEO", mateo)
    monkeypatch.setattr(director, "lookup", lambda *_: None)
    context = {
        "brief": brief,
        "adjustment": {
            "number": 1,
            "instructions": "Ampliar los subtítulos.",
            "revision": 1,
            "review_sha256": hashlib.sha256(director.canonical(original)).hexdigest(),
        },
    }
    model_calls, results = [], []

    def model(_receipt, _strategy, _brief, path, _key, *, adjustment):
        model_calls.append(adjustment)
        director.publish(path, director.canonical(corrected) + b"\n")

    def dispatch(body, _token):
        if body["action"] == "manual_context":
            return context
        assert body["action"] == "manual_adjustment_result"
        results.append(body)
        if len(results) == 1:
            raise TimeoutError("Respuesta no observada")
        return {"recorded": True}

    monkeypatch.setattr(director, "process", model)
    monkeypatch.setattr(director, "dispatch_request", dispatch)
    profile = {"owner": order["user_id"], "fixture": str(uuid.uuid4())}
    now = 1_794_680_000
    monkeypatch.setattr(director.time, "time", lambda: now)
    director.run_pending(profile, "b" * 64, output)
    assert target.read_bytes() == raw
    now += 110
    director.run_pending(profile, "b" * 64, output)
    assert json.loads(target.read_bytes()) == corrected
    assert model_calls == [context["adjustment"]["instructions"]]
    assert len(results) == 2 and results[0] == results[1]
    assert results[0]["status"] == "ready"
    assert results[0]["review_sha256"] == context["adjustment"]["review_sha256"]
    assert (
        results[0]["direction_sha256"] == hashlib.sha256(director.canonical(corrected)).hexdigest()
    )


def test_elena_aprueba_solo_con_reglas_duras_y_pruebas_visuales(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_elena import make_verdict

    review = {
        "puntaje": 92,
        "marca_free": "Creado con Agencia AI · Free",
        "observaciones": [{"muestra": 2, "detalle": "Marca legible y título dentro del cuadro"}],
        "defectos": [],
    }
    metrics = {
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "codec": "h264",
        "color": "bt709",
        "duration": 4.0,
        "lufs": -14.0,
        "peak": -11.2,
    }
    assert make_verdict(review, metrics, 4.0)["decision"] == "aprobado"
    for plan in ("Pro", "Ultra", "Ilimitado"):
        assert make_verdict(review, metrics, 4.0, plan=plan)["decision"] == "rechazado"
        assert (
            make_verdict({**review, "marca_free": ""}, metrics, 4.0, plan=plan)["decision"]
            == "aprobado"
        )
    assert make_verdict(review, {**metrics, "peak": -0.5}, 4.0)["decision"] == "rechazado"
    assert make_verdict({**review, "marca_free": ""}, metrics, 4.0)["decision"] == "rechazado"
    assert make_verdict({**review, "puntaje": 84}, metrics, 4.0)["decision"] == "rechazado"
    paid_review = {**review, "marca_free": ""}
    four_k = {**metrics, "width": 2160, "height": 3840}
    assert (
        make_verdict(paid_review, four_k, 4.0, plan="Ultra", resolution="4k")["decision"]
        == "aprobado"
    )
    assert (
        make_verdict(paid_review, metrics, 4.0, plan="Ultra", resolution="4k")["decision"]
        == "rechazado"
    )
    with pytest.raises(ValueError):
        make_verdict(
            {**review, "observaciones": [{"muestra": 4, "detalle": "No existe"}]}, metrics, 4.0
        )


def test_elena_reintenta_un_dictamen_sin_json_con_las_mismas_muestras(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_elena as elena

    metrics = {
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "codec": "h264",
        "color": "bt709",
        "duration": 4.0,
        "lufs": -14.0,
        "peak": -11.2,
    }
    images = [(0, b"fotograma"), (2, b"otro fotograma"), (3, b"cierre")]
    monkeypatch.setattr(elena, "decode", lambda *_args, **_options: (metrics, images))
    requests = []

    def run_visual(request):
        requests.append(request)
        if len(requests) == 1:
            raise elena.VisualResponseFormatError("Respuesta visual sin JSON válido")
        return {
            "puntaje": 92,
            "marca_free": "",
            "observaciones": [{"muestra": 1, "detalle": "Rótulo legible"}],
            "defectos": [],
        }

    monkeypatch.setattr(elena, "run_visual", run_visual)
    request_id = str(uuid.uuid4())
    reviewed = elena.review_video(
        tmp_path / "master.mp4",
        "a" * 64,
        {"title": "Sansevierias", "subtitle": "De interior", "duration_seconds": 4.0},
        1,
        "Ilimitado",
        "1080p",
        request_id,
    )
    assert reviewed["dictamen"]["decision"] == "aprobado"
    assert len(requests) == 2
    assert requests[0]["idempotencyKey"] == request_id
    assert requests[1]["idempotencyKey"] != request_id
    assert requests[1]["attachments"] == requests[0]["attachments"]


@pytest.mark.parametrize(
    ("plan", "marca", "resolution", "width", "height"),
    [
        ("Free", True, "1080p", 1080, 1920),
        ("Pro", False, "1080p", 1080, 1920),
        ("Ultra", False, "4k", 2160, 3840),
        ("Ilimitado", False, "4k", 2160, 3840),
    ],
)
def test_lucas_exige_la_marca_del_plan_en_las_capturas(
    monkeypatch, plan, marca, resolution, width, height
):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_lucas as lucas

    body = {
        "project_id": "job-prueba",
        "job_id": "orden-v1",
        "plan": plan,
        "resolution": resolution,
    }
    expected = "proyectos/job-prueba/05_revision/orden-v1/stills/still-0.png"
    file = {"path": expected}
    checked = []
    monkeypatch.setattr(lucas, "verify_file", lambda item, paths: checked.append((item, paths)))
    data = {"width": width, "height": height, "watermark": marca, "frames": [0], "files": [file]}
    lucas.verify_stills(data, body, [0])
    assert checked == [(file, {expected})]
    with pytest.raises(ValueError, match="plan"):
        lucas.verify_stills({**data, "watermark": not marca}, body, [0])
    with pytest.raises(ValueError, match="plan"):
        lucas.verify_stills({**data, "height": height - 2}, body, [0])
    assert checked == [(file, {expected})]


def test_vista_previa_rechazada_bloquea_el_master(monkeypatch, tmp_path, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_lucas as lucas

    order["plan_id"] = "ilimitado"
    order["output_resolution"] = "4k"
    order["output_edit"] = "reel"
    job = order["media"]
    root = f"proyectos/{job['project_slug']}"
    raw, audio = f"{root}/01_raw/{job['asset_id']}.mp4", f"{root}/03_assets/intake-source.wav"
    fingerprint = {key: job[key] for key in ("sha256", "bytes")}
    receipt = {"order": order, "raw": raw, "audio": {"path": audio, **fingerprint}}
    payload = {
        "project_id": job["project_slug"],
        "job_id": order["order_id"] + "-v1",
        "plan": "Ilimitado",
        "composition": "AgencyVertical",
        "format": "mp4",
        "resolution": "4k",
        "props": {
            "video_path": raw,
            "audio_path": audio,
            "duration_seconds": 34,
            "scenes": [
                {
                    "source_start_frame": 0,
                    "source_end_frame": 360,
                    "label": {
                        "text": "Catálogo",
                        "start_frame": 0,
                        "end_frame": 90,
                        "position": "bottom",
                    },
                },
                {"source_start_frame": 660, "source_end_frame": 1320},
            ],
        },
    }
    direction = {
        "order_id": order["order_id"],
        "source_sha256": job["sha256"],
        "brief_sha256": "a" * 64,
        "escaleta": {"estado": "lista_para_stills", "render": payload},
    }
    preview = {
        "path": f"{root}/06_renders/{payload['job_id']}-preview/render/master.mp4",
        **fingerprint,
    }
    calls = []

    def render(kind, body, *args, **kwargs):
        calls.append((kind, body["resolution"], body["job_id"]))
        assert body["job_id"].endswith("-preview")
        return {
            "codec": "h264_nvenc",
            "hardware_acceleration": True,
            "watermark": False,
            "audio_verified": True,
            "audio": {"integrated_lufs": -14, "true_peak_dbtp": -3},
            "media": {"width": 1080, "height": 1920, "fps": 30, "duration_seconds": 34},
            "files": [preview],
        }

    def review(_order, _edl, _job, item, out):
        assert item == preview
        content = {"dictamen": {"decision": "rechazado"}}
        out.write_text(json.dumps(content), encoding="utf-8")
        return content

    monkeypatch.setattr(lucas, "fingerprint", lambda _: fingerprint)
    monkeypatch.setattr(lucas, "verify_file", lambda item, paths: None)
    monkeypatch.setattr(lucas, "submit_with_receipt", render)
    monkeypatch.setattr(lucas, "audit_preview", review, raising=False)
    target = tmp_path / "orden.json"
    lucas.run(order, receipt, direction, "fixture", 1, target)
    assert calls == [("render", "1080p", payload["job_id"] + "-preview")]
    record = json.loads(target.read_bytes())
    assert record["preview"] == preview
    assert "master" not in record


def test_lucas_verifica_todas_las_fuentes_y_elena_puede_rechazar(monkeypatch, tmp_path, order):
    import production_lucas as lucas
    from production_lucas import run

    first, second = order["media"]["asset_id"], str(uuid.uuid4())
    project = order["media"]["project_slug"]
    extra = {
        **order["media"],
        "asset_id": second,
        "object_key": f"ingest/{order['order_id']}/{second}.mp4",
    }
    order = {**order, "output_edit": "reel", "additional_media": [extra]}
    mark = {"sha256": order["media"]["sha256"], "bytes": order["media"]["bytes"]}
    audio1 = f"proyectos/{project}/03_assets/intake-source.wav"
    audio2 = f"proyectos/{project}/03_assets/intake-source-{second}.wav"
    video1, video2 = (f"proyectos/{project}/01_raw/{item}.mp4" for item in (first, second))
    receipt = {
        "order": order,
        "raw": video1,
        "duration_seconds": 50,
        "audio": {**mark, "path": audio1},
        "additional_sources": [
            {
                "media": extra,
                "raw": video2,
                "duration_seconds": 30,
                "audio": {**mark, "path": audio2},
            }
        ],
    }
    sources = [
        {"id": first, "video_path": video1, "audio_path": audio1},
        {"id": second, "video_path": video2, "audio_path": audio2},
    ]
    body = {
        "project_id": project,
        "job_id": order["order_id"] + "-v1",
        "plan": "Free",
        "composition": "AgencyVertical",
        "format": "mp4",
        "resolution": "1080p",
        "props": {"sources": sources, "duration_seconds": 30, "scenes": [{"source_id": first}]},
    }
    edl = {
        "order_id": order["order_id"],
        "source_sha256": mark["sha256"],
        "brief_sha256": "a" * 64,
        "escaleta": {"estado": "lista_para_stills", "render": body},
    }
    checks, submissions = [], []
    monkeypatch.setattr(lucas, "fingerprint", lambda path: checks.append(str(path)) or mark)
    monkeypatch.setattr(
        lucas,
        "submit_with_receipt",
        lambda kind, payload, *_args, **opts: (
            submissions.append((kind, payload, opts["source"])) or {}
        ),
    )
    monkeypatch.setattr(lucas, "verify_mp4", lambda *_: {"path": "vista"})

    def reject(_order, _edl, _job, _asset, report):
        result = {"dictamen": {"decision": "rechazado"}}
        report.write_text(json.dumps(result), encoding="utf-8")
        return result

    monkeypatch.setattr(lucas, "audit_preview", reject)
    target = tmp_path / "resultado.json"
    run(order, receipt, edl, "fixture", 1, target)
    assert len(checks) == 4
    assert len(submissions) == 1 and submissions[0][0] == "render"
    assert [item["id"] for item in submissions[0][2]["materials"]] == [first, second]
    assert "master" not in json.loads(target.read_bytes())


def test_lucas_serializa_los_textos_como_utf8(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_lucas import render_payload

    body = {"props": {"title": "Revisión de edición", "subtitle": "Imagen y audio · demostración"}}
    raw = render_payload(body)
    assert isinstance(raw, bytes)
    assert "Revisión".encode() in raw
    assert json.loads(raw) == body


def test_lucas_espera_el_intervalo_de_cache_nfs(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_lucas as lucas

    attempts = []
    item = {"path": "master.mp4", "bytes": 4, "sha256": "a" * 64}

    def measure(_path):
        attempts.append(1)
        if len(attempts) <= 12:
            raise FileNotFoundError
        return {"bytes": item["bytes"], "sha256": item["sha256"]}

    monkeypatch.setattr(lucas.Path, "resolve", lambda path, **_: path)
    monkeypatch.setattr(lucas, "fingerprint", measure)
    monkeypatch.setattr(lucas.time, "sleep", lambda _: None)
    lucas.verify_file(item, {item["path"]})
    assert len(attempts) == 13


def test_lucas_reutiliza_el_recibo_del_puente_en_un_reintento(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_lucas as lucas

    calls = []
    result = {"frames": [0, 60, 119]}

    def submit(kind, body, token):
        calls.append((kind, body, token))
        return result

    monkeypatch.setattr(lucas, "submit", submit)
    journal = tmp_path / "stills.json"
    request = {"job_id": "orden-v1", "frames": [0, 60, 119]}
    assert lucas.submit_with_receipt("stills", request, "prueba", journal) == result
    assert lucas.submit_with_receipt("stills", request, "prueba", journal) == result
    assert calls == [("stills", request, "prueba")]
    assert "prueba" not in journal.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="no corresponde"):
        lucas.submit_with_receipt("stills", {**request, "frames": [0]}, "prueba", journal)
    assert len(calls) == 1


def test_lucas_y_elena_relevan_la_misma_orden_vigente(monkeypatch, tmp_path, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_elena as elena
    import production_lucas as lucas

    journal, direction, rendered, reviewed = (
        tmp_path / n for n in ("ingesta", "edl", "render", "qa")
    )
    for directory in (journal, direction, rendered, reviewed):
        directory.mkdir()
    name = order["order_id"] + ".json"
    brief = {"objective": "Relevo automático"}
    digest = hashlib.sha256(lucas.canonical(brief)).hexdigest()
    edl = {"brief_sha256": digest, "escaleta": {"estado": "lista_para_stills"}}
    profile = {"owner": order["user_id"], "fixture": str(uuid.uuid4())}
    (journal / name).write_text(json.dumps({"order": order}), encoding="utf-8")
    (direction / name).write_text(json.dumps(edl), encoding="utf-8")
    for service in (lucas, elena):
        monkeypatch.setattr(service, "JOURNAL", journal)
        monkeypatch.setattr(service, "VALERIA", direction)
        monkeypatch.setattr(service, "lookup", lambda *_: brief)
    monkeypatch.setattr(elena, "LUCAS", rendered)
    receipt = {
        "order_id": order["order_id"],
        "brief_sha256": digest,
        "master": {"sha256": "a" * 64},
    }

    def render(actual_order, _intake, actual_edl, _token, revision, output):
        assert (actual_order, actual_edl, revision, output) == (order, edl, 1, rendered / name)
        output.write_text(json.dumps(receipt), encoding="utf-8")

    audits = []
    monkeypatch.setattr(lucas, "run", render)

    def approve(*args, **kwargs):
        audits.append((args, kwargs))
        return {"dictamen": {"decision": "aprobado"}}

    monkeypatch.setattr(elena, "audit", approve)
    lucas.run_pending(profile, "b" * 64, "a" * 64, rendered)
    elena.run_pending(profile, "a" * 64, reviewed)
    assert len(audits) == 1
    arguments, options = audits[0]
    assert arguments[:3] == (order, edl, receipt["master"]["sha256"])
    assert arguments[4:] == (reviewed / name, receipt, 1)
    assert options == {"synthetic": False}


def test_lucas_respeta_el_reintento_y_renderiza_otra_orden(monkeypatch, tmp_path, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_lucas as lucas

    journal, direction, rendered = (tmp_path / n for n in ("ingesta", "edl", "render"))
    for directory in (journal, direction, rendered):
        directory.mkdir()
    blocked = "11111111-1111-4111-8111-111111111111"
    ready = "22222222-2222-4222-8222-222222222222"
    brief = {"objective": "Prueba de reintento de render"}
    digest = hashlib.sha256(lucas.canonical(brief)).hexdigest()
    for identity in (blocked, ready):
        media = {
            **order["media"],
            "job_id": identity,
            "project_slug": "job-" + identity.replace("-", ""),
            "object_key": f"ingest/{identity}/{order['media']['asset_id']}.mp4",
        }
        pending = {**order, "order_id": identity, "media": media}
        name = f"{identity}.json"
        (journal / name).write_bytes(lucas.canonical({"order": pending}))
        edl = {"brief_sha256": digest, "escaleta": {"estado": "lista_para_stills"}}
        (direction / name).write_bytes(lucas.canonical(edl))
    profile = {"owner": order["user_id"], "fixture": str(uuid.uuid4())}
    clock, fails = 1000, True
    monkeypatch.setattr(lucas, "JOURNAL", journal)
    monkeypatch.setattr(lucas, "VALERIA", direction)
    monkeypatch.setattr(lucas, "lookup", lambda *_: brief)
    monkeypatch.setattr(lucas, "time", SimpleNamespace(time=lambda: clock))

    def render(pending, _receipt, _edl, _token, _revision, target):
        if pending["order_id"] == blocked and fails:
            raise TimeoutError("Puente temporalmente no disponible")
        target.write_bytes(lucas.canonical({"order_id": pending["order_id"]}))

    monkeypatch.setattr(lucas, "run", render)
    with pytest.raises(TimeoutError):
        lucas.run_pending(profile, "b" * 64, "a" * 64, rendered)
    clock += 55
    lucas.run_pending(profile, "b" * 64, "a" * 64, rendered)
    assert (rendered / f"{ready}.json").is_file()
    clock += 5
    with pytest.raises(TimeoutError):
        lucas.run_pending(profile, "b" * 64, "a" * 64, rendered)
    fails = False
    clock += 119
    lucas.run_pending(profile, "b" * 64, "a" * 64, rendered)
    assert not (rendered / f"{blocked}.json").exists()
    clock += 1
    lucas.run_pending(profile, "b" * 64, "a" * 64, rendered)
    assert (rendered / f"{blocked}.json").is_file()


@pytest.mark.parametrize(
    ("plan", "rounds", "approval"),
    [("pro", 2, 0), ("ultra", 3, 3), ("ilimitado", 3, 0), ("ultra", 3, 1)],
)
def test_revisiones_pagadas_relevan_el_dictamen_y_el_master_exacto(
    monkeypatch, tmp_path, order, plan, rounds, approval
):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_delivery as delivery
    import production_elena as elena
    import production_lucas as lucas
    import production_valeria as valeria
    from production_handoff import canonical, revision_from_stem, revision_leaf

    folders = {key: tmp_path / key for key in ("JOURNAL", "MATEO", "VALERIA", "LUCAS", "ELENA")}
    for key, folder in folders.items():
        folder.mkdir()
        for stage in (valeria, lucas, elena, delivery):
            if hasattr(stage, key):
                monkeypatch.setattr(stage, key, folder)

    identity = order["order_id"]
    order["plan_id"] = plan
    order["output_resolution"] = "1080p" if plan == "pro" else "4k"
    lead = {"owner": str(uuid.uuid4()), "fixture": str(uuid.uuid4())}
    brief = {"order_id": identity, "user_id": order["user_id"], "confirmed_at": 123}
    metadata = {
        "order_id": identity,
        "brief_sha256": hashlib.sha256(canonical(brief)).hexdigest(),
        "source_sha256": order["media"]["sha256"],
    }
    (folders["JOURNAL"] / revision_leaf(identity, 1)).write_bytes(canonical({"order": order}))
    (folders["MATEO"] / revision_leaf(identity, 1)).write_bytes(canonical({}))
    for stage in (valeria, lucas, elena):
        monkeypatch.setattr(stage, "lookup", lambda *_: brief)

    directed, rendered, reviewed, reports = [], [], [], []

    def direct(_receipt, _strategy, _brief, target, _request_id, *, prior=None, synthetic):
        _, revision = revision_from_stem(target.stem)
        assert not synthetic
        if revision > 1:
            assert prior["dictamen"]["defectos"] == [{"muestra": revision - 1}]
        else:
            assert prior is None
        directed.append(revision)
        target.write_bytes(canonical({**metadata, "escaleta": {"estado": "lista_para_stills"}}))

    def render(_order, _receipt, _edl, _token, revision, target):
        rendered.append(revision)
        target.write_bytes(
            canonical(
                {
                    **metadata,
                    "job_id": f"{identity}-v{revision}",
                    "master": {"sha256": str(revision) * 64},
                }
            )
        )

    def audit(_order, _edl, digest, _request_id, target, artifact, revision, *, synthetic):
        assert not synthetic and digest == str(revision) * 64
        reviewed.append(revision)
        report = {
            **metadata,
            "job_id": artifact["job_id"],
            "master_sha256": digest,
            "dictamen": {
                "decision": "aprobado" if revision == approval else "rechazado",
                "defectos": [] if revision == approval else [{"muestra": revision}],
            },
        }
        target.write_bytes(canonical(report))
        return report

    def record(payload, _token):
        reports.append((payload["revision"], payload["master_sha256"]))
        return {"recorded": True}

    monkeypatch.setattr(valeria, "process", direct)
    monkeypatch.setattr(lucas, "run", render)
    monkeypatch.setattr(elena, "audit", audit)
    monkeypatch.setattr(elena, "dispatch_request", record)
    for _ in range(rounds):
        valeria.run_pending(lead, "a" * 64, folders["VALERIA"])
        lucas.run_pending(lead, "b" * 64, "a" * 64, folders["LUCAS"])
        elena.run_pending(lead, "a" * 64, folders["ELENA"])
    valeria.run_pending(lead, "a" * 64, folders["VALERIA"])
    lucas.run_pending(lead, "b" * 64, "a" * 64, folders["LUCAS"])
    expected = list(range(1, (approval or rounds) + 1))
    assert directed == rendered == reviewed == expected
    approved = delivery.approved_revision(identity, max_revision=rounds)
    if approval:
        assert reports == []
        assert approved[0]["master"]["sha256"] == approved[1]["master_sha256"] == str(approval) * 64
    else:
        assert reports == [(rounds, str(rounds) * 64)]
        assert approved is None


def test_elena_reintenta_el_rechazo_guardado_sin_repetir_la_auditoria(monkeypatch, tmp_path, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_elena as elena

    journal, direction, rendered, reviewed = (
        tmp_path / name for name in ("ingesta", "escaleta", "render", "qa")
    )
    for folder in (journal, direction, rendered, reviewed):
        folder.mkdir()
    identity = order["order_id"]
    leaf = identity + ".json"
    profile = {"owner": order["user_id"], "fixture": str(uuid.uuid4())}
    brief = {"order_id": identity, "user_id": order["user_id"], "confirmed_at": 123}
    digest = hashlib.sha256(elena.canonical(brief)).hexdigest()
    master = "d" * 64
    receipt = {"order_id": identity, "job_id": identity + "-v1", "brief_sha256": digest}
    review = {**receipt, "master_sha256": master, "dictamen": {"decision": "rechazado"}}
    for path, body in (
        (journal / leaf, {"order": order}),
        (direction / leaf, {"brief_sha256": digest}),
        (
            rendered / leaf,
            {**receipt, "source_sha256": order["media"]["sha256"], "master": {"sha256": master}},
        ),
    ):
        path.write_bytes(elena.canonical(body))
    monkeypatch.setattr(elena, "JOURNAL", journal)
    monkeypatch.setattr(elena, "VALERIA", direction)
    monkeypatch.setattr(elena, "LUCAS", rendered)
    monkeypatch.setattr(elena, "lookup", lambda *_: brief)
    calls, audits = [], []

    def audit(*_args, **_kwargs):
        audits.append(review)
        (reviewed / leaf).write_bytes(elena.canonical(review))
        return review

    def dispatch(payload, token):
        assert token == "a" * 64
        calls.append(payload)
        if len(calls) == 1:
            raise TimeoutError("Respuesta perdida")
        return {"recorded": True}

    monkeypatch.setattr(elena, "audit", audit)
    monkeypatch.setattr(elena, "dispatch_request", dispatch)
    with pytest.raises(TimeoutError):
        elena.run_pending(profile, "a" * 64, reviewed)
    elena.run_pending(profile, "a" * 64, reviewed)
    assert audits == [review]
    assert calls == [calls[0], calls[0]]
    assert calls[0] == {
        "action": "quality_rejected",
        "order_id": identity,
        "user_id": order["user_id"],
        "upload_id": order["upload_id"],
        "source_sha256": order["media"]["sha256"],
        "master_sha256": master,
        "revision": 1,
        "brief_confirmed_at": 123,
    }
    (reviewed / leaf).write_bytes(elena.canonical({**review, "master_sha256": "e" * 64}))
    with pytest.raises(ValueError, match="revisión vigente"):
        elena.run_pending(profile, "a" * 64, reviewed)
    assert len(calls) == 2


@pytest.mark.parametrize("escenario", ["prueba", "titular", "titular_designado"])
def test_relevo_tras_rechazo_respeta_el_limite_del_piloto(monkeypatch, tmp_path, order, escenario):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_elena as elena
    import production_lucas as lucas
    import production_valeria as valeria

    intake, strategy, direction, render, review = (
        tmp_path / name for name in ("ingesta", "estrategia", "escaleta", "render", "qa")
    )
    for folder in (intake, strategy, direction, render, review):
        folder.mkdir()
    identity = order["order_id"]
    first = f"{identity}.json"
    second = f"{identity}-v2.json"
    brief = {
        "order_id": identity,
        "objective": "Demostración",
        "audience": "Clientes",
        "tone": "",
        "confirmed_at": 123,
    }
    brief_hash = hashlib.sha256(valeria.canonical(brief)).hexdigest()
    source_hash = order["media"]["sha256"]
    master_hash = "a" * 64
    initial_edl = {
        "order_id": identity,
        "source_sha256": source_hash,
        "brief_sha256": brief_hash,
        "escaleta": {"estado": "lista_para_stills", "angulo": "Primero"},
    }
    initial_render = {
        "order_id": identity,
        "brief_sha256": brief_hash,
        "source_sha256": source_hash,
        "job_id": f"{identity}-v1",
        "master": {"sha256": master_hash},
    }
    verdict = {
        "order_id": identity,
        "brief_sha256": brief_hash,
        "job_id": f"{identity}-v1",
        "master_sha256": master_hash,
        "dictamen": {
            "decision": "rechazado",
            "defectos": [{"muestra": 2, "correccion": "Acortar"}],
        },
    }
    for folder, body in (
        (intake, {"order": order}),
        (strategy, {}),
        (direction, initial_edl),
        (render, initial_render),
        (review, verdict),
    ):
        (folder / first).write_text(json.dumps(body), encoding="utf-8")
    prueba_configurada = escenario == "prueba"
    profile = {
        "owner": str(uuid.uuid4()) if prueba_configurada else order["user_id"],
        "fixture": str(uuid.uuid4()) if escenario == "titular" else identity,
    }
    for stage in (valeria, lucas, elena):
        monkeypatch.setattr(stage, "JOURNAL", intake)
        monkeypatch.setattr(stage, "lookup", lambda *_: brief)
    monkeypatch.setattr(valeria, "MATEO", strategy)
    monkeypatch.setattr(valeria, "LUCAS", render)
    monkeypatch.setattr(valeria, "ELENA", review)
    monkeypatch.setattr(lucas, "VALERIA", direction)
    monkeypatch.setattr(elena, "VALERIA", direction)
    monkeypatch.setattr(elena, "LUCAS", render)
    correction_receipt = {}

    def correct(_receipt, _strategy, _brief, target, _request_id, *, prior, synthetic):
        assert prueba_configurada
        assert target == direction / second and synthetic
        assert prior["dictamen"]["defectos"] == verdict["dictamen"]["defectos"]
        correction_receipt.update(initial_edl)
        target.write_text(json.dumps(correction_receipt), encoding="utf-8")

    def render_second(_order, _receipt, edl, _token, revision, target):
        assert (revision, target, edl) == (2, render / second, correction_receipt)
        (render / second).write_text(
            json.dumps(
                {**initial_render, "job_id": f"{identity}-v2", "master": {"sha256": "b" * 64}}
            ),
            encoding="utf-8",
        )

    audits = []
    monkeypatch.setattr(valeria, "process", correct)
    monkeypatch.setattr(lucas, "run", render_second)
    monkeypatch.setattr(elena, "audit", lambda *args, **kwargs: audits.append((args, kwargs)))
    if not prueba_configurada:
        rejected = []

        def record(payload, _token):
            rejected.append(payload["revision"])
            return {"recorded": True}

        monkeypatch.setattr(elena, "dispatch_request", record)
        valeria.run_pending(profile, "a" * 64, direction)
        assert not (direction / second).exists()
        (direction / second).write_text(json.dumps(initial_edl), encoding="utf-8")
        lucas.run_pending(profile, "b" * 64, "a" * 64, render)
        assert not (render / second).exists()
        (render / second).write_text(json.dumps(initial_render), encoding="utf-8")
        elena.run_pending(profile, "a" * 64, review)
        assert not audits
        assert rejected == [1]
        return
    invalid = {**verdict, "master_sha256": "c" * 64}
    (review / first).write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValueError, match="no corresponde"):
        valeria.prior_rejection(order, brief, 1, direction, invalid)
    now = 1_794_680_000
    monkeypatch.setattr(valeria.time, "time", lambda: now)
    valeria.run_pending(profile, "a" * 64, direction)
    assert not (direction / second).exists()
    now += 110
    approved = {**verdict, "dictamen": {"decision": "aprobado", "defectos": []}}
    (review / first).write_text(json.dumps(approved), encoding="utf-8")
    valeria.run_pending(profile, "a" * 64, direction)
    assert not (direction / second).exists()
    (review / first).write_text(json.dumps(verdict), encoding="utf-8")
    valeria.run_pending(profile, "a" * 64, direction)
    valeria.run_pending(profile, "a" * 64, direction)
    lucas.run_pending(profile, "b" * 64, "a" * 64, render)
    lucas.run_pending(profile, "b" * 64, "a" * 64, render)
    elena.run_pending(profile, "a" * 64, review)
    assert len(audits) == 1
    arguments, _ = audits[0]
    assert (arguments[2], arguments[4], arguments[6]) == ("b" * 64, review / second, 2)
    (review / second).write_text(json.dumps(verdict), encoding="utf-8")
    valeria.run_pending(profile, "a" * 64, direction)
    lucas.run_pending(profile, "b" * 64, "a" * 64, render)
    elena.run_pending(profile, "a" * 64, review)
    assert len(audits) == 1
    assert json.loads((review / first).read_text(encoding="utf-8")) == verdict
    assert not any(
        folder.joinpath(f"{identity}-v3.json").exists() for folder in (direction, render, review)
    )


def test_kit_exige_aprobacion_y_huella_del_master(monkeypatch, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_delivery import package

    master = b"video acreditado"
    digest = hashlib.sha256(master).hexdigest()
    render = {
        "order_id": order["order_id"],
        "job_id": "prueba-v3",
        "master": {"sha256": digest, "bytes": len(master)},
    }
    qa = {
        "order_id": order["order_id"],
        "job_id": "prueba-v3",
        "master_sha256": digest,
        "dictamen": {"decision": "aprobado", "puntaje": 94, "reglas_duras_incumplidas": []},
    }
    result = package(order["order_id"], master, render, qa, {"titulo": "Reel de prueba"})
    assert result == package(order["order_id"], master, render, qa, {"titulo": "Reel de prueba"})
    stream = io.BytesIO()
    package(
        order["order_id"],
        io.BytesIO(master),
        render,
        qa,
        {"titulo": "Reel de prueba"},
        output=stream,
    )
    assert stream.getvalue() == result
    caption = "Una revisión de cuatro segundos."
    with zipfile.ZipFile(
        io.BytesIO(package(order["order_id"], master, render, qa, {}, caption=caption))
    ) as kit:
        encoded = (caption + "\n").encode()
        assert kit.read("05-caption.txt") == encoded
        assert (hashlib.sha256(encoded).hexdigest() + "  05-caption.txt\n").encode() in kit.read(
            "SHA256SUMS"
        )
    with pytest.raises(ValueError):
        package(order["order_id"], b"otro", render, qa, {})
    qa["dictamen"]["reglas_duras_incumplidas"] = ["Audio fuera de contrato"]
    with pytest.raises(ValueError):
        package(order["order_id"], master, render, qa, {})


@pytest.mark.parametrize("round_number", (1, 2))
def test_base_de_revision_acredita_zip_escaleta_y_aprobacion(
    handoff, order, tmp_path, monkeypatch, round_number
):
    import production_elena as reviewer
    import production_lucas as editor
    import production_revision as revisions
    import production_valeria as director
    from production_delivery import package
    from production_revision import (
        approved_base,
        draft_revision,
        load_claim_context,
        publish_staged_revision,
        render_and_review,
        stage_approved_revision,
    )

    identity = order["order_id"]
    brief = {"order_id": identity, "user_id": order["user_id"], "objective": "Plantas"}
    brief_hash = hashlib.sha256(handoff.canonical(brief)).hexdigest()
    direction = {
        "order_id": identity,
        "source_sha256": order["media"]["sha256"],
        "brief_sha256": brief_hash,
        "escaleta": {"estado": "lista_para_stills"},
    }
    direction_hash = hashlib.sha256(handoff.canonical(direction)).hexdigest()
    master = b"video aprobado"
    master_hash = hashlib.sha256(master).hexdigest()
    render = {
        "order_id": identity,
        "source_sha256": order["media"]["sha256"],
        "brief_sha256": brief_hash,
        "direction_sha256": direction_hash,
        "job_id": identity + "-v1",
        "master": {"sha256": master_hash, "bytes": len(master)},
    }
    qa = {
        "order_id": identity,
        "job_id": render["job_id"],
        "brief_sha256": brief_hash,
        "direction_sha256": direction_hash,
        "master_sha256": master_hash,
        "mediciones": {"duration": 30},
        "dictamen": {"decision": "aprobado", "puntaje": 94, "reglas_duras_incumplidas": []},
    }
    raw = package(identity, master, render, qa, brief)
    path = tmp_path / "kit.zip"
    path.write_bytes(raw)
    claim = {
        "id": str(uuid.uuid4()),
        "base_kit_id": str(uuid.uuid4()),
        "order_id": identity,
        "user_id": order["user_id"],
        "base_kit_sha256": hashlib.sha256(raw).hexdigest(),
        "instructions": "Cambiar el ritmo",
        "lease": str(uuid.uuid4()),
    }
    assert approved_base(claim, {"order": order}, direction, path)["review"] == qa
    monkeypatch.setattr(revisions, "MEDIA", tmp_path)
    context_dirs = {
        name: tmp_path / name for name in ("journal", "strategy", "direction", "drafts")
    }
    for directory in context_dirs.values():
        directory.mkdir()
    (context_dirs["journal"] / f"{identity}.json").write_text(json.dumps({"order": order}))
    (context_dirs["strategy"] / f"{identity}.json").write_text(json.dumps({"strategy": "test"}))
    (context_dirs["direction"] / f"{identity}.json").write_text(json.dumps(direction))
    stored_base = (
        tmp_path
        / "proyectos"
        / order["media"]["project_slug"]
        / "05_revision"
        / f"kit-studio-{claim['base_kit_sha256']}.zip"
    )
    stored_base.parent.mkdir(parents=True)
    stored_base.write_bytes(raw)
    context = load_claim_context(claim, *context_dirs.values())
    assert context["receipt"]["order"] == order and context["direction"] == direction
    generated = []

    def fake_process(*args, **kwargs):
        generated.append((args, kwargs))
        args[3].write_text(json.dumps(direction))

    monkeypatch.setattr(director, "process", fake_process)
    target = tmp_path / f"{claim['id']}.json"
    draft_revision(claim, {"order": order}, {"strategy": "test"}, direction, path, target)
    assert generated[0][1]["adjustment"] == "Cambiar el ritmo"
    assert generated[0][1]["user_revision"] is True
    assert generated[0][1]["prior"]["dictamen"] == qa["dictamen"]
    assert (
        draft_revision(claim, {"order": order}, {"strategy": "test"}, direction, path, target)
        == direction
    )
    assert len(generated) == 1
    attempt_id = revisions.revision_attempt_id(claim["id"], 2)
    rejected = {**direction["escaleta"], "dictamen": {"decision": "rechazado"}}
    draft_revision(
        {**claim, "id": attempt_id},
        {"order": order},
        {"strategy": "test"},
        direction,
        path,
        tmp_path / f"{attempt_id}.json",
        rejected=rejected,
    )
    assert generated[-1][1]["user_revision"] is True
    assert generated[-1][1]["prior"] == rejected
    with pytest.raises(ValueError, match="contenido distinto"):
        draft_revision(
            {**claim, "instructions": "Otro cambio"}, {"order": order}, {}, direction, path, target
        )
    render_dir, review_dir = tmp_path / "render", tmp_path / "review"
    render_dir.mkdir()
    review_dir.mkdir()
    calls = []

    def fake_render(_order, _receipt, edl, _token, _revision, dest, *, revision_id):
        calls.append(("render", revision_id))
        dest.write_text(
            json.dumps(
                {
                    "order_id": order["order_id"],
                    "source_sha256": order["media"]["sha256"],
                    "brief_sha256": brief_hash,
                    "job_id": f"{revision_id}-r",
                    "direction_sha256": hashlib.sha256(handoff.canonical(edl)).hexdigest(),
                    "master": {"sha256": "c" * 64},
                }
            )
        )

    def fake_audit(_order, _edl, digest, _request, dest, _rendered, _revision, *, revision_id):
        calls.append(("review", revision_id, digest))
        record = {
            "order_id": order["order_id"],
            "job_id": f"{revision_id}-r",
            "brief_sha256": brief_hash,
            "direction_sha256": direction_hash,
            "master_sha256": digest,
            "dictamen": {"decision": "aprobado"},
        }
        dest.write_text(json.dumps(record))
        return record

    monkeypatch.setattr(editor, "run", fake_render)
    monkeypatch.setattr(reviewer, "audit", fake_audit)
    result = render_and_review(
        claim,
        {"order": order},
        direction,
        path,
        direction,
        render_dir / target.name,
        review_dir / target.name,
        "token",
    )
    assert result["status"] == "aprobado"
    assert calls == [("render", claim["id"]), ("review", claim["id"], "c" * 64)]
    repeat = render_and_review(
        claim,
        {"order": order},
        direction,
        path,
        direction,
        render_dir / target.name,
        review_dir / target.name,
        "token",
    )
    assert repeat == result
    assert calls.count(("review", claim["id"], "c" * 64)) == 1
    forged = {**repeat["review"], "master_sha256": "f" * 64}
    (review_dir / target.name).write_text(json.dumps(forged))
    with pytest.raises(ValueError, match="dictamen no corresponde"):
        render_and_review(
            claim,
            {"order": order},
            direction,
            path,
            direction,
            render_dir / target.name,
            review_dir / target.name,
            "token",
        )
    monkeypatch.setattr(revisions, "MEDIA", tmp_path)
    import production_delivery as delivery

    monkeypatch.setattr(delivery, "MEDIA", tmp_path)
    attempt_id = revisions.revision_attempt_id(claim["id"], round_number)
    attempt_claim = {**claim, "id": attempt_id}
    new_job = f"{attempt_id}-r"
    new_path = (
        Path("proyectos")
        / order["media"]["project_slug"]
        / "06_renders"
        / new_job
        / "render"
        / "master.mp4"
    )
    (tmp_path / new_path).parent.mkdir(parents=True)
    (tmp_path / new_path).write_bytes(master)
    new_render = {
        **render,
        "job_id": new_job,
        "master": {"sha256": master_hash, "bytes": len(master), "path": new_path.as_posix()},
    }
    new_review = {**qa, "job_id": new_job}
    staged = stage_approved_revision(
        attempt_claim,
        {"order": order},
        direction,
        path,
        direction,
        new_render,
        new_review,
        None,
        tmp_path / "recibo",
    )
    assert (tmp_path / staged["path"]).is_file()
    uploaded = (tmp_path / staged["path"]).read_bytes()
    calls_published = []

    def r2(method, _path, headers, body, **options):
        if method == "PUT":
            assert body.read() == uploaded
            return 200, {}, b""
        assert method == "GET"
        return (
            200,
            {
                "content-length": str(len(uploaded)),
                "content-type": "application/zip",
                "x-amz-meta-sha256": staged["sha256"],
            },
            options["read_body"](io.BytesIO(uploaded)),
        )

    def studio(manifest):
        calls_published.append(manifest)
        return {
            "kit": {
                "id": manifest["id"],
                "order_id": manifest["orderId"],
                "title": manifest["title"],
                "bytes": manifest["bytes"],
            }
        }

    published = publish_staged_revision(
        claim,
        {"order": order},
        direction,
        path,
        direction,
        new_render,
        new_review,
        staged,
        "Revisión",
        r2,
        studio,
        attempt=round_number,
    )
    assert published["id"] == calls_published[0]["id"]
    assert calls_published[0]["revisionId"] == claim["id"]
    assert calls_published[0]["lease"] == claim["lease"]
    (context_dirs["drafts"] / f"{attempt_id}.json").write_text(json.dumps(direction))
    next_claim = {**claim, "base_kit_id": published["id"], "base_kit_sha256": staged["sha256"]}
    assert load_claim_context(next_claim, *context_dirs.values())["direction"] == direction
    with pytest.raises(ValueError, match="no tiene una aprobación"):
        stage_approved_revision(
            attempt_claim,
            {"order": order},
            direction,
            path,
            direction,
            new_render,
            {**new_review, "direction_sha256": "b" * 64},
            None,
            tmp_path / "recibo",
        )
    with pytest.raises(ValueError, match="no comparten"):
        approved_base(claim, {"order": order}, {**direction, "escaleta": {}}, path)
    with pytest.raises(ValueError, match="Solicitud"):
        approved_base({**claim, "user_id": str(uuid.uuid4())}, {"order": order}, direction, path)
    path.write_bytes(raw + b"alterado")
    with pytest.raises(ValueError, match="entrega base cambió"):
        approved_base(claim, {"order": order}, direction, path)


def test_kit_en_nas_se_publica_una_vez_y_rechaza_un_master_cambiado(monkeypatch, order, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_delivery as delivery

    monkeypatch.setattr(delivery, "MEDIA", tmp_path)
    project = tmp_path / "proyectos" / order["media"]["project_slug"]
    project.mkdir(parents=True)
    master = project / "master.mp4"
    master.write_bytes(b"video")
    digest = hashlib.sha256(b"video").hexdigest()
    render = {
        "order_id": order["order_id"],
        "job_id": order["order_id"] + "-v1",
        "master": {"sha256": digest, "bytes": 5},
    }
    qa = {
        "order_id": order["order_id"],
        "job_id": render["job_id"],
        "master_sha256": digest,
        "dictamen": {"decision": "aprobado", "puntaje": 94, "reglas_duras_incumplidas": []},
    }
    first = delivery.stage_package(order, master, render, qa, {})
    assert delivery.stage_package(order, master, render, qa, {}) == first
    saved = tmp_path / first[0]
    assert saved.read_bytes() == delivery.package(order["order_id"], b"video", render, qa, {})
    master.write_bytes(b"ajeno")
    with pytest.raises(ValueError, match="huella del máster"):
        delivery.stage_package(order, master, render, qa, {})
    assert list((project / "05_revision").iterdir()) == [saved]


def test_entrega_programada_selecciona_la_ultima_revision(monkeypatch, order, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_delivery as delivery

    identity = order["order_id"]
    for role in ("LUCAS", "ELENA"):
        folder = tmp_path / role
        folder.mkdir()
        monkeypatch.setattr(delivery, role, folder)
    for name, job, decision in ((identity, 1, "rechazado"), (identity + "-v2", 2, "aprobado")):
        row = {"order_id": identity, "job_id": f"{identity}-v{job}"}
        (delivery.LUCAS / f"{name}.json").write_text(json.dumps(row), encoding="utf-8")
        qa = {**row, "dictamen": {"decision": decision}}
        (delivery.ELENA / f"{name}.json").write_text(json.dumps(qa), encoding="utf-8")
    render, qa = delivery.approved_revision(identity)
    assert render["job_id"] == qa["job_id"] == f"{identity}-v2"
    assert delivery.approved_revision(identity, max_revision=1) is None
    (delivery.LUCAS / f"{identity}-v3.json").write_text(
        json.dumps({"order_id": identity, "job_id": f"{identity}-v3"}), encoding="utf-8"
    )
    assert delivery.approved_revision(identity) is None
    (delivery.ELENA / f"{identity}-v3.json").write_text(
        json.dumps(
            {
                "order_id": identity,
                "job_id": f"{identity}-v3",
                "dictamen": {"decision": "rechazado"},
            }
        ),
        encoding="utf-8",
    )
    assert delivery.approved_revision(identity) is None


def test_caption_usa_el_angulo_del_mismo_brief(monkeypatch, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_delivery import approved_caption

    render = {"source_sha256": order["media"]["sha256"], "brief_sha256": "b" * 64}
    receipt = {"order_id": order["order_id"], **render}
    direction = {**receipt, "escaleta": {"angulo": "Segundo"}}
    angles = [
        {"angulo": angle, "gancho": "Gancho", "copy": f"Caption {angle}", "cta": "Revisa el clip"}
        for angle in ("Primero", "Segundo", "Tercero")
    ]
    strategy = {**receipt, "angulos": angles}
    assert approved_caption(order, render, direction, strategy) == "Caption Segundo"
    with pytest.raises(ValueError, match="otro encargo"):
        approved_caption(order, render, direction, {**strategy, "brief_sha256": "c" * 64})


def test_prores_usa_la_escaleta_y_el_audio_del_master_aprobado(monkeypatch, order, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_delivery as delivery
    import production_lucas as lucas
    from production_handoff import canonical

    if os.name == "nt":
        monkeypatch.setattr(os, "O_NOFOLLOW", os.O_BINARY, raising=False)
        monkeypatch.setattr(os, "O_NONBLOCK", 0, raising=False)

    order = {
        **order,
        "plan_id": "ilimitado",
        "output_resolution": "4k",
        "output_format": "prores4444",
    }
    identity = order["order_id"]
    prefix = f"proyectos/{order['media']['project_slug']}"
    audio_path = f"{prefix}/03_assets/intake-source.wav"
    audio = tmp_path / audio_path
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"pista")
    audio_hash = hashlib.sha256(b"pista").hexdigest()
    props = {
        "title": "Título de la revisión dos",
        "subtitle": "Subtítulo verificado",
        "subtitle_size": "large",
        "duration_seconds": 60,
        "video_path": f"{prefix}/01_raw/{order['media']['asset_id']}.mp4",
        "audio_path": audio_path,
    }
    intake = {
        "order": order,
        "raw": props["video_path"],
        "audio": {
            "path": audio_path,
            "bytes": 5,
            "sha256": audio_hash,
        },
    }
    direction = {
        "order_id": identity,
        "source_sha256": order["media"]["sha256"],
        "brief_sha256": "b" * 64,
        "escaleta": {
            "estado": "lista_para_stills",
            "render": {
                "project_id": order["media"]["project_slug"],
                "job_id": identity + "-v1",
                "composition": "AgencyVertical",
                "plan": "Ilimitado",
                "format": "mp4",
                "resolution": "4k",
                "props": props,
            },
        },
    }
    render = {
        "order_id": identity,
        "job_id": identity + "-v2",
        "source_sha256": order["media"]["sha256"],
        "brief_sha256": "b" * 64,
        "master": {"sha256": "c" * 64},
        "direction_sha256": hashlib.sha256(canonical(direction)).hexdigest(),
    }
    qa = {
        "order_id": identity,
        "job_id": render["job_id"],
        "master_sha256": render["master"]["sha256"],
        "dictamen": {"decision": "aprobado", "puntaje": 94, "reglas_duras_incumplidas": []},
        "mediciones": {"duration": 60},
    }
    job = render["job_id"] + "-grafica"
    path = f"{prefix}/06_renders/{job}/render/master.mov"
    graphic = tmp_path / path
    graphic.parent.mkdir(parents=True)
    graphic.write_bytes(b"mov")
    artifact = {"path": path, "sha256": hashlib.sha256(b"mov").hexdigest(), "bytes": 3}
    monkeypatch.setattr(delivery, "MEDIA", tmp_path)
    monkeypatch.setattr(lucas, "MEDIA", tmp_path)
    calls = []

    def submit(kind, body, token):
        calls.append(body)
        assert kind == "render" and token == "g" * 64
        assert body["job_id"] == job and body["format"] == "prores4444"
        assert body["resolution"] == "4k"
        assert body["props"] == {k: v for k, v in props.items() if k != "video_path"}
        return {
            "files": [artifact],
            "codec": "prores_ks",
            "resolution": "4k",
            "hardware_acceleration": False,
            "audio_verified": True,
            "alpha_verified": True,
            "watermark": False,
            "audio": {"integrated_lufs": -14, "true_peak_dbtp": -2},
            "media": {
                "video_codec": "prores",
                "video_profile": "4444",
                "codec_tag": "ap4h",
                "audio_codec": "pcm_s24le",
                "sample_rate": 48000,
                "pixel_format": "yuva444p12le",
                "width": 2160,
                "height": 3840,
                "fps": 30,
                "duration_seconds": 60,
                "color_space": "bt709",
                "color_transfer": "bt709",
                "color_primaries": "bt709",
            },
        }

    monkeypatch.setattr(lucas, "submit", submit)
    receipt = tmp_path / "grafica.receipt"
    args = (order, intake, render, qa, direction, "g" * 64, receipt)
    assert delivery.approved_graphic(*args) == artifact
    assert delivery.approved_graphic(*args) == artifact
    assert len(calls) == 1
    with pytest.raises(ValueError, match="dictamen"):
        delivery.approved_graphic(
            order, intake, render, {**qa, "master_sha256": "d" * 64}, direction, "g" * 64, receipt
        )
    props["title"] = "Título de otra revisión"
    with pytest.raises(ValueError, match="escaleta"):
        delivery.approved_graphic(*args)


@pytest.mark.parametrize("falla", [None, "gráfica", "vista previa"])
def test_entrega_prores_solapa_vista_previa_y_espera_ambas_operaciones(
    monkeypatch, order, tmp_path, falla
):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_delivery as delivery

    identity = order["order_id"]
    order.update(plan_id="ilimitado", output_resolution="4k", output_format="prores4444")
    for name in ("JOURNAL", "MATEO", "VALERIA"):
        folder = tmp_path / name.lower()
        folder.mkdir()
        (folder / f"{identity}.json").write_text(json.dumps({"order": order}), encoding="utf-8")
        monkeypatch.setattr(delivery, name, folder)
    output = tmp_path / "salida"
    output.mkdir()
    target = output / f"{identity}.json"
    artifact = {"bytes": 3, "path": "grafica.mov", "sha256": "a" * 64}
    render = {"job_id": f"{identity}-v1", "master": {"sha256": "b" * 64}}
    result = {"bytes": 4, "sha256": "c" * 64}
    view_started, graphic_started = Event(), Event()
    view_done, graphic_done = Event(), Event()
    calls = []

    def attach_preview(project, task, revision, path, sha256):
        calls.append("vista previa")
        assert (project, task, revision, path, sha256) == (
            "proyecto",
            "tarea",
            1,
            "master.mp4",
            "b" * 64,
        )
        view_started.set()
        assert graphic_started.wait(2)
        if falla == "vista previa":
            raise ValueError(falla)
        view_done.set()

    def graphic(*args):
        graphic_started.set()
        assert view_started.wait(2)
        if falla == "gráfica":
            raise ValueError(falla)
        graphic_done.set()
        return artifact

    def attach_zip(project, task, revision, path, sha256):
        calls.append("ZIP")
        assert view_done.is_set() and graphic_done.is_set() and not target.exists()
        assert (project, task, revision, path, sha256) == (
            "proyecto",
            "tarea",
            1,
            "kit.zip",
            result["sha256"],
        )
        return result

    monkeypatch.setitem(
        sys.modules,
        "call_stdio",
        SimpleNamespace(ENV_BY_SERVER={"mcp-kitsu-review": set()}, runtime_settings=dict),
    )
    monkeypatch.setitem(
        sys.modules,
        "kitsu_review",
        SimpleNamespace(attach_preview=attach_preview, attach_delivery_zip=attach_zip),
    )
    monkeypatch.setenv("AGENCY_MEDIA_PROJECTS", "")
    monkeypatch.setenv("KITSU_REVIEW_PROJECTS", "")
    monkeypatch.setattr(delivery, "MEDIA", tmp_path)
    monkeypatch.setattr(delivery, "approved_revision", lambda *args, **kwargs: (render, {}))
    monkeypatch.setattr(delivery, "lookup", lambda *args: {"brief": "Vídeo"})
    monkeypatch.setattr(delivery, "approved_master", lambda *args: tmp_path / "master.mp4")
    monkeypatch.setattr(delivery, "approved_caption", lambda *args: "Texto")
    monkeypatch.setattr(
        delivery, "stage_package", lambda *args, **kwargs: ("kit.zip", result["sha256"], 4)
    )
    monkeypatch.setattr(delivery, "approved_graphic", graphic)
    profile = {"owner": order["user_id"], "fixture": str(uuid.uuid4())}
    assignments = {identity: {"id": "kit", "project": "proyecto", "task": "tarea"}}

    if falla is not None:
        assert delivery.run_pending(profile, assignments, "despacho", output, "gpu") == 1
        assert calls == ["vista previa"] and not target.exists()
        return
    delivery.run_pending(profile, assignments, "despacho", output, "gpu")
    delivery.run_pending(profile, assignments, "despacho", output, "gpu")
    assert calls == ["vista previa", "ZIP"]
    assert json.loads(target.read_bytes())["prores"] == artifact


def test_publicacion_exige_lectura_integra_antes_de_cerrar_el_pedido(monkeypatch, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_publish import deliver

    kit = b"archivo zip"
    manifest = {
        "orderId": order["order_id"],
        "id": str(uuid.uuid4()),
        "sha256": hashlib.sha256(kit).hexdigest(),
        "bytes": len(kit),
    }
    history = []

    def request(method, path, headers, body, **options):
        history.append(method)
        if method == "PUT":
            assert headers["If-None-Match"] == "*" and body.read(len(kit)) == kit
            assert headers["Content-Length"] == str(len(kit))
            assert options["payload_sha256"] == manifest["sha256"]
            return 412, {}, b""
        return (
            200,
            {
                "x-amz-meta-sha256": manifest["sha256"],
                "content-type": "application/zip",
                "content-length": str(len(kit)),
            },
            options["read_body"](io.BytesIO(b"x" * len(kit))),
        )

    with pytest.raises(ValueError):
        deliver(io.BytesIO(kit), manifest, request, lambda _: history.append("PUBLICAR"))
    assert history == ["PUT", "GET"]

    manifest["title"] = "Prueba"

    def intact(method, path, headers, body, **options):
        status, received, raw = request(method, path, headers, body, **options)
        return status, received, options["read_body"](io.BytesIO(kit)) if method == "GET" else raw

    published = {
        "id": manifest["id"],
        "order_id": manifest["orderId"],
        "title": manifest["title"],
        "bytes": len(kit),
    }
    assert deliver(io.BytesIO(kit), manifest, intact, lambda _: {"kit": published}) == published


def test_entrega_reintenta_el_adjuntar_por_pedido_sin_bloquear_el_siguiente(
    monkeypatch, order, tmp_path
):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_delivery as delivery

    first = "00000000-0000-4000-8000-000000000001"
    second = "00000000-0000-4000-8000-000000000002"
    output = tmp_path / "salida"
    output.mkdir()
    assignments = {
        identity: {"id": identity, "project": identity, "task": identity}
        for identity in (first, second)
    }
    for name in ("JOURNAL", "MATEO", "VALERIA"):
        folder = tmp_path / name.lower()
        folder.mkdir()
        for identity in assignments:
            media = {
                **order["media"],
                "job_id": identity,
                "project_slug": "job-" + identity.replace("-", ""),
            }
            media["object_key"] = f"ingest/{identity}/{media['asset_id']}.{media['extension']}"
            (folder / f"{identity}.json").write_text(
                json.dumps({"order": {**order, "order_id": identity, "media": media}}),
                encoding="utf-8",
            )
        monkeypatch.setattr(delivery, name, folder)

    attempts = []
    attachment = {"sha256": "c" * 64, "bytes": 4}

    def attach_zip(project, task, revision, path, digest):
        attempts.append(task)
        if attempts == [first]:
            raise OSError("Kitsu no disponible para el primer pedido")
        return attachment

    monkeypatch.setitem(
        sys.modules,
        "call_stdio",
        SimpleNamespace(ENV_BY_SERVER={"mcp-kitsu-review": set()}, runtime_settings=dict),
    )
    monkeypatch.setitem(
        sys.modules,
        "kitsu_review",
        SimpleNamespace(attach_preview=lambda *_: None, attach_delivery_zip=attach_zip),
    )
    monkeypatch.setenv("AGENCY_MEDIA_PROJECTS", "")
    monkeypatch.setenv("KITSU_REVIEW_PROJECTS", "")
    monkeypatch.setattr(delivery, "MEDIA", tmp_path)
    monkeypatch.setattr(
        delivery, "approved_revision", lambda *a, **k: ({"master": {"sha256": "b" * 64}}, {})
    )
    monkeypatch.setattr(delivery, "lookup", lambda *a: {"brief": "Vídeo"})
    monkeypatch.setattr(delivery, "approved_master", lambda *a: tmp_path / "master.mp4")
    monkeypatch.setattr(delivery, "approved_caption", lambda *a: "Texto")
    monkeypatch.setattr(
        delivery, "stage_package", lambda *a, **k: ("kit.zip", attachment["sha256"], 4)
    )
    profile = {"owner": order["user_id"], "fixture": str(uuid.uuid4())}
    now = 100
    monkeypatch.setattr("time.time", lambda: now)

    assert delivery.run_pending(profile, assignments, "despacho", output) == 1
    assert attempts == [first, second]
    assert not (output / f"{first}.json").exists()
    assert json.loads((output / f"{second}.json").read_text())["kitsu"] == attachment
    now = 129
    assert delivery.run_pending(profile, assignments, "despacho", output) == 0
    assert attempts == [first, second]
    now = 130
    assert delivery.run_pending(profile, assignments, "despacho", output) == 0
    assert attempts == [first, second, first]
    assert json.loads((output / f"{first}.json").read_text())["kitsu"] == attachment
    with sqlite3.connect(output / "reintentos/entrega.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM retries").fetchone() == (0,)


def test_publicador_obtiene_la_duracion_del_dictamen_exacto(monkeypatch, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from production_publish import approved_duration_ms

    def kit(identity):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "04-dictamen-qa.json",
                json.dumps(
                    {
                        "order_id": identity,
                        "mediciones": {"duration": 60.034},
                        "dictamen": {"decision": "aprobado"},
                    }
                ),
            )
        buffer.seek(0)
        return buffer

    assert approved_duration_ms(kit(order["order_id"]), order["order_id"]) == 60034
    with pytest.raises(ValueError, match="Duración del dictamen inválida"):
        approved_duration_ms(kit(str(uuid.uuid4())), order["order_id"])


def test_publica_mov_por_partes_y_cierra_solo_tras_verificarlo(monkeypatch, order):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_publish as publisher

    monkeypatch.setattr(publisher, "PRORES_PART_BYTES", 4)
    movie, kit = b"grafica", b"zip"
    manifest = {
        "id": str(uuid.uuid4()),
        "orderId": order["order_id"],
        "title": "Prueba",
        "bytes": len(kit),
        "sha256": hashlib.sha256(kit).hexdigest(),
        "durationMs": 60000,
        "proresBytes": len(movie),
        "proresSha256": hashlib.sha256(movie).hexdigest(),
    }
    accepted = False
    have_movie = False
    uploaded = {}
    second_uploaded = Event()
    calls = []

    def request(method, path, headers, body, **options):
        nonlocal have_movie
        calls.append(method)
        if path.endswith(".zip"):
            if method == "PUT":
                assert body.read() == kit
                return 200, {}, b""
            expected, payload, mime = manifest["sha256"], kit, "application/zip"
        else:
            assert path.endswith(".mov")
            if method == "HEAD":
                return (200 if have_movie else 404), {}, b""
            query = dict(options.get("query", ()))
            if method == "POST" and query == {"uploads": ""}:
                return (
                    200,
                    {},
                    b"<InitiateMultipartUploadResult><UploadId>a+/=</UploadId></InitiateMultipartUploadResult>",
                )
            if method == "PUT":
                number = int(query["partNumber"])
                assert query["uploadId"] == "a+/=" and number in {1, 2}
                if number == 1:
                    assert second_uploaded.wait(2)
                uploaded[number] = body
                if number == 2:
                    second_uploaded.set()
                assert headers["Content-Length"] == str(len(body))
                return 200, {"etag": '"' + str(number) * 32 + '"'}, b""
            if method == "POST":
                assert query == {"uploadId": "a+/="} and headers["If-None-Match"] == "*"
                parts = ElementTree.fromstring(body)
                assert [part.findtext("PartNumber") for part in parts] == ["1", "2"]
                assert [part.findtext("ETag") for part in parts] == [
                    '"' + n * 32 + '"' for n in "12"
                ]
                have_movie = True
                return 200, {}, b"<CompleteMultipartUploadResult/>"
            expected, payload, mime = manifest["proresSha256"], movie, "video/quicktime"
            if not accepted:
                payload = b"x" * len(movie)
        return (
            200,
            {
                "content-type": mime,
                "content-length": str(len(payload)),
                "x-amz-meta-sha256": expected,
            },
            options["read_body"](io.BytesIO(payload)),
        )

    def close(value):
        assert accepted and value == manifest
        return {
            "kit": {
                "id": manifest["id"],
                "order_id": manifest["orderId"],
                "title": manifest["title"],
                "bytes": len(kit),
                "prores": {"bytes": len(movie)},
            }
        }

    with pytest.raises(ValueError, match="descarga de R2"):
        publisher.deliver(io.BytesIO(kit), manifest, request, close, graphic=io.BytesIO(movie))
    assert uploaded == {1: movie[:4], 2: movie[4:]}
    attempts = len(uploaded)
    accepted = True
    assert publisher.deliver(io.BytesIO(kit), manifest, request, close, graphic=io.BytesIO(movie))[
        "prores"
    ]["bytes"] == len(movie)
    assert len(uploaded) == attempts
    assert calls[-2:] == ["HEAD", "GET"]


def test_publicador_aborta_carga_prores_despues_de_esperar_las_partes(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_publish as publisher

    monkeypatch.setattr(publisher, "PRORES_PART_BYTES", 4)
    movie = b"grafica"
    finished = Event()
    calls = []

    def request(method, path, headers, body, **options):
        query = dict(options.get("query", ()))
        calls.append(method)
        if method == "HEAD":
            return 404, {}, b""
        if method == "POST" and query == {"uploads": ""}:
            return (
                200,
                {},
                b"<InitiateMultipartUploadResult><UploadId>a</UploadId></InitiateMultipartUploadResult>",
            )
        if method == "PUT" and query["partNumber"] == "1":
            return 503, {}, b""
        if method == "PUT" and query["partNumber"] == "2":
            finished.set()
            return 200, {"etag": '"' + "2" * 32 + '"'}, b""
        assert method == "DELETE" and query == {"uploadId": "a"}
        assert finished.is_set()
        return 204, {}, b""

    with pytest.raises(ValueError, match="rechazó una parte"):
        publisher.put_graphic(
            request,
            "/grafica.mov",
            io.BytesIO(movie),
            len(movie),
            hashlib.sha256(movie).hexdigest(),
        )
    assert calls == ["HEAD", "POST", "PUT", "PUT", "DELETE"]


def test_la_siguiente_parte_prores_avanza_sin_esperar_a_la_mas_lenta(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_publish as publisher

    monkeypatch.setattr(publisher, "PRORES_PART_BYTES", 4)
    monkeypatch.setattr(publisher, "PRORES_PART_WORKERS", 3)
    movie = b"grafica en partes"
    digest = hashlib.sha256(movie).hexdigest()
    fourth_started = Event()

    def request(method, path, headers, body, **options):
        query = dict(options.get("query", ()))
        if method == "HEAD":
            return 404, {}, b""
        if method == "POST" and query == {"uploads": ""}:
            return (
                200,
                {},
                b"<InitiateMultipartUploadResult><UploadId>a</UploadId></InitiateMultipartUploadResult>",
            )
        if method == "PUT":
            index = int(query["partNumber"])
            assert body == movie[(index - 1) * 4 : index * 4]
            if index == 1:
                assert fourth_started.wait(2)
            if index == 4:
                fourth_started.set()
            return 200, {"etag": '"' + str(index) * 32 + '"'}, b""
        if method == "POST":
            parts = ElementTree.fromstring(body)
            assert [part.findtext("PartNumber") for part in parts] == ["1", "2", "3", "4", "5"]
            return 200, {}, b"<CompleteMultipartUploadResult/>"
        if method == "DELETE":
            return 204, {}, b""
        assert method == "GET"
        return (
            200,
            {
                "content-type": "video/quicktime",
                "content-length": str(len(movie)),
                "x-amz-meta-sha256": digest,
            },
            options["read_body"](io.BytesIO(movie)),
        )

    publisher.put_graphic(request, "/grafica.mov", io.BytesIO(movie), len(movie), digest)


def test_publicador_no_cierra_multipart_si_la_huella_local_difiere(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_publish as publisher

    movie = b"grafica"
    calls = []

    def request(method, path, headers, body, **options):
        calls.append(method)
        if method == "HEAD":
            return 404, {}, b""
        if method == "POST" and dict(options["query"]) == {"uploads": ""}:
            return (
                200,
                {},
                b"<InitiateMultipartUploadResult><UploadId>a</UploadId></InitiateMultipartUploadResult>",
            )
        if method == "PUT":
            return 200, {"etag": '"' + "1" * 32 + '"'}, b""
        assert method == "DELETE" and dict(options["query"]) == {"uploadId": "a"}
        return 204, {}, b""

    with pytest.raises(ValueError, match="fuera del manifiesto"):
        publisher.put_graphic(request, "/grafica.mov", io.BytesIO(movie), len(movie), "f" * 64)
    assert calls == ["HEAD", "POST", "PUT", "DELETE"]


def test_publicador_reintenta_por_pedido_sin_bloquear_otras_entregas(monkeypatch, order, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import production_publish as publisher

    first = "00000000-0000-4000-8000-000000000001"
    second = "00000000-0000-4000-8000-000000000002"
    owner = order["user_id"]
    source = tmp_path / "ingesta"
    delivery = tmp_path / "entregas"
    output = tmp_path / "publicadas"
    for directory in (source, delivery, output):
        directory.mkdir()
    monkeypatch.setattr(publisher, "DELIVERY", delivery)
    monkeypatch.setattr(publisher, "JOURNAL", source)
    monkeypatch.setattr(publisher, "validate", lambda record, identity: None)
    calls = []
    lookup = {"id": second, "task": first, "project": second, "title": "Prueba"}
    for identity in (first, second):
        (source / f"{identity}.json").write_text(
            json.dumps({"order": {**order, "order_id": identity}})
        )
        (delivery / f"{identity}.json").write_text(
            json.dumps(
                {
                    "order_id": identity,
                    "id": lookup["id"],
                    "project": lookup["project"],
                    "kitsu": {"task_id": lookup["task"]},
                }
            )
        )
    profile = {"owner": owner, "fixture": first}

    def publish(record, reviewed, info, connections, target):
        calls.append(record["order_id"])
        if calls.count(first) == 1 and record["order_id"] == first:
            raise publisher.StorageQuotaExhausted("Cuota agotada")
        target.write_bytes(b"recibo")

    monkeypatch.setattr(publisher, "publish_order", publish)
    monkeypatch.setattr(publisher, "credentials", lambda *_: ())
    now = 100
    monkeypatch.setattr(publisher.time, "time", lambda: now)
    assignments = {first: lookup, second: lookup}
    assert publisher.run_pending(profile, assignments, None, None, output) == 1
    assert calls == [first, second]
    assert (output / f"{second}.json").is_file()
    now = 101
    assert publisher.run_pending(profile, assignments, None, None, output) == 0
    assert calls == [first, second]
    now = 130
    assert publisher.run_pending(profile, assignments, None, None, output) == 0
    assert calls == [first, second, first]
    assert (output / f"{first}.json").is_file()


def test_anuncio_verifica_voz_mezclada_y_alineacion(handoff):
    from production_elena import compare_mix_speech

    captions = [
        {"start_frame": 0, "end_frame": 30, "text": "Sí, la planta."},
        {"start_frame": 60, "end_frame": 87, "text": "Mira el macetero."},
    ]
    vocabulary = ("Sí", "la", "planta", "Mira", "el", "macetero")
    start = (0.05, 0.25, 0.5, 2.05, 2.2, 2.45)
    words = [
        {"word": word, "start": at, "end": at + 0.15}
        for word, at in zip(vocabulary, start, strict=True)
    ]
    assert compare_mix_speech(captions, words)["aprobado"]
    assert not compare_mix_speech(captions, [])["aprobado"]
    for word in words:
        word["start"] += 4
        word["end"] += 4
    assert not compare_mix_speech(captions, words)["aprobado"]


def test_subtitulos_anuncio_conservan_frases_y_posesivos(handoff):
    from production_valeria import ad_captions

    words = [
        {"word": "Algunos", "start": 0, "end": 0.5},
        {"word": "maceteros", "start": 0.5, "end": 1},
        {"word": "los", "start": 1, "end": 1.3},
        {"word": "hace", "start": 1.3, "end": 1.7},
        {"word": "mi", "start": 1.7, "end": 1.9},
        {"word": "marido.", "start": 2.5, "end": 2.9},
        {"word": "Son", "start": 3.05, "end": 3.4},
        {"word": "artesanales.", "start": 3.4, "end": 4},
    ]
    cues = ad_captions([{"source_start_frame": 0, "source_end_frame": 135}], words)
    assert [cue["text"] for cue in cues] == [
        "Algunos maceteros los hace",
        "mi marido.",
        "Son artesanales.",
    ]
