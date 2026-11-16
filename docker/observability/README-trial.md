# Ensayo de métricas en vm-media-01

`compose.trial.yml` define un proyecto separado para probar node-exporter, cAdvisor y OpenTelemetry en `.85`. El Collector lee ambos exportadores cada 15 s y expone las series seleccionadas en `:8889/metrics`, dentro de un bridge interno sin puertos publicados. El ensayo usa 1 CPU y 384 MiB como suma de límites. No requiere secretos, modificaciones al Compose de aplicaciones ni cambios al NAS.

## Preparación y prueba

1. Confirma el host, Docker/Compose, cgroups v2 y driver `systemd`. Comprueba que el cgroup de un contenedor propio tiene ruta `/system.slice/docker-<CID>.scope`. Acredita un directorio de trabajo propio y ausencia de colisiones del proyecto `agency-observability-trial-v1`. Instala la configuración pública como archivo regular de lectura `0444` para el UID 65534, dentro del directorio privado. Respalda rutas, hashes e identidades antes de usarlos.
2. Coteja el manifiesto amd64 de cada imagen con el inventario `docker/stack-artifacts.json`, captura su config OCI real y completa los controles de imagen y configuración. El inventario actual fija tres candidatos, pero sus versiones en ejecución y su SCA siguen pendientes. Precarga sólo los digests exactos por una operación separada. El Compose no descarga imágenes.
3. Con un entorno limpio que tenga `COMPOSE_DISABLE_ENV_FILE=true` y sin `.env`, valida `docker compose -f compose.trial.yml config --quiet`. Valida también `otelcol-contrib validate --config=/etc/agency-otel.yaml` con la imagen exacta y sólo su bind de configuración de lectura. Registra ambos resultados por separado.
4. Crea los tres contenedores sin iniciarlos. Inspecciona y respalda sus CIDs y la red propia antes del arranque. Verifica imágenes exactas, UID/GID 65534, rootfs de lectura, capacidades descartadas, límites, bind mounts de lectura y ausencia de puertos publicados. Arranca los CIDs acreditados por separado y coteja versión real y continuidad.
5. Desde la VM, consulta con `curl --noproxy '*' --max-time 5` la IP privada del Collector en el bridge acreditado y `:8889/metrics`. Acota la respuesta y el plazo total a 60 s. Exige dos muestras con timestamps crecientes y `up=1` de cada job, memoria disponible de nodo mayor que cero y series de CPU y memoria de cAdvisor con `id` del CID exacto de un contenedor propio. Un HTTP 200 vacío o sólo con métricas internas del proceso no acredita la recolección. Conserva contadores, versión y correlación de CID en el recibo.
6. Detén los CIDs propios y comprueba salida y estado final. Si falla el ensayo, conserva los recibos y acota el diagnóstico a esos recursos. La variante prueba CPU/memoria de cgroups sin nombres ni etiquetas Docker. Si falla ese acceso en el host real, revisa primero la ruta y el permiso de lectura observados. No sustituyas esa comprobación por acceso privilegiado, bind de `/` o socket Docker.

