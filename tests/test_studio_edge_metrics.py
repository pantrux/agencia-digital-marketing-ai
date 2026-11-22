import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError

import pytest

from sre import studio_edge_metrics as edge

NOW = datetime(2026, 11, 15, 15, tzinfo=UTC)


def test_mide_apertura_y_firmas_multipart_para_archivos_y_fuentes():
    archivo = "/api/uploads/1024e07e-7733-4770-8cc1-3931e28a4d32"
    fuente = (
        "/api/orders/8d23dbf8-666c-466e-9055-16ac19e06cda"
        "/sources/a822e19d-b15c-40d2-9fb0-ed6fda609493"
    )
    presign = [
        "/api/upload/presign",
        "/api/uploads",
        f"{archivo}/parts/4/sign",
        f"{archivo}/parts/sign",
        f"{fuente}/upload",
        f"{fuente}/parts/4/sign",
        f"{fuente}/parts/sign",
    ]
    otras_rutas = [
        "/api/auth/me",
        "/api/orders",
        f"{archivo}/parts",
        f"{fuente}/parts",
        f"{fuente}/complete",
    ]
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE peticiones (clientRequestPath TEXT)")
        database.executemany(
            "INSERT INTO peticiones VALUES (?)", [(route,) for route in presign + otras_rutas]
        )
        result = database.execute(
            f"SELECT clientRequestPath FROM peticiones WHERE {edge.PRESIGN} ORDER BY rowid"
        ).fetchall()
    assert result == [(route,) for route in presign]


def fake_source(query):
    assert "clientRequestHttpHost = $host" in query and "requestSource = 'eyeball'" in query
    assert "/api/uploads/%/parts/%/sign" in query and "GROUP BY familia LIMIT 2" in query
    if edge.ORIGIN_FIELD in query:
        return {"auth": {"medidos": 2, "p95_ms": 189}}
    if edge.EDGE_FIELD in query:
        return {"auth": {"medidos": 3, "p95_ms": 0}}
    return {"auth": dict(zip(edge.COUNT_FIELDS, (4, 2, 1, 1), strict=True))}


def test_publica_ttfb_y_origen_como_mediciones_independientes():
    report = edge.build_report(fake_source, NOW)
    auth = report["rutas"]["auth"]

    assert report["inicio_utc"] == "2026-10-16T15:00:00+00:00"
    assert auth["proporcion_2xx_3xx"] == 0.5
    assert auth["latencia_origen"] == {"medidos": 2, "p95_ms": 189}
    assert auth["ttfb_edge"] == {"medidos": 3, "p95_ms": 0}
    assert report["rutas"]["presign"]["proporcion_2xx_3xx"] is None


def test_denegacion_especifica_mantiene_los_recuentos_y_omite_p95_del_borde():
    def source(query):
        if edge.EDGE_FIELD in query:
            raise HTTPError(edge.SQL_API, 403, "", None, io.BytesIO(edge.EDGE_DENIED.encode()))
        return fake_source(query)

    report = edge.build_report(source, NOW)
    payload = edge.metrics_payload(report, 1_700_000_000_000_000_000)
    series = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    metrics = {item["name"]: item["gauge"]["dataPoints"] for item in series}

    assert report["ttfb_edge_consultable"] is False
    assert report["rutas"]["auth"]["ttfb_edge"] is None
    assert metrics["studio_edge_ttfb_query_allowed"][0]["asInt"] == "0"
    assert metrics["studio_edge_30d_origin_p95_milliseconds"][0]["asInt"] == "189"
    assert metrics["studio_edge_30d_server_errors"][0]["asInt"] == "1"
    assert "studio_edge_30d_ttfb_p95_milliseconds" not in metrics


def test_denegacion_general_descarta_la_medicion():
    def source(query):
        if edge.EDGE_FIELD in query:
            raise HTTPError(edge.SQL_API, 403, "", None, io.BytesIO(b"Access denied"))
        return fake_source(query)

    with pytest.raises(HTTPError, match="403"):
        edge.build_report(source, NOW)


