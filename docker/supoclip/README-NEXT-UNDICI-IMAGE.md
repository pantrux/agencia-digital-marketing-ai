# Imagen SupoClip con Undici corregido

El ensayo Linux del 7 de noviembre pasó con Next 15.5.28 y los dos bundles de Undici 6.29.1. Respondieron el sitemap y el estático con 200, la carga anónima con 401 y las tres sondas de Fetch y despacho con 200. El contenedor terminó con PID 0 y código 0. El recibo tiene SHA `62c10af71bc0a6020ed26cecd42304eefb18da857cb435ea80022028730f4776`.

El commit Next 15.5.28 `195cd6a714086ee0e442cb551da376ad71f37035` contiene los mismos bytes de `taskfile.js` y `taskfile-ncc.js` que 15.5.29. También coinciden los 14 snapshots y sus SRI para la reconstrucción de Edge. La aplicación y su lock conservan Next 15.5.28 y `@next/swc-linux-x64-gnu` 15.5.28. El recibo de fuente tiene SHA `1770b379b3fe2a826e97a158a4d55ca7057fbab3daed8d3ad234daa7f43e7621`. [Commit Next 15.5.28](https://github.com/vercel/next.js/commit/195cd6a714086ee0e442cb551da376ad71f37035) · [Receta de bundles](README-NEXT-UNDICI.md)

Next 15.5.29 corrige tres avisos propios que siguen aplicando a la versión base. Upstream los clasifica como moderados. El escaneo debe conservar los avisos y cualquier discrepancia de severidad de sus fuentes. [CVE-2026-119346](https://github.com/vercel/next.js/security/advisories/GHSA-f3mh-rxg3-f5g2) · [CVE-2026-119347](https://github.com/vercel/next.js/security/advisories/GHSA-7qx7-33qx-rqx2) · [CVE-2026-119348](https://github.com/vercel/next.js/security/advisories/GHSA-pc58-hwjq-p385)

## Materializar y construir

Desde el proyecto, `python -I -B docker/supoclip/prepare_undici_image_context.py` calcula hashes sin escribir. Añadir `--prepare` publica exclusivamente en `.tools/backup-source-build/supoclip-next-undici-image-v1/context-v1`. `context.tar` contiene cinco archivos ordinarios y ordenados. Incluye los dos bundles aceptados en Linux, el Dockerfile derivado, el ensamblador original y la licencia MIT de Undici verificada contra su TAR npm.

Verificar el plan y todos los miembros del TAR antes de trasladarlo. En Linux usar una raíz nueva, privada y acreditada. Verificar el host y daemon exactos del build V2, el `result.json` original, identidad y propiedad de los tres contextos locales. El standalone de entrada es el de V2, con 3747 archivos, 114 enlaces y 164.622.626 bytes. Cotejar su manifiesto, sus preimágenes y su contenido antes de usarlo. Mantener los archivos de origen y los recibos previos intactos.

Extraer sólo los cinco miembros permitidos a `CONTEXT`, validar sus huellas y conservar los resultados del build en `OUTPUT`, otro directorio propio. Usar el `DOCKER_CONFIG` privado vacío del intento y el socket del daemon acreditado. El comando reproduce el build V2, con un solo cambio de árbol de aplicación.

```sh
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
  DOCKER_CONFIG="$DOCKER_CONFIG" \
  BUILDX_GIT_INFO=false BUILDX_GIT_LABELS=false BUILDX_GIT_CHECK_DIRTY=false \
  /usr/bin/docker --host unix:///var/run/docker.sock buildx build \
  --builder default --platform linux/amd64 --network none --pull=false --no-cache \
  --provenance=false --sbom=false --progress plain \
  --build-context sdk=/opt/media-platform/immich-js-seed-sdk-v1/inputs \
  --build-context standalone=/opt/media-platform/supoclip-frontend-stage-v1/frontend-runtime-build-v2/standalone \
  --build-context wolfi=oci-layout:///var/lib/media-platform/build/supoclip-ffmpeg-sdk-v3/base-oci@sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72 \
  --label agency.owner=agencia-digital-marketing-ai:supoclip-frontend-build:undici:v1 \
  --iidfile "$OUTPUT/image-id.txt" --metadata-file "$OUTPUT/metadata.json" \
  --file "$CONTEXT/Dockerfile.frontend" --load "$CONTEXT"
```

El stage temporal `projected` superpone las dos copias al standalone. El stage final comienza en el mismo Wolfi OCI y copia en un solo paso el árbol fusionado. Los layers de la imagen previa `aeee…` y del stage intermedio quedan fuera del manifiesto final. El ensamblador mantiene la clausura de 26 APK y Node 24.22.2. El árbol de aplicación proyectado tiene 3747 archivos y 114 enlaces con 164.692.755 bytes. La licencia vive fuera de ese árbol en `/usr/share/licenses/agency-undici-6.29.1/LICENSE`. [Build Docker por etapas](https://docs.docker.com/build/building/multi-stage/)

## Contrato de verificación y escaneo

1. Conservar salida, IID y metadata, incluso si falla un cotejo posterior. Respaldar los digests exactos de manifiesto y config antes de exportar o escanear. Verificar el archivo OCI físico, sus descriptores, hashes comprimidos, DiffIDs y config del runtime Linux amd64 UID 1000. Resolver el tipo de IID mediante metadata y contraste con el OCI, como en el operador BuildKit acreditado.
2. Comparar la capa de aplicación con `projected-standalone.json`, incluidos los 114 enlaces, las dos huellas nuevas y los 3745 archivos regulares sin cambios. Verificar la licencia física por tamaño y SHA. Buscar las dos huellas anteriores en todas las capas del OCI, también bajo otros nombres. La evidencia de las dos nuevas rutas debe incluir procedencia, versión Undici 6.29.1, hashes y comprobación de la ausencia de marcadores 6.21.0 en esos bundles. La atribución se liga a los bytes del OCI nuevo.
3. Correr los mismos modos `bundles` y `application` sobre la imagen derivada en red `none`, `HOSTNAME=127.0.0.1`, sin puertos publicados, UID 1000 y el mismo límite de recursos del ensayo. En este ciclo se omiten los dos montajes del overlay. Verificar el PID 0 y la identidad del CID al detenerlo.
4. Escanear el OCI nuevo con la base vigente y conservar JSON crudo, herramientas, hashes y cobertura exacta por ámbito. Contrastar 26 APK físicos y 78 paquetes npm con manifiesto físico contra los inventarios de referencia. Auditar 18 identidades Edge/Next ya atribuidas, con la nueva fila de Undici 6.29.1 y nueva evidencia del bundle de Primitives. Conservar los informes de identidad 6.21.0 del build anterior como historia y mantener la cobertura incompleta del Next compilado. El Undici integrado en Node tiene identidad y evidencia separadas. Aplicar el umbral vigente a los hallazgos actuales y conservar los moderados de Next. El plan aún no contiene un resultado de escaneo ni una aprobación.