Los binds del host se limitan a tres ficheros de procfs, un árbol CPU de sysfs y cgroups de lectura. cAdvisor comparte el namespace de cgroups para leer los del host. Esa visibilidad incluye identificadores y estadísticas de cgroups Docker, por lo que la prueba mantiene los tres servicios en una red dedicada sin clientes adicionales. El modo crudo de cAdvisor no requiere Docker Engine API ni recoge etiquetas de contenedor. [Opciones de cAdvisor](https://raw.githubusercontent.com/google/cadvisor/5bf5d43ac6f60d7ee36a13b4d5b4a78ad2d0abc7/docs/runtime_options.md), [filtro de cgroups crudos](https://raw.githubusercontent.com/google/cadvisor/5bf5d43ac6f60d7ee36a13b4d5b4a78ad2d0abc7/lib/container/raw/factory.go) y [colectores de node-exporter](https://raw.githubusercontent.com/prometheus/node_exporter/6044da783597cc3b57aef7580ddcdcff58a4ee99/README.md).

El bridge interno admite lectura desde el host en el modo habitual de Docker. En el ensayo el receptor scrapea los dos exportadores y el exportador del Collector vuelve a exponer las series al consumidor local. [Redes Docker](https://docs.docker.com/engine/network/port-publishing/) y [exportador Prometheus del Collector](https://raw.githubusercontent.com/open-telemetry/opentelemetry-collector-contrib/v0.164.0/exporter/prometheusexporter/README.md).

## Comandos para el operador

Ejecuta desde `.85` con `work` y `cli_home` propios ya acreditados. `work` contiene los dos YAML cotejados por hash. `cli_home` es un directorio privado nuevo y vacío. Define `cid` a partir del recibo de un contenedor propio en ejecución, con los 64 caracteres de su ID completo. El host ya acreditó el driver `systemd`. Verifica cgroups v2 y la ruta del contenedor antes de probar permisos efectivos. El PID sólo se consulta para cotejar esa ruta.

```bash
set -eu
docker_local() {
  /usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C HOME="$cli_home" \
    DOCKER_CONFIG="$cli_home" COMPOSE_DISABLE_ENV_FILE=true \
    /usr/bin/timeout --kill-after=2s 25s /usr/bin/docker \
    --host unix:///var/run/docker.sock "$@"
}
test "$(docker_local info --format '{{.ID}}|{{.CgroupDriver}}|{{.CgroupVersion}}')" = "$daemon_id|systemd|2"
test "$(/usr/bin/stat -f -c %T /sys/fs/cgroup)" = cgroup2fs
test "$(docker_local inspect --type container --format '{{.Id}}' "$cid")" = "$cid"
pid=$(docker_local inspect --type container --format '{{.State.Pid}}' "$cid")
test "$pid" -gt 0
cg="/system.slice/docker-$cid.scope"
/usr/bin/grep -Fxq "0::$cg" "/proc/$pid/cgroup"
test "$(docker_local inspect --type container --format '{{.State.Pid}}' "$cid")" = "$pid"
for file in /proc/stat /proc/meminfo /proc/loadavg \
  /sys/devices/system/cpu/online \
  "/sys/fs/cgroup$cg/cpu.stat" "/sys/fs/cgroup$cg/cpu.max" \
  "/sys/fs/cgroup$cg/memory.current" "/sys/fs/cgroup$cg/memory.stat" \
  "/sys/fs/cgroup$cg/memory.max" "/sys/fs/cgroup$cg/cgroup.procs"; do
  /usr/bin/setpriv --reuid=65534 --regid=65534 --clear-groups \
    --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs \
    /usr/bin/head -c 1 "$file" >/dev/null
done
docker_local compose --project-directory "$work" -f "$work/compose.trial.yml" config --quiet
```

La lectura verifica permisos efectivos del host. AppArmor, los binds y el namespace reales se verifican después con la prueba de métricas. Para validar la sintaxis con el binario exacto, coteja primero `otel_ref` y la entrada OCI del Collector. Usa un nombre nuevo para `validate_name` y guarda su CID e identidad antes de iniciarlo. La configuración se monta con el mismo UID y modo que en el ensayo.

```bash
validate_cid=$(docker_local create --name "$validate_name" --pull=never \
  --network=none --read-only --user 65534:65534 --cap-drop=ALL \
  --security-opt no-new-privileges=true --cpus=0.50 --memory=192m \
  --memory-swap=192m --pids-limit=64 \
  --mount "type=bind,src=$work/otel-collector.trial.yml,dst=/etc/agency-otel.yaml,readonly" \
  "$otel_ref" validate --config=/etc/agency-otel.yaml)
# El operador inspecciona y respalda la identidad exacta antes del siguiente comando.
docker_local start --attach "$validate_cid"
test "$(docker_local inspect --type container --format '{{.State.Status}}|{{.State.Pid}}|{{.State.ExitCode}}' "$validate_cid")" = 'exited|0|0'
```

Para crear el ensayo usa `docker_local compose --project-directory "$work" -f "$work/compose.trial.yml" create --no-build`. Comprueba la identidad de cada CID y el bridge antes de iniciar cada servicio. El ID de imagen que devuelve Docker se toma del recibo de `inspect`, con su correlación manifiesto/config, en vez de inferirlo por el tag o el tipo de digest.

Para la parada usa el CID completo y su prueba de creación ya respaldada. `expected_image_id` y `expected_created` son los valores de Docker del recibo propio. `service` es el nombre exacto del servicio. El límite externo acota la llamada del cliente. Si vence, reconcilia el estado por `inspect` del mismo CID antes de actuar de nuevo.

```bash
case "$cid" in *[!a-f0-9]*|'') exit 1 ;; esac
test "${#cid}" -eq 64
test "$(docker_local info --format '{{.ID}}')" = "$daemon_id"
identity() {
  docker_local inspect --type container --format \
    '{{.Id}}|{{.Image}}|{{.Created}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' "$cid"
}
expected="$cid|$expected_image_id|$expected_created|agency-observability-trial-v1|$service"
test "$(identity)" = "$expected"
state() {
  docker_local inspect --type container --format \
    '{{.State.Status}}|{{.State.Pid}}|{{.State.OOMKilled}}' "$cid"
}
case "$(state)" in
  'running|'[1-9]*'|false') docker_local stop --timeout 10 "$cid" >/dev/null ;;
  'exited|0|false') ;;
  *) exit 1 ;;
esac
test "$(identity)" = "$expected"
test "$(state)" = 'exited|0|false'
docker_local inspect --type container --format '{{.Id}}|{{.State.ExitCode}}' "$cid"
```

`docker stop` delega al daemon el cierre del contenedor propio. Puede enviar SIGKILL si se agota su plazo de gracia. Registra el código final y distingue ese resultado de una parada normal. El procedimiento no envía señales a PIDs del host ni elimina contenedores o redes.

## Entrega al NAS

La evidencia local no acredita Prometheus ni OTLP en `.8:9090/4318`. Grafana saludable sin datasource verificado y una consulta fallida al Prometheus configurado tampoco demuestran recepción. Antes de añadir una salida, el responsable debe identificar un receptor propio, respaldar su URL y transporte, y comprobar la recepción y lectura de una serie de ensayo desde el lado del NAS. Mantén esa operación separada de estos archivos. El ensayo no acredita OTLP, NAS, alertas, almacenamiento de series, producción ni cierre de Fase 4.
