"""Mide el ingreso de Sofía y publica los agregados HTTP de Studio."""

import http.client
import json
import math
import re
import ssl
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import error, request

if __package__:
    from . import studio_production_metrics as otlp
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import studio_production_metrics as otlp

SQL_API = "https://api.cloudflare.com/client/v4/analytics/sql"
GRAPHQL_API = "https://api.cloudflare.com/client/v4/graphql"
BROWSER_API = "https://api.cloudflare.com/client/v4/accounts/{account}/analytics_engine/sql"
BROWSER_ROUTES = ("auth_me", "panel", "multipart_inicio", "multipart_firma")
SOFIA_INGRESS = "https://api-studio.e-scale.cl/v1/sofia/chat"
SOFIA_REJECTED = b'{"error":"SOFIA_MTLS_REJECTED"}'
HOST = "studio.e-scale.cl"
SERVICE = "agency-studio-edge"
FAMILIES = ("auth", "presign")
EDGE_FIELD = "edgeTimeToFirstByteMs"
EDGE_DENIED = f"not permitted to access column on `events.httpRequests`: {EDGE_FIELD}"
ORIGIN_FIELD = "originResponseDurationMs"
AUTH = "clientRequestPath LIKE '/api/auth/%'"
PRESIGN = """(clientRequestPath IN ('/api/upload/presign', '/api/uploads')
              OR clientRequestPath LIKE '/api/uploads/%/parts/%/sign')"""
STATUS_COUNTS = """
    COUNT(*) AS total,
    countIf(edgeResponseStatus BETWEEN 200 AND 399) AS respuestas_2xx_3xx,
    countIf(edgeResponseStatus BETWEEN 400 AND 499) AS respuestas_4xx,
    countIf(edgeResponseStatus BETWEEN 500 AND 599) AS respuestas_5xx
"""
COUNT_FIELDS = ("total", "respuestas_2xx_3xx", "respuestas_4xx", "respuestas_5xx")
WINDOW = timedelta(days=30)
INGEST_DELAY = timedelta(minutes=3)
R2_BUCKET = "studio-media-bucket"
R2_WINDOW = timedelta(minutes=15)
R2_ACTIONS = ("CreateMultipartUpload", "UploadPart", "CompleteMultipartUpload")
R2_QUERY = """
query StudioR2($account: string!, $start: Time!, $end: Time!, $bucket: string!) {
  viewer {
    accounts(filter: { accountTag: $account }) {
      r2OperationsAdaptiveGroups(limit: 100, filter: {
        bucketName: $bucket, datetime_geq: $start, datetime_leq: $end,
        actionType_in: ["CreateMultipartUpload", "UploadPart", "CompleteMultipartUpload"]
      }) {
        dimensions { actionType actionStatus responseStatusCode }
        sum { requests }
      }
    }
  }
}
"""


def probe_sofia(opener, timestamp):
    query = request.Request(
        SOFIA_INGRESS,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "studio-sre-sofia-contract-v1",
            "timestamp": str(timestamp),
            "signature": "v1=" + "0" * 64,
        },
    )
    try:
        try:
            response = opener.open(query, timeout=5)
        except error.HTTPError as rejected:
            response = rejected
        with response:
            return response.code == 401 and response.read(len(SOFIA_REJECTED) + 1) == SOFIA_REJECTED
    except (OSError, http.client.HTTPException):
        return False


def statement(select, filter_by=""):
    return f"""
      SELECT CASE WHEN {AUTH} THEN 'auth' ELSE 'presign' END AS familia, {select}
      FROM events.httpRequests
      WHERE clientRequestHttpHost = $host AND requestSource = 'eyeball'
        AND ({AUTH} OR {PRESIGN}) {filter_by}
      GROUP BY familia LIMIT 2
    """


def read_rows(opener, token, account, start, end, query):
    status, raw = otlp.post(
        opener,
        SQL_API,
        {
            "scope": {"accountTag": account},
            "time_range": {"start": start.isoformat(), "end": end.isoformat()},
            "params": {"host": HOST},
            "query": query,
        },
        {"Authorization": f"Bearer {token}"},
        limit=16384,
    )
    response = json.loads(raw)
    rows = response["data"]
    if status != 200 or type(rows) is not list or response["rows"] != len(rows):
        raise ValueError("RESPUESTA_ANALITICA")
    by_family = {}
    for row in rows:
        family = row["familia"]
        if family not in FAMILIES or family in by_family:
            raise ValueError("FAMILIA_ANALITICA")
        by_family[family] = row
    return by_family


