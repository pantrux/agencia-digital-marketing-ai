"""Publica métricas de ingesta y entrega de Studio en el Prometheus del NAS."""

import json
import os
import re
import ssl
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib import error, request

if __package__:
    from . import audit_studio_production_latency as audit
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import audit_studio_production_latency as audit

PROFILE = Path(__file__).resolve().with_name("profile.json")
PROMETHEUS = "https://192.168.0.8:9090/api/v1/otlp/v1/metrics"
METRICS = {
    "studio_delivery_30d_completed": "completados",
    "studio_delivery_30d_completed_with_manual_review": "completados_con_intervencion_manual",
    "studio_delivery_30d_duration_verified": "completados_con_duracion_cotejada",
    "studio_delivery_30d_pending_overdue": "pendientes_con_demora",
    "studio_delivery_30d_manual_review": "revision_manual",
    "studio_delivery_30d_cancelled": "cancelados",
    "studio_delivery_30d_reel_samples": "muestras_de_60_s",
    "studio_delivery_30d_reel_under_target": "entregados_antes_de_360_s",
    "studio_delivery_30d_brief_p95_seconds": "p95_observado_s",
    "studio_delivery_30d_reservation_p95_seconds": "p95_observado_s_desde_reserva",
}
UPLOAD_OUTCOMES = (
    "verificada",
    "completa_sin_despacho",
    "sha256_distinto",
    "metadatos_distintos",
    "cancelada",
    "vencida",
    "motivo_desconocido",
    "pendiente",
)
UPLOAD_SIGNALS = ("http_403", "sin_respuesta")
UPLOAD_QUERY = """
    WITH sesiones AS (
        SELECT CASE WHEN o.portal_started_at IS NOT NULL THEN 1 END AS portal_started_at,
               lower(u.email) LIKE '%.invalid' AS reserved_test_domain,
               COALESCE(o.provider_mode,'production') AS provider_mode,
               CASE
                   WHEN s.status = 'completed' AND j.order_id IS NOT NULL THEN 'verificada'
                   WHEN s.status = 'completed' THEN 'completa_sin_despacho'
                   WHEN s.status <> 'aborted' THEN 'pendiente'
                   WHEN s.abort_reason = 'sha256_mismatch' THEN 'sha256_distinto'
                   WHEN s.abort_reason = 'metadata_mismatch' THEN 'metadatos_distintos'
                   WHEN s.abort_reason = 'cancelled' THEN 'cancelada'
                   WHEN s.abort_reason = 'expired' THEN 'vencida'
                   ELSE 'motivo_desconocido'
               END AS resultado,
               s.direct_put_403_observed, s.direct_put_no_response_observed
        FROM upload_sessions s JOIN orders o ON o.id=s.order_id AND o.user_id=s.user_id
        JOIN users u ON u.id=s.user_id
        LEFT JOIN job_outbox j ON j.order_id=s.order_id
        WHERE o.kind='production' AND s.created_at BETWEEN ? AND ?
    )
    SELECT portal_started_at,reserved_test_domain,provider_mode,resultado,COUNT(*) AS total,
           SUM(direct_put_403_observed) AS http_403,
           SUM(direct_put_no_response_observed) AS sin_respuesta
    FROM sesiones GROUP BY portal_started_at,reserved_test_domain,provider_mode,resultado
"""


def read_upload_window(query, now):
    cohorts = (*audit.PORTAL_COHORTS, audit.UNATTRIBUTED)
    counts = {cohort: dict.fromkeys(UPLOAD_OUTCOMES, 0) for cohort in cohorts}
    signals = {cohort: dict.fromkeys(UPLOAD_SIGNALS, 0) for cohort in cohorts}
    for row in query(UPLOAD_QUERY, [now - audit.WINDOW, now]):
        total = row["total"]
        if type(total) is not int or total <= 0:
            raise ValueError("RECUENTO_INGESTA_INVALIDO")
        cohort = audit.cohort(row)
        counts[cohort][row["resultado"]] += total
        for signal in UPLOAD_SIGNALS:
            observed = row[signal]
            if type(observed) is not int or not 0 <= observed <= total:
                raise ValueError("RECUENTO_INGESTA_INVALIDO")
            signals[cohort][signal] += observed
    return counts, signals


def label(key, value):
    return {"key": key, "value": {"stringValue": value}}


