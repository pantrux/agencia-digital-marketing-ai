"""Coteja la retención de los kits y crudos de Studio en D1, R2 y Synology."""

import argparse
import base64
import ctypes
import hashlib
import http.client
import json
import os
import re
import shlex
import stat
import sys
import zipfile
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAS = Path("//192.168.0.8/producciones/") if os.name == "nt" else Path("/mnt/producciones")
PAGE_SIZE = 32
UTC = timezone(timedelta(0))  # Compatible con Python 3.8 de DSM.
QUERY = """
    SELECT k.created_at, k.id, k.order_id, k.provider_mode, k.bytes, k.sha256, k.r2_etag,
           k.prores_bytes, k.prores_sha256, k.prores_r2_etag, o.project_slug, o.plan_id,
           CASE WHEN p.retention_days IS NOT NULL
                THEN k.created_at + p.retention_days * 86400 END AS expires_at
    FROM delivery_kits k JOIN orders o ON o.id=k.order_id AND o.user_id=k.user_id
    JOIN plan_quotas p ON p.plan_id=o.plan_id
    WHERE o.kind='production' AND o.status='completed'
      AND k.provider_mode=COALESCE(o.provider_mode,'production')
      AND (k.created_at,k.id) > (?,?)
    ORDER BY k.created_at,k.id LIMIT ?
"""
RAW_SCOPE = """
    SELECT s.id,s.order_id,s.object_key,s.extension,s.bytes,s.sha256,s.object_etag AS r2_etag,
           s.created_at,o.project_slug,o.plan_id,o.status AS order_status,
           COALESCE(o.provider_mode,'production') AS provider_mode,
           CASE WHEN p.retention_days IS NOT NULL THEN COALESCE((
               SELECT MAX(k.created_at) FROM delivery_kits k WHERE k.order_id=o.id
                 AND k.user_id=o.user_id AND k.provider_mode=COALESCE(o.provider_mode,'production')
           ),o.updated_at)+p.retention_days*86400 END AS expires_at
    FROM upload_sessions s JOIN orders o ON o.id=s.order_id AND o.user_id=s.user_id
    JOIN plan_quotas p ON p.plan_id=o.plan_id
    WHERE o.kind='production' AND o.status IN ('completed','failed','cancelled')
      AND s.status='completed'
"""
RAW_QUERY = RAW_SCOPE + " AND (s.created_at,s.id) > (?,?) ORDER BY s.created_at,s.id LIMIT ?"
RAW_CURRENT = RAW_SCOPE + " AND s.id=? AND s.order_id=?"
UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}"
ETAG = r"[0-9a-f]{32}(?:-[1-9][0-9]*)?"
SHARED = """
    SELECT EXISTS(SELECT 1 FROM delivery_kits k JOIN orders o ON o.id=k.order_id
        AND o.user_id=k.user_id JOIN plan_quotas p ON p.plan_id=o.plan_id
       WHERE o.project_slug=? AND k.sha256=? AND k.id<>?
         AND (p.retention_days IS NULL OR k.created_at+p.retention_days*86400>?)) AS compartido
"""


def read_nas(path, size, digest):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "ausente"
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_file_attributes", 0) & 0x400
        or path.resolve(strict=True) != path
        or info.st_size != size
    ):
        return "distinto"
    with path.open("rb") as file:
        opened = os.fstat(file.fileno())
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            return "distinto"
        measured = file_sha256(file)
        end = os.fstat(file.fileno())
    linked = path.lstat()

    return (
        "coincide"
        if digest == measured and file_identity(info) == file_identity(end) == file_identity(linked)
        else "distinto"
    )


def matches_object(row, headers, hash_header):
    return (
        headers.get("content-length") == str(row["bytes"])
        and headers.get(hash_header) == row["sha256"]
        and headers.get("etag", "").strip('"') == row["r2_etag"]
    )


def matches_original(row, headers):
    return matches_object(row, headers, "x-amz-meta-sha256")


def matches_raw_original(row, headers):
    return (
        raw_leaf(row) == f"{row['id']}.{row['extension']}"
        and matches_object(row, headers, "x-amz-meta-agency_sha256")
        and headers.get("x-amz-meta-agency_asset_id") == row["id"]
    )


