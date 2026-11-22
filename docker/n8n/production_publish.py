"""Publica en Studio un kit cuyo ZIP ya cotejó el adaptador de Kitsu."""

import argparse
import hashlib
import hmac
import http.client
import json
import logging
import re
import sqlite3
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import ExitStack, closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

from production_delivery import MAX_KIT, digest_stream, load_assignments, open_media
from production_handoff import canonical, eligible, load_profile, publish, read_regular, validate
from production_inbox import MEDIA, UUID, matches, render_format
from production_sofia import INTAKE_LIMIT, JOURNAL, LIMIT

DELIVERY = Path("/var/lib/agency-production-delivery")
ENDPOINT = "/api/internal/kits/publish"
BUCKET = "studio-media-bucket"
FIELDS = ("id", "orderId", "userId", "title", "bytes", "sha256", "uploadId", "sourceSha256")
PRORES_PART_BYTES = 32 * 1024**2
PRORES_PART_WORKERS = 6
MAX_PRORES = 5 * 1024**4
PRORES_TYPE = "video/quicktime"
KIT_MODES = ("sandbox", "production")
ORDER_ERRORS = (OSError, ValueError, KeyError, TypeError, http.client.HTTPException)


class StorageQuotaExhausted(ValueError):
    """Studio conserva el kit preparado hasta que vuelva a haber espacio."""


def expect_local(stream, size, digest):
    stream.seek(0)
    if not hmac.compare_digest(digest_stream(stream, size), digest):
        raise ValueError("Archivo fuera del manifiesto")
    stream.seek(0)


def verify_object(r2, path, size, digest, media_type):
    status, headers, remote_digest = r2(
        "GET",
        path,
        {"Accept-Encoding": "identity"},
        b"",
        read_body=lambda response: digest_stream(response, size),
    )
    if (
        status != 200
        or headers.get("x-amz-meta-sha256") != digest
        or headers.get("content-type") != media_type
        or headers.get("content-length") != str(size)
        or headers.get("content-encoding") not in (None, "identity")
        or not hmac.compare_digest(remote_digest, digest)
    ):
        raise ValueError("La descarga de R2 difiere del archivo acreditado")


def r2_xml(wrapper, name):
    if len(wrapper) > LIMIT:
        raise ValueError("Respuesta multipart de R2 excesiva")
    try:
        document = ElementTree.fromstring(wrapper)
    except ElementTree.ParseError as error:
        raise ValueError("Respuesta multipart de R2 inválida") from error
    if document.tag.rsplit("}", 1)[-1] != name:
        raise ValueError("R2 rechazó la operación multipart")
    return document


def upload_graphic_part(r2, path, query, index, part):
    status, headers, _ = r2(
        "PUT",
        path,
        {"Content-Length": str(len(part))},
        part,
        query=(*query, ("partNumber", str(index))),
    )
    etag = headers.get("etag", "")
    if status != 200 or not re.fullmatch(r'"[a-fA-F0-9]{32}"', etag):
        raise ValueError("R2 rechazó una parte de la gráfica")
    return etag


