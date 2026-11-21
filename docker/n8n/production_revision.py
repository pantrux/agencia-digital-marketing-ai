"""Verifica la entrega aprobada que servirá de base para una revisión."""

import argparse
import hashlib
import hmac
import json
import re
import threading
import uuid
import zipfile
from contextlib import ExitStack
from pathlib import Path

from production_delivery import MAX_KIT, digest_stream, open_media
from production_handoff import (
    QA_ROUNDS,
    canonical,
    eligible,
    load_profile,
    publish,
    read_regular,
    revision_leaf,
    validate,
)
from production_inbox import MEDIA, UUID, matches, render_format
from production_sofia import INTAKE_LIMIT, LIMIT

METADATA_LIMIT = 65536


def legacy_direction_proof(order, receipt, direction, render, brief_hash):
    """Reconstruye el vínculo de escaletas antiguas desde el recibo del puente GPU."""
    from production_lucas import verified_inputs, verify_mp4

    identity = order["order_id"]
    job = render.get("job_id")
    valid_jobs = {f"{identity}-v{number}": number for number in (1, 2, 3)}
    if job not in valid_jobs:
        raise ValueError("La entrega antigua no tiene un recibo verificable")
    leaf = revision_leaf(identity, valid_jobs[job])
    raw = read_regular(LUCAS / (Path(leaf).stem + ".render.receipt"), 131072)
    stored = json.loads(raw)
    original = direction["escaleta"]["render"]
    body = {**original, "job_id": job}
    source = verified_inputs(order, receipt, original["props"], brief_hash)
    expected = hashlib.sha256(canonical({"request": body, "source": source})).hexdigest()
    if (
        type(stored) is not dict
        or set(stored) != {"request_sha256", "result"}
        or not isinstance(stored["request_sha256"], str)
        or not hmac.compare_digest(stored["request_sha256"], expected)
    ):
        raise ValueError("El recibo antiguo no corresponde a la escaleta")
    if verify_mp4(stored["result"], body) != render.get("master"):
        raise ValueError("El recibo antiguo no corresponde al máster aprobado")


LUCAS = Path("/var/lib/agency-production-lucas")


def load_claim_context(claim, journal, strategies, directions, revision_drafts):
    """Localiza el material original usando solo identidades y huellas acreditadas."""
    if (
        not isinstance(claim, dict)
        or not matches(UUID, claim.get("id"))
        or not matches(UUID, claim.get("order_id"))
    ):
        raise ValueError("Solicitud de revisión inválida")
    identity = claim["order_id"]
    receipt = json.loads(read_regular(Path(journal) / f"{identity}.json", INTAKE_LIMIT))
    order = receipt.get("order")
    if not isinstance(order, dict):
        raise ValueError("Ingesta de revisión inválida")
    validate(order, identity)
    if order["mode"] != "sandbox" or order["user_id"] != claim.get("user_id"):
        raise ValueError("La revisión no corresponde a la orden")
    digest = claim.get("base_kit_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Huella de entrega base inválida")
    kit_path = (
        MEDIA
        / "proyectos"
        / order["media"]["project_slug"]
        / "05_revision"
        / f"kit-studio-{digest}.zip"
    )
    if kit_path.resolve(strict=True) != kit_path:
        raise ValueError("Ruta del kit inválida")
    with open_media(kit_path, MAX_KIT) as (stream, size):
        if not hmac.compare_digest(digest_stream(stream, size), digest):
            raise ValueError("La entrega base cambió")
        stream.seek(0)
        try:
            with zipfile.ZipFile(stream) as archive:
                entries = [
                    item for item in archive.infolist() if item.filename == "04-dictamen-qa.json"
                ]
                if len(entries) != 1 or not 0 < entries[0].file_size <= METADATA_LIMIT:
                    raise ValueError("Dictamen del kit inválido")
                review = json.loads(archive.read(entries[0]))
        except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Kit de revisión inválido") from error
    job = review.get("job_id") if isinstance(review, dict) else None
    if job in {f"{identity}-v{number}" for number in (1, 2, 3)}:
        number = int(job[-1])
        leaf = f"{identity}.json" if number == 1 else f"{identity}-v{number}.json"
        direction_path = Path(directions) / leaf
    elif isinstance(job, str) and job.endswith("-r") and matches(UUID, job[:-2]):
        direction_path = Path(revision_drafts) / f"{job[:-2]}.json"
    else:
        raise ValueError("Trabajo de la entrega base inválido")
    direction = json.loads(read_regular(direction_path, METADATA_LIMIT))
    strategy = json.loads(read_regular(Path(strategies) / f"{identity}.json", LIMIT))
    approved_base(claim, receipt, direction, kit_path)
    return {"receipt": receipt, "strategy": strategy, "direction": direction, "kit_path": kit_path}


