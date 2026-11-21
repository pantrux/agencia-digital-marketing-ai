"""Mide el tiempo entre el brief confirmado y la primera entrega de Studio."""

import argparse
import json
import math
import os
import stat
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WINDOW = 30 * 86400
TARGET = 6 * 60
PAGE_SIZE = 32
MAX_KIT = 128 * 1024**2
MAX_REVIEW = 256 * 1024

QUERY = """
    SELECT o.id AS order_id,o.project_slug,o.plan_id,o.status,b.confirmed_at,o.portal_started_at,
           COALESCE(o.provider_mode,'production') AS provider_mode,
           lower(u.email) LIKE '%.invalid' AS reserved_test_domain,
           r.order_id IS NOT NULL AS manual_review,
           EXISTS (SELECT 1 FROM production_manual_adjustments a
                   WHERE a.order_id=o.id) AS manual_adjustment,
           k.id,k.created_at AS delivered_at,k.bytes,k.sha256,k.duration_ms
    FROM orders o JOIN production_briefs b ON b.order_id=o.id AND b.user_id=o.user_id
    JOIN users u ON u.id=o.user_id
    LEFT JOIN production_manual_reviews r ON r.order_id=o.id
    LEFT JOIN delivery_kits k ON k.id=(SELECT first.id FROM delivery_kits first
        WHERE first.order_id=o.id AND first.user_id=o.user_id
          AND first.provider_mode=COALESCE(o.provider_mode,'production')
        ORDER BY first.created_at,first.id LIMIT 1)
    WHERE o.kind='production' AND b.confirmed_at BETWEEN ? AND ?
      AND (b.confirmed_at,o.id) > (?,?)
    ORDER BY b.confirmed_at,o.id LIMIT ?
"""

PORTAL_COHORTS = ("portal_produccion", "portal_ensayo", "portal_sandbox")
UNATTRIBUTED = "origen_sin_acreditar"


def signed_duration(row):
    duration = row["duration_ms"]
    return duration / 1000 if type(duration) is int and 0 < duration <= 600_100 else None


def delivered_duration(row, nas):
    """Obtiene la duración del dictamen del ZIP exacto que figura en D1."""
    if row["duration_ms"] is not None:
        return signed_duration(row)
    from audit_studio_kit_retention import file_identity, file_sha256, object_path

    if not 0 < row["bytes"] <= MAX_KIT:
        return None
    object_path(row)
    target = (
        nas / "proyectos" / row["project_slug"] / "05_revision" / f"kit-studio-{row['sha256']}.zip"
    )
    try:
        if target.resolve(strict=True) != target:
            return None
        before = target.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != row["bytes"]
        ):
            return None
        with target.open("rb") as stream:
            if file_identity(os.fstat(stream.fileno())) != file_identity(before):
                return None
            if file_sha256(stream) != row["sha256"]:
                return None
            with zipfile.ZipFile(stream) as archive:
                entry = archive.getinfo("04-dictamen-qa.json")
                if entry.file_size > MAX_REVIEW or archive.namelist().count(entry.filename) != 1:
                    return None
                review = json.loads(archive.read(entry))
            if file_identity(os.fstat(stream.fileno())) != file_identity(before):
                return None
        if file_identity(target.lstat()) != file_identity(before):
            return None
        if review["order_id"] != row["order_id"] or review["dictamen"]["decision"] != "aprobado":
            return None
        duration = review["mediciones"]["duration"]
        return duration if type(duration) in (int, float) and 0 < duration <= 600 else None
    except (FileNotFoundError, KeyError, TypeError, ValueError, zipfile.BadZipFile):
        return None


def percentile95(seconds):
    return sorted(seconds)[math.ceil(0.95 * len(seconds)) - 1] if seconds else None


