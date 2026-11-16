"""Auditoría SSH de DSM: pin estricto, credenciales de bóveda y consultas sin mutaciones."""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import re
import shlex
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
MAX_OUTPUT = 16_384
SUPPORTED_KEYS = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
}
STORAGE_VALUES = {
    "SYNOLOGY_STORAGE_VOLUME": "/volume2",
    "SYNOLOGY_PRODUCCIONES_PATH": "/volume2/producciones",
    "SYNOLOGY_BACKUPS_PATH": "/volume2/producciones/_backups",
    "SYNOLOGY_NFS_EXPORT": "192.168.0.8:/volume2/producciones",
}


class AuditError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def pending(value):
    # Criterio compartido con las reservas declaradas en scripts/ci/check_env.py.
    return not value or bool(
        re.search(
            r"(?:^|[^a-z0-9])(?:CHANGE_ME|REPLACE_ME|PENDIENTE|GENERAR|COMPLETAR|CONFIGURAR|POR_DEFINIR|TODO)(?:$|[^a-z0-9])|^<.*>$",
            value,
            re.IGNORECASE,
        )
    )


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    username: str
    auth_method: str
    host_key_type: str
    host_key_data: bytes = field(repr=False)
    password: str = field(repr=False, default="")
    key_path: str = ""
    private_b64: str = field(repr=False, default="")
    client_public: str = field(repr=False, default="")
    client_fingerprint: str = field(repr=False, default="")
    timeout: int = 10

    def secrets(self):
        return [
            value for value in (self.password, self.private_b64) if value and not pending(value)
        ]


def read_vault(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)", line)
        if not match:
            raise AuditError("BOVEDA_INVALIDA", "Una línea de la bóveda no cumple CLAVE=valor.")
        key, value = match.groups()
        if not key.startswith("SYNOLOGY_"):
            continue
        if key in values:
            raise AuditError("BOVEDA_INVALIDA", "La bóveda contiene una clave Synology duplicada.")
        value = value.strip()
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise AuditError(
                    "BOVEDA_INVALIDA", "La bóveda contiene un valor con comillas incompletas."
                )
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def load_settings(path, auth_method="password", timeout=10):
    values = read_vault(path)
    required = [
        "SYNOLOGY_HOST",
        "SYNOLOGY_SSH_PORT",
        "SYNOLOGY_USER",
        "SYNOLOGY_SSH_HOST_PUBLIC_KEY",
        "SYNOLOGY_SSH_HOSTKEY_SHA256",
    ]
    required += (
        ["SYNOLOGY_PASSWORD"]
        if auth_method == "password"
        else [
            "SYNOLOGY_SSH_KEY_PATH",
            "SYNOLOGY_SSH_PRIVATE_KEY_B64",
            "SYNOLOGY_SSH_PUBLIC_KEY",
            "SYNOLOGY_SSH_CLIENT_FINGERPRINT",
        ]
    )
    if auth_method not in ("password", "key"):
        raise AuditError(
            "AUTENTICACION_INVALIDA", "Debe elegirse contraseña o clave explícitamente."
        )
    for key in required:
        if pending(values.get(key, "")):
            raise AuditError(
                "CREDENCIAL_PENDIENTE",
                f"Debe completar {key} localmente en la bóveda antes de conectar.",
            )
    if values["SYNOLOGY_HOST"] != "192.168.0.8" or values["SYNOLOGY_SSH_PORT"] != "2222":
        raise AuditError("DESTINO_INVALIDO", "Solo se permite auditar Synology 192.168.0.8:2222.")
    if any(values.get(key) != expected for key, expected in STORAGE_VALUES.items()):
        raise AuditError(
            "ALMACENAMIENTO_INVALIDO",
            "Las rutas deben coincidir con /volume2/producciones y su decisión respaldada.",
        )
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", values["SYNOLOGY_USER"]):
        raise AuditError("USUARIO_INVALIDO", "El usuario SSH no tiene un formato admitido.")
    parts = values["SYNOLOGY_SSH_HOST_PUBLIC_KEY"].split()
    if parts and parts[0] not in SUPPORTED_KEYS:
        if parts[0] not in ("192.168.0.8", "[192.168.0.8]:2222"):
            raise AuditError("PIN_INVALIDO", "La clave de host pertenece a otro destino.")
        parts = parts[1:]
    try:
        key_type, encoded = parts[:2]
        wire = base64.b64decode(encoded, validate=True)
        length = struct.unpack(">I", wire[:4])[0]
        if key_type not in SUPPORTED_KEYS or wire[4 : 4 + length].decode("ascii") != key_type:
            raise ValueError("Tipo de clave no coincidente")
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip(
            "="
        )
        if not hmac.compare_digest(fingerprint, values["SYNOLOGY_SSH_HOSTKEY_SHA256"]):
            raise ValueError("Huella no coincidente")
    except (ValueError, UnicodeError, struct.error):
        raise AuditError(
            "PIN_INVALIDO", "La clave de host y la huella respaldada no coinciden."
        ) from None
    password = values.get("SYNOLOGY_PASSWORD", "")
    if pending(password):
        password = ""
    if "\r" in password or "\n" in password or "\x00" in password:
        raise AuditError(
            "CREDENCIAL_INVALIDA",
            "La contraseña no puede contener saltos de línea ni caracteres nulos.",
        )
    return Settings(
        host="192.168.0.8",
        port=2222,
        username=values["SYNOLOGY_USER"],
        auth_method=auth_method,
        host_key_type=key_type,
        host_key_data=wire,
        password=password,
        key_path=values.get("SYNOLOGY_SSH_KEY_PATH", ""),
        private_b64=values.get("SYNOLOGY_SSH_PRIVATE_KEY_B64", ""),
        client_public=values.get("SYNOLOGY_SSH_PUBLIC_KEY", ""),
        client_fingerprint=values.get("SYNOLOGY_SSH_CLIENT_FINGERPRINT", ""),
        timeout=timeout,
    )


