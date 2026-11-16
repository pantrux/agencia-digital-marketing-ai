# Imágenes Linux amd64 de los runners n8n 2.46.6

El runner JavaScript arranca con Node 26.12.1 y la ABI 147. Su árbol de producción contiene `@n8n/task-runner` 2.46.5 y QuickJS corregido. El preparador verifica los 42.683 miembros y 1.571 enlaces del tar de entrada, normaliza los permisos para ejecutar como UID 1000 y usa la copia corregida de Flatted compilada en el mismo release del servidor. Se cotejan el JS, su mapa y el consumidor `parse-flatted.js` antes de la sustitución. El Dockerfile elimina npm y npx del sistema de archivos de la imagen. El launcher y el código JavaScript invocan Node directamente.

El runner Python usa CPython 3.13.18 Alpine 3.24. El Dockerfile reutiliza la construcción offline de `Dockerfile.runner-python` y copia únicamente su entorno virtual a la etapa final. La sonda importa la extensión nativa de `websockets` y verifica las cinco distribuciones de ejecución como UID 1000.

## Contextos y construcción

```powershell
python -B docker/n8n/prepare_runner_runtime_contexts.py --language javascript --output <contexto-js-nuevo>
python -B docker/n8n/prepare_runner_runtime_contexts.py --language python --output <contexto-python-nuevo>
```

El preparador entrega `SHA256SUMS` y `context.json` por lenguaje. Para un launcher Linux nuevo, añadir `--launcher <archivo> --launcher-sha256 <sha256>` a las dos invocaciones y conservar sus recibos de compilación, SCA y ejecución. El valor predeterminado reproduce el launcher del ensayo integrado.

Preparar las bases Linux amd64 por sus manifiestos fijados. Para una compilación sin registro, crear una copia del Dockerfile de cada contexto y sustituir la referencia `FROM` por el alias de un `--build-context <alias>=oci-layout://<layout-local>@sha256:<digest-fijado>`. En Python, conservar los dos nombres de etapa y usar el mismo alias para ambas. Registrar el SHA de la copia y verificar que las capas de la base sean el prefijo de las de la imagen final.

```sh
sha256sum -c SHA256SUMS
BUILDX_GIT_INFO=false BUILDX_GIT_LABELS=false BUILDX_GIT_CHECK_DIRTY=false \
  docker buildx build --builder default --platform linux/amd64 --network none \
  --pull=false --provenance=false --sbom=false --load --tag <tag-local> .
```

Iniciar cada contenedor con `--init` o `init: true`, el broker interno y las variables autorizadas del ensayo. El launcher escucha en `5680` y usa la configuración fijada de `/etc/n8n-task-runners.json`. Ejecutar las sondas de broker y cierre sobre las imágenes finales y conservar los resultados asociados a sus digests. Escanear las imágenes completas con Trivy, incluida la base, y comprobar los paquetes Go del launcher. Para Python, comprobar las cinco dependencias del entorno y el inventario del Python de la base. Para JavaScript, cotejar además el cierre npm del runner y sus binarios.

## Imagen Python sin pip

El escaneo de V1 encontró cinco avisos altos en los módulos vendorizados por pip 26.2.2. El entorno de ejecución está separado por `include-system-site-packages = false` y usa `urllib3` 2.8.0. El proceso Python se inicia mediante `python -I -B -X disable_remote_debug -m src.main`. El delta V2 retira pip, sus metadatos, los tres comandos y `ensurepip` con su rueda de arranque. Los hashes de la extensión de `websockets` y de la capa del entorno virtual siguen fijados.

```powershell
python -B docker/n8n/prepare_runner_python_runtime_v2.py --source-oci <OCI-V1-verificado> --output <contexto-python-v2-nuevo>
```

Construir con `--build-context runnerbase=oci-layout://<layout-local-V1>@sha256:f26b05501da64d8abbc2ce65c056b69a2f26c3c6c25461eb10491885206938a0` y con las demás opciones offline de arriba. El `context.json` registra las capas de V1. Cotejar el prefijo de capas, las pruebas de ambos intérpretes como UID 1000 y el escaneo del sistema de archivos de la imagen final. Los blobs de V1 se conservan para la trazabilidad de la base.