def count(value):
    if type(value) is str and re.fullmatch(r"[0-9]{1,16}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("RECUENTO_ANALITICO")
    return value


def read_browser(opener, token, account, end):
    if not re.fullmatch(r"[a-f0-9]{32}", account):
        raise ValueError("CUENTA_ANALITICA")
    start_text = (end - WINDOW).strftime("%Y-%m-%d %H:%M:%S")
    end_text = end.strftime("%Y-%m-%d %H:%M:%S")
    sql = f"""
      SELECT index1 AS ruta, count() AS observaciones,
        quantileExactWeighted(0.95)(double1, _sample_interval) AS p95_ms
      FROM studio_browser_http_v1
      WHERE blob1 = 'produccion' AND double2 BETWEEN 200 AND 299
        AND timestamp >= toDateTime('{start_text}') AND timestamp < toDateTime('{end_text}')
      GROUP BY ruta
    """
    query = request.Request(
        BROWSER_API.format(account=account),
        data=sql.encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "text/plain"},
    )
    with opener.open(query, timeout=8) as response:
        data = response.read(16385)
        if response.code != 200 or len(data) > 16384:
            raise ValueError("ANALITICA_NAVEGADOR")
    doc = json.loads(data)
    rows = doc["data"]
    if type(rows) is not list or doc["rows"] != len(rows) or len(rows) > len(BROWSER_ROUTES):
        raise ValueError("ANALITICA_NAVEGADOR")
    timings = {}
    for row in rows:
        route, observations, p95 = row["ruta"], count(row["observaciones"]), row["p95_ms"]
        if (
            route not in BROWSER_ROUTES
            or route in timings
            or observations <= 0
            or type(p95) not in (int, float)
            or not 1 <= p95 <= 120_000
        ):
            raise ValueError("ANALITICA_NAVEGADOR")
        timings[route] = {"observaciones": observations, "p95_ms": p95}
    return timings


def read_r2_operations(opener, token, account, end):
    status, raw = otlp.post(
        opener,
        GRAPHQL_API,
        {
            "query": R2_QUERY,
            "variables": {
                "account": account,
                "bucket": R2_BUCKET,
                "start": (end - R2_WINDOW).isoformat(),
                "end": end.isoformat(),
            },
        },
        {"Authorization": f"Bearer {token}"},
        limit=16384,
    )
    doc = json.loads(raw)
    if status != 200 or not isinstance(doc, dict) or doc.get("errors"):
        raise ValueError("ANALITICA_R2")
    accounts = doc["data"]["viewer"]["accounts"]
    if not isinstance(accounts, list) or len(accounts) != 1:
        raise ValueError("ANALITICA_R2")
    rows = accounts[0]["r2OperationsAdaptiveGroups"]
    if not isinstance(rows, list) or len(rows) >= 100:
        raise ValueError("ANALITICA_R2")
    total = errors = 0
    for row in rows:
        dimensions = row["dimensions"]
        action, state, code = (
            dimensions[k] for k in ("actionType", "actionStatus", "responseStatusCode")
        )
        if action not in R2_ACTIONS or state not in ("success", "userError", "internalError"):
            raise ValueError("ANALITICA_R2")
        if type(code) is not int or not 100 <= code <= 599:
            raise ValueError("ANALITICA_R2")
        operations = count(row["sum"]["requests"])
        total += operations
        if state == "internalError" or code >= 500:
            errors += operations
    return {"operaciones": total, "errores_servidor": errors}


def duration(rows, family):
    if family not in rows:
        return {"medidos": 0, "p95_ms": None}
    row = rows[family]
    measured, p95 = count(row["medidos"]), row["p95_ms"]
    if measured <= 0 or type(p95) not in (int, float) or not 0 <= p95 or not math.isfinite(p95):
        raise ValueError("DURACION_ANALITICA")
    return {"medidos": measured, "p95_ms": p95}


def build_report(read, end):
    counts = read(statement(STATUS_COUNTS))

    def read_duration(field, lower_bound):
        return read(
            statement(
                f"COUNT(*) AS medidos, quantileWeighted(0.95)({field}, sampleInterval) AS p95_ms",
                f"AND {field} {lower_bound}",
            )
        )

    origins = read_duration(ORIGIN_FIELD, "> 0")
    try:
        edges = read_duration(EDGE_FIELD, ">= 0")
    except error.HTTPError as denied:
        with denied:
            if denied.code != 403 or denied.read(256).decode("utf-8").strip() != EDGE_DENIED:
                raise
        edges = None
    report = {}
    for family in FAMILIES:
        row = {
            name: count(counts[family][name]) if family in counts else 0 for name in COUNT_FIELDS
        }
        classified = sum(row[name] for name in COUNT_FIELDS[1:])
        if classified > row["total"]:
            raise ValueError("COBERTURA_ANALITICA")
        row["otras_respuestas"] = row["total"] - classified
        row["proporcion_2xx_3xx"] = (
            row["respuestas_2xx_3xx"] / row["total"] if row["total"] else None
        )
        row["latencia_origen"] = duration(origins, family)
        row["ttfb_edge"] = duration(edges, family) if edges is not None else None
        report[family] = row
    return {
        "inicio_utc": (end - WINDOW).isoformat(),
        "fin_utc": end.isoformat(),
        "ttfb_edge_consultable": edges is not None,
        "rutas": report,
    }


def gauge(name, points):
    return {"name": f"studio_edge_{name}", "gauge": {"dataPoints": points}}


