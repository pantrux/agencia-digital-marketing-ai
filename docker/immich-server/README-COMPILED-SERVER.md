# Imagen de ensayo del servidor compilado Immich 3.3.6

El build Linux exportó 1.391 archivos desde la revisión
`587f549f46f1cd1822a06e16dcccecef04ea4565`. El export mide 1.163.476 bytes y
tiene SHA256 `93a0ddc32aa515f7a44bc06f98c5cd5a98070872e3bb96731b256839f440e62b`.
`server/dist/main.js` mide 5.311 bytes y tiene SHA256
`7894328add69a1fe0a44f7d8c84cd527efeafb9bc6dc01b3b0280d236f010861`.
La compilación incluyó una corrección de tipo para la respuesta opcional
de Nodemailer. [Dockerfile de la revisión original](https://github.com/immich-app/immich/blob/587f549f46f1cd1822a06e16dcccecef04ea4565/server/Dockerfile).

## Contrato de la imagen

Usar la base local `ghcr.io/immich-app/immich-server` con manifiesto Linux
amd64 `sha256:c0a8e008ad9befe8af0e3a515e3b3661e98d38c37eadb6efee18a6d5098136c1`
y configuración `sha256:fb0c7c1a1b2a553c1c60e4bbb3d999d3d1483c9a223807e0856cd42d30b9cbea`.
El inspector debe identificar la misma configuración y las mismas capas
originales. La única capa adicional sustituye 1.378 archivos bajo
`/usr/src/app/server/dist` y siete bajo `dist` del `@immich/plugin-sdk` del
servidor. Antes de sustituirlos, la receta resuelve y coteja la ruta física
pnpm del SDK de plugins. Los seis archivos `@immich/sdk` se cotejan y
permanecen en el export de compilación. El servidor no declara ese SDK
como dependencia y el JS compilado no lo importa.

| Componente | Contrato del candidato |
| --- | --- |
| Proceso inicial | `tini -- /bin/bash -c` y `CMD ["start.sh"]` heredados. `start.sh` conserva la lectura de `DB_PASSWORD_FILE` y `REDIS_PASSWORD_FILE` |
| Proceso Node | Node `v24.22.1` de la base y `/usr/src/app/server/dist/main.js`, con workers API y de trabajos |
| Salud y escucha | `immich-healthcheck` heredado, TCP interno `2283`, UID/GID `1000:1000` aplicado por Compose |
| ABI nativa | Debian 13.7, glibc 2.41, libmimalloc 3.5.3+ds, Sharp 0.35.5 y bcrypt 6.0.0 de la imagen oficial |
| Audio y vídeo | Jellyfin FFmpeg 7.1.4-3 y sus bibliotecas, FFprobe, rutas de codecs y controladores de la base |
| ML | Cliente HTTP compilado de la misma revisión y servicio separado `immich-ml-auth` en la red interna del ensayo |
| Recursos | Web `/build/www`, plugins de `/build/plugins`, CLI, política ImageMagick y licencia heredados |

El escaneo histórico de la base registró 96 hallazgos HIGH/CRITICAL en
Debian y 42 en paquetes Node. Este build permite verificar la distribución
compilada con el runtime original. La actualización de esas dependencias,
el escaneo del candidato y su aceptación de producción requieren trabajo
posterior.

## Preparación y build

Desde el workspace, el siguiente comando reproduce el contexto sin red ni
Docker. El directorio de destino debe ser nuevo.

```powershell
python -I -B docker/immich-server/prepare_compiled_server.py `
  .tools/backup-source-build/immich-server-compile-v1/linux-dist-v4.tar.gz `
  $staging
```

El recibo `receipt.json` registra el tamaño y el SHA256 del contexto junto
con los hashes del export Linux y del instalador.
En un builder Linux existente, verificar el TAR, extraer a una ruta nueva,
e inspeccionar `base_tag` por manifiesto/configuración antes de usarlo.
`candidate_tag` debe ser una etiqueta local nueva. El build no necesita
transferir ni volver a descargar la base oficial cuando el builder ya la
tiene.

```bash
docker --context default buildx build --builder default \
  --platform linux/amd64 --network none --pull=false --provenance=false --sbom=false \
  --build-arg "BASE_IMAGE=$base_tag" --tag "$candidate_tag" "$context_dir"
docker --context default image inspect "$base_tag" "$candidate_tag"
```

El instalador valida los hashes, comprueba el arranque oficial, reemplaza
ambos árboles, fija sus directorios a `0755` y verifica byte a byte las
1.385 salidas y `node --check`. La corrección de permisos puede aplicarse
a una imagen ya construida mediante `Dockerfile.dist-permissions`. La
imagen reparada debe conservar la configuración y las capas previas.
Guardar los ID exactos y la evidencia del build antes de probar el
candidato. Si se exporta para escanear, se puede transferir el TAR por
stdout sin guardar otra copia en el builder.

## Pruebas del candidato

1. Cotejar configuración de la imagen candidata y DiffIDs con la base.
   Exigir un único DiffID adicional y los mismos valores de `Entrypoint`,
   `Cmd`, `WorkingDir`, `Env`, `Healthcheck`, `Volumes` y puertos.
2. En un proceso efímero sin red, UID 1000, rootfs de sólo lectura y
   `--pull=never`, verificar `node --version`, cargar `sharp` y `bcrypt`
   desde `/usr/src/app/server`, recorrer los 1.378 archivos de `dist` y
   validar `ffmpeg`/`ffprobe`. La fixture
   sintética `.tools/fixtures/whisper-probe.wav` debe poder leerse y
   decodificarse sin salida de red. `verify_compiled_runtime.cjs` ejecuta
   estas comprobaciones. Montar el script y la fixture como archivos de
   sólo lectura en `/run/verify.cjs` y `/run/speech.wav`, respectivamente.
   Ejecutar con `--workdir /usr/src/app/server --entrypoint node` y los
   argumentos `/run/verify.cjs /run/speech.wav`.
3. Sobre PostgreSQL y almacenamiento descartables propios, seguir la
   [receta privada](README-functional.md) con una sobreescritura local que
   cambie únicamente el `image` de `immich-server` al ID del candidato.
   Registrar salud, migraciones, sesión autenticada y búsqueda textual
   sintética mediante el proxy ML. Un reinicio debe conservar los datos
   del ensayo y las identidades de PostgreSQL, Valkey y el proxy.
4. Escanear el OCI candidato con Trivy vigente. Conservar el recibo de
   cobertura y cualquier hallazgo nuevo. Verificar producción por separado.