def approved_base(claim, receipt, direction, kit_path):
    """Acredita ZIP, escaleta y dictamen antes de usar sus datos como contexto."""
    if (
        not isinstance(claim, dict)
        or not isinstance(receipt, dict)
        or not isinstance(direction, dict)
    ):
        raise ValueError("Contexto de revisión inválido")
    order = receipt.get("order")
    if not isinstance(order, dict):
        raise ValueError("Ingesta de revisión inválida")
    validate(order, order.get("order_id"))
    identity = order["order_id"]
    digest = claim.get("base_kit_sha256")
    if (
        claim.get("order_id") != identity
        or claim.get("user_id") != order["user_id"]
        or not matches(UUID, claim.get("id"))
        or not matches(UUID, claim.get("base_kit_id"))
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or not isinstance(claim.get("instructions"), str)
        or not 0 < len(claim["instructions"].strip()) <= 800
    ):
        raise ValueError("Solicitud de revisión inválida")
    path = Path(kit_path)
    if path.resolve(strict=True) != path:
        raise ValueError("Ruta del kit inválida")
    with open_media(path, MAX_KIT) as (stream, size):
        if not hmac.compare_digest(digest_stream(stream, size), digest):
            raise ValueError("La entrega base cambió")
        stream.seek(0)
        try:
            with zipfile.ZipFile(stream) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                required = ("02-brief.json", "03-render.json", "04-dictamen-qa.json")
                if any(names.count(name) != 1 for name in required):
                    raise ValueError("El kit no contiene una auditoría única")
                records = []
                for name in required:
                    info = archive.getinfo(name)
                    if info.file_size < 1 or info.file_size > METADATA_LIMIT:
                        raise ValueError("Auditoría del kit fuera de límite")
                    records.append(json.loads(archive.read(info)))
        except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Kit de revisión inválido") from error
    brief, render, review = records
    if any(type(record) is not dict for record in records):
        raise ValueError("Auditoría del kit inválida")
    brief_hash = hashlib.sha256(canonical(brief)).hexdigest()
    direction_hash = hashlib.sha256(canonical(direction)).hexdigest()
    verdict = review.get("dictamen", {})
    if (
        type(verdict) is not dict
        or type(direction.get("escaleta")) is not dict
        or type(render.get("master")) is not dict
    ):
        raise ValueError("Auditoría del kit inválida")
    if (
        brief.get("order_id") != identity
        or brief.get("user_id") != order["user_id"]
        or direction.get("order_id") != identity
        or direction.get("source_sha256") != order["media"]["sha256"]
        or direction.get("brief_sha256") != brief_hash
        or direction.get("escaleta", {}).get("estado") != "lista_para_stills"
        or render.get("order_id") != identity
        or render.get("source_sha256") != order["media"]["sha256"]
        or render.get("brief_sha256") != brief_hash
        or ("direction_sha256" in render and render["direction_sha256"] != direction_hash)
        or ("direction_sha256" in review and review["direction_sha256"] != direction_hash)
        or review.get("order_id") != identity
        or review.get("brief_sha256") != brief_hash
        or review.get("job_id") != render.get("job_id")
        or review.get("master_sha256") != render.get("master", {}).get("sha256")
        or verdict.get("decision") != "aprobado"
        or type(verdict.get("puntaje")) is not int
        or not 85 <= verdict["puntaje"] <= 100
        or verdict.get("reglas_duras_incumplidas") != []
    ):
        raise ValueError("La entrega y la escaleta no comparten una aprobación verificable")
    if "direction_sha256" not in render or "direction_sha256" not in review:
        legacy_direction_proof(order, receipt, direction, render, brief_hash)
    return {"brief": brief, "render": render, "review": review, "direction": direction}