def upload_graphic_parts(r2, path, query, graphic, size, digest):
    part_bytes = max(PRORES_PART_BYTES, (size + 9999) // 10000)
    # Mantiene el límite de partes en vuelo aunque R2 exija partes más grandes.
    workers = max(1, PRORES_PART_BYTES * PRORES_PART_WORKERS // part_bytes)
    local_hash = hashlib.sha256()
    etags = {}
    pending = {}
    graphic.seek(0)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for index, offset in enumerate(range(0, size, part_bytes), 1):
            if len(pending) == workers:
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for request in finished:
                    etags[pending.pop(request)] = request.result()
            length = min(part_bytes, size - offset)
            part = graphic.read(length)
            if len(part) != length:
                raise ValueError("Cambió el tamaño de la gráfica durante la carga")
            local_hash.update(part)
            request = executor.submit(upload_graphic_part, r2, path, query, index, part)
            pending[request] = index
            del part
        if graphic.read(1):
            raise ValueError("Cambió el tamaño de la gráfica durante la carga")
        if not hmac.compare_digest(local_hash.hexdigest(), digest):
            raise ValueError("Archivo fuera del manifiesto")
        for request in as_completed(pending):
            etags[pending[request]] = request.result()
    return sorted(etags.items())


def put_graphic(r2, path, graphic, size, digest):
    status, _, _ = r2("HEAD", path, {}, b"")
    if status == 200:
        expect_local(graphic, size, digest)
        verify_object(r2, path, size, digest, PRORES_TYPE)
        return
    if status != 404:
        raise ValueError("No se pudo consultar la gráfica en R2")
    headers = {"Content-Type": PRORES_TYPE, "X-Amz-Meta-Sha256": digest}
    status, _, raw = r2("POST", path, headers, b"", query=(("uploads", ""),))
    if status != 200:
        raise ValueError("R2 rechazó el inicio de la carga")
    document = r2_xml(raw, "InitiateMultipartUploadResult")
    upload = document.findtext("{*}UploadId")
    if not upload or not re.fullmatch(r"[\x21-\x7e]{1,1024}", upload):
        raise ValueError("Identidad multipart de R2 inválida")
    query = (("uploadId", upload),)
    manifest = ElementTree.Element("CompleteMultipartUpload")
    completed = False
    try:
        for index, etag in upload_graphic_parts(r2, path, query, graphic, size, digest):
            item = ElementTree.SubElement(manifest, "Part")
            ElementTree.SubElement(item, "PartNumber").text = str(index)
            ElementTree.SubElement(item, "ETag").text = etag
        body = ElementTree.tostring(manifest)
        status, _, raw = r2(
            "POST",
            path,
            {"Content-Type": "application/xml", "If-None-Match": "*"},
            body,
            query=query,
        )
        if status == 200:
            r2_xml(raw, "CompleteMultipartUploadResult")
            completed = True
        elif status != 412:
            raise ValueError("R2 rechazó el cierre de la carga")
    finally:
        if not completed:
            with suppress(*ORDER_ERRORS):
                r2("DELETE", path, {}, b"", query=query)
    verify_object(r2, path, size, digest, PRORES_TYPE)


def deliver(kit, manifest, r2, studio, *, mode="sandbox", graphic=None):
    if mode not in KIT_MODES:
        raise ValueError("Entorno de publicación inválido")
    digest, size = manifest["sha256"], manifest["bytes"]
    if (
        not matches(UUID, manifest["orderId"])
        or not matches(UUID, manifest["id"])
        or not matches(re.compile(r"[a-f0-9]{64}"), digest)
        or type(size) is not int
        or not 0 < size <= MAX_KIT
    ):
        raise ValueError("Kit fuera del manifiesto")
    graphic_size, graphic_hash = manifest.get("proresBytes"), manifest.get("proresSha256")
    if graphic is not None:
        if (
            type(graphic_size) is not int
            or not 0 < graphic_size <= MAX_PRORES
            or not matches(re.compile(r"[a-f0-9]{64}"), graphic_hash)
            or "durationMs" not in manifest
        ):
            raise ValueError("Gráfica fuera del manifiesto")
    elif "proresBytes" in manifest or "proresSha256" in manifest:
        raise ValueError("Falta el archivo ProRes")
    expect_local(kit, size, digest)
    object_path = f"/{BUCKET}/kits/{mode}/{manifest['orderId']}/{manifest['id']}.zip"
    status, _, _ = r2(
        "PUT",
        object_path,
        {
            "If-None-Match": "*",
            "Content-Type": "application/zip",
            "Content-Length": str(size),
            "X-Amz-Meta-Sha256": digest,
        },
        kit,
        payload_sha256=digest,
    )
    if status not in {200, 412}:
        raise ValueError("R2 rechazó la escritura condicional")
    verify_object(r2, object_path, size, digest, "application/zip")
    if graphic is not None:
        put_graphic(
            r2, object_path.removesuffix(".zip") + ".mov", graphic, graphic_size, graphic_hash
        )
    result = studio(manifest)
    row = result["kit"]
    if (
        row["id"] != manifest["id"]
        or row["order_id"] != manifest["orderId"]
        or row["bytes"] != size
        or row["title"] != manifest["title"]
        or row.get("prores", {}).get("bytes") != graphic_size
    ):
        raise ValueError("Studio confirmó otro kit")
    return row


def r2_request(
    host,
    access,
    secret,
    method,
    path,
    headers,
    body,
    limit,
    *,
    read_body=None,
    payload_sha256=None,
    query=(),
):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = payload_sha256 if payload_sha256 is not None else hashlib.sha256(body).hexdigest()
    signed = {
        **{k.lower(): v for k, v in headers.items()},
        "host": host,
        "x-amz-date": stamp,
        "x-amz-content-sha256": digest,
    }
    names = ";".join(sorted(signed))
    header_block = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
    scope = f"{stamp[:8]}/auto/s3/aws4_request"
    encoded = sorted((quote(k, safe="-._~"), quote(v, safe="-._~")) for k, v in query)
    parameters = "&".join(f"{name}={value}" for name, value in encoded)
    canonical_request = "\n".join((method, path, parameters, header_block, names, digest))
    message = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            stamp,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        )
    )
    key = ("AWS4" + secret).encode("ascii")
    for part in (stamp[:8], "auto", "s3", "aws4_request"):
        key = hmac.digest(key, part.encode("ascii"), "sha256")
    signature = hmac.digest(key, message.encode("ascii"), "sha256").hex()
    signed["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={names}, Signature={signature}"
    )
    client = http.client.HTTPSConnection(host, timeout=25)
    try:
        target = path + ("?" + parameters if parameters else "")
        client.request(
            method, target, body=body if method in {"PUT", "POST"} else None, headers=signed
        )
        with client.getresponse() as response:
            headers = {name.lower(): value for name, value in response.getheaders()}
            payload = (
                read_body(response)
                if read_body and response.status == 200
                else response.read(limit + 1)
            )
            return response.status, headers, payload
    finally:
        client.close()


