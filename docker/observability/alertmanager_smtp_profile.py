"""Genera alertmanager.yml y web.yml en un directorio POSIX privado existente."""

import argparse
import json
import os
import re
import stat
from pathlib import Path

SMTP_USER = "AGENCY_ALERTMANAGER_SMTP_USER"
SMTP_TO = "AGENCY_ALERTMANAGER_SMTP_TO"
SERVER_SAN = "agency-alertmanager.nas.invalid"
CLIENT_SAN = "agency-prometheus-notifier.nas.invalid"
RUNTIME_DIR = "/run/alertmanager"
SMTP_HOST = "smtp.gmail.com"
PROFILE_NAMES = ("alertmanager.yml", "web.yml")
ALERTS = (
    ("AgenciaNodeExporterSinMuestras", "agency-node", "Recolección de node-exporter"),
    ("AgenciaCAdvisorSinMuestras", "agency-cgroups", "Recolección de cAdvisor"),
    ("AgenciaPuenteGPUSinMuestras", "agency-gpu-bridge", "Recolección del puente GPU"),
    ("AgenciaServicioGPUDegradado", "agency-gpu-bridge", "Salud de un servicio GPU"),
    (
        "AgenciaStudioMedicionEntregasDesactualizada",
        "agency-studio-delivery",
        "Actualización de la medición de entregas de Studio",
    ),
    (
        "AgenciaStudioEntregasConDemora",
        "agency-studio-delivery",
        "Pedidos de Studio con al menos seis minutos desde el brief",
    ),
    (
        "AgenciaStudioLatenciaFueraObjetivo",
        "agency-studio-delivery",
        "p95 de entrega de reels de 60 s en 360 s o más",
    ),
    (
        "AgenciaStudioSofiaIngresoDegradado",
        "agency-studio-edge",
        "Dos comprobaciones consecutivas del ingreso público de Sofía fallaron",
    ),
    (
        "AgenciaStudioSofiaSondaDesactualizada",
        "agency-studio-edge",
        "Comprobaciones recientes del ingreso de Sofía ausentes",
    ),
    (
        "AgenciaWhisperLatenciaFueraObjetivo",
        "agency-whisper",
        "El p95 de Whisper en las inferencias observadas, incluidos ensayos, alcanza 8 segundos por minuto de audio",
    ),
    (
        "AgenciaStudioAudioFueraObjetivo",
        "agency-studio-render-audio",
        "La conformidad de audio NVENC del portal en producción cayó bajo el 99,5 %",
    ),
    (
        "AgenciaStudioMedicionAudioDesactualizada",
        "agency-studio-render-audio",
        "Informes recientes de audio de Studio ausentes",
    ),
    (
        "AgenciaStudioR2ServidorDegradado",
        "agency-studio-edge",
        "R2 registró errores de servidor en las operaciones multipart del bucket studio-media-bucket",
    ),
    (
        "AgenciaStudioR2AnaliticaInaccesible",
        "agency-studio-edge",
        "La medición de operaciones multipart de R2 carece de consultas válidas recientes",
    ),
    (
        "AgenciaStudioIntegridadIngestaFueraObjetivo",
        "agency-studio-delivery",
        "Los fallos de metadatos o SHA-256 superan el 0,1 % de las cargas con resultado conocido en producción",
    ),
    (
        "AgenciaD1CopiaDiariaSinConfirmar",
        "agency-studio-d1-backup",
        "Faltan los dos recibos válidos de la copia diaria de D1",
    ),
    (
        "AgenciaD1MonitorCopiaSinMuestras",
        "agency-studio-d1-backup",
        "Informes recientes del respaldo diario de D1 ausentes",
    ),
)
STATUS = '{{ if eq .Status "firing" }}activa{{ else }}resuelta{{ end }}'
ROLLBACK_ALERT = "AgenciaReversionStudio"
MAILBOX = re.compile(
    r"[A-Za-z0-9_+%-]+(?:\.[A-Za-z0-9_+%-]+)*@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)


def mailbox(values, name):
    value = values.get(name, "")
    if len(value) > 254 or not MAILBOX.fullmatch(value) or len(value.split("@", 1)[0]) > 64:
        raise ValueError(f"{name} debe contener una única dirección sin nombre de presentación.")
    return value


def rollback_notification(email_config):
    route = {
        "receiver": ROLLBACK_ALERT,
        "matchers": [
            f'alertname="{ROLLBACK_ALERT}"',
            'job="agency-studio-sre"',
            'severity="critical"',
        ],
        "group_by": ["alertname", "entrega", "modo"],
        "group_wait": "5s",
        "continue": False,
    }
    email = {
        **email_config,
        "send_resolved": False,
        "headers": {
            "Subject": "[agency-studio-sre] Reversion {{ .CommonLabels.modo }} {{ .CommonLabels.entrega }}"
        },
        "text": "{{ .CommonAnnotations.summary }}\n{{ .CommonAnnotations.description }}\n",
    }
    return route, {"name": ROLLBACK_ALERT, "email_configs": [email]}


def render_profiles(values):
    """Sólo consulta remitente y destinatario. El servidor verifica el SAN cliente."""
    user = mailbox(values, SMTP_USER)
    destination = mailbox(values, SMTP_TO)
    routes = []
    receivers = [{"name": "discard"}]
    for alert, job, description in ALERTS:
        receiver = job if job in {"agency-node", "agency-cgroups"} else alert
        routes.append(
            {
                "receiver": receiver,
                "matchers": [f'alertname="{alert}"', f'job="{job}"', 'severity="warning"'],
                "continue": False,
            }
        )
        receivers.append(
            {
                "name": receiver,
                "email_configs": [
                    {
                        "to": destination,
                        "from": user,
                        "smarthost": f"{SMTP_HOST}:587",
                        "auth_username": user,
                        "auth_password_file": f"{RUNTIME_DIR}/smtp-password",
                        "require_tls": True,
                        "force_implicit_tls": False,
                        "tls_config": {
                            "ca_file": f"{RUNTIME_DIR}/smtp-ca-bundle.pem",
                            "server_name": SMTP_HOST,
                            "min_version": "TLS12",
                            "insecure_skip_verify": False,
                        },
                        "send_resolved": True,
                        "headers": {"Subject": f"[Agencia observabilidad] {job} {STATUS}"},
                        "text": f"Alerta {STATUS}. {description}.\n",
                        "html": "",
                    }
                ],
            }
        )
    rollback_route, rollback_receiver = rollback_notification(receivers[1]["email_configs"][0])
    routes.append(rollback_route)
    receivers.append(rollback_receiver)
    config = {
        "route": {
            "receiver": "discard",
            "group_by": ["alertname", "job"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
            "routes": routes,
        },
        "receivers": receivers,
    }
    # El certificado del servidor debe incluir SERVER_SAN. El cliente Prometheus
    # comprueba ese nombre mediante su tls_config.server_name.
    web = {
        "tls_server_config": {
            "cert_file": f"{RUNTIME_DIR}/server.crt",
            "key_file": f"{RUNTIME_DIR}/server.key",
            "client_ca_file": f"{RUNTIME_DIR}/ca.crt",
            "client_auth_type": "RequireAndVerifyClientCert",
            "client_allowed_sans": [CLIENT_SAN],
            "min_version": "TLS13",
        }
    }
    # JSON es un subconjunto de YAML y permite producir siempre los mismos bytes.
    return {
        name: (json.dumps(profile, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
            "ascii"
        )
        for name, profile in {"alertmanager.yml": config, "web.yml": web}.items()
    }


def open_private_dir(path):
    path = Path(path)
    if os.name != "posix" or not path.is_absolute() or path.anchor != "/" or ".." in path.parts:
        raise ValueError("Indica un directorio POSIX absoluto y privado.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("El directorio debe pertenecer al usuario actual y tener modo 0700.")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def publish_profiles(path, profiles):
    """Publica archivos nuevos con 0600 sin reemplazar destinos existentes."""
    descriptor = open_private_dir(path)
    created = {}
    try:
        for name in PROFILE_NAMES:
            try:
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError(name)
        for name in PROFILE_NAMES:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            with os.fdopen(os.open(name, flags, 0o600, dir_fd=descriptor), "wb") as handle:
                info = os.fstat(handle.fileno())
                created[name] = (info.st_dev, info.st_ino)
                os.fchmod(handle.fileno(), 0o600)
                handle.write(profiles[name])
                handle.flush()
                os.fsync(handle.fileno())
        os.fsync(descriptor)
    except BaseException:
        for name, identity in created.items():
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if identity == (info.st_dev, info.st_ino):
                os.unlink(name, dir_fd=descriptor)
        raise
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="Directorio existente 0700")
    args = parser.parse_args()
    try:
        publish_profiles(args.output_dir, render_profiles(os.environ))
    except ValueError as error:
        parser.exit(1, f"Perfil Alertmanager rechazado. {error}\n")
    except OSError:
        parser.exit(1, "No se pudo publicar el perfil privado Alertmanager.\n")


if __name__ == "__main__":
    main()