def draft_revision(claim, receipt, strategy, direction, kit_path, target, *, rejected=None):
    """Solicita una escaleta nueva sin alterar la escaleta ni el kit aprobados."""
    from production_valeria import process

    base = approved_base(claim, receipt, direction, kit_path)
    target = Path(target)
    if target.name != f"{claim['id']}.json" or target.parent.resolve(strict=True) != target.parent:
        raise ValueError("Destino de revisión inválido")
    intent = {
        "id": claim["id"],
        "order_id": claim["order_id"],
        "user_id": claim["user_id"],
        "base_kit_id": claim["base_kit_id"],
        "base_kit_sha256": claim["base_kit_sha256"],
        "instructions": claim["instructions"].strip(),
        "direction_sha256": hashlib.sha256(canonical(direction)).hexdigest(),
        "brief_sha256": hashlib.sha256(canonical(base["brief"])).hexdigest(),
        "strategy_sha256": hashlib.sha256(canonical(strategy)).hexdigest(),
    }
    if rejected is not None:
        intent["rejected_sha256"] = hashlib.sha256(canonical(rejected)).hexdigest()
    publish(target.parent / f".{claim['id']}.intent.json", canonical(intent) + b"\n")
    prior = (
        rejected
        if rejected is not None
        else {
            **base["direction"]["escaleta"],
            "dictamen": base["review"]["dictamen"],
            "muestras": base["review"].get("muestras", []),
        }
    )
    request_id = str(uuid.uuid5(uuid.UUID(claim["id"]), "direccion-revision-v1"))
    if not target.exists():
        process(
            receipt,
            strategy,
            base["brief"],
            target,
            request_id,
            prior=prior,
            adjustment=claim["instructions"].strip(),
            user_revision=rejected is None,
        )
    result = json.loads(read_regular(target, METADATA_LIMIT))
    if (
        result.get("order_id") != claim["order_id"]
        or result.get("source_sha256") != receipt["order"]["media"]["sha256"]
        or result.get("brief_sha256") != intent["brief_sha256"]
        or result.get("escaleta", {}).get("estado")
        not in ("lista_para_stills", "requiere_revision")
    ):
        raise ValueError("La escaleta guardada no corresponde a la revisión")
    return result


