#!/usr/bin/env bash
# Configura exclusivamente el cliente local .85 o .7; no conecta por SSH ni toca el NAS.
set -Eeuo pipefail
umask 077

readonly nfs_source='192.168.0.8:/volume2/producciones'
readonly nfs_target='/mnt/producciones'
readonly nfs_runtime_options='rw,vers=4.1,proto=tcp,hard,nosuid,nodev,retry=0'
nfs_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly nfs_directory
readonly nfs_helper="${nfs_directory}/nfs_client_config.py"

nfs_fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

nfs_usage() {
  printf '%s\n' \
    'Uso: configure-nfs-client.sh --target-ip 192.168.0.85|192.168.0.7 --expected-hostname NOMBRE [--check|--apply]' \
    'Por defecto solo valida el cliente, fstab y el montaje existente; no crea ni monta nada.' \
    'nfs-common y el helper nfs_client_config.py deben estar disponibles previamente.'
}

nfs_mount_present() {
  local nfs_mount_json nfs_findmnt_status
  if nfs_mount_json=$(findmnt --json --nocanonicalize --mountpoint "$nfs_target" --output SOURCE,TARGET,FSTYPE,OPTIONS); then
    python3 "$nfs_helper" mount <<<"$nfs_mount_json" || nfs_fail 'El montaje del destino no pertenece al recurso permitido.'
    return 0
  else
    nfs_findmnt_status=$?
    [[ "$nfs_findmnt_status" == 1 ]] || nfs_fail 'findmnt no pudo consultar el destino de manera fiable.'
    return 1
  fi
}

nfs_check_empty_directory() {
  [[ ! -L "$nfs_target" ]] || nfs_fail 'El destino no puede ser un enlace.'
  [[ "$(readlink -m -- "$nfs_target")" == "$nfs_target" ]] || nfs_fail 'El destino contiene un enlace inesperado.'
  if [[ -e "$nfs_target" ]]; then
    [[ -d "$nfs_target" ]] || nfs_fail 'El destino existente no es un directorio.'
    [[ -z "$(find "$nfs_target" -mindepth 1 -maxdepth 1 -print -quit)" ]] || nfs_fail 'El destino contiene archivos; no se ocultarán bajo un montaje.'
  fi
}

nfs_verify_candidate() {
  local nfs_candidate_path=$1
  # findmnt exige que el destino local exista incluso con nofail; no consulta el export NFS.
  if ! nfs_mount_present; then
    nfs_check_empty_directory
    mkdir -p -- "$nfs_target"
  fi
  findmnt --verify --tab-file "$nfs_candidate_path" >/dev/null 2>&1 || nfs_fail 'El candidato fstab no supera la validación local; no se monta ni publica.'
}

