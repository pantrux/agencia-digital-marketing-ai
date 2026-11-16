#!/usr/bin/env bash
# Restaura un volcado pg_dumpall acreditado en una fixture PostgreSQL nueva.
set +x
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C

fallar() {
    printf '%s\n' "$1" >&2
    exit 1
}

[[ $# -eq 3 ]] || fallar 'Uso: verify-postgres-restore.sh CID_COMPLETO VOLCADO_SQL_ZST SHA256'
cid=$1
volcado=$2
sha256=$3
[[ $cid =~ ^[a-f0-9]{64}$ && $sha256 =~ ^[a-f0-9]{64}$ ]] || fallar 'Se requiere CID y SHA256 completos.'
[[ $volcado =~ ^/var/lib/media-platform(/[A-Za-z0-9][A-Za-z0-9._-]*)+\.sql\.zst$ ]] ||
    fallar 'El volcado debe estar bajo /var/lib/media-platform.'
[[ $(uname -s) == Linux && $EUID -eq 0 && $(id -g) -eq 0 && $(hostname) == vm-media-01 ]] ||
    fallar 'La restauración requiere Root en vm-media-01.'
if ! ip -o -4 address show scope global | awk '$4 ~ /^192\.168\.0\.85\// { presente = 1 } END { exit !presente }'; then
    fallar 'La VM no tiene la dirección esperada.'
fi
[[ -x /usr/bin/docker && -x /usr/bin/zstd ]] || fallar 'Falta Docker o /usr/bin/zstd.'
padre=${volcado%/*}
[[ -f $volcado && ! -L $volcado && -s $volcado && $(realpath -e -- "$volcado") == "$volcado" &&
    $(stat -c '%u:%g:%a' -- "$padre") == 0:0:700 ]] || fallar 'El volcado requiere staging Root privado sin enlaces.'

# Retener el nodo verificado durante el hash, la prueba Zstd y la restauración.
exec {descriptor}< "$volcado"
referencia=/proc/$$/fd/$descriptor
formato_archivo='%d:%i:%s:%u:%g:%a:%h:%y:%z'
identidad_archivo=$(stat -Lc "$formato_archivo" -- "$referencia")
archivo_estable() {
    [[ $(stat -Lc '%u:%g:%a:%h' -- "$referencia") == 0:0:600:1 &&
        $(stat -Lc "$formato_archivo" -- "$referencia") == "$identidad_archivo" &&
        $(stat -c "$formato_archivo" -- "$volcado") == "$identidad_archivo" ]]
}
archivo_estable || fallar 'El archivo debe pertenecer a Root y tener modo 0600 y un único enlace.'
hash=$(sha256sum -- "$referencia")
[[ ${hash%% *} == "$sha256" ]] || fallar 'SHA256 del volcado distinto del respaldado.'
/usr/bin/zstd -q -t < "$referencia" >/dev/null 2>&1 || fallar 'El volcado Zstandard no es íntegro.'

config_docker=$padre/.docker-config
[[ ! -e $config_docker && ! -L $config_docker ]] || fallar 'La ruta de configuración aislada de Docker está ocupada.'
docker_local=(/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C /usr/bin/docker
    --host unix:///var/run/docker.sock --config "$config_docker")
plantilla=$(cat <<'FORMATO_INSPECCION'
{{if and
  (eq (index .Config.Labels "agency.owner") "agencia-digital-marketing-ai:postgres-restore:v1")
  (eq .Config.User "70:70") (eq .State.Status "running")
  (eq .HostConfig.NetworkMode "none") (not .HostConfig.Privileged)
  (not .HostConfig.PortBindings) (not .HostConfig.PublishAllPorts)
}}{{.Id}} {{.State.Pid}} {{.State.StartedAt}}{{range $nombre, $red := .NetworkSettings.Networks}}{{if ne $nombre "none"}} red{{end}}{{end}}{{range .NetworkSettings.Ports}}{{if .}} puerto{{end}}{{end}}{{end}}
FORMATO_INSPECCION
)

identidad_fixture() {
    local identidad
    identidad=$("${docker_local[@]}" inspect --type container --format "$plantilla" "$cid" 2>/dev/null) || return 1
    [[ $identidad =~ ^$cid\ [1-9][0-9]*\ [0-9T:.+-]+Z$ ]] || return 1
    printf '%s\n' "$identidad"
}

cliente_psql=$(cat <<'SHELL_CLIENTE'
set +x
set -eu
PGPASSWORD=""
IFS= read -r PGPASSWORD < /run/secrets/postgres_password || [ -n "$PGPASSWORD" ]
[ -n "$PGPASSWORD" ]
PGCONNECT_TIMEOUT=10
export PGPASSWORD PGCONNECT_TIMEOUT
exec /usr/local/bin/psql -X -q -w --set=ON_ERROR_STOP=1 --host=127.0.0.1 --port=5432 --username=agency_restore_admin "$@"
SHELL_CLIENTE
)
psql_fixture() {
    "${docker_local[@]}" exec -i --user 70:70 "$cid" /usr/bin/env -i \
        PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C /bin/sh -c "$cliente_psql" sh "$@"
}

inicio=$(identidad_fixture) || fallar 'El CID no corresponde a la fixture propia aislada en ejecución.'
# PostgreSQL exige iniciar en postgres con un superusuario distinto de los roles del origen.
preflight="SELECT current_database() = 'postgres'
    AND current_user = 'agency_restore_admin'
    AND (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user)
    AND (SELECT count(*) FROM pg_catalog.pg_database WHERE NOT datistemplate) = 1
    AND (SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname !~ '^pg_') = 1
    AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = 'public');"
estado=$(psql_fixture --dbname=postgres -At -c "$preflight" </dev/null 2>/dev/null) ||
    fallar 'No se pudo autenticar el superusuario de restauración en postgres.'
[[ $estado == t && $(identidad_fixture) == "$inicio" ]] || fallar 'La fixture no está recién inicializada o cambió.'
archivo_estable || fallar 'El volcado cambió antes de restaurar.'

if ! /usr/bin/zstd -q -d -c < "$referencia" 2>/dev/null | psql_fixture --dbname=postgres >/dev/null 2>&1; then
    fallar 'Falló la restauración. Conservar la fixture parcial para diagnóstico.'
fi

consulta="SELECT (SELECT version_num FROM public.alembic_version),
    (SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public'),
    (SELECT count(*) FROM public.person WHERE role = 'admin');"
resultado=$(psql_fixture --dbname=zoudb -At -F '|' -c "$consulta" </dev/null 2>/dev/null) ||
    fallar 'No se pudo comprobar la base restaurada.'
[[ $resultado == 'e5b8d1f4a920|81|1' && $(identidad_fixture) == "$inicio" ]] ||
    fallar 'La migración, tablas, administradores o identidad de la fixture no coinciden.'
archivo_estable || fallar 'El volcado cambió durante la restauración.'

printf '{"cid":"%s","sha256":"%s","head":"e5b8d1f4a920","public_tables":81,"admin_count":1}\n' "$cid" "$sha256"