def test_lee_percentiles_ponderados_de_navegador_en_la_ventana_de_produccion():
    class BrowserSource:
        def open(self, query, timeout):
            assert query.full_url.endswith("/accounts/" + "1" * 32 + "/analytics_engine/sql")
            assert query.get_header("Authorization") == "Bearer prueba"
            assert query.get_header("Content-type") == "text/plain"
            assert 0 < timeout <= 8
            sql = query.data.decode()
            assert "FROM studio_browser_http_v1" in sql and "blob1 = 'produccion'" in sql
            assert "quantileExactWeighted(0.95)(double1, _sample_interval)" in sql
            assert "2026-10-16 15:00:00" in sql and "2026-11-15 15:00:00" in sql
            response = io.BytesIO(
                json.dumps(
                    {
                        "data": [
                            {"ruta": "panel", "observaciones": "21", "p95_ms": 287},
                        ],
                        "rows": 1,
                    }
                ).encode()
            )
            response.code = 200
            return response

    timings = edge.read_browser(BrowserSource(), "prueba", "1" * 32, NOW)
    assert timings == {"panel": {"observaciones": 21, "p95_ms": 287}}


def test_muestras_vacias_y_consulta_fallida_nunca_publican_un_percentil_cero():
    def exported(browser):
        payload = edge.metrics_payload(None, 10**9, browser=browser)
        metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        return {m["name"].removeprefix("studio_edge_"): m["gauge"]["dataPoints"] for m in metrics}

    failed = exported(None)
    assert failed["browser_query_ok"][0]["asInt"] == "0"
    assert "30d_browser_samples" not in failed
    empty = exported({})
    assert empty["browser_query_ok"][0]["asInt"] == "1"
    assert len(empty["30d_browser_samples"]) == 4
    assert all(p["asInt"] == "0" for p in empty["30d_browser_samples"])
    assert "30d_browser_p95_milliseconds" not in empty
    available = exported({"panel": {"observaciones": 21, "p95_ms": 287}})
    assert len(available["30d_browser_p95_milliseconds"]) == 1
    assert available["30d_browser_p95_milliseconds"][0]["asInt"] == "287"


def test_r2_cuenta_solo_errores_de_servidor_de_la_ventana(monkeypatch):
    def post(_opener, url, payload, headers, *, limit):
        assert url == "https://api.cloudflare.com/client/v4/graphql"
        assert "actionType_in:" in payload["query"]
        assert payload["variables"] == {
            "account": "cuenta-de-prueba",
            "bucket": "studio-media-bucket",
            "start": "2026-11-15T14:45:00+00:00",
            "end": "2026-11-15T15:00:00+00:00",
        }
        assert headers == {"Authorization": "Bearer prueba"}
        assert limit == 16384
        items = [
            ("UploadPart", "success", 200, 20),
            ("UploadPart", "userError", 403, 2),
            ("UploadPart", "internalError", 503, 3),
            ("CreateMultipartUpload", "internalError", 429, 1),
            ("CompleteMultipartUpload", "userError", 502, 2),
        ]
        rows = [
            {
                "dimensions": {
                    "actionType": operation,
                    "actionStatus": state,
                    "responseStatusCode": status,
                },
                "sum": {"requests": requests},
            }
            for operation, state, status, requests in items
        ]
        return 200, json.dumps(
            {
                "data": {"viewer": {"accounts": [{"r2OperationsAdaptiveGroups": rows}]}},
                "errors": None,
            }
        ).encode()

    monkeypatch.setattr(edge.otlp, "post", post)
    assert edge.read_r2_operations(object(), "prueba", "cuenta-de-prueba", NOW) == {
        "operaciones": 28,
        "errores_servidor": 6,
    }