def studio_request(key, manifest, *, mode="sandbox"):
    if mode not in KIT_MODES:
        raise ValueError("Entorno de publicación inválido")
    stamp = str(int(time.time()))
    values = [manifest[k] for k in FIELDS]
    version = "v1"
    if "durationMs" in manifest:
        version = "v2"
        values.append(manifest["durationMs"])
    if "proresBytes" in manifest:
        version = "v3"
        values.extend((manifest["proresBytes"], manifest["proresSha256"]))
    if "revisionId" in manifest:
        if "durationMs" not in manifest or "lease" not in manifest:
            raise ValueError("La revisión requiere duración y reserva")
        version = "v5" if "proresBytes" in manifest else "v4"
        values.extend((manifest["revisionId"], manifest["lease"]))
    doc = ["studio-kit-" + version, "POST", ENDPOINT, mode, stamp, values]
    wire = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
    signature = hmac.digest(key, wire, "sha256").hex()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Studio Publication)",
        "timestamp": stamp,
        "signature": version + "=" + signature,
    }
    client = http.client.HTTPSConnection("studio.e-scale.cl", timeout=22)
    try:
        client.request("POST", ENDPOINT, body=canonical(manifest), headers=headers)
        with client.getresponse() as response:
            raw = response.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ValueError("Respuesta de publicación excesiva")
            if response.status == 409:
                try:
                    detail = json.loads(raw)
                except (ValueError, UnicodeError):
                    detail = None
                if detail == {"error": "ALMACENAMIENTO_AGOTADO"}:
                    raise StorageQuotaExhausted("Espacio insuficiente en Studio")
            if response.status != 200:
                raise ValueError("Studio rechazó el manifiesto firmado")
            return json.loads(raw)
    finally:
        client.close()