def render_and_review(
    claim, receipt, direction, kit_path, draft, render_target, review_target, token
):
    """Renderiza y revisa la nueva escaleta sin modificar la entrega base."""
    from production_elena import audit
    from production_lucas import run

    base = approved_base(claim, receipt, direction, kit_path)
    identity = claim["id"]
    for target in (render_target, review_target):
        if target.name != f"{identity}.json" or target.parent.resolve(strict=True) != target.parent:
            raise ValueError("Destino de revisión inválido")
    if (
        draft.get("order_id") != claim["order_id"]
        or draft.get("source_sha256") != receipt["order"]["media"]["sha256"]
        or draft.get("brief_sha256") != hashlib.sha256(canonical(base["brief"])).hexdigest()
        or draft.get("escaleta", {}).get("estado") != "lista_para_stills"
    ):
        raise ValueError("La nueva escaleta no corresponde a la entrega base")
    run(receipt["order"], receipt, draft, token, 1, render_target, revision_id=identity)
    rendered = json.loads(read_regular(render_target, METADATA_LIMIT))
    direction_hash = hashlib.sha256(canonical(draft)).hexdigest()
    if "master" not in rendered:
        raw = read_regular(render_target.with_suffix(".preview-review.receipt"), METADATA_LIMIT)
        review = json.loads(raw)
        if (
            hashlib.sha256(raw).hexdigest() != rendered.get("preview_review_sha256")
            or review.get("order_id") != claim["order_id"]
            or review.get("job_id") != f"{identity}-r"
            or review.get("brief_sha256") != draft["brief_sha256"]
            or review.get("direction_sha256") != direction_hash
            or review.get("master_sha256") != rendered.get("preview", {}).get("sha256")
            or review.get("fase") != "vista_previa"
            or review.get("dictamen", {}).get("decision") != "rechazado"
        ):
            raise ValueError("El dictamen de vista previa no corresponde a la revisión")
        return {"status": "rechazado", "review": review}
    if (
        rendered.get("order_id") != claim["order_id"]
        or rendered.get("source_sha256") != receipt["order"]["media"]["sha256"]
        or rendered.get("brief_sha256") != draft["brief_sha256"]
        or rendered.get("job_id") != f"{identity}-r"
        or rendered.get("direction_sha256") != direction_hash
    ):
        raise ValueError("Render de revisión ajeno a la escaleta")
    request_id = str(uuid.uuid5(uuid.UUID(identity), "calidad-revision-v1"))
    if review_target.exists():
        review = json.loads(read_regular(review_target, METADATA_LIMIT))
    else:
        review = audit(
            receipt["order"],
            draft,
            rendered["master"]["sha256"],
            request_id,
            review_target,
            rendered,
            1,
            revision_id=identity,
        )
    if (
        review.get("order_id") != claim["order_id"]
        or review.get("job_id") != f"{identity}-r"
        or review.get("brief_sha256") != draft["brief_sha256"]
        or review.get("direction_sha256") != direction_hash
        or review.get("master_sha256") != rendered["master"]["sha256"]
        or review.get("dictamen", {}).get("decision") not in ("aprobado", "rechazado")
    ):
        raise ValueError("El dictamen no corresponde al render de revisión")
    return {"status": review["dictamen"]["decision"], "render": rendered, "review": review}


def stage_approved_revision(
    claim, receipt, direction, kit_path, draft, rendered, review, gpu, receipt_path
):
    """Empaqueta solo la nueva versión que Elena aprobó para esta escaleta."""
    from production_delivery import approved_graphic, stage_package

    base = approved_base(claim, receipt, direction, kit_path)
    order = receipt["order"]
    job = f"{claim['id']}-r"
    direction_hash = hashlib.sha256(canonical(draft)).hexdigest()
    brief_hash = hashlib.sha256(canonical(base["brief"])).hexdigest()
    master = rendered.get("master", {})
    verdict = review.get("dictamen", {})
    if (
        type(master) is not dict
        or type(verdict) is not dict
        or draft.get("order_id") != order["order_id"]
        or draft.get("source_sha256") != order["media"]["sha256"]
        or draft.get("brief_sha256") != brief_hash
        or rendered.get("order_id") != order["order_id"]
        or rendered.get("source_sha256") != order["media"]["sha256"]
        or rendered.get("brief_sha256") != brief_hash
        or rendered.get("job_id") != job
        or rendered.get("direction_sha256") != direction_hash
        or review.get("order_id") != order["order_id"]
        or review.get("job_id") != job
        or review.get("brief_sha256") != brief_hash
        or review.get("direction_sha256") != direction_hash
        or review.get("master_sha256") != master.get("sha256")
        or verdict.get("decision") != "aprobado"
        or type(verdict.get("puntaje")) is not int
        or not 85 <= verdict["puntaje"] <= 100
        or verdict.get("reglas_duras_incumplidas") != []
    ):
        raise ValueError("La nueva versión no tiene una aprobación verificable")
    expected = MEDIA / "proyectos" / order["media"]["project_slug"] / "06_renders" / job / "render"
    relative = master.get("path")
    if not isinstance(relative, str):
        raise ValueError("Ruta de la nueva versión inválida")
    path = MEDIA / relative
    if (
        path.parent != expected
        or path.name not in {"master.mp4", "master-corrected.mp4"}
        or path.resolve(strict=True) != path
    ):
        raise ValueError("Ruta de la nueva versión inválida")
    result = {}
    if render_format(order) == "prores4444":
        result["prores"] = approved_graphic(
            order, receipt, rendered, review, draft, gpu, receipt_path
        )
    relative, digest, length = stage_package(order, path, rendered, review, base["brief"])
    return {"path": relative, "sha256": digest, "bytes": length, **result}