nfs_main() {
  local nfs_ip='' nfs_hostname='' nfs_mode='check' nfs_candidate='' nfs_backup=''
  local nfs_addresses nfs_changed=false nfs_was_mounted=false nfs_mounted_now=false nfs_program
  local nfs_original_hash nfs_current_hash nfs_original_mode nfs_backup_dir
  while (($#)); do
    case "$1" in
      --target-ip) [[ $# -ge 2 ]] || nfs_fail 'Falta la IP del cliente.'; nfs_ip=$2; shift 2 ;;
      --expected-hostname) [[ $# -ge 2 ]] || nfs_fail 'Falta el hostname auditado.'; nfs_hostname=$2; shift 2 ;;
      --check) nfs_mode='check'; shift ;;
      --apply) nfs_mode='apply'; shift ;;
      --help|-h) nfs_usage; return 0 ;;
      *) nfs_fail 'Parámetro desconocido; no se admiten fuentes, rutas ni comandos arbitrarios.' ;;
    esac
  done
  [[ "$nfs_ip" == '192.168.0.85' || "$nfs_ip" == '192.168.0.7' ]] || nfs_fail 'Solo se permiten los clientes .85 y .7.'
  [[ -n "$nfs_hostname" ]] || nfs_fail 'Se requiere el hostname real previamente auditado.'
  [[ "$(uname -s)" == Linux && "$(id -u)" == 0 ]] || nfs_fail 'Ejecutar como root únicamente en el cliente Linux autorizado.'
  for nfs_program in python3 ip hostname findmnt mount.nfs mount timeout flock find cmp readlink stat sha256sum; do
    command -v "$nfs_program" >/dev/null || nfs_fail "Falta el prerrequisito local: ${nfs_program}. No se instalan paquetes automáticamente."
  done
  [[ -f "$nfs_helper" && ! -L "$nfs_helper" ]] || nfs_fail 'Falta el helper regular de contratos junto al script.'
  [[ -f /etc/fstab && ! -L /etc/fstab && "$(stat -c '%u:%h' /etc/fstab)" == '0:1' ]] || nfs_fail 'fstab debe ser un archivo regular de root sin enlaces adicionales.'
  nfs_addresses=$(ip -j -4 address show scope global)
  python3 "$nfs_helper" identity --target-ip "$nfs_ip" --expected-hostname "$nfs_hostname" --actual-hostname "$(hostname -s)" <<<"$nfs_addresses"
  # Validar primero: nunca se adopta una entrada ajena aunque apunte al mismo NAS.
  python3 "$nfs_helper" render </etc/fstab >/dev/null
  if nfs_mount_present; then
    nfs_was_mounted=true
  else
    nfs_check_empty_directory
  fi
  if ! cmp -s /etc/fstab <(python3 "$nfs_helper" render </etc/fstab); then nfs_changed=true; fi
  if [[ "$nfs_mode" == check ]]; then
    printf '{"mode":"check","target_ip":"%s","fstab_change_required":%s,"share_mounted_and_verified":%s,"mutations":false}\n' "$nfs_ip" "$nfs_changed" "$nfs_was_mounted"
    return 0
  fi

  # El bloqueo es local y propio; no se retienen bloqueos de otros equipos.
  exec 9>/run/lock/agencia-producciones-nfs.lock
  flock -n 9 || nfs_fail 'Ya hay otra reconciliación propia en curso.'
  nfs_original_hash=$(sha256sum /etc/fstab)
  nfs_original_mode=$(stat -c '%a' /etc/fstab)
  nfs_candidate=$(mktemp /etc/.fstab.agencia-producciones.XXXXXX)
  trap '[[ -z ${nfs_candidate:-} || ! -f $nfs_candidate ]] || rm -- "$nfs_candidate"' EXIT
  python3 "$nfs_helper" render </etc/fstab >"$nfs_candidate"
  chmod --reference=/etc/fstab "$nfs_candidate"
  chown --reference=/etc/fstab "$nfs_candidate"
  nfs_verify_candidate "$nfs_candidate"
  if ! nfs_mount_present; then
    nfs_check_empty_directory
    # El montaje exacto acredita exportación y versión; no se usa mount -a ni se desmontan recursos.
    timeout --signal=TERM --kill-after=5s 35s mount -t nfs4 -o "$nfs_runtime_options" "$nfs_source" "$nfs_target"
    nfs_mounted_now=true
  fi
  nfs_mount_present || nfs_fail 'El montaje no acredita fuente, versión y acceso esperados.'
  nfs_current_hash=$(sha256sum /etc/fstab)
  [[ "$nfs_current_hash" == "$nfs_original_hash" ]] || nfs_fail 'fstab cambió durante la operación; se conserva la versión ajena sin publicar el candidato.'
  if ! cmp -s /etc/fstab "$nfs_candidate"; then
    for nfs_backup_dir in /var/lib/media-platform-storage /var/lib/media-platform-storage/backups; do
      [[ "$(readlink -m "$nfs_backup_dir")" == "$nfs_backup_dir" ]] || nfs_fail 'El directorio de respaldo contiene enlaces inesperados.'
      if [[ -e "$nfs_backup_dir" ]]; then
        [[ -d "$nfs_backup_dir" && "$(stat -c '%u:%a' "$nfs_backup_dir")" == '0:700' ]] || nfs_fail 'El respaldo existente no es un directorio privado de root; se conserva sin cambiar permisos.'
      else
        install -d -m 0700 "$nfs_backup_dir"
      fi
    done
    nfs_backup="/var/lib/media-platform-storage/backups/fstab-$(date -u +%Y%m%dT%H%M%SZ)-$$.bak"
    cp --preserve=mode,ownership,timestamps /etc/fstab "$nfs_backup"
    chmod 0600 "$nfs_backup"
    mv -f -- "$nfs_candidate" /etc/fstab
    nfs_candidate=''
    if [[ -d /run/systemd/system ]]; then
      if ! systemctl daemon-reload; then
        nfs_candidate=$(mktemp /etc/.fstab.agencia-producciones.XXXXXX)
        cp --preserve=ownership,timestamps "$nfs_backup" "$nfs_candidate"
        chmod "$nfs_original_mode" "$nfs_candidate"
        mv -f -- "$nfs_candidate" /etc/fstab
        nfs_candidate=''
        if ! systemctl daemon-reload; then
          printf 'No se pudo recargar systemd después de restaurar el archivo anterior.\n' >&2
        fi
        nfs_fail 'Se restauró fstab por fallo de recarga; el montaje exacto permanece disponible para revisión.'
      fi
    fi
    nfs_changed=true
  else
    nfs_changed=false
  fi
  if [[ -n "$nfs_candidate" ]]; then
    rm -- "$nfs_candidate"
    nfs_candidate=''
  fi
  printf '{"mode":"apply","target_ip":"%s","fstab_changed":%s,"mounted_this_run":%s,"share_mounted_and_verified":true,"backup":"%s"}\n' "$nfs_ip" "$nfs_changed" "$nfs_mounted_now" "$nfs_backup"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  nfs_main "$@"
fi
