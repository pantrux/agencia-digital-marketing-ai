import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path

from sre import studio_render_audio_metrics as metrics
from sre.audit_studio_render_audio import audit, read_origins

NOW = 1_794_752_000
TOKEN = "credencial-sintetica"


def make_render(root, job, lufs, peak, *, tampered=False, timestamp=NOW - 60):
    relative = f"proyectos/job-ejemplo/06_renders/{job}/render"
    destination = root / relative
    destination.mkdir(parents=True)
    (destination / "master.mp4").write_bytes(b"mp4")
    body = json.dumps(
        {
            "request_sha256": "a" * 64,
            "result": {
                "files": [{"path": f"{relative}/master.mp4", "bytes": 3, "sha256": "b" * 64}],
                "codec": "h264_nvenc",
                "hardware_acceleration": True,
                "audio_verified": True,
                "audio": {"integrated_lufs": lufs, "true_peak_dbtp": peak},
            },
        }
    )
    sig = hmac.new(
        TOKEN.encode(), b"agencia-puente-gpu:recibo:v1\n" + body.encode(), hashlib.sha256
    ).hexdigest()
    if tampered:
        body = body.replace("master.mp4", "master-other.mp4")
    receipt = destination / ".recibo-puente-v1.json"
    receipt.write_text(json.dumps({"body": body, "signature": sig}), encoding="utf-8")
    os.utime(receipt, (timestamp, timestamp))
    return destination


def test_denominador_solo_incluye_recibos_validos_dentro_de_la_ventana(tmp_path: Path):
    make_render(tmp_path, "limite-inferior", -15, -1)
    make_render(tmp_path, "limite-superior", -13, -1.1)
    make_render(tmp_path, "pico-excedido", -14, -0.99)
    make_render(tmp_path, "alterado", -14, -1, tampered=True)
    make_render(tmp_path, "vencido", -14, -2, timestamp=NOW - 31 * 86400)

    def origins(projects):
        assert set(projects) == {"job-ejemplo"}
        return {"job-ejemplo": "portal_ensayo"}

    results = audit(tmp_path, TOKEN, NOW, origins)

    assert results["recibos_invalidos"] == 1
    assert results["muestra"]["nvenc"] == {
        "medidos": 3,
        "conformes": 2,
        "fuera_de_objetivo": 1,
        "porcentaje_observado": 66.667,
    }
    assert results["cohortes_nvenc"]["portal_ensayo"]["medidos"] == 3
    assert results["cohortes_nvenc"]["portal_produccion"]["porcentaje_observado"] is None


def test_recibo_sin_archivo_asociado_queda_fuera_de_la_muestra(tmp_path: Path):
    folder = make_render(tmp_path, "sin-archivo", -14, -1)
    (folder / "master.mp4").unlink()
    (folder.parent.parent / "abierto" / "render").mkdir(parents=True)

    results = audit(tmp_path, TOKEN, NOW, lambda projects: {})

    assert results["recibos_invalidos"] == 1
    assert results["directorios_sin_recibo"] == 1
    assert results["muestra"]["nvenc"]["medidos"] == 0


def test_historico_mide_el_archivo_sin_atribuirlo_a_nvenc(tmp_path: Path):
    make_render(tmp_path, "firmado", -14, -1.1)
    make_render(tmp_path, "alterado", -14, -1.1, tampered=True)
    for job in ("en-rango", "sonoridad-baja", "agotado", "antiguo", "duplicado"):
        folder = make_render(tmp_path, job, -14, -1.1)
        (folder / ".recibo-puente-v1.json").unlink()
        if job == "duplicado":
            (folder / "master-corrected.mov").write_bytes(b"mov")
        timestamp = NOW - (31 * 86400 if job == "antiguo" else 60)
        for media in folder.iterdir():
            os.utime(media, (timestamp, timestamp))

    def measure(media):
        job = media.parent.parent.name
        if job == "agotado":
            raise subprocess.TimeoutExpired("ffmpeg", 1)
        return {"en-rango": (-14, -1.1), "sonoridad-baja": (-15.2, -1.1)}[job]

    results = audit(tmp_path, TOKEN, NOW, lambda projects: {}, measure_unsigned=measure)

    assert results["muestra"]["nvenc"]["medidos"] == 1
    assert results["recibos_invalidos"] == 1
    historical = results["historico_sin_recibo"]
    assert historical["candidatos_en_ventana"] == 3
    assert historical["sin_master_univoco"] == 1
    assert historical["sin_medicion"] == 1
    assert historical["muestra"]["mp4"]["conformes"] == 1
    assert historical["muestra"]["mp4"]["fuera_de_objetivo"] == 1
    assert historical["muestra"]["mov"]["medidos"] == 0


def test_lee_procedencia_de_los_proyectos_medidos_sin_filtrar_fecha_del_brief():
    projects = {f"studio-{number:02}" for number in range(33)}
    batches = []

    def query(sql, params):
        assert "production_briefs" not in sql
        assert sql.count("?") == len(params)
        batches.append(params)
        return [
            {
                "project_slug": project,
                "portal_started_at": 1,
                "provider_mode": "production",
                "reserved_test_domain": False,
            }
            for project in params[:-1]
        ]

    origins = read_origins(query, projects)

    assert [len(batch) for batch in batches] == [32, 1]
    assert origins["studio-00"] == "portal_produccion"
    assert "studio-32" not in origins


def test_publica_muestras_por_origen_sin_proyectar_identidades(tmp_path: Path):
    make_render(tmp_path, "private-render-name", -14, -1.2)
    report = audit(tmp_path, TOKEN, NOW, lambda projects: {"job-ejemplo": "portal_ensayo"})

    payload = metrics.metrics_payload(report, NOW * 1_000_000_000)
    assert "private-render-name" not in json.dumps(payload)
    assert "job-ejemplo" not in json.dumps(payload)
    gauges = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    names = {gauge["name"]: gauge["gauge"]["dataPoints"] for gauge in gauges}
    points = names["studio_render_audio_30d_samples"]
    counts = {
        tuple(label["value"]["stringValue"] for label in point["attributes"]): point["asInt"]
        for point in points
    }
    assert counts[("nvenc", "total")] == "1"
    assert counts[("nvenc", "portal_ensayo")] == "1"
    assert counts[("nvenc", "portal_produccion")] == "0"
    assert counts[("prores_4444", "total")] == "0"
    assert names["studio_render_audio_target_ratio"][0]["asDouble"] == 0.995
