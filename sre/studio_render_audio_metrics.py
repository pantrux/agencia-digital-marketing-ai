"""Publica en el Prometheus del NAS el control de audio de los recibos firmados."""

import json
import os
import re
import ssl
import sys
import time
from pathlib import Path
from urllib import error

if __package__:
    from . import audit_studio_render_audio as audio
    from . import studio_production_metrics as otlp
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import audit_studio_render_audio as audio
    import studio_production_metrics as otlp

NAS = Path("/mnt/producciones")
SERVICE = "agency-studio-render-audio"


def metrics_payload(report, time_ns):
    def point(value, attributes=()):
        encoded = {"asInt": str(value)} if type(value) is int else {"asDouble": value}
        return {"timeUnixNano": str(time_ns), **encoded, "attributes": list(attributes)}

    def gauge(name, points):
        return {"name": f"studio_render_audio_{name}", "gauge": {"dataPoints": points}}

    groups = {(codec, "total"): counts for codec, counts in report["muestra"].items()}
    groups.update(
        {("nvenc", cohort): counts for cohort, counts in report["cohortes_nvenc"].items()}
    )
    metrics = [
        gauge(
            name,
            [
                point(counts[field], [otlp.label("codec", codec), otlp.label("cohorte", cohort)])
                for (codec, cohort), counts in groups.items()
            ],
        )
        for name, field in (("30d_samples", "medidos"), ("30d_compliant", "conformes"))
    ]
    metrics += [
        gauge("invalid_receipts", [point(report["recibos_invalidos"])]),
        gauge("directories_without_receipt", [point(report["directorios_sin_recibo"])]),
        gauge("target_ratio", [point(report["objetivo_porcentaje_nvenc"] / 100)]),
        gauge("report_unixtime_seconds", [point(time_ns // 1_000_000_000)]),
    ]
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        otlp.label("service.name", SERVICE),
                        otlp.label("service.instance.id", "vm-media-01"),
                    ]
                },
                "scopeMetrics": [{"scope": {"name": "studio.render.audio"}, "metrics": metrics}],
            }
        ]
    }


def main():
    try:
        if not NAS.is_mount():
            raise ValueError("MONTAJE_AUSENTE")
        profile, token, tls = otlp.configuration()
        credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
        signing_key = (credentials / "gpu-token").read_text(encoding="ascii").rstrip("\r\n")
        if not re.fullmatch(r"[!-~]{1,4096}", signing_key) or signing_key == "CHANGE_ME":
            raise ValueError("CREDENCIAL_INVALIDA")
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{profile['account_id']}/d1/database/"
            f"{profile['database_id']}/query"
        )
        query = otlp.read_cloudflare(url, token, otlp.client(ssl.create_default_context()))
        report = audio.audit(
            NAS, signing_key, int(time.time()), lambda p: audio.read_origins(query, p)
        )
        status, ack = otlp.post(
            otlp.client(tls),
            otlp.PROMETHEUS,
            metrics_payload(report, time.time_ns()),
            {},
            limit=16384,
        )
        otlp.check_ack(status, ack)
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
    print(json.dumps({"success": True, **report}, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