def test_r2_distingue_cero_de_consulta_fallida(monkeypatch):
    valid = {
        "data": {"viewer": {"accounts": [{"r2OperationsAdaptiveGroups": []}]}},
        "errors": [],
    }
    monkeypatch.setattr(edge.otlp, "post", lambda *_, **__: (200, json.dumps(valid).encode()))
    report = edge.read_r2_operations(object(), "prueba", "cuenta", NOW)
    assert report == {"operaciones": 0, "errores_servidor": 0}
    failed = {**valid, "errors": [{"message": "consulta rechazada"}]}
    monkeypatch.setattr(edge.otlp, "post", lambda *_, **__: (200, json.dumps(failed).encode()))
    with pytest.raises(ValueError, match="ANALITICA_R2"):
        edge.read_r2_operations(object(), "prueba", "cuenta", NOW)

    def exported(result):
        payload = edge.metrics_payload(None, 10**9, r2=result)
        metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        return {
            item["name"]: item["gauge"]["dataPoints"][0]["asInt"]
            for item in metrics
            if item["name"].startswith("studio_edge_r2_")
        }

    assert exported(None) == {"studio_edge_r2_query_ok": "0"}
    expected = exported(report)
    assert expected["studio_edge_r2_query_ok"] == "1"
    assert expected["studio_edge_r2_15m_server_errors"] == "0"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, b'{"error":"SOFIA_MTLS_REJECTED"}', True),
        (401, b'{"error":"OTRO_ORIGEN"}', False),
        (502, b'{"error":"SOFIA_MTLS_REJECTED"}', False),
        (200, b"{}", False),
        (None, None, False),
    ],
)
def test_sonda_sofia_exige_rechazo_del_ingreso(status, body, expected):
    class Ingress:
        def open(self, query, timeout):
            assert query.full_url == "https://api-studio.e-scale.cl/v1/sofia/chat"
            assert query.get_method() == "POST" and query.data == b"{}"
            assert query.get_header("Content-type") == "application/json"
            assert query.get_header("Timestamp") == "1794754800"
            assert query.get_header("Signature") == "v1=" + "0" * 64
            assert 0 < timeout <= 8
            if status is None:
                raise URLError("sin conexión")
            if status == 200:
                stream = io.BytesIO(body)
                stream.code = status
                return stream
            raise HTTPError(query.full_url, status, "", None, io.BytesIO(body))

    assert edge.probe_sofia(Ingress(), 1794754800) is expected


@pytest.mark.parametrize("r2", [None, {"operaciones": 7, "errores_servidor": 1}])
def test_la_sonda_y_la_analitica_r2_se_publican_aunque_falle_sql(monkeypatch, r2):
    def unavailable(*_):
        raise HTTPError(edge.SQL_API, 503, "", None, io.BytesIO(b""))

    captured = []

    def publish(_opener, _url, payload, _headers, *, limit):
        assert limit >= 1024
        captured.append(payload)
        return 200, b""

    monkeypatch.setattr(
        edge.otlp, "configuration", lambda: ({"account_id": "prueba"}, "", object())
    )
    monkeypatch.setattr(edge.otlp, "client", lambda _: object())
    monkeypatch.setattr(edge, "probe_sofia", lambda *_: False)
    monkeypatch.setattr(edge, "read_browser", unavailable)
    monkeypatch.setattr(edge, "build_report", unavailable)
    monkeypatch.setattr(edge, "read_r2_operations", unavailable if r2 is None else lambda *_: r2)
    monkeypatch.setattr(edge.otlp, "post", publish)
    assert edge.main() == 1
    metrics = captured[0]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    base = {
        "studio_edge_sofia_ingress_healthy",
        "studio_edge_sofia_probe_unixtime_seconds",
        "studio_edge_r2_query_ok",
        "studio_edge_browser_query_ok",
    }
    if r2 is not None:
        base.update(
            {
                "studio_edge_r2_report_unixtime_seconds",
                "studio_edge_r2_15m_operations",
                "studio_edge_r2_15m_server_errors",
            }
        )
    assert {item["name"] for item in metrics} == base
    values = {metric["name"]: metric["gauge"]["dataPoints"][0]["asInt"] for metric in metrics}
    assert values["studio_edge_sofia_ingress_healthy"] == "0"
    assert values["studio_edge_r2_query_ok"] == ("0" if r2 is None else "1")
    if r2 is not None:
        assert values["studio_edge_r2_15m_server_errors"] == "1"