def metrics_payload(report, time_ns, *, uploads, upload_signals):
    def point(value, attributes):
        return {"timeUnixNano": str(time_ns), "asInt": str(value), "attributes": attributes}

    metrics = []
    for name, field in METRICS.items():
        points = []
        for cohort in (*audit.PORTAL_COHORTS, audit.UNATTRIBUTED):
            value = report["cohortes"][cohort][field]
            if value is not None:
                points.append(point(value, [label("cohorte", cohort)]))
        if points:
            metrics.append({"name": name, "gauge": {"dataPoints": points}})
    metrics.extend(
        {
            "name": name,
            "gauge": {
                "dataPoints": [
                    point(value, [label("cohorte", cohort), label(dimension, outcome)])
                    for cohort, outcomes in groups.items()
                    for outcome, value in outcomes.items()
                ]
            },
        }
        for name, dimension, groups in (
            ("studio_upload_30d_sessions", "resultado", uploads),
            ("studio_upload_30d_put_observed_sessions", "senal", upload_signals),
        )
    )
    metrics.extend(
        {"name": name, "gauge": {"dataPoints": [point(value, [])]}}
        for name, value in (
            ("studio_delivery_report_unixtime_seconds", time_ns // 1_000_000_000),
            ("studio_delivery_target_seconds", audit.TARGET),
        )
    )
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        label("service.name", "agency-studio-delivery"),
                        label("service.instance.id", "vm-media-01"),
                    ]
                },
                "scopeMetrics": [{"scope": {"name": "studio.delivery"}, "metrics": metrics}],
            }
        ]
    }


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def client(context):
    return request.build_opener(
        request.ProxyHandler({}), request.HTTPSHandler(context=context), NoRedirect()
    )


def post(opener, url, body, headers, *, limit):
    payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
    req = request.Request(
        url, data=payload, method="POST", headers={"Content-Type": "application/json", **headers}
    )
    with opener.open(req, timeout=12) as response:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("RESPUESTA_EXCESIVA")
        return response.status, data


def read_cloudflare(url, token, opener) -> Callable:
    def query(sql, params):
        status, raw = post(
            opener,
            url,
            {"sql": sql, "params": params},
            {"Authorization": f"Bearer {token}"},
            limit=1024**2,
        )
        response = json.loads(raw)
        if (
            status != 200
            or response.get("success") is not True
            or len(response.get("result", [])) != 1
        ):
            raise ValueError("CONSULTA_D1")
        result = response["result"][0]
        if result.get("success") is not True or not isinstance(result.get("results"), list):
            raise ValueError("RESULTADO_D1")
        return result["results"]

    return query


def check_ack(status, raw):
    if status != 200:
        raise ValueError("OTLP_RESPUESTA")
    response = json.loads(raw) if raw.strip() else {}
    partial = response.get("partialSuccess", {})
    if int(partial.get("rejectedDataPoints", 0)) != 0 or partial.get("errorMessage"):
        raise ValueError("OTLP_PARCIAL")


def configuration():
    profile = json.loads(PROFILE.read_bytes())
    if (
        set(profile) != {"account_id", "database_id"}
        or not re.fullmatch(r"[a-f0-9]{32}", profile["account_id"])
        or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", profile["database_id"])
    ):
        raise ValueError("PERFIL_INVALIDO")
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    token = (credentials / "cloudflare-token").read_text(encoding="ascii").rstrip("\r\n")
    if not re.fullmatch(r"[!-~]{1,4096}", token) or token == "CHANGE_ME":
        raise ValueError("CREDENCIAL_INVALIDA")
    tls = ssl.create_default_context(cafile=str(credentials / "nas-ca"))
    tls.minimum_version = ssl.TLSVersion.TLSv1_3
    tls.load_cert_chain(credentials / "nas-client", credentials / "nas-key")
    return profile, token, tls


def main():
    try:
        profile, token, tls = configuration()
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{profile['account_id']}/d1/database/"
            f"{profile['database_id']}/query"
        )
        now = int(time.time())
        query = read_cloudflare(url, token, client(ssl.create_default_context()))
        report = audit.build_report(audit.read_window(query, now), now, audit.signed_duration)
        uploads, signals = read_upload_window(query, now)
        status, ack = post(
            client(tls),
            PROMETHEUS,
            metrics_payload(report, time.time_ns(), uploads=uploads, upload_signals=signals),
            {},
            limit=16384,
        )
        check_ack(status, ack)
    except error.HTTPError as failure:
        print(json.dumps({"success": False, "http_status": failure.code}), file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError, AttributeError, ValueError) as failure:
        code = (
            str(failure)
            if type(failure) is ValueError and re.fullmatch(r"[A-Z_]+", str(failure))
            else type(failure).__name__
        )
        print(json.dumps({"success": False, "error": code}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "success": True,
                "cohortes": report["cohortes"],
                "ingesta": uploads,
                "senales_put": signals,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
