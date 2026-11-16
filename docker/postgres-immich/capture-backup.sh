#!/usr/bin/env bash
# Variante del volcado de aplicaciones para la instancia dedicada de Immich.
set +x
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C

fallar() {
    printf '%s\n' "$1" >&2
    exit 1
}

[[ $# -eq 3 || $# -eq 4 ]] || fallar 'Uso: capture-backup.sh CID_COMPLETO PGDATA_HOST STAGING_NUEVO [production]'
cid=$1
datos=$2
destino=$3
alcance=${4:-acceptance}
imagen=sha256:a00b7ac5ed30069abde2702a2b2b3d8321ea0f22d2ba64cc9f836c86b4c47601
case $alcance in
    acceptance)
        proyecto=agency-media-immich-db
        propietario=agencia-digital-marketing-ai:postgres-immich-backup:v1
        ;;
    production)
        proyecto=agency-media-immich-lan
        imagen=sha256:4e2f960dcbc4c5fecf8c8b8a07231700c2f8920350539220ef6e8c6053c78dae
        propietario=agencia-digital-marketing-ai:postgres-immich-lan:v1
        [[ $datos == /var/lib/media-platform/immich-lan-v1/postgres ]] || fallar 'PGDATA de producción inesperado.'
        ;;
    *) fallar 'Alcance de captura desconocido.' ;;
esac
raiz=/var/lib/media-platform
limite=2147483648
[[ $cid =~ ^[a-f0-9]{64}$ ]] || fallar 'Se requiere el CID completo de PostgreSQL Immich.'
for ruta in "$datos" "$destino"; do
    [[ $ruta =~ ^/var/lib/media-platform(/[A-Za-z0-9][A-Za-z0-9._-]*)+$ ]] ||
        fallar 'Las dos rutas deben pertenecer a /var/lib/media-platform.'
done
[[ $(uname -s) == Linux && $EUID -eq 0 && $(id -g) -eq 0 && $(hostname) == vm-media-01 ]] ||
    fallar 'La captura requiere Root en vm-media-01.'
if ! ip -o -4 address show scope global | awk '$4 ~ /^192\.168\.0\.85\// { presente = 1 } END { exit !presente }'; then
    fallar 'La VM no tiene la dirección esperada.'
fi
[[ -x /usr/bin/docker && -x /usr/bin/zstd && -x /usr/bin/timeout ]] ||
    fallar 'Falta Docker, zstd o timeout.'
[[ $(realpath -e -- "$datos") == "$datos" && -d $datos && $(stat -c '%u:%g:%a' -- "$datos") == 70:70:700 ]] ||
    fallar 'PGDATA debe ser el directorio local propio 70:70/0700.'

