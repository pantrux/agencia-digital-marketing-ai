"""Exporta lecturas reales de NVIDIA; no instala ni inicia nada al importar."""

import asyncio
import csv
import hashlib
import math
import os
import re
import subprocess
from pathlib import Path

from bridge_common import BridgeConfig, GuardedAsgi, json_response

FIELDS = (
    "name,uuid,memory.total,memory.used,temperature.gpu,utilization.gpu,encoder.stats.sessionCount"
)
METRICS = {
    "memory_total_mib": "gpu_memory_total_mib",
    "memory_used_mib": "gpu_memory_used_mib",
    "temperature_celsius": "gpu_temperature_celsius",
    "utilization_percent": "gpu_utilization_percent",
    "encoder_sessions": "gpu_encoder_sessions",
}


def parse_gpu_csv(output: str, expected_uuid: str) -> dict:
    rows = list(csv.reader(output.strip().splitlines(), skipinitialspace=True))
    if len(rows) != 1 or len(rows[0]) != 7 or rows[0][1] != expected_uuid:
        raise ValueError("La lectura no corresponde a la única GPU asignada al puente.")
    row = rows[0]
    values = [float(value) for value in row[2:6]]
    total, used, temperature, utilization = values
    if not all(math.isfinite(value) for value in values) or not (
        0 < total <= 1024 * 1024
        and 0 <= used <= total
        and 0 <= temperature <= 130
        and 0 <= utilization <= 100
    ):
        raise ValueError("NVIDIA devolvió medidas fuera del intervalo válido.")
    sessions = None if row[6].strip() in ("N/A", "[N/A]", "[Not Supported]") else int(row[6])
    if sessions is not None and not 0 <= sessions <= 1024:
        raise ValueError("Cantidad de sesiones de codificación inválida.")
    return dict(zip(("name", "uuid", *METRICS), (row[0], row[1], *values, sessions), strict=True))


def prometheus_metrics(measures: dict) -> str:
    lines = []
    for key, metric in METRICS.items():
        if measures[key] is not None:
            lines.extend((f"# TYPE {metric} gauge", f"{metric} {measures[key]}"))
    lines.extend(
        (
            "# TYPE gpu_encoder_sessions_available gauge",
            f"gpu_encoder_sessions_available {int(measures['encoder_sessions'] is not None)}",
        )
    )
    return "\n".join(lines) + "\n"


def read_gpu(binary: Path, expected_uuid: str) -> dict:
    result = subprocess.run(
        [
            str(binary),
            f"--id={expected_uuid}",
            f"--query-gpu={FIELDS}",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return parse_gpu_csv(result.stdout, expected_uuid)


def supervisor_metrics(snapshot: dict) -> str:
    lines = []
    for name, child in sorted(snapshot.get("children", {}).items()):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            raise ValueError("Nombre de proceso propio no válido.")
        for key in ("alive", "healthy"):
            metric = "gpu_bridge_service_" + key
            lines.append(f'{metric}{{service="{name}"}} {int(child.get(key) is True)}')
        restarts = child.get("restarts", 0)
        if type(restarts) is not int or restarts < 0:
            raise ValueError("Contador de reinicios propio inválido.")
        lines.append(f'gpu_bridge_service_restarts_total{{service="{name}"}} {restarts}')
    return "\n".join(lines) + ("\n" if lines else "")


def create_app(mapping=None, supervisor=None):
    mapping = os.environ if mapping is None else mapping
    config = BridgeConfig.from_env(mapping)
    binary = Path(mapping["GPU_SMI_PATH"])
    with binary.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != mapping["GPU_SMI_SHA256"] or not binary.is_absolute():
        raise ValueError("El ejecutable NVIDIA no coincide con el respaldo autorizado.")
    expected_uuid = mapping["GPU_UUID"]

    async def readiness():
        measures = await asyncio.to_thread(read_gpu, binary, expected_uuid)
        state = supervisor.snapshot() if supervisor is not None else None
        return {
            "ready": state is None or state.get("healthy") is True,
            "gpu": measures["name"],
            "memory_used_mib": measures["memory_used_mib"],
            "encoder_sessions": measures["encoder_sessions"],
            "supervisor": state,
        }

    async def backend(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    if supervisor is not None:
                        try:
                            await supervisor.start()
                        except Exception:
                            await supervisor.stop()
                            await send(
                                {
                                    "type": "lifespan.startup.failed",
                                    "message": "No se pudo iniciar la supervisión propia.",
                                }
                            )
                            return
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    if supervisor is not None:
                        await supervisor.stop()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        elif scope.get("path") == "/metrics" and scope.get("method") == "GET":
            try:
                text = prometheus_metrics(await asyncio.to_thread(read_gpu, binary, expected_uuid))
                if supervisor is not None:
                    text += supervisor_metrics(supervisor.snapshot())
                body = text.encode()
            except (OSError, ValueError, subprocess.SubprocessError):
                await json_response(send, 503, {"error": "No se pudo leer NVIDIA."})
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/plain; version=0.0.4; charset=utf-8"),
                        (b"cache-control", b"no-store"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
        else:
            await json_response(send, 404, {"error": "Ruta no encontrada."})

    return GuardedAsgi(backend, config, "gpu-metrics", "1", readiness)