def marker(row):
    metadata = {
        "Content-Type": "application/octet-stream",
        "X-Amz-Meta-Studio-Retirado": "v1",
        "X-Amz-Meta-Studio-Sha256-Original": row["sha256"],
        "X-Amz-Meta-Studio-Etag-Original": row["r2_etag"],
    }
    if row.get("legacy_verified"):
        metadata["X-Amz-Meta-Studio-Registro-Original"] = row["id"]
    return metadata


def matches_marker(row, headers):
    return headers.get("content-length") == "0" and all(
        headers.get(name.lower()) == value for name, value in marker(row).items()
    )


def legacy_row(row, etag):
    return {**row, "r2_etag": etag, "legacy_verified": True}


def legacy_marker(row, headers):
    original = headers.get("x-amz-meta-studio-etag-original", "")
    return (
        raw_leaf(row) != f"{row['id']}.{row['extension']}"
        and re.fullmatch(ETAG, original) is not None
        and matches_marker(legacy_row(row, original), headers)
    )


def historical_etag(row, headers):
    etag = headers.get("etag", "").strip('"')
    if (
        raw_leaf(row) != f"{row['id']}.{row['extension']}"
        and headers.get("content-length") == str(row["bytes"])
        and headers.get("x-amz-meta-sha256") == row["sha256"]
        and "x-amz-meta-agency_asset_id" not in headers
        and "x-amz-meta-agency_sha256" not in headers
        and re.fullmatch(ETAG, etag)
    ):
        return etag
    return None


def verify_historical_raw(row, headers, download):
    etag = historical_etag(row, headers)
    if etag is None:
        return None
    status, verified_headers, digest = download(raw_object_path(row), etag, row["bytes"])
    if status == 200 and historical_etag(row, verified_headers) == etag and digest == row["sha256"]:
        return legacy_row(row, etag)
    return None


def retire_r2(row, path, current, r2, *, raw=False, download=None):
    expires = row["expires_at"]
    if expires is None or expires > current:
        return "vigente"
    status, headers, _ = r2("HEAD", path, {}, b"")
    if status == 404:
        return "ausente"
    if status != 200:
        raise ValueError("R2 rechazó el cotejo antes del retiro")
    if matches_marker(row, headers) or (raw and legacy_marker(row, headers)):
        return "retirado"
    identifies = matches_raw_original if raw else matches_original
    if not identifies(row, headers):
        verified = verify_historical_raw(row, headers, download) if raw and download else None
        if verified is None:
            return "distinto"
        row = verified
    etag = row["r2_etag"]
    if not re.fullmatch(ETAG, etag):
        raise ValueError("ETag original inválido")
    status, _, _ = r2("PUT", path, {**marker(row), "If-Match": f'"{etag}"'}, b"")
    if status not in {200, 412}:
        raise ValueError("R2 rechazó la escritura condicional de retiro")
    status, headers, _ = r2("HEAD", path, {}, b"")
    return "retirado" if status == 200 and matches_marker(row, headers) else "distinto"