padre=${destino%/*}
[[ $(realpath -e -- "$padre") == "$padre" ]] || fallar 'El padre de staging debe existir sin enlaces.'
while :; do
    [[ -d $padre && ! -L $padre && $(stat -c '%u:%g:%a' -- "$padre") == 0:0:700 ]] ||
        fallar 'El staging requiere ancestros Root privados.'
    [[ $padre != "$raiz" ]] || break
    padre=${padre%/*}
done
[[ ! -e $destino && ! -L $destino ]] || fallar 'El staging ya existe.'

docker_local=(/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C /usr/bin/docker
    --host unix:///var/run/docker.sock --config "$destino/.docker-config")
printf -v plantilla '{{if and (eq (index .Config.Labels "com.docker.compose.project") "%s") (eq (index .Config.Labels "com.docker.compose.service") "postgres-immich") (eq (index .Config.Labels "agency.owner") "%s") (eq .Config.User "70:70") (eq .Image "%s") (eq .State.Status "running") (eq .State.Health.Status "healthy")}}{{.Id}} {{.State.Pid}} {{.State.StartedAt}} {{range .Mounts}}{{if and (eq .Destination "/var/lib/postgresql/data") (eq .Type "bind") .RW}}{{.Source}}{{end}}{{end}}{{end}}' "$proyecto" "$propietario" "$imagen"

identidad_postgres() {
    local identidad proceso bind
    identidad=$("${docker_local[@]}" inspect --type container --format "$plantilla" "$cid" 2>/dev/null) || return 1
    proceso=${identidad% *}
    bind=${identidad##* }
    [[ $proceso =~ ^$cid\ [1-9][0-9]*\ [0-9T:.+-]+Z$ && $bind == "$datos" ]] || return 1
    printf '%s\n' "$identidad"
}

volcar_postgres() {
    /usr/bin/timeout -k 5s 310s "${docker_local[@]}" exec -i --user 70:70 "$cid" \
        /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C \
        PGPASSFILE=/run/secrets/postgres_pgpass PGCONNECT_TIMEOUT=10 /bin/sh -s -- "$alcance" <<'SHELL_CONTENEDOR'
set -eu
if [ "$1" = production ]; then
    extensions=$(/usr/local/bin/psql -X -q -A -t -w -h 127.0.0.1 -p 5432 -U postgres -d immich \
    --set=ON_ERROR_STOP=1 -c "SELECT
    current_setting('server_version_num') = '160016'
    AND current_setting('data_checksums') = 'on'
    AND current_setting('shared_preload_libraries') = 'vchord.so'
    AND count(*) = 4 FROM pg_catalog.pg_extension WHERE (extname, extversion) IN
    (('vector', '0.8.7'), ('vchord', '1.1.1'), ('cube', '1.5'), ('earthdistance', '1.2'))")
else
    extensions=$(/usr/local/bin/psql -X -q -A -t -w -h 127.0.0.1 -p 5432 -U postgres -d immich \
    --set=ON_ERROR_STOP=1 -c "SELECT current_setting('server_version_num') = '160015'
    AND current_setting('shared_preload_libraries') = 'vchord.so'
    AND count(*) = 4 FROM pg_extension WHERE (extname, extversion) IN
    (('vector', '0.8.5'), ('vchord', '1.1.1')) OR extname IN ('cube', 'earthdistance')")
fi
[ "$extensions" = t ]
exec /bin/busybox timeout -k 5 300 /usr/local/bin/pg_dumpall --clean --if-exists -w \
    --host=127.0.0.1 --port=5432 --username=postgres
SHELL_CONTENEDOR
}

inicio=$(identidad_postgres) || fallar 'El CID no acredita el PostgreSQL Immich propio y saludable.'
mkdir -m 0700 -- "$destino" || fallar 'No se pudo crear el staging exclusivo.'
parcial=$destino/postgres-immich.sql.zst.partial
salida=$destino/postgres-immich.sql.zst
set -o noclobber
if ! volcar_postgres 2>/dev/null | /usr/bin/zstd -q -T1 --check -c 2>/dev/null |
    /usr/bin/head -c "$((limite + 1))" > "$parcial"; then
    fallar "Falló la captura. Se conserva el staging: $destino"
fi
[[ -f $parcial && ! -L $parcial && $(stat -c '%u:%g:%a:%h' -- "$parcial") == 0:0:600:1 ]] ||
    fallar 'El parcial no cumple la política de staging.'
bytes=$(stat -c '%s' -- "$parcial")
[[ $bytes =~ ^[1-9][0-9]*$ && $bytes -le $limite ]] || fallar 'Captura vacía o superior a 2 GiB.'

if ! /usr/bin/zstd -q -d -c -- "$parcial" 2>/dev/null | awk '
    NR <= 8 && $0 == "-- PostgreSQL database cluster dump" { cabecera = 1 }
    $0 == "-- PostgreSQL database cluster dump complete" { pie = NR }
    END { exit !(cabecera && pie && NR - pie <= 8) }
'; then
    fallar 'El volcado comprimido está incompleto o dañado.'
fi
fin=$(identidad_postgres) || fallar 'El contenedor cambió durante la captura.'
[[ $fin == "$inicio" ]] || fallar 'PostgreSQL reinició durante la captura.'
sha256=$(sha256sum -- "$parcial")
sha256=${sha256%% *}
[[ $sha256 =~ ^[a-f0-9]{64}$ ]] || fallar 'No se pudo acreditar el SHA del parcial.'
nodo=$(stat -c '%d:%i' -- "$parcial")
sync -f -- "$parcial"
mv -n -T -- "$parcial" "$salida" || fallar 'No se pudo publicar la captura.'
[[ ! -e $parcial && ! -L $parcial && -f $salida && ! -L $salida && $(stat -c '%d:%i' -- "$salida") == "$nodo" ]] ||
    fallar 'Colisión al publicar la captura.'
sync -f -- "$destino"
printf '{"cid":"%s","service":"postgres-immich","path":"%s","sha256":"%s","bytes":%s}\n' \
    "$cid" "$salida" "$sha256" "$bytes"