def credentials(r2_keys, signing_key):
    keys = json.loads(read_regular(r2_keys, LIMIT))
    account, access, secret = (keys[k] for k in ("account", "access", "secret"))
    if not all(
        re.fullmatch(pattern, value)
        for pattern, value in (
            (r"[0-9a-f]{32}", account),
            (r"[A-Za-z0-9]{1,128}", access),
            (r"[\x21-\x7e]{1,256}", secret),
        )
    ):
        raise ValueError("Identidad R2 inválida")
    sign = read_regular(signing_key, 64)
    if not re.fullmatch(rb"[a-f0-9]{64}", sign):
        raise ValueError("Clave de publicación inválida")
    return account + ".r2.cloudflarestorage.com", access, secret, sign


def approved_duration_ms(kit, order, *, graphic=None, project=None):
    try:
        with zipfile.ZipFile(kit) as archive:
            entries = [
                entry for entry in archive.infolist() if entry.filename == "04-dictamen-qa.json"
            ]
            if not entries and graphic is None:
                return None  # Los ZIP del primer piloto usaban otras rutas.
            if len(entries) != 1 or entries[0].file_size > LIMIT:
                raise ValueError("Dictamen del kit inválido")
            review = json.loads(archive.read(entries[0]))
    except zipfile.BadZipFile as error:
        raise ValueError("El kit no es un ZIP válido") from error
    duration = review["mediciones"]["duration"]
    if (
        review["order_id"] != order
        or review["dictamen"]["decision"] != "aprobado"
        or type(duration) not in (int, float)
        or not 0.001 <= duration <= 600.1
    ):
        raise ValueError("Duración del dictamen inválida")
    if graphic is not None:
        job = review["job_id"]
        if job not in {f"{order}-v{revision}" for revision in (1, 2, 3)}:
            raise ValueError("Revisión de ProRes fuera del dictamen")
        folder = f"proyectos/{project}/06_renders/{job}-grafica/render"
        if graphic["path"] not in {f"{folder}/master.mov", f"{folder}/master-corrected.mov"}:
            raise ValueError("La gráfica no corresponde a la revisión aprobada")
    return round(duration * 1000)