def summarize(rows, now, duration_for):
    elapsed = []
    from_reservation = []
    completed = 0
    measurable = 0
    pending = 0
    overdue = 0
    manual = 0
    completed_with_intervention = 0
    cancelled = 0
    for row in rows:
        start = row["confirmed_at"]
        if row["status"] == "processing":
            if row["manual_review"]:
                manual += 1
            else:
                pending += 1
                overdue += now - start >= TARGET
        cancelled += row["status"] == "cancelled"
        if row["status"] != "completed":
            continue
        completed += 1
        assisted = row["manual_review"] or row["manual_adjustment"]
        completed_with_intervention += bool(assisted)
        delivered = row["delivered_at"]
        if delivered is None or not start <= delivered <= now:
            continue
        duration = duration_for(row)
        if duration is None:
            continue
        measurable += 1
        if not assisted and abs(duration - 60) <= 1 / 30:
            elapsed.append(delivered - start)
            reserved = row["portal_started_at"]
            if type(reserved) is int and 0 <= reserved <= start:
                from_reservation.append(delivered - reserved)
    return {
        "completados": completed,
        "completados_con_intervencion_manual": completed_with_intervention,
        "completados_con_duracion_cotejada": measurable,
        "pendientes": pending,
        "pendientes_con_demora": overdue,
        "revision_manual": manual,
        "cancelados": cancelled,
        "muestras_de_60_s": len(elapsed),
        "entregados_antes_de_360_s": sum(value < TARGET for value in elapsed),
        "p95_observado_s": percentile95(elapsed),
        "muestras_desde_reserva": len(from_reservation),
        "p95_observado_s_desde_reserva": percentile95(from_reservation),
    }


def cohort(row):
    if row["portal_started_at"] is None:
        return UNATTRIBUTED
    if row["reserved_test_domain"]:
        return "portal_ensayo"
    return "portal_produccion" if row["provider_mode"] == "production" else "portal_sandbox"


def read_window(query, now):
    start = now - WINDOW
    cursor = (start - 1, "")
    rows = []
    while page := query(QUERY, [start, now, *cursor, PAGE_SIZE]):
        next_cursor = page[-1]["confirmed_at"], page[-1]["order_id"]
        if len(page) > PAGE_SIZE or next_cursor <= cursor:
            raise ValueError("Paginación de producción inválida")
        rows.extend(page)
        cursor = next_cursor
    return rows


def build_report(rows, now, duration_for):
    groups = {name: [] for name in (*PORTAL_COHORTS, UNATTRIBUTED)}
    for row in rows:
        groups[cohort(row)].append(row)
    measured = {}

    def duration(row):
        key = id(row)
        if key not in measured:
            measured[key] = duration_for(row)
        return measured[key]

    counts = {name: summarize(group, now, duration) for name, group in groups.items()}
    return {
        "utc": datetime.fromtimestamp(now, UTC).isoformat(),
        "ventana_dias": WINDOW // 86400,
        "umbral_segundos": TARGET,
        "portal_autenticado": summarize(
            (row for name in PORTAL_COHORTS for row in groups[name]), now, duration
        ),
        "origen_sin_acreditar": counts[UNATTRIBUTED],
        "cohortes": counts,
    }


def main():
    from audit_studio_kit_retention import NAS
    from check_env import parse_env
    from provision_order import query_orders

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registrar", action="store_true")
    args = parser.parse_args()
    values, errors = parse_env((ROOT / ".env").read_text(encoding="utf-8-sig"))
    if errors:
        raise ValueError("Bóveda inválida")
    now = int(datetime.now(UTC).timestamp())
    rows = read_window(lambda sql, params: query_orders(values, sql, params), now)
    report = build_report(rows, now, lambda row: delivered_duration(row, NAS))
    record = json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n"
    if args.registrar:
        path = ROOT / ".tools/evidence/studio-production-latency-last-run.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(record, encoding="utf-8")
        temporary.replace(path)
    else:
        print(record, end="")


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT / path) for path in ("sre", "scripts/ci", "docker/kitsu-zou")]
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("Falló el cotejo de duración de Studio") from None
