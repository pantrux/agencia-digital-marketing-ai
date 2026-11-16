#!/usr/bin/env bash
# Captura local de PostgreSQL apps. El destino es un directorio nuevo y privado.
set +x
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C

fallar() {
    printf '%s\n' "$1" >&2
    exit 1
}

if [[ $# -ne 2 ]]; then
    fallar 'Uso: capture-postgres-backup.sh CID_COMPLETO /var/lib/media-platform/DIRECTORIO_NUEVO'
fi

cid=$1
destino=$2
raiz=/var/lib/media-platform
[[ $cid =~ ^[a-f0-9]{64}$ ]] || fallar 'Se requiere el CID completo de PostgreSQL apps.'
[[ $destino =~ ^/var/lib/media-platform(/[A-Za-z0-9][A-Za-z0-9._-]*)+$ ]] ||
    fallar 'El destino debe ser una ruta de staging bajo /var/lib/media-platform.'
[[ $(uname -s) == Linux && $EUID -eq 0 && $(id -g) -eq 0 && $(hostname) == vm-media-01 ]] ||
    fallar 'La captura requiere Root en vm-media-01.'
if ! ip -o -4 address show scope global | awk '$4 ~ /^192\.168\.0\.85\// { presente = 1 } END { exit !presente }'; then
    fallar 'La VM no tiene la dirección esperada.'
fi
[[ -x /usr/bin/docker && -x /usr/bin/zstd ]] || fallar 'Falta Docker o /usr/bin/zstd.'

padre=${destino%/*}
[[ $(realpath -e -- "$padre") == "$padre" ]] || fallar 'El padre del destino debe existir sin enlaces.'
directorio=$padre
while :; do
    [[ -d $directorio && ! -L $directorio && $(stat -c '%u:%g:%a' -- "$directorio") == 0:0:700 ]] ||
        fallar 'La ruta de staging requiere directorios Root privados.'
    [[ $directorio != "$raiz" ]] || break
    directorio=${directorio%/*}
done
[[ ! -e $destino && ! -L $destino ]] || fallar 'El directorio de staging ya existe.'

docker_local=(/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C /usr/bin/docker
    --host unix:///var/run/docker.sock --config "$destino/.docker-config")
plantilla='{{if and (eq (index .Config.Labels "com.docker.compose.project") "agency-media") (eq (index .Config.Labels "com.docker.compose.service") "postgres-apps") (eq .Config.User "70:70") (eq .State.Status "running")}}{{.Id}} {{.State.Pid}} {{.State.StartedAt}}{{end}}'

identidad_postgres() {
    local identidad
    identidad=$("${docker_local[@]}" inspect --type container --format "$plantilla" "$cid" 2>/dev/null) || return 1
    [[ $identidad =~ ^$cid\ [1-9][0-9]*\ [0-9T:.+-]+Z$ ]] || return 1
    printf '%s\n' "$identidad"
}

volcar_postgres() {
    "${docker_local[@]}" exec -i --user 70:70 "$cid" /bin/sh -s <<'SHELL_CONTENEDOR'
set +x
set -eu
admin=${POSTGRES_USER:?}
exec /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C /bin/sh -s -- "$admin" <<'SHELL_VOLCADO'
set -eu
PGPASSWORD=''
IFS= read -r PGPASSWORD < /run/secrets/postgres_password || [ -n "$PGPASSWORD" ]
[ -n "$PGPASSWORD" ]
PGCONNECT_TIMEOUT=10
export PGPASSWORD PGCONNECT_TIMEOUT
exec /usr/local/bin/pg_dumpall --clean --if-exists -w --host=127.0.0.1 --port=5432 --username="$1"
SHELL_VOLCADO
SHELL_CONTENEDOR
}

inicio=$(identidad_postgres) || fallar 'El CID no corresponde al PostgreSQL apps propio en ejecución.'
mkdir -m 0700 -- "$destino" || fallar 'No se pudo crear el staging exclusivo.'
parcial=$destino/postgres.sql.zst.partial
salida=$destino/postgres.sql.zst
set -o noclobber
if ! volcar_postgres 2>/dev/null | /usr/bin/zstd -q -T1 --check -c > "$parcial" 2>/dev/null; then
    fallar "Falló la captura o compresión. Se conserva el staging: $destino"
fi
[[ -f $parcial && ! -L $parcial && -s $parcial && $(stat -c '%u:%g:%a:%h' -- "$parcial") == 0:0:600:1 ]] ||
    fallar "El archivo parcial no cumple la política de staging: $destino"

# Leer hasta el final comprueba el frame Zstandard y las marcas del volcado.
if ! /usr/bin/zstd -q -d -c -- "$parcial" 2>/dev/null | awk '
    NR <= 8 && $0 == "-- PostgreSQL database cluster dump" { cabecera = 1 }
    $0 == "-- PostgreSQL database cluster dump complete" { pie = NR }
    END { exit !(cabecera && pie && NR - pie <= 8) }
'; then
    fallar "El volcado comprimido está incompleto o dañado. Se conserva el staging: $destino"
fi
fin=$(identidad_postgres) || fallar "El contenedor cambió durante la captura. Se conserva el staging: $destino"
[[ $fin == "$inicio" ]] || fallar "PostgreSQL reinició durante la captura. Se conserva el staging: $destino"

bytes=$(stat -c '%s' -- "$parcial")
sha256=$(sha256sum -- "$parcial")
sha256=${sha256%% *}
nodo=$(stat -c '%d:%i' -- "$parcial")
[[ $bytes =~ ^[1-9][0-9]*$ && $sha256 =~ ^[a-f0-9]{64}$ ]] || fallar 'No se pudo acreditar el archivo parcial.'
mv -n -T -- "$parcial" "$salida" || fallar "No se pudo publicar la captura: $destino"
[[ ! -e $parcial && ! -L $parcial && -f $salida && ! -L $salida && $(stat -c '%d:%i' -- "$salida") == "$nodo" ]] ||
    fallar "Colisión al publicar la captura. Se conserva el staging: $destino"

printf '{"cid":"%s","service":"postgres-apps","path":"%s","sha256":"%s","bytes":%s}\n' \
    "$cid" "$salida" "$sha256" "$bytes"
