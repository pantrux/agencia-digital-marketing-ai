# Modelo local de SupoClip

El modelo Qwen2.5 1.5B Instruct Q4_K_M produjo dos clips en el ensayo
con llama.cpp b12248. El archivo verificado dura 19 segundos, contiene
H.264/AAC a 1080 × 1920 y muestra subtítulos incrustados.
[Recibo de la tarea](../../.tools/evidence/phase4-supoclip-local-model-generated-task-20261113-v1.json) ·
[Recibo de la descarga](../../.tools/evidence/phase4-supoclip-local-model-generated-download-20261113-v1.json).

## Artefactos e imagen

Usar los tres archivos de `local-model-inputs.json`. El archivo de pesos
de 1.117.320.736 bytes queda en almacenamiento persistente y entra al
contenedor en sólo lectura. Mantenerlo fuera del contexto Docker. El
directorio local `.tools/backup-source-build/supoclip-local-model-20261113-v1`
conserva los tres insumos verificados. Los pesos corresponden a la revisión
`91cad51170dc346986eccefdc2dd33a9da36ead9`.
[llama.cpp b12248](https://github.com/ggml-org/llama.cpp/releases/tag/b12248) ·
[Pesos Qwen fijados](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/blob/91cad51170dc346986eccefdc2dd33a9da36ead9/qwen2.5-1.5b-instruct-q4_k_m.gguf) ·
[Ubuntu libgomp1](https://packages.ubuntu.com/noble-updates/libgomp1).

```bash
python3 -I -B "$config_dir/prepare_local_model.py" "$artifact_dir" "$context_dir"
base_ref=agency-supoclip-model-base:noble-77eb101c
base_archive="$artifact_dir/ubuntu-noble-amd64-docker-load-v1.tar"
printf '%s  %s\n' 8ee830b075b0fffd838155665b2c8d590cb05108bd76f6f6885baee729d81597 \
  "$base_archive" | sha256sum -c -
docker image load --input "$base_archive"
case "$(docker image inspect "$base_ref" --format '{{.Id}} {{.Os}}/{{.Architecture}}')" in
  'sha256:77eb101c607d6e3d4f5ad0c8a02c2136c689b98aba1f359e6502ba517e5c57ec linux/amd64'|\
  'sha256:7da97f743f397971f35dd21cd2d19a7da070a150e0cfa3eda559e71891ec7533 linux/amd64') ;;
  *) exit 1 ;;
esac
test "$(docker image inspect "$base_ref" --format '{{range .RootFS.Layers}}{{.}}{{end}}')" = \
  'sha256:21210938f5a37a9887b2d0a20428917b923a636ca6efe038c3c7d9cd9f1f9b61'
docker --context default buildx build --builder default \
  --platform linux/amd64 --network none --pull=false --provenance=false --sbom=false \
  --build-arg "BASE_IMAGE=$base_ref" --tag "$model_tag" "$context_dir"
```

El TAR de Ubuntu conserva el manifiesto amd64, su config y la capa del
registro. Su recibo queda en la misma carpeta de artefactos. La receta
verifica el cierre ELF, extrae únicamente la biblioteca fijada del
paquete Ubuntu y ejecuta `llama-server --version`. Escanear el OCI
resultante y registrar su manifiesto y config antes de desplegar.
Reservar espacio persistente para los pesos, el motor, la base y el
staging. Usar un archivo físico protegido contra escritura por el
UID 1000. El servicio comprueba tamaño y SHA de los pesos al arrancar.

## Conectar el worker

Agregar `compose.local-model.yml` a los dos archivos Compose del
ensayo. El worker conserva su comando y sus redes de datos y Speaches.
El modelo comparte el namespace de red de ese worker y escucha
exclusivamente en `127.0.0.1:18963`. Sólo esos dos procesos de servicio
comparten el loopback, bajo el límite habitual del administrador del
host Docker. No hay puertos publicados para el modelo.

Las variables del overlay configuran LLM y rerank en el worker. Antes
del cambio, comprobar en la interfaz de administración que
`LLM`, `OLLAMA_BASE_URL` y `OLLAMA_API_KEY` carecen de una preferencia
por un valor de administrador distinto. Las preferencias de administrador
pueden prevalecer sobre el entorno. Conservarlas para una reversión
exacta si este ensayo exige cambiarlas. `local-only` es un valor fijo
para la librería cliente y carece de privilegios.

En una ventana sin tareas activas, guardar el CID original del worker y
el recibo de sus montajes. Usar el nombre y las variables del mismo
proyecto. El operador de siete contenedores de la prueba inicial valida
su conjunto fijo. Este overlay se administra mediante Compose.

```bash
export MEDIA_SUPOCLIP_MODEL_IMAGE="$model_tag"
export MEDIA_SUPOCLIP_MODEL_WEIGHTS="$weights_absolute_path"
compose=(docker compose --project-name "$trial_project" --profile migration
  -f "$config_dir/compose.functional.yml"
  -f "$config_dir/compose.integration.override.yml")
model_compose=("${compose[@]}" -f "$config_dir/compose.local-model.yml")
"${model_compose[@]}" config --quiet
"${model_compose[@]}" stop model
"${model_compose[@]}" rm -f model
"${model_compose[@]}" up -d --no-deps worker
"${model_compose[@]}" up -d --no-deps model
worker_cid=$("${model_compose[@]}" ps -q worker)
model_cid=$("${model_compose[@]}" ps -q model)
test "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$model_cid")" = "container:$worker_cid"
test "$(docker inspect --format '{{len .HostConfig.PortBindings}}' "$model_cid")" = 0
test "$(docker inspect --format '{{.State.Health.Status}}' "$model_cid")" = healthy
```

Repetir el último control de salud hasta que termine la carga del
modelo. Verificar `/health` desde el worker y una conexión rechazada
desde el contenedor API hacia la IP de datos del worker en el puerto
18963. Comprobar que API, PostgreSQL, Valkey, Next, Nginx y migrador
conservaron sus CID. Ejecutar la [sonda de generación](README-functional.md)
y verificar la descarga y sus subtítulos. Si Compose recrea el worker
en una actualización futura, recrear el modelo para que apunte al CID
nuevo y repetir las comprobaciones. Las políticas de reinicio están
limitadas a worker y modelo en este overlay. El reinicio completo del
host queda por verificar como parte de la activación operativa.

### Aceptar el par y comprobar un reinicio

Usar el `containers.json` del ensayo original, con la sustitución previa
de Next ya registrada. La sonda comprueba el CID e imagen de los otros
seis contenedores, el SO del modelo, el montaje de pesos, el namespace
real y el estado de salud. Prueba `/health` desde el worker y exige
`ECONNREFUSED` desde la API a la IP de datos del worker. También
comprueba el latido de la cola a través de la API. La sonda emite JSON
y no modifica contenedores, redes ni datos.

```bash
worker_cid=$("${model_compose[@]}" ps -q worker)
model_cid=$("${model_compose[@]}" ps -q model)
model_probe=(python3 -I -B "$config_dir/probe_local_model.py"
  --project "$trial_project" --worker "$worker_cid" --model "$model_cid"
  --weights "$weights_absolute_path" --trial-receipt "$trial_root/containers.json")
"${model_probe[@]}" > "$model_before"
```

Con la cola vacía, detener únicamente el modelo y reiniciar el worker por
CID. Luego arrancar el mismo CID del modelo. Este orden vuelve a unir el
modelo al namespace del worker después de su arranque. Esperar a que
vuelva a estar saludable y al siguiente latido de la cola antes de repetir
la sonda con el recibo anterior.

```bash
docker --context default stop --time 30 "$model_cid"
docker --context default restart --time 30 "$worker_cid"
docker --context default start "$model_cid"
for attempt in $(seq 1 60); do
  health=$(docker --context default inspect --format '{{.State.Health.Status}}' "$model_cid")
  test "$health" = healthy && break
  sleep 2
done
test "$health" = healthy
"${model_probe[@]}" --previous "$model_before" > "$model_after"
```

El segundo recibo exige los mismos ocho CID e imágenes, una hora de inicio
posterior únicamente para el par y un contador de reinicios automáticos
estable. Guardar ambos recibos y repetir la descarga de aceptación.

## Reversión

Cuando la cola esté vacía, detener y retirar el modelo con
`"${model_compose[@]}" stop model` y `"${model_compose[@]}" rm -f model`.
Restaurar con `"${compose[@]}" up -d --no-deps --force-recreate worker`.
Volver a cotejar el worker con el nuevo CID y los recibos del ensayo.
Restaurar los ajustes de administrador previos si se modificaron.
Los pesos y los datos de PostgreSQL, Valkey y medios persisten.