def report_metrics(report, time_ns):
    def point(value, labels=()):
        encoded = {"asInt": str(value)} if type(value) is int else {"asDouble": value}
        return {"timeUnixNano": str(time_ns), **encoded, "attributes": list(labels)}

    metrics = []
    for metric, field in (
        ("30d_requests", "total"),
        ("30d_successful_requests", "respuestas_2xx_3xx"),
        ("30d_client_errors", "respuestas_4xx"),
        ("30d_server_errors", "respuestas_5xx"),
    ):
        metrics.append(
            gauge(
                metric,
                [
                    point(report["rutas"][family][field], [otlp.label("ruta", family)])
                    for family in FAMILIES
                ],
            )
        )
    for label, metric in (("latencia_origen", "origin"), ("ttfb_edge", "ttfb")):
        series = [
            (family, row[label])
            for family, row in report["rutas"].items()
            if row[label] is not None
        ]
        if series:
            metrics.append(
                gauge(
                    f"30d_{metric}_samples",
                    [
                        point(timings["medidos"], [otlp.label("ruta", family)])
                        for family, timings in series
                    ],
                )
            )
        p95 = [
            point(timings["p95_ms"], [otlp.label("ruta", family)])
            for family, timings in series
            if timings["p95_ms"] is not None
        ]
        if p95:
            metrics.append(gauge(f"30d_{metric}_p95_milliseconds", p95))
    metrics.extend(
        (
            gauge("ttfb_query_allowed", [point(int(report["ttfb_edge_consultable"]))]),
            gauge("report_unixtime_seconds", [point(time_ns // 1_000_000_000)]),
        )
    )
    return metrics


def metrics_payload(report, time_ns, *, sofia=None, r2=None, browser=None):
    metrics = report_metrics(report, time_ns) if report is not None else []
    bucket = [otlp.label("bucket", R2_BUCKET)]
    r2_values = {"r2_query_ok": int(r2 is not None)}
    if r2 is not None:
        r2_values.update(
            r2_15m_operations=r2["operaciones"],
            r2_15m_server_errors=r2["errores_servidor"],
            r2_report_unixtime_seconds=time_ns // 1_000_000_000,
        )
    metrics.extend(
        gauge(name, [{"timeUnixNano": str(time_ns), "asInt": str(value), "attributes": bucket}])
        for name, value in r2_values.items()
    )
    metrics.append(
        gauge(
            "browser_query_ok",
            [{"timeUnixNano": str(time_ns), "asInt": str(int(browser is not None))}],
        )
    )
    if browser is not None:
        samples = []
        p95_points = []
        for route in BROWSER_ROUTES:
            point = {"timeUnixNano": str(time_ns), "attributes": [otlp.label("ruta", route)]}
            series = browser.get(route)
            samples.append({**point, "asInt": str(series["observaciones"] if series else 0)})
            if series:
                p95 = series["p95_ms"]
                number = {"asInt": str(p95)} if type(p95) is int else {"asDouble": p95}
                p95_points.append({**point, **number})
        metrics.append(gauge("30d_browser_samples", samples))
        if p95_points:
            metrics.append(gauge("30d_browser_p95_milliseconds", p95_points))
    if sofia is not None:
        healthy, checked_at = sofia
        for name, value in (
            ("sofia_ingress_healthy", int(healthy)),
            ("sofia_probe_unixtime_seconds", checked_at),
        ):
            metrics.append(gauge(name, [{"timeUnixNano": str(time_ns), "asInt": str(value)}]))
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        otlp.label("service.name", SERVICE),
                        otlp.label("service.instance.id", "vm-media-01"),
                    ]
                },
                "scopeMetrics": [{"scope": {"name": "studio.edge"}, "metrics": metrics}],
            }
        ]
    }


def main():
    try:
        profile, token, tls = otlp.configuration()
        end = datetime.now(UTC).replace(microsecond=0) - INGEST_DELAY
        source = otlp.client(ssl.create_default_context())
        checked_at = int(time.time())
        healthy = probe_sofia(source, checked_at)
        browser = None
        try:
            browser = read_browser(source, token, profile["account_id"], end)
        except error.HTTPError as failed:
            failed.close()
        except (OSError, http.client.HTTPException, KeyError, TypeError, ValueError):
            pass
        r2 = None
        try:
            r2 = read_r2_operations(source, token, profile["account_id"], end)
        except error.HTTPError as failed:
            failed.close()
        except (OSError, http.client.HTTPException, KeyError, TypeError, ValueError):
            pass
        report = None
        try:
            report = build_report(
                lambda query: read_rows(
                    source, token, profile["account_id"], end - WINDOW, end, query
                ),
                end,
            )
        finally:
            ack = otlp.post(
                otlp.client(tls),
                otlp.PROMETHEUS,
                metrics_payload(
                    report, time.time_ns(), sofia=(healthy, checked_at), r2=r2, browser=browser
                ),
                {},
                limit=16384,
            )
            otlp.check_ack(*ack)
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
                "sofia_ingress": healthy,
                "analitica_r2": r2,
                "analitica_navegador": browser,
                **report,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