def publish_staged_revision(
    claim,
    receipt,
    direction,
    kit_path,
    draft,
    rendered,
    review,
    staged,
    title,
    r2,
    studio,
    *,
    attempt=1,
):
    """Publica de forma idempotente la versión aprobada usando el productor firmado."""
    from production_publish import MAX_PRORES, deliver

    base = approved_base(claim, receipt, direction, kit_path)
    order = receipt["order"]
    job = f"{revision_attempt_id(claim['id'], attempt)}-r"
    direction_hash = hashlib.sha256(canonical(draft)).hexdigest()
    verdict = review.get("dictamen", {})
    duration = review.get("mediciones", {}).get("duration")
    if (
        order["mode"] != "sandbox"
        or type(verdict) is not dict
        or verdict.get("decision") != "aprobado"
        or type(verdict.get("puntaje")) is not int
        or not 85 <= verdict["puntaje"] <= 100
        or verdict.get("reglas_duras_incumplidas") != []
        or review.get("order_id") != order["order_id"]
        or review.get("job_id") != job
        or review.get("direction_sha256") != direction_hash
        or rendered.get("job_id") != job
        or rendered.get("direction_sha256") != direction_hash
        or review.get("master_sha256") != rendered.get("master", {}).get("sha256")
        or type(duration) not in (int, float)
        or not 0 < duration <= 600.1
        or not isinstance(title, str)
        or not 0 < len(title.strip()) <= 160
        or title != title.strip()
        or not matches(UUID, claim.get("lease"))
    ):
        raise ValueError("La revisión no está lista para publicación")
    digest = staged.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Huella del kit inválida")
    relative = f"proyectos/{order['media']['project_slug']}/05_revision/kit-studio-{digest}.zip"
    if staged.get("path") != relative:
        raise ValueError("Ruta de kit revisado inválida")
    path = MEDIA / relative
    if path.resolve(strict=True) != path:
        raise ValueError("Ruta de kit revisado inválida")
    kit_id = str(
        uuid.UUID(
            bytes=hashlib.sha256((claim["id"] + ":kit-revision-v1").encode()).digest()[:16],
            version=4,
        )
    )
    manifest = {
        "id": kit_id,
        "orderId": order["order_id"],
        "userId": order["user_id"],
        "title": title,
        "bytes": staged.get("bytes"),
        "sha256": digest,
        "uploadId": order["upload_id"],
        "sourceSha256": order["media"]["sha256"],
        "durationMs": round(duration * 1000),
        "revisionId": claim["id"],
        "lease": claim["lease"],
    }
    with ExitStack() as files:
        kit, size = files.enter_context(open_media(path, MAX_KIT))
        if size != staged.get("bytes") or not hmac.compare_digest(digest_stream(kit, size), digest):
            raise ValueError("El kit revisado cambió")
        kit.seek(0)
        with zipfile.ZipFile(kit) as archive:
            for name, expected in (
                ("02-brief.json", base["brief"]),
                ("03-render.json", rendered),
                ("04-dictamen-qa.json", review),
            ):
                matches_in_kit = [item for item in archive.infolist() if item.filename == name]
                if (
                    len(matches_in_kit) != 1
                    or matches_in_kit[0].file_size > METADATA_LIMIT
                    or json.loads(archive.read(matches_in_kit[0])) != expected
                ):
                    raise ValueError("El kit revisado no coincide con su auditoría")
        kit.seek(0)
        graphic = None
        if render_format(order) == "prores4444":
            item = staged.get("prores")
            if not isinstance(item, dict) or item.get("path") not in {
                f"proyectos/{order['media']['project_slug']}/06_renders/{job}-grafica/render/master.mov",
                f"proyectos/{order['media']['project_slug']}/06_renders/{job}-grafica/render/master-corrected.mov",
            }:
                raise ValueError("Gráfica revisada inválida")
            graphic_path = MEDIA / item["path"]
            if graphic_path.resolve(strict=True) != graphic_path:
                raise ValueError("Gráfica revisada inválida")
            graphic, graphic_size = files.enter_context(open_media(graphic_path, MAX_PRORES))
            if graphic_size != item.get("bytes"):
                raise ValueError("La gráfica revisada cambió")
            manifest.update(proresBytes=graphic_size, proresSha256=item.get("sha256"))
        elif "prores" in staged:
            raise ValueError("Formato de entrega inválido")
        return deliver(kit, manifest, r2, studio, graphic=graphic)


