# Marcas de tiempo de SupoClip

El modelo local devolvió rangos como `00:00 - 00:03` en un campo que
espera una sola marca. El esquema de Pydantic ahora exige una marca
individual en `start_time` y `end_time`. El prompt muestra cómo tomar
los extremos de varias líneas contiguas. Pydantic AI usa sus reintentos
de salida estructurada. El selector conserva las comprobaciones de
duración y correspondencia con la transcripción. La versión de caché V7
hace que las tareas nuevas calculen de nuevo su análisis.

## Construir el worker

Preparar el contexto con el `backend-source.tar` V2 de la captura
privada. El preparador comprueba la preimagen exacta de `src/ai.py` y
genera un contexto nuevo con dos archivos.

```bash
python3 -I -B "$config_dir/prepare_transcript_timestamps.py" \
  "$source_archive" "$context_dir"
(cd "$context_dir" && sha256sum -c <<'SHA256'
e7fabe7e466f6e69b309cb3493a24f411c1cef22ff2eb40ed17fb76aa8da805c  ai.py
bc411101cff04fc522ceab719067e155e663068f90d618969f2dbc8ca92f3ade  Dockerfile
SHA256
)
```

`base_tag` apunta al backend local ya escaneado. Su manifiesto Linux amd64
es `sha256:ea3a3d46a7f576f5daed983542cc3eba0af87b595e26a201ead8d39c9a6b7f53`
y su configuración es
`sha256:fb8108cd7e49a0ffbb0c64cd89d2e3939cdc6ff8bbf7bc98cb058123982091a1`.
`candidate_tag` es un tag local nuevo. Ejecutar en Bash con `set -e`.

```bash
case "$(docker image inspect "$base_tag" --format '{{.Id}} {{.Os}}/{{.Architecture}}')" in
  'sha256:ea3a3d46a7f576f5daed983542cc3eba0af87b595e26a201ead8d39c9a6b7f53 linux/amd64'|\
  'sha256:fb8108cd7e49a0ffbb0c64cd89d2e3939cdc6ff8bbf7bc98cb058123982091a1 linux/amd64') ;;
  *) exit 1 ;;
esac
test "$(docker image inspect "$base_tag" --format '{{range .RootFS.Layers}}{{println .}}{{end}}')" = \
  "$(printf '%s\n' \
    sha256:683a833394ebad1990f7cdcbccd98c83ecc9c704d1935f49a45eca7d3bd002a1 \
    sha256:5f70bf18a086007016e948b04aed3b82103a36bea41755b6cddfaf10ace3c6ef)"
docker --context default buildx build --builder default \
  --platform linux/amd64 --network none --pull=false --provenance=false --sbom=false \
  --build-arg "BASE_IMAGE=$base_tag" --tag "$candidate_tag" "$context_dir"
candidate_id=$(docker image inspect "$candidate_tag" --format '{{.Id}}')
test "$(docker image inspect "$base_tag" --format '{{json .Config}}')" = \
  "$(docker image inspect "$candidate_id" --format '{{json .Config}}')"
docker run --rm --network none --entrypoint /usr/bin/python3.12 "$candidate_id" \
  -I -S -B -c 'import ast,hashlib,pathlib; source=pathlib.Path("/opt/supoclip/backend/src/ai.py").read_bytes(); ast.parse(source); assert hashlib.sha256(source).hexdigest()=="e7fabe7e466f6e69b309cb3493a24f411c1cef22ff2eb40ed17fb76aa8da805c"'
```

Registrar el digest y verificar una capa adicional sobre los `diff_ids`
de la base. Ejecutar la SCA del candidato antes de su promoción.

## Sustituir el worker del ensayo privado

Usar el mismo proyecto, los tres Compose de
[modelo local](README-LOCAL-MODEL.md#conectar-el-worker), sus variables y
el recibo existente `containers.json`. Ejecutar con la cola vacía.
Guardar el digest activo del worker y el recibo de aceptación del par.
El modelo se recrea para compartir el namespace del CID nuevo.

```bash
export MEDIA_SUPOCLIP_WORKER_TIMESTAMPS_IMAGE="$candidate_tag"
timestamps_compose=("${model_compose[@]}" -f "$config_dir/compose.transcript-timestamps.yml")
"${timestamps_compose[@]}" config --quiet
"${timestamps_compose[@]}" stop model
"${timestamps_compose[@]}" rm -f model
"${timestamps_compose[@]}" up --no-start --no-deps --force-recreate --no-build --pull never worker
worker_cid=$("${timestamps_compose[@]}" ps -aq worker)
worker_cid=$(docker inspect "$worker_cid" --format '{{.Id}}')
test "$(docker inspect "$worker_cid" --format '{{.Image}}')" = "$candidate_id"
docker start "$worker_cid"
"${timestamps_compose[@]}" up -d --no-deps --no-build --pull never model
model_cid=$("${timestamps_compose[@]}" ps -q model)
model_cid=$(docker inspect "$model_cid" --format '{{.Id}}')
```

Cuando el modelo esté saludable y la API reciba el latido de la cola,
ejecutar `probe_local_model.py` con los CID nuevos, el recibo original y
`--worker-image "$candidate_id"`. El probe verifica la imagen del worker
y los otros seis CID, incluido el de la API. Para revertir, repetir la
secuencia de reemplazo usando los tres Compose previos y verificar el
digest original antes de arrancar el worker. Repetir la sonda del par
sin `--worker-image`.

Ejecutar la [sonda HTTP con `--clip-output`](README-functional.md) con
una cuenta y una carga nuevas del fixture `synthetic-clips-speech-v2.mp4`
de SHA256 `b0ec158e3d73c41d9ba4c2cf070a8400382b5f7b50ab7fc0f6722c18eed40606`.
Guardar el recibo de una descarga autenticada sin fallback. Confirmar
en los logs del worker que la tarea usó `qwen-supoclip` y validó al menos
un segmento. Revisar los subtítulos incrustados en el clip resultante.
