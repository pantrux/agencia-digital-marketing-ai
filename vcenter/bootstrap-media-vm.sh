#!/usr/bin/env bash
# Preparación exclusiva de vm-media-01; ningún modo ejecuta comandos Git ni contenedores.
set -euo pipefail
umask 077

RUNNER_DIR=/opt/actions-runner
RUNNER_USER=jandrade
RUNNER_UID=1000
RUNNER_REPOSITORY=https://github.com/pantrux/agencia-digital-marketing-ai
RUNNER_NAME=vm-media-01
RUNNER_VERSION=2.338.2
RUNNER_SHA256=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40
DOCKER_VERSION='5:29.9.3-1~ubuntu.24.04~noble'
COMPOSE_VERSION='2.40.3-1~ubuntu.24.04~noble'
CONTAINERD_VERSION='2.4.2-3~ubuntu.24.04~noble'
BUILDX_VERSION='0.38.1-1~ubuntu.24.04~noble'
DOCKER_GPG_SUM256=1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570

fallar() { printf 'Error: %s\n' "$*" >&2; return 1; }

validar_destino() {
    [[ "$(uname -s)" == Linux ]] || fallar 'El destino debe ser Linux.'
    [[ "$(hostname -s)" == vm-media-01 ]] || fallar 'El hostname debe ser vm-media-01.'
    ip -o -4 addr show scope global | awk '{print $4}' | grep -Fxq '192.168.0.85/24' ||
        fallar 'La interfaz debe tener 192.168.0.85/24.'
    [[ "$(getent passwd "$RUNNER_UID" | cut -d: -f1)" == "$RUNNER_USER" ]] ||
        fallar 'El UID 1000 debe pertenecer a jandrade; no se modificará otro usuario.'
    [[ "$(getent passwd "$RUNNER_UID" | cut -d: -f6)" == /home/jandrade ]] ||
        fallar 'El directorio personal de jandrade no coincide.'
}

