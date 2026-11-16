#!/usr/bin/env bash
# Prepara las skills y valida el patch mediante la CLI del gateway elegido.
set -euo pipefail
umask 077

usage() {
  cat <<'USO'
Uso:
  bash setup.sh --config RUTA --state-dir RUTA --runtime-root RUTA \
    --workspace-root RUTA --review-root RUTA --local-root RUTA --openclaw RUTA \
    [--apply --expect-config-sha256 SHA256 [--live]]

Sin --apply comprueba el plan. --live habilita aplicar al estado estándar.
Las siete rutas deben ser absolutas. El runtime debe incluir el plugin y catálogo.
USO
}

die() {
  printf '%s\n' "$1" >&2
  exit 1
}

config=
state_dir=
runtime_root=
workspace_root=
review_root=
local_root=
openclaw=
expect=
apply=0
live=0

while (($# > 0)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --apply) apply=1; shift; continue ;;
    --live) live=1; shift; continue ;;
    --config|--state-dir|--runtime-root|--workspace-root|--review-root|--local-root|--openclaw|--expect-config-sha256)
      (($# >= 2)) || die "Falta el valor de $1."
      case "$1" in
        --config) config=$2 ;;
        --state-dir) state_dir=$2 ;;
        --runtime-root) runtime_root=$2 ;;
        --workspace-root) workspace_root=$2 ;;
        --review-root) review_root=$2 ;;
        --local-root) local_root=$2 ;;
        --openclaw) openclaw=$2 ;;
        --expect-config-sha256) expect=$2 ;;
      esac
      shift 2
      ;;
    *) die "Opción desconocida: $1." ;;
  esac
done

for route in "$config" "$state_dir" "$runtime_root" "$workspace_root" "$review_root" "$local_root" "$openclaw"; do
  [[ "$route" == /* && "$route" != / && "$route" != */ && "$route" != *//* && "$route" != *$'\n'* && "$route" != *$'\r'* ]] ||
    die "Usa rutas absolutas canónicas."
  case "/${route#/}/" in
    */./*|*/../*) die "Usa rutas sin segmentos relativos." ;;
  esac
done

[[ -x "$openclaw" && -f "$openclaw" ]] || die "La CLI de OpenClaw no está disponible."
[[ "${config%/*}" == "$state_dir" ]] || die "El archivo debe estar directamente dentro de --state-dir."
if ((apply)); then
  [[ "$expect" =~ ^[0-9a-f]{64}$ ]] || die "La aplicación requiere la huella SHA-256 de la preimagen."
  if ((!live)); then
    [[ "$state_dir" != "$HOME/.openclaw" && "$state_dir" != "$HOME/.openclaw/"* ]] ||
      die "El estado estándar requiere --live."
    [[ "$workspace_root" == "$state_dir" || "$workspace_root" == "$state_dir/"* ]] ||
      die "El ensayo requiere los workspaces dentro de --state-dir."
  fi
elif ((live)); then
  die "--live requiere --apply."
fi

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
stage=(python3 -I -B "$source_root/stage_skills.py")
configure=(python3 -I -B "$source_root/patch_config.py")
digest() {
  "${stage[@]}" --config "$1" --digest
}

initial="$(digest "$config")"
if [[ -n "$expect" && "$initial" != "$expect" ]]; then
  die "La configuración difiere de la huella esperada."
fi

temporary="$(mktemp -d)"
cleanup() {
  rm -f -- "$temporary/preimage.json" "$temporary/patch.json" "$temporary/cli.log"
  rmdir -- "$temporary"
}
trap cleanup EXIT

preimage="$temporary/preimage.json"
patch="$temporary/patch.json"
cp -- "$config" "$preimage"
[[ "$(digest "$preimage")" == "$initial" ]] || die "La configuración cambió al preparar la copia."
prepare_args=(--config "$preimage" --runtime-root "$runtime_root" --workspace-root "$workspace_root")
skills_args=(--config "$preimage" --workspace-root "$workspace_root" --review-root "$review_root" --local-root "$local_root")
"${configure[@]}" "${prepare_args[@]}" --output "$patch"
"${stage[@]}" --config "$preimage" --check-patch "$patch"
"${stage[@]}" "${skills_args[@]}"

export OPENCLAW_CONFIG_PATH="$config"
export OPENCLAW_STATE_DIR="$state_dir"
if ! "$openclaw" config patch --file "$patch" --dry-run >"$temporary/cli.log" 2>&1; then
  die "OpenClaw rechazó el ensayo del patch."
fi
[[ "$(digest "$config")" == "$initial" ]] || die "La configuración cambió durante el ensayo."
if ((!apply)); then
  printf '%s\n' 'Ensayo del patch de OpenClaw aprobado.'
  exit 0
fi

"${stage[@]}" "${skills_args[@]}" --apply
[[ "$(digest "$config")" == "$initial" ]] || die "La configuración cambió antes de aplicar el patch."
actual_args=(--config "$config" --runtime-root "$runtime_root" --workspace-root "$workspace_root")
if ! "${configure[@]}" "${actual_args[@]}" --check-applied >"$temporary/cli.log" 2>&1; then
  [[ "$(digest "$config")" == "$initial" ]] || die "La configuración cambió antes de la escritura de OpenClaw."
  if ! "$openclaw" config patch --file "$patch" >"$temporary/cli.log" 2>&1; then
    die "OpenClaw no confirmó el patch. Revisa su estado antes de reintentar."
  fi
fi
"${configure[@]}" "${actual_args[@]}" --check-applied
"${stage[@]}" --config "$config" --compare "$preimage" "$patch"