def publish_order(intake, reviewed, info, connections, target, *, graphic=None):
    mode = intake["mode"]
    digest = reviewed["sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or digest != info.get("kit_sha256", digest):
        raise ValueError("Huella de publicación inválida")
    path = (
        MEDIA
        / "proyectos"
        / intake["media"]["project_slug"]
        / "05_revision"
        / f"kit-studio-{digest}.zip"
    )
    if path.resolve(strict=True) != path:
        raise ValueError("Ruta de kit no canónica")
    manifest = {
        "id": info["id"],
        "orderId": intake["order_id"],
        "userId": intake["user_id"],
        "title": info["title"],
        "bytes": reviewed["bytes"],
        "sha256": digest,
        "uploadId": intake["upload_id"],
        "sourceSha256": intake["media"]["sha256"],
    }
    host, access, secret, sign = connections
    with ExitStack() as files:
        kit, size = files.enter_context(open_media(path, MAX_KIT))
        if size != manifest["bytes"]:
            raise ValueError("El tamaño del kit difiere de Kitsu")
        duration = approved_duration_ms(
            kit, intake["order_id"], graphic=graphic, project=intake["media"]["project_slug"]
        )
        if duration is not None:
            manifest["durationMs"] = duration
        mov = None
        if graphic is not None:
            mov_path = MEDIA / graphic["path"]
            if mov_path.resolve(strict=True) != mov_path:
                raise ValueError("Ruta ProRes no canónica")
            mov, mov_size = files.enter_context(open_media(mov_path, MAX_PRORES))
            if mov_size != graphic["bytes"]:
                raise ValueError("El ProRes difiere del recibo de entrega")
            manifest.update(proresBytes=mov_size, proresSha256=graphic["sha256"])
        row = deliver(
            kit,
            manifest,
            lambda m, p, h, b, **opts: r2_request(host, access, secret, m, p, h, b, LIMIT, **opts),
            lambda value: studio_request(sign, value, mode=mode),
            mode=mode,
            graphic=mov,
        )
    publish(
        target,
        canonical(
            {
                "order_id": intake["order_id"],
                "kit": row,
                "sha256": digest,
                "kitsu_attachment_id": reviewed["attachment_id"],
            }
        )
        + b"\n",
    )


def publish_assigned(profile, identity, info, path, output, r2_keys, signing_key, *, pending):
    local = json.loads(read_regular(path, LIMIT))
    if local["order_id"] != identity or local["kitsu"]["task_id"] != info["task"]:
        raise ValueError("Recibo de Kitsu fuera de la orden")
    if pending and (local["id"], local["project"]) != (info["id"], info["project"]):
        raise ValueError("El recibo de Kitsu no coincide con la asignación")
    intake = json.loads(read_regular(JOURNAL / path.name, INTAKE_LIMIT))["order"]
    validate(intake, identity)
    if not eligible(intake, profile):
        raise ValueError("La orden no corresponde al piloto")
    graphic = local.get("prores")
    if (graphic is not None) != (render_format(intake) == "prores4444"):
        raise ValueError("Recibo fuera del formato de la orden")
    publish_order(
        intake,
        local["kitsu"],
        info,
        credentials(r2_keys, signing_key),
        output / path.name,
        **({"graphic": graphic} if graphic is not None else {}),
    )


def run_pending(profile, assignments, r2_keys, signing_key, output):
    state = output / "reintentos"
    state.mkdir(mode=0o700, exist_ok=True)
    if state.resolve(strict=True) != state:
        raise ValueError("Directorio de reintentos inválido")
    failures = 0
    with closing(sqlite3.connect(state / "publicacion.sqlite3")) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS retries (
            order_id TEXT PRIMARY KEY, failures INTEGER NOT NULL, due_at INTEGER NOT NULL
        ) STRICT""")
        for identity, info in sorted(assignments.items()):
            path = DELIVERY / f"{identity}.json"
            if (output / path.name).exists() or not path.is_file():
                continue
            retry = db.execute(
                "SELECT failures,due_at FROM retries WHERE order_id=?", (identity,)
            ).fetchone()
            if retry and retry[1] > int(time.time()):
                continue
            try:
                publish_assigned(
                    profile, identity, info, path, output, r2_keys, signing_key, pending=True
                )
            except ORDER_ERRORS as error:
                attempts = min(retry[0] + 1, 5) if retry else 1
                delay = min(30 * 2 ** (attempts - 1), 300)
                with db:
                    db.execute(
                        """INSERT INTO retries (order_id, failures, due_at) VALUES (?, ?, ?)
                        ON CONFLICT(order_id) DO UPDATE
                        SET failures=excluded.failures,due_at=excluded.due_at""",
                        (identity, attempts, int(time.time()) + delay),
                    )
                reason = (
                    "ALMACENAMIENTO_AGOTADO"
                    if isinstance(error, StorageQuotaExhausted)
                    else type(error).__name__
                )
                logging.warning(
                    "Publicación pendiente para %s (%s). Reintento en %s s.",
                    identity,
                    reason,
                    delay,
                )
                failures += 1
                continue
            if retry:
                with db:
                    db.execute("DELETE FROM retries WHERE order_id=?", (identity,))
            break
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("publication", type=Path)
    parser.add_argument("r2_keys", type=Path)
    parser.add_argument("signing_key", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pending", action="store_true")
    args = parser.parse_args()
    profile = load_profile(args.profile)
    if args.pending:
        assignments = load_assignments(args.publication)
        count = run_pending(profile, assignments, args.r2_keys, args.signing_key, args.output)
        if count:
            logging.warning("Entregas pendientes de reintento en esta ejecución: %d", count)
        return
    info = json.loads(read_regular(args.publication, LIMIT))
    if info.keys() != {"task", "title", "id", "kit_sha256"}:
        raise ValueError("Configuración de publicación inválida")
    identity = profile["fixture"]
    path = DELIVERY / f"{identity}.json"
    if not (args.output / path.name).exists() and path.is_file():
        publish_assigned(
            profile,
            identity,
            info,
            path,
            args.output,
            args.r2_keys,
            args.signing_key,
            pending=False,
        )


if __name__ == "__main__":
    try:
        main()
    except (*ORDER_ERRORS, sqlite3.Error):
        raise SystemExit("Falló la publicación del kit verificado") from None