validar_sistema() {
    # El archivo es metadato local del SO del destino ya validado.
    # shellcheck source=/dev/null
    source /etc/os-release
    [[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || fallar 'Se requiere Ubuntu 24.04.'
    [[ "$(dpkg --print-architecture)" == amd64 ]] || fallar 'Se requiere arquitectura amd64.'
}

requiere_root() {
    [[ "$EUID" -eq 0 ]] || fallar 'Este modo exige root para instalar paquetes o el servicio.'
}

verificar_recibo_respaldo() {
    local recibo=${1:-}
    [[ -n "$recibo" && -f "$recibo" && ! -L "$recibo" ]] ||
        fallar 'Se requiere recibo de respaldo escrito por root tras persistir los tres archivos en la bóveda.'
    python3 - "$recibo" "$RUNNER_DIR" "$RUNNER_REPOSITORY" "$RUNNER_NAME" <<'PY'
import datetime
import hashlib
import json
from pathlib import Path
import re
import stat
import sys

try:
    receipt_path, directory = Path(sys.argv[1]), Path(sys.argv[2])
    meta = receipt_path.stat()
    if meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o600:
        raise ValueError("El recibo debe pertenecer a root con permisos 0600")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    required = {"schema_version", "repository_url", "runner_name", "vault_path", "env_example_synchronized", "vault_persisted", "created_at_utc", "files"}
    if set(receipt) != required or receipt["schema_version"] != 1:
        raise ValueError("Esquema del recibo inválido")
    if receipt["repository_url"] != sys.argv[3] or receipt["runner_name"] != sys.argv[4]:
        raise ValueError("El recibo pertenece a otro ejecutor")
    if receipt["vault_path"] != r"D:\Proyectos\Agencia Digital de Marketing AI\.env" or receipt["vault_persisted"] is not True or receipt["env_example_synchronized"] is not True:
        raise ValueError("Falta constancia de persistencia y sincronización de la bóveda")
    created = datetime.datetime.fromisoformat(receipt["created_at_utc"].replace("Z", "+00:00"))
    if created.tzinfo is None or created > datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5):
        raise ValueError("La fecha del recibo no es válida")
    names = {".runner", ".credentials", ".credentials_rsaparams"}
    if set(receipt["files"]) != names:
        raise ValueError("Deben respaldarse exactamente los tres archivos de identidad")
    for name, expected in receipt["files"].items():
        file = directory / name
        if file.is_symlink() or not file.is_file() or not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("Artefacto de identidad inválido")
        identity_meta = file.stat()
        if identity_meta.st_uid != 1000 or stat.S_IMODE(identity_meta.st_mode) != 0o600:
            raise ValueError("La identidad debe pertenecer a UID 1000 y tener permisos 0600")
        if hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError("El contenido actual no coincide con el respaldo confirmado")
    # El SDK puede escribir BOM; la huella anterior siempre usa los bytes RAW completos.
    runner = json.loads((directory / ".runner").read_text(encoding="utf-8-sig"))
    if runner.get("gitHubUrl", "").rstrip("/").lower() != sys.argv[3].lower() or runner.get("agentName") != sys.argv[4]:
        raise ValueError("La identidad del ejecutor no coincide")
except (OSError, ValueError, KeyError, TypeError):
    print("Error: recibo o archivos de respaldo inválidos; el servicio no se iniciará.", file=sys.stderr)
    sys.exit(1)
PY
}

emitir_fuente_docker() {
    cat <<'FUENTE'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
FUENTE
}

seleccionar_fuente_docker() {
    # Comparar todos los bytes evita adoptar opciones adicionales, firmas o suites diferentes.
    # La plantilla autorizada usa exactamente este formato Deb822 oficial.
    local esperada=$1 directorio=${2:-/etc/apt/sources.list.d} candidata
    [[ -f "$esperada" && ! -L "$esperada" ]] || { fallar 'Falta una fuente esperada regular.'; return 1; }
    for candidata in "$directorio/docker.sources" "$directorio/media-platform-docker.sources"; do
        if [[ -e "$candidata" || -L "$candidata" ]]; then
            [[ -f "$candidata" && ! -L "$candidata" ]] || { fallar 'La fuente existente no es un archivo regular equivalente.'; return 1; }
            cmp -s -- "$esperada" "$candidata" || { fallar 'La fuente existente no es exactamente equivalente; no se sobrescribe ni archiva.'; return 1; }
        fi
    done
    if [[ -f "$directorio/docker.sources" ]]; then
        printf '%s\n' "$directorio/docker.sources"
    else
        printf '%s\n' "$directorio/media-platform-docker.sources"
    fi
}

configurar_fuente_docker() {
    requiere_root
    local directorio=/etc/apt/sources.list.d respaldo=/var/backups/media-platform-apt
    local esperada seleccionada propia huella archivo_respaldo
    [[ -d "$directorio" && ! -L "$directorio" ]] || fallar 'El directorio APT no es el esperado.'
    printf '%s  %s\n' "$DOCKER_GPG_SUM256" /etc/apt/keyrings/docker.asc | sha256sum --check --status
    esperada=$(mktemp /var/tmp/media-docker-sources.XXXXXX)
    emitir_fuente_docker > "$esperada"
    if ! seleccionada=$(seleccionar_fuente_docker "$esperada" "$directorio"); then
        rm -f -- "$esperada"
        return 1
    fi
    propia="$directorio/media-platform-docker.sources"
    if [[ "$seleccionada" == "$directorio/docker.sources" ]]; then
        if [[ -f "$propia" ]]; then
            [[ "$(stat -c %u "$propia")" == 0 ]] || fallar 'La fuente propia no pertenece a root.'
            if [[ -e "$respaldo" || -L "$respaldo" ]]; then
                [[ -d "$respaldo" && ! -L "$respaldo" && "$(stat -c %u "$respaldo")" == 0 ]] ||
                    fallar 'El directorio de respaldo existente no pertenece al ámbito administrado.'
            fi
            install -d -m 0700 -o root -g root "$respaldo"
            huella=$(sha256sum "$esperada" | cut -d ' ' -f1)
            archivo_respaldo="$respaldo/media-platform-docker.sources.$huella"
            if [[ -e "$archivo_respaldo" || -L "$archivo_respaldo" ]]; then
                [[ -f "$archivo_respaldo" && ! -L "$archivo_respaldo" ]] || fallar 'El respaldo no es un archivo regular.'
                cmp -s -- "$propia" "$archivo_respaldo" || fallar 'El respaldo previo difiere; se conserva la fuente actual.'
                rm -f -- "$propia"
            else
                mv -- "$propia" "$archivo_respaldo"
            fi
            chmod 0600 "$archivo_respaldo"
            printf 'Fuente propia duplicada archivada; se conserva docker.sources sin modificar.\n'
        else
            printf 'Se reutiliza docker.sources, exactamente equivalente a la fuente declarada.\n'
        fi
    else
        install -m 0644 "$esperada" "$propia"
    fi
    rm -f -- "$esperada"
}

instalar_paquetes() {
    requiere_root
    local paquete estado archivo_llave
    # Se detiene ante paquetes ajenos en vez de desinstalar componentes de la plantilla.
    for paquete in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
        if estado=$(dpkg-query -W -f='${db:Status-Status}' "$paquete" 2>/dev/null); then
            [[ "$estado" != installed ]] || fallar "Paquete incompatible presente: $paquete; revisar la plantilla antes de continuar."
        fi
    done
    export DEBIAN_FRONTEND=noninteractive
    apt-get -o DPkg::Lock::Timeout=120 update
    apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends \
        ca-certificates curl gnupg python3 nfs-common rclone restic jq \
        libicu74 libssl3t64 libkrb5-3 zlib1g liblttng-ust1t64
    install -d -m 0755 /etc/apt/keyrings
    archivo_llave=$(mktemp /var/tmp/media-docker-key.XXXXXX)
    curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
        --connect-timeout 10 --max-time 120 https://download.docker.com/linux/ubuntu/gpg -o "$archivo_llave"
    printf '%s  %s\n' "$DOCKER_GPG_SUM256" "$archivo_llave" | sha256sum --check --status
    install -m 0644 "$archivo_llave" /etc/apt/keyrings/docker.asc
    rm -f -- "$archivo_llave"
    configurar_fuente_docker
    apt-get -o DPkg::Lock::Timeout=120 update
    # Versiones obtenidas del índice oficial noble/amd64 el 2026-11-05.
    # Compose se mantiene en 2.x conforme a la decisión arquitectónica congelada.
    apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends \
        "docker-ce=$DOCKER_VERSION" "docker-ce-cli=$DOCKER_VERSION" \
        "containerd.io=$CONTAINERD_VERSION" "docker-buildx-plugin=$BUILDX_VERSION" \
        "docker-compose-plugin=$COMPOSE_VERSION"
    usermod -aG docker "$RUNNER_USER"
    systemctl enable --now docker
    [[ "$(docker compose version --short)" == 2.* ]] || fallar 'La versión instalada de Compose no es 2.x.'
    docker compose version
    dpkg-query -W -f='${Package}=${Version}\n' docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin nfs-common rclone restic jq
}

instalar_runner() {
    requiere_root
    [[ ! -L "$RUNNER_DIR" ]] || fallar 'El directorio del ejecutor no puede ser un enlace.'
    if [[ -f "$RUNNER_DIR/.bootstrap-runner-sha256" ]]; then
        [[ "$(< "$RUNNER_DIR/.bootstrap-runner-sha256")" == "$RUNNER_SHA256" ]] ||
            fallar 'Existe otra versión del ejecutor; requiere actualización explícita.'
        [[ -x "$RUNNER_DIR/bin/Runner.Listener" && -x "$RUNNER_DIR/config.sh" ]] ||
            fallar 'La instalación anterior está incompleta.'
        printf 'El ejecutor fijado ya está instalado; se conserva su configuración.\n'
        return 0
    fi
    if [[ -d "$RUNNER_DIR" && -n "$(find "$RUNNER_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        fallar 'El directorio contiene otra instalación sin marca verificable; no se sobrescribirá.'
    fi
    local archivo url
    archivo=$(mktemp /var/tmp/media-actions-runner.XXXXXX.tar.gz)
    url="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
    # Versión y SHA256 publicados por la API oficial de actions/runner; no se usa latest al instalar.
    curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
        --connect-timeout 10 --max-time 600 "$url" -o "$archivo"
    printf '%s  %s\n' "$RUNNER_SHA256" "$archivo" | sha256sum --check --status
    install -d -m 0750 -o "$RUNNER_USER" -g "$RUNNER_USER" "$RUNNER_DIR"
    tar -xzf "$archivo" -C "$RUNNER_DIR" --no-same-owner
    rm -f -- "$archivo"
    printf '%s\n' "$RUNNER_SHA256" > "$RUNNER_DIR/.bootstrap-runner-sha256"
    chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR"
    runuser -u "$RUNNER_USER" -- "$RUNNER_DIR/bin/Runner.Listener" --version
    printf 'Ejecutor instalado sin registro ni servicio en ejecución.\n'
}

configurar_runner() {
    local archivo_token=${1:-} huella_boveda=${2:-} token
    [[ "$EUID" -eq "$RUNNER_UID" && "$(id -un)" == "$RUNNER_USER" ]] ||
        fallar 'El registro debe ejecutarse como jandrade, UID 1000, sin root.'
    [[ -x "$RUNNER_DIR/config.sh" && -f "$RUNNER_DIR/.bootstrap-runner-sha256" ]] ||
        fallar 'Primero debe instalarse el ejecutor verificado.'
    [[ "$(< "$RUNNER_DIR/.bootstrap-runner-sha256")" == "$RUNNER_SHA256" ]] ||
        fallar 'La distribución del ejecutor no coincide con la versión fijada.'
    if [[ -f "$RUNNER_DIR/.runner" ]]; then
        python3 - "$RUNNER_DIR/.runner" "$RUNNER_REPOSITORY" "$RUNNER_NAME" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
if data.get("gitHubUrl", "").rstrip("/").lower() != sys.argv[2].lower() or data.get("agentName") != sys.argv[3]:
    sys.exit("Error: existe un registro de otro ejecutor; no se reemplazará.")
PY
        [[ -f "$RUNNER_DIR/.credentials" && -f "$RUNNER_DIR/.credentials_rsaparams" ]] ||
            fallar 'El registro existente está incompleto; no se regenerarán credenciales automáticamente.'
        printf 'El registro ya existe; respalde su identidad antes de iniciar el servicio.\n'
        return 0
    fi
    [[ -n "$archivo_token" && -f "$archivo_token" && ! -L "$archivo_token" ]] ||
        fallar 'Se requiere un archivo 0600 con el token previamente persistido en la bóveda.'
    [[ "$(stat -c %u "$archivo_token")" == "$RUNNER_UID" && "$(stat -c %a "$archivo_token")" == 600 ]] ||
        fallar 'El archivo del token debe pertenecer al UID 1000 y tener permisos 0600.'
    [[ "$huella_boveda" =~ ^[a-f0-9]{64}$ ]] || fallar 'Falta SHA256 del token respaldado en la bóveda.'
    printf '%s  %s\n' "$huella_boveda" "$archivo_token" | sha256sum --check --status
    token=$(< "$archivo_token")
    [[ "$token" =~ ^[A-Za-z0-9_-]{20,}$ ]] || fallar 'El token no tiene formato válido.'
    cd -- "$RUNNER_DIR"
    # Prefijo oficial comprobado en CommandSettings.cs de actions/runner v2.338.2.
    # El secreto se transmite por entorno y nunca como argumento visible en el listado de procesos.
    ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh --unattended --url "$RUNNER_REPOSITORY" \
        --name "$RUNNER_NAME" --labels media-platform,homelab,vm-media-01 --work _work --disableupdate
    unset token
    chmod 0600 .runner .credentials .credentials_rsaparams
    sha256sum .runner .credentials .credentials_rsaparams
    printf 'Registro completado; NO se instaló ni inició el servicio. Respalde los tres archivos en .env y sincronice .env.example.\n'
}

iniciar_runner() {
    requiere_root
    verificar_recibo_respaldo "${1:-}"
    local servicio esperado pid
    esperado=actions.runner.pantrux-agencia-digital-marketing-ai.vm-media-01.service
    cd -- "$RUNNER_DIR"
    if [[ ! -f .service ]]; then
        ./svc.sh install "$RUNNER_USER"
    fi
    servicio=$(< .service)
    [[ "$servicio" == "$esperado" ]] || fallar 'La unidad de servicio no coincide con el ejecutor previsto.'
    [[ "$(systemctl show "$servicio" --property User --value)" == "$RUNNER_USER" ]] ||
        fallar 'La unidad no ejecuta como jandrade; no se iniciará.'
    ./svc.sh start
    systemctl is-active --quiet "$servicio"
    pid=$(systemctl show "$servicio" --property MainPID --value)
    [[ "$pid" =~ ^[1-9][0-9]*$ && "$(ps -o uid= -p "$pid" | tr -d ' ')" == "$RUNNER_UID" ]] ||
        fallar 'El proceso no ejecuta con UID 1000.'
    printf 'Ejecutor activo con UID 1000: %s\n' "$servicio"
}

main() {
    local modo=${1:-}
    case "$modo" in
        packages|runner-install|runner-configure|runner-start) ;;
        *) fallar 'Modo requerido: packages, runner-install, runner-configure o runner-start.'; return 1 ;;
    esac
    shift
    local archivo_token='' huella_token='' recibo=''
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --token-file) [[ $# -ge 2 ]] || { fallar 'Falta archivo del token.'; return 1; }; archivo_token=$2; shift 2 ;;
            --token-vault-sha256) [[ $# -ge 2 ]] || { fallar 'Falta huella del token respaldado.'; return 1; }; huella_token=$2; shift 2 ;;
            --vault-backup-receipt) [[ $# -ge 2 ]] || { fallar 'Falta recibo de respaldo.'; return 1; }; recibo=$2; shift 2 ;;
            *) fallar "Argumento no reconocido: $1"; return 1 ;;
        esac
    done
    validar_destino
    validar_sistema
    case "$modo" in
        packages) instalar_paquetes ;;
        runner-install) instalar_runner ;;
        runner-configure) configurar_runner "$archivo_token" "$huella_token" ;;
        runner-start) iniciar_runner "$recibo" ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