def rename_exclusive(source, name, destination, new_name):
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(source, name.encode("ascii"), destination, new_name.encode("ascii"), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def open_directory(stack, path, parent=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    stack.callback(os.close, fd)
    return fd


def file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_nlink


def file_sha256(file):
    digest = hashlib.sha256()
    for part in iter(lambda: file.read(1024 * 1024), b""):
        digest.update(part)
    return digest.hexdigest()


def bounded_sha256(response, size):
    if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
        return None
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        block = response.read(min(remaining, 1024**2))
        if not block:
            return None
        digest.update(block)
        remaining -= len(block)
    return None if response.read(1) else digest.hexdigest()


def verified_file(directory, leaf, row):
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return "ausente", None
    with os.fdopen(fd, "rb") as file:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != row["bytes"]
        ):
            return "distinto", None
        sha = file_sha256(file)
        expected = file_identity(before)
        if sha != row["sha256"] or expected != file_identity(os.fstat(fd)):
            return "distinto", None
    seen = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
    return ("coincide", expected) if file_identity(seen) == expected else ("distinto", None)


def retire_nas(row, current, nas, quarantine=None, *, raw=False):
    expires = row["expires_at"]
    if expires is None or expires > current:
        return "vigente"
    import fcntl

    order, identity = (row[name] for name in ("order_id", "id"))
    if any(
        not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", x)
        for x in (order, identity)
    ):
        raise ValueError("Identidad NAS inválida")
    slug, digest = row["project_slug"], row["sha256"]
    if slug != "job-" + order.replace("-", "") or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Ruta NAS inválida")
    stage = quarantine if quarantine is not None else nas / ".studio-kit-retention"
    if nas.resolve(strict=True) != nas or stage.resolve(strict=True) != stage:
        raise ValueError("Raíz NAS inválida")
    if raw:
        folder = "01_raw"
        leaf = raw_leaf(row)
        pending = f"crudo-{identity}.{row['extension']}"
    else:
        folder = "05_revision"
        leaf = f"kit-studio-{digest}.zip"
        pending = f"{identity}.zip"
    with ExitStack() as stack:
        hold = open_directory(stack, stage)
        info = os.fstat(hold)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            raise ValueError("El directorio de retiro debe ser privado")
        fcntl.flock(hold, fcntl.LOCK_EX | fcntl.LOCK_NB)
        root = open_directory(stack, nas)
        projects = open_directory(stack, "proyectos", root)
        try:
            project = open_directory(stack, slug, projects)
            revision = open_directory(stack, folder, project)
        except FileNotFoundError:
            if not raw:
                raise
            moved, _ = verified_file(hold, pending, row)
            return "ausente" if moved == "ausente" else "distinto"
        if os.fstat(revision).st_dev != info.st_dev:
            raise ValueError("El retiro requiere el mismo volumen")
        source, expected = verified_file(revision, leaf, row)
        moved, moved_identity = verified_file(hold, pending, row)
        if moved == "ausente":
            if source != "coincide":
                return source
            rename_exclusive(revision, leaf, hold, pending)
            moved, moved_identity = verified_file(hold, pending, row)
            if moved != "coincide" or moved_identity != expected:
                rename_exclusive(hold, pending, revision, leaf)
                return "distinto"
        elif source != "ausente" or moved != "coincide":
            return "distinto"
        os.fsync(hold)
        os.fsync(revision)
        os.unlink(pending, dir_fd=hold)
        os.fsync(hold)
        return "retirado"


def object_path(row):
    mode, order, kit = (row[field] for field in ("provider_mode", "order_id", "id"))
    slug, size, digest = (row[field] for field in ("project_slug", "bytes", "sha256"))
    if (
        mode not in {"sandbox", "production"}
        or not all(re.fullmatch(UUID, value) for value in (order, kit))
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", slug)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        or type(size) is not int
        or not 0 < size <= 5 * 1024**4
    ):
        raise ValueError("Manifiesto de entrega inválido")
    return f"/studio-media-bucket/kits/{mode}/{order}/{kit}.zip"


def raw_object_path(row):
    order, asset, extension = (row[field] for field in ("order_id", "id", "extension"))
    if (
        not all(re.fullmatch(UUID, value) for value in (order, asset))
        or not re.fullmatch(r"[a-z0-9]{1,10}", extension)
        or row["project_slug"] != "job-" + order.replace("-", "")
        or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
        or type(row["bytes"]) is not int
        or not 0 < row["bytes"] <= 1024**3
    ):
        raise ValueError("Identidad del crudo inválida")
    key = row["object_key"]
    if not re.fullmatch(rf"ingest/{order}/{UUID}\.{extension}", key):
        raise ValueError("Clave de ingesta inválida")
    return "/studio-media-bucket/" + key


def raw_leaf(row):
    return raw_object_path(row).rsplit("/", 1)[-1]


def audit_prores(row, check_r2, project, kit, kit_state):
    info = {field: row["prores_" + field] for field in ("bytes", "sha256", "r2_etag")}
    if (
        row["plan_id"] != "ilimitado"
        or row["expires_at"] is not None
        or type(info["bytes"]) is not int
        or not 0 < info["bytes"] <= 5 * 1024**4
        or not re.fullmatch(r"[a-f0-9]{64}", info["sha256"] or "")
        or not re.fullmatch(ETAG, info["r2_etag"] or "")
    ):
        raise ValueError("Manifiesto ProRes inválido")
    code, headers, _ = check_r2(object_path(row)[:-4] + ".mov")
    if code not in {200, 404}:
        raise ValueError("R2 rechazó el cotejo ProRes")
    cloud = "ausente" if code == 404 else "distinto"
    if code == 200 and matches_original(info, headers):
        cloud = "coincide"
    result = {"r2": cloud, "nas": "sin_dictamen"}
    if kit_state != "coincide":
        return result

    try:
        with zipfile.ZipFile(kit) as archive:
            entries = [
                entry for entry in archive.infolist() if entry.filename == "04-dictamen-qa.json"
            ]
            if len(entries) != 1 or entries[0].file_size > 16_384:
                return result
            review = json.loads(archive.read(entries[0]))
        job = review["job_id"]
        if (
            review["order_id"] != row["order_id"]
            or review["dictamen"]["decision"] != "aprobado"
            or job not in tuple(f"{row['order_id']}-v{revision}" for revision in (1, 2, 3))
        ):
            return result
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        return result

    folder = project / "06_renders" / (job + "-grafica") / "render"
    result["nas"] = "ausente"
    for name in ("master.mov", "master-corrected.mov"):
        observed = read_nas(folder / name, info["bytes"], info["sha256"])
        if observed == "coincide":
            result["nas"] = observed
            break
        if observed == "distinto":
            result["nas"] = observed
    return result


def audit_row(row, check_r2, current, nas, *, raw=False, check_prores=True):
    code, headers, _ = check_r2(raw_object_path(row) if raw else object_path(row))
    if code not in {200, 404}:
        raise ValueError("R2 rechazó la consulta del manifiesto")
    cloud = "ausente" if code == 404 else "distinto"
    identifies = matches_raw_original if raw else matches_original
    if code == 200 and identifies(row, headers):
        cloud = "coincide"
    elif code == 200 and (matches_marker(row, headers) or (raw and legacy_marker(row, headers))):
        cloud = "retirado"
    elif raw and code == 200 and historical_etag(row, headers):
        cloud = "registro_historico"
    project = nas / "proyectos" / row["project_slug"]
    target = (
        project / "01_raw" / raw_leaf(row)
        if raw
        else project / "05_revision" / f"kit-studio-{row['sha256']}.zip"
    )
    expires = row["expires_at"]
    result = {
        "plan": row["plan_id"],
        "entorno": row["provider_mode"],
        "vence_utc": datetime.fromtimestamp(expires, tz=UTC).isoformat()
        if expires is not None
        else None,
        "vencido": expires is not None and expires <= current,
        "r2": cloud,
        "nas": read_nas(target, row["bytes"], row["sha256"]),
    }
    if raw:
        result["estado_orden"] = row["order_status"]
    elif check_prores and row.get("prores_bytes") is not None:
        result["prores"] = audit_prores(row, check_r2, project, target, result["nas"])
    return result


def retire_kit(row, cloud, nas):
    r2 = cloud(row)
    if r2 != "retirado":
        return {"r2": r2, "nas": "pendiente"}
    return {"r2": r2, "nas": "compartido" if row["compartido"] else nas(row)}


def retire_on_dsm(vault, row, *, raw=False):
    from synology_storage_audit import connect_client, load_settings

    settings = load_settings(vault, timeout=10)
    source = base64.b64encode(Path(__file__).read_bytes()).decode("ascii")
    runner = (
        "import base64,json,sys\n"
        "from pathlib import Path\n"
        "data=json.load(sys.stdin)\n"
        "ns={'__name__':'studio_retention','__file__':'/tmp/studio_retention.py'}\n"
        "exec(compile(base64.b64decode(data['source'],validate=True),ns['__file__'],'exec'),ns)\n"
        "target=Path('/volume2/agency-studio-retention')\n"
        "try:\n"
        " target.mkdir(mode=0o700)\n"
        "except FileExistsError:\n"
        " pass\n"
        "status=ns['retire_nas'](data['row'],int(ns['datetime'].now(ns['UTC']).timestamp()),"
        "Path('/volume2/producciones'),target,raw=data['es_crudo'])\n"
        "print(json.dumps({'status':status}))\n"
    )
    with connect_client(settings) as client:
        command = "sudo -S -p '' /usr/bin/python3 -I -B -c " + shlex.quote(runner)
        stdin, stdout, _stderr = client.exec_command(command, timeout=90)
        stdin.write(
            settings.password + "\n" + json.dumps({"source": source, "row": row, "es_crudo": raw})
        )
        stdin.channel.shutdown_write()
        raw = stdout.read(2048)
        if stdout.channel.recv_exit_status() != 0:
            raise ValueError("DSM no pudo retirar el archivo")
        return json.loads(raw)["status"]


def main():
    from check_env import parse_env
    from production_publish import r2_request
    from provision_order import query_orders

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retirar-vencidos", action="store_true")
    parser.add_argument("--registrar", action="store_true")
    args = parser.parse_args()
    vault = ROOT / ".env"
    values, errors = parse_env(vault.read_text(encoding="utf-8-sig"))
    if errors or not (NAS / "proyectos").is_dir() or NAS.resolve(strict=True) != NAS:
        raise ValueError("Bóveda o montaje de producciones inválido")
    host = values["CF_ACCOUNT_ID"] + ".r2.cloudflarestorage.com"
    access, secret = (
        values[key]
        for key in ("R2_STUDIO_MEDIA_ACCESS_KEY_ID", "R2_STUDIO_MEDIA_SECRET_ACCESS_KEY")
    )

    def r2(method, path, headers, payload):
        return r2_request(host, access, secret, method, path, headers, payload, 0)

    def head(path):
        return r2("HEAD", path, {}, b"")

    def download(path, etag, size):
        return r2_request(
            host,
            access,
            secret,
            "GET",
            path,
            {"If-Match": f'"{etag}"', "Accept-Encoding": "identity"},
            b"",
            0,
            read_body=lambda response: bounded_sha256(response, size),
        )

    now = datetime.now(UTC)
    when = int(now.timestamp())
    kits = []
    actions = []
    raws = []
    raw_actions = []
    for query, is_raw, inventory, retired in (
        (QUERY, False, kits, actions),
        (RAW_QUERY, True, raws, raw_actions),
    ):
        cursor = (-1, "")
        while page := query_orders(values, query, [*cursor, PAGE_SIZE]):
            for row in page:
                report = audit_row(
                    row, head, when, NAS, raw=is_raw, check_prores=not args.retirar_vencidos
                )
                inventory.append(report)
                if not args.retirar_vencidos or not report["vencido"]:
                    continue
                if is_raw and query_orders(values, RAW_CURRENT, [row["id"], row["order_id"]]) != [
                    row
                ]:
                    retired.append({"r2": "registro_cambio", "nas": "pendiente"})
                    continue
                row["compartido"] = False
                if not is_raw:
                    shared = query_orders(
                        values, SHARED, [row["project_slug"], row["sha256"], row["id"], when]
                    )
                    if len(shared) != 1 or shared[0].get("compartido") not in (0, 1):
                        raise ValueError("Compartición del kit indeterminada")
                    row["compartido"] = shared[0]["compartido"] == 1
                key = raw_object_path(row) if is_raw else object_path(row)
                retired.append(
                    retire_kit(
                        row,
                        lambda item, path=key, original=is_raw: retire_r2(
                            item, path, when, r2, raw=original, download=download
                        ),
                        lambda item, original=is_raw: retire_on_dsm(vault, item, raw=original),
                    )
                )
            cursor = (page[-1]["created_at"], page[-1]["id"])
    record = json.dumps(
        {
            "utc": now.isoformat(),
            "solo_lectura": not args.retirar_vencidos,
            "kits": kits,
            "retiros": actions,
            "crudos": raws,
            "retiros_crudos": raw_actions,
        },
        ensure_ascii=False,
    )
    if args.registrar:
        destination = ROOT / ".tools" / "evidence" / "studio-kit-retention-last-run.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(record + "\n", encoding="utf-8")
        temporary.replace(destination)
    elif sys.stdout is not None:
        print(record)
    return int(any(r["nas"] in {"pendiente", "distinto"} for r in actions + raw_actions))


if __name__ == "__main__":
    sys.path[:0] = [
        str(ROOT / path) for path in ("scripts/ci", "docker/n8n", "docker/kitsu-zou", "storage")
    ]
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
        raise SystemExit("Falló la auditoría de retención de Studio") from None