def revision_attempt_id(identity, attempt):
    """Deriva una identidad estable de render por ronda de la misma solicitud."""
    if attempt == 1:
        return identity
    digest = hashlib.sha256(f"{identity}:revision-interna:{attempt}".encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def process_claim(
    claim, receipt, strategy, direction, kit_path, folders, token, gpu, r2, studio, renew, title
):
    """Orquesta una revisión reclamada; nunca publica un dictamen rechazado."""
    if set(folders) != {"draft", "render", "review"}:
        raise ValueError("Directorios de revisión inválidos")
    identity = claim.get("id")
    if not matches(UUID, identity) or not matches(UUID, claim.get("lease")):
        raise ValueError("Reserva de revisión inválida")
    folders = {name: Path(folder) for name, folder in folders.items()}
    for directory in folders.values():
        if not directory.is_dir() or directory.resolve(strict=True) != directory:
            raise ValueError("Directorio de revisión inválido")
    rejected = None
    for attempt in range(1, QA_ROUNDS[receipt["order"]["plan_id"]] + 1):
        attempt_id = revision_attempt_id(identity, attempt)
        current = {**claim, "id": attempt_id}
        targets = {name: directory / f"{attempt_id}.json" for name, directory in folders.items()}
        renew(claim)
        draft = draft_revision(
            current, receipt, strategy, direction, kit_path, targets["draft"], rejected=rejected
        )
        if draft["escaleta"]["estado"] != "lista_para_stills":
            return {"status": "needs_review", "stage": "direction"}
        renew(claim)
        result = render_and_review(
            current, receipt, direction, kit_path, draft, targets["render"], targets["review"], gpu
        )
        if result["status"] == "aprobado":
            break
        rejected = {
            **draft["escaleta"],
            "dictamen": result["review"]["dictamen"],
            "muestras": result["review"].get("muestras", []),
        }
    else:
        return {"status": "needs_review", "stage": "quality"}
    renew(claim)
    staged = stage_approved_revision(
        current,
        receipt,
        direction,
        kit_path,
        draft,
        result["render"],
        result["review"],
        gpu,
        targets["render"].with_suffix(".prores.receipt"),
    )
    renew(claim)
    kit = publish_staged_revision(
        claim,
        receipt,
        direction,
        kit_path,
        draft,
        result["render"],
        result["review"],
        staged,
        title,
        r2,
        studio,
        attempt=attempt,
    )
    return {"status": "completed", "kit": kit}


def run_once(profile, paths, token, gpu, r2, studio, dispatch):
    """Reclama una revisión y ejecuta las etapas sobre archivos acreditados."""
    required = {"journal", "strategy", "direction", "draft", "render", "review"}
    if set(paths) != required:
        raise ValueError("Directorios de revisión inválidos")
    answer = dispatch({"action": "delivery_revision_claim"}, token)
    if not isinstance(answer, dict) or set(answer) != {"revision"}:
        raise ValueError("Respuesta del despacho inválida")
    claim = answer["revision"]
    if claim is None:
        return {"status": "idle"}
    if not isinstance(claim, dict) or not matches(UUID, claim.get("lease")):
        raise ValueError("Reserva de revisión inválida")
    context = load_claim_context(
        claim, paths["journal"], paths["strategy"], paths["direction"], paths["draft"]
    )
    if not eligible(context["receipt"]["order"], profile):
        raise ValueError("La revisión está fuera del perfil autorizado")
    title = claim.get("base_kit_title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
        raise ValueError("Título de entrega inválido")

    def renew(current):
        result = dispatch(
            {"action": "delivery_revision_renew", "id": current["id"], "lease": current["lease"]},
            token,
        )
        if not isinstance(result, dict) or result.get("revision", {}).get("id") != current["id"]:
            raise ValueError("Studio no confirmó la reserva")

    stop = threading.Event()
    lost = threading.Event()

    def maintain():
        while not stop.wait(300):
            try:
                renew(claim)
            except Exception:
                lost.set()
                return

    def guarded_renew(current):
        if lost.is_set():
            raise ValueError("No se pudo mantener la reserva de revisión")
        renew(current)
        if lost.is_set():
            raise ValueError("No se pudo mantener la reserva de revisión")

    worker = threading.Thread(target=maintain, daemon=True)
    worker.start()
    try:
        result = process_claim(
            claim,
            context["receipt"],
            context["strategy"],
            context["direction"],
            context["kit_path"],
            {name: paths[name] for name in ("draft", "render", "review")},
            token,
            gpu,
            r2,
            studio,
            guarded_renew,
            title.strip(),
        )
    finally:
        stop.set()
        worker.join(timeout=12)
    if result["status"] == "needs_review":
        saved = dispatch(
            {
                "action": "delivery_revision_failed",
                "id": claim["id"],
                "lease": claim["lease"],
                "stage": result["stage"],
            },
            token,
        )
        if saved != {"recorded": True}:
            raise ValueError("Studio no confirmó el estado de revisión")
    return result


def main():
    from production_mateo import dispatch_request
    from production_publish import credentials, r2_request, studio_request
    from production_sofia import JOURNAL
    from production_valeria import MATEO, VALERIA

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("dispatch", type=Path)
    parser.add_argument("gpu", type=Path)
    parser.add_argument("r2", type=Path)
    parser.add_argument("signing", type=Path)
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    profile = load_profile(args.profile)
    token = read_regular(args.dispatch, 64).decode("ascii")
    gpu = read_regular(args.gpu, 256).decode("ascii")
    if not re.fullmatch(r"[a-f0-9]{64}", token) or not 32 <= len(gpu) <= 256:
        raise ValueError("Credenciales de revisión inválidas")
    if args.state.resolve(strict=True) != args.state or not args.state.is_dir():
        raise ValueError("Directorio de revisión inválido")
    paths = {"journal": JOURNAL, "strategy": MATEO, "direction": VALERIA}
    for name in ("draft", "render", "review"):
        directory = args.state / name
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.resolve(strict=True) != directory or not directory.is_dir():
            raise ValueError("Directorio de revisión inválido")
        paths[name] = directory
    host, access, secret, signing = credentials(args.r2, args.signing)

    def r2(method, path, headers, body, **options):
        return r2_request(host, access, secret, method, path, headers, body, LIMIT, **options)

    def studio(manifest):
        return studio_request(signing, manifest)

    result = run_once(profile, paths, token, gpu, r2, studio, dispatch_request)
    print(json.dumps({"status": result["status"]}, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("Falló el procesamiento de revisión") from None