def redact(value, secrets):
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in sorted(set(secrets), key=len, reverse=True):
        if not secret:
            continue
        for variant in (
            secret,
            json.dumps(secret, ensure_ascii=False)[1:-1],
            base64.b64encode(secret.encode()).decode(),
        ):
            value = value.replace(variant, "[OCULTO]")
    return "".join(
        character
        for character in value
        if character in "\n\t" or ord(character) >= 32 and ord(character) != 127
    )


def connect_client(settings):
    # La dependencia se carga solo al conectar: las validaciones locales no requieren red ni SDK.
    import paramiko

    client = paramiko.SSHClient()
    host_key = paramiko.PKey.from_type_string(settings.host_key_type, settings.host_key_data)
    client.get_host_keys().add(f"[{settings.host}]:{settings.port}", host_key.get_name(), host_key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    pkey = None
    password = settings.password if settings.auth_method == "password" else None
    if settings.auth_method == "key":
        key_path = Path(settings.key_path)
        if not key_path.is_file() or key_path.read_bytes() != base64.b64decode(
            settings.private_b64, validate=True
        ):
            raise AuditError(
                "CLAVE_INVALIDA", "La clave local no coincide con su copia RAW respaldada."
            )
        pkey = paramiko.PKey.from_path(key_path)
        if settings.client_public.split()[:2] != [pkey.get_name(), pkey.get_base64()]:
            raise AuditError(
                "CLAVE_INVALIDA", "La clave de cliente no coincide con su pública respaldada."
            )
        client_fingerprint = "SHA256:" + base64.b64encode(
            hashlib.sha256(pkey.asbytes()).digest()
        ).decode().rstrip("=")
        if not hmac.compare_digest(client_fingerprint, settings.client_fingerprint):
            raise AuditError(
                "CLAVE_INVALIDA", "La huella de cliente no coincide con su respaldo canónico."
            )
    try:
        client.connect(
            hostname=settings.host,
            port=settings.port,
            username=settings.username,
            pkey=pkey,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=settings.timeout,
            banner_timeout=settings.timeout,
            auth_timeout=settings.timeout,
        )
        if client.get_transport().get_remote_server_key().asbytes() != settings.host_key_data:
            raise AuditError("PIN_INVALIDO", "El servidor no presentó la clave exacta respaldada.")
        return client
    except Exception:
        client.close()
        raise


class RemoteReader:
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings

    def run(self, command, sudo_mode="none", password_stdin=False):
        if sudo_mode == "noninteractive":
            command = "sudo -n -- /bin/sh -c " + shlex.quote(command)
        elif sudo_mode == "password":
            command = "sudo -S -p '' -- /bin/sh -c " + shlex.quote(command)
            password_stdin = True
        stdin, stdout, _stderr = self.client.exec_command(command, timeout=self.settings.timeout)
        channel = stdout.channel
        if password_stdin:
            if not self.settings.password or not command.startswith("sudo -S"):
                channel.close()
                raise AuditError("SUDO_NO_DISPONIBLE", "No hay contraseña respaldada para sudo.")
            stdin.write(self.settings.password + "\n")
            stdin.flush()
            stdin.channel.shutdown_write()
        output, error = bytearray(), bytearray()
        truncated = False
        deadline = time.monotonic() + self.settings.timeout
        try:
            while True:
                if channel.recv_ready():
                    data = channel.recv(4096)
                    truncated |= len(output) + len(data) > MAX_OUTPUT
                    output.extend(data[: max(0, MAX_OUTPUT - len(output))])
                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(4096)
                    truncated |= len(error) + len(data) > MAX_OUTPUT
                    error.extend(data[: max(0, MAX_OUTPUT - len(error))])
                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    code = channel.recv_exit_status()
                    break
                if time.monotonic() >= deadline:
                    code = 124
                    break
                time.sleep(0.01)
        finally:
            channel.close()
        return redact(
            {
                "codigo_salida": code,
                "salida": output.decode("utf-8", "replace").strip(),
                "error": error.decode("utf-8", "replace").strip(),
                "truncado": truncated,
            },
            self.settings.secrets(),
        )

    def prepare_sudo(self):
        probes = {"sin_password": self.run("sudo -n true")}
        if probes["sin_password"]["codigo_salida"] == 0:
            return "noninteractive", probes
        needs_password = probes["sin_password"]["codigo_salida"] == 1 and re.search(
            r"password.*required|se (?:necesita|requiere).*contrase|contrase.*necesaria",
            probes["sin_password"]["error"],
            re.IGNORECASE | re.DOTALL,
        )
        if self.settings.password and needs_password:
            probes["password_por_stdin"] = self.run("sudo -S -p '' -- true", password_stdin=True)
            if probes["password_por_stdin"]["codigo_salida"] == 0:
                return "password", probes
        return "none", probes


COMMANDS = [
    ("hostname", "hostname", False),
    ("identidad", "id", False),
    ("grupos", "id -Gn", False),
    (
        "version_dsm",
        "LC_ALL=C awk -F= '$1 ~ /^(majorversion|minorversion|productversion|buildnumber|smallfixnumber|buildphase|builddate)$/ {print}' /etc/VERSION",
        False,
    ),
    (
        "directorios_producciones",
        'for p in /volume2/producciones /volume2/producciones/_backups; do if [ -e "$p" ]; then stat -c \'ruta=%n modo=%a uid=%u gid=%g tipo=%F\' "$p"; else printf \'ruta=%s existe=no\\n\' "$p"; fi; done',
        False,
    ),
    (
        "nfs_versiones",
        "if [ -r /proc/fs/nfsd/versions ]; then cat /proc/fs/nfsd/versions; else printf 'Estado NFS no disponible o no legible\\n'; fi",
        True,
    ),
    (
        "nfs_threads",
        "if [ -r /proc/fs/nfsd/threads ]; then cat /proc/fs/nfsd/threads; else printf 'Estado NFS no disponible o no legible\\n'; fi",
        True,
    ),
    (
        "export_producciones",
        "if [ -r /etc/exports ]; then awk '$0 ~ /^\"?\\/volume2\\/producciones\"?([[:space:]]|$)/ {print}' /etc/exports; else printf 'Exportaciones no disponibles o no legibles\\n'; fi",
        True,
    ),
    (
        "share_smb_producciones",
        "if [ -r /etc/samba/smb.share.conf ]; then awk '/^[[:space:]]*\\[/{activo=($0 ~ /^[[:space:]]*\\[producciones\\][[:space:]]*$/)} activo && ($0 ~ /^[[:space:]]*\\[/ || $0 ~ /^[[:space:]]*(path|browseable|read only|valid users|write list|force user|force group|guest ok|smb encrypt)[[:space:]]*=/) {print}' /etc/samba/smb.share.conf; else printf 'Configuración SMB no disponible o no legible\\n'; fi",
        True,
    ),
    (
        "herramientas_dsm_disponibles",
        'for p in /usr/syno/sbin/synoshare /usr/syno/sbin/synonfs /usr/syno/bin/synosystemctl /usr/sbin/exportfs; do if [ -x "$p" ]; then printf \'%s\\n\' "$p"; fi; done',
        False,
    ),
]


def audit_vault(vault_path, auth_method="password", timeout=10, connector=None):
    result = {
        "fecha_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "modo": "Solo auditoría",
        "autenticado": False,
        "comandos": {},
    }
    settings = None
    client = None
    try:
        settings = load_settings(vault_path, auth_method, timeout)
        result.update(
            host=settings.host,
            puerto=settings.port,
            usuario=settings.username,
            autenticacion=auth_method,
        )
        client = (connector or connect_client)(settings)
        result.update(autenticado=True, pin_exacto_verificado=True)
        reader = RemoteReader(client, settings)
        sudo_mode, probes = reader.prepare_sudo()
        result.update(sudo=probes, modo_sudo=sudo_mode)
        for name, command, privileged in COMMANDS:
            result["comandos"][name] = reader.run(command, sudo_mode if privileged else "none")
        result["limitaciones"] = [
            "Solo se consultaron directorios y configuración del área producciones; no se listaron archivos de clientes.",
            "No se ejecutaron comandos synoshare/exportfs ni acciones de creación, exportación o montaje.",
        ]
    except AuditError as error:
        result["error"] = {"codigo": error.code, "mensaje": str(error)}
    except Exception as error:
        result["error"] = {
            "codigo": "ACCESO_O_LECTURA_FALLIDA",
            "tipo": type(error).__name__,
            "mensaje": "La auditoría se detuvo; no se probaron otras credenciales ni pins nuevos.",
        }
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                result.setdefault(
                    "error",
                    {
                        "codigo": "CIERRE_FALLIDO",
                        "mensaje": "La sesión no se cerró con normalidad; no se publican detalles sensibles.",
                    },
                )
    result["auditoria_completa"] = (
        "error" not in result
        and len(result["comandos"]) == len(COMMANDS)
        and all(
            item["codigo_salida"] == 0
            and not item["truncado"]
            and not re.search(r"no disponibles? o no legibles?", item["salida"], re.IGNORECASE)
            for item in result["comandos"].values()
        )
    )
    return redact(result, settings.secrets() if settings else [])


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Auditar Synology sin modificar shares, exportaciones ni montajes"
    )
    parser.add_argument("--vault-path", type=Path, default=PROJECT / ".env")
    parser.add_argument(
        "--output", type=Path, default=PROJECT / ".tools/evidence/synology-phase2-audit.json"
    )
    parser.add_argument("--auth-method", choices=("password", "key"), default="password")
    parser.add_argument("--timeout", type=int, choices=range(2, 31), default=10)
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Explicitar auditoría; es el comportamiento predeterminado y el único admitido",
    )
    args = parser.parse_args(argv)
    if args.output.resolve() == args.vault_path.resolve() or args.output.name.startswith(".env"):
        print(
            json.dumps(
                {
                    "error": {
                        "codigo": "SALIDA_INVALIDA",
                        "mensaje": "El informe no puede sobrescribir la bóveda ni su plantilla.",
                    }
                },
                ensure_ascii=False,
            )
        )
        return 2
    result = audit_vault(args.vault_path, args.auth_method, args.timeout)
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        print(
            json.dumps(
                {
                    "error": {
                        "codigo": "INFORME_NO_GUARDADO",
                        "mensaje": "No se pudo guardar el informe seguro en la ruta indicada.",
                    }
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if "error" in result else (0 if result["auditoria_completa"] else 2)


if __name__ == "__main__":
    sys.exit(main())
