# Remediación del candidato compilado Immich 3.3.6

El candidato conserva el pin de Immich `3.3.6`, revisión
`587f549f46f1cd1822a06e16dcccecef04ea4565`. El escaneo del OCI
`sha256:50ed464e8a0b9883dda048bdd7015508eba753065340b2f2074c22b1282dc8ba`
con configuración `sha256:c4eb8f57a12b78f373e396606f07421c4727091163f510ecd315b5185781c450`
usó Trivy 0.76.2 y su DB del 13 de noviembre. Informe
`.tools/evidence/phase4-immich-server-compiled-trivy-20261113-v1.json`,
SHA256 `63cab8a7ea393a6545251b0f32822a07111b61cefa4c919bc5ac6374374e225a`.

| Alcance observado | CRITICAL | HIGH | Rutas o familias |
| --- | ---: | ---: | --- |
| Debian 13.7, 222 paquetes | 4 | 150 | Runtime nativo, multimedia y componentes del sistema, más dos HIGH del par APT |
| Dependencias Node del servidor | 4 | 32 | `/usr/src/app/server/node_modules/.pnpm` |
| npm que distribuye Node | 0 | 6 | `/usr/local/lib/node_modules/npm/node_modules` |

## Incremento Debian offline

`Dockerfile.debian-update` aplica exclusivamente los paquetes siguientes al
candidato compilado. La revisión de bibliotecas y APT usa el mismo
repositorio `trixie-security`. Los valores de cobertura son registros del
escaneo actual con versión corregida indicada, pendientes de reescaneo de
la imagen derivada.

| Paquete | Versión de seguridad | Registros con corrección indicada |
| --- | --- | --- |
| `libbrotli1` | `1.1.0-2+deb13u1` | 1 CRITICAL |
| `libopenexr-3-1-30` | `3.2.12-1~deb13u1` | 2 CRITICAL, 14 HIGH y 10 MEDIUM |
| `zlib1g` | `1:1.3.dfsg+really1.3.1-1+deb13u1` | 2 HIGH y 1 MEDIUM |
| `apt` y `libapt-pkg7.0` | `3.0.4` | 2 HIGH |

[Brotli](https://security-tracker.debian.org/tracker/CVE-2026-111497) ·
[OpenEXR](https://packages.debian.org/en/trixie/amd64/libopenexr-3-1-30) ·
[zlib](https://security-tracker.debian.org/tracker/DSA-6613-1) ·
[APT](https://security-tracker.debian.org/tracker/DSA-6607-1).

El archivo `InRelease` de Debian del 12 de noviembre (válido hasta el 19)
pasó `gpgv` con las dos claves oficiales publicadas para Trixie y Bookworm.
El Release autenticado fija el SHA de `main/binary-amd64/Packages.xz` y el
índice fija los cinco archivos `.deb`. OpenEXR incorpora `libdeflate0 >=
1.19`; la imagen ya incluye `1.23-2`. El paquete mantiene los SONAME
`.so.30` de OpenEXR, OpenEXRCore, OpenEXRUtil, Iex e IlmThread. Las otras
dependencias nuevas quedan satisfechas por el par APT instalado junto.
[Claves Debian](https://ftp-master.debian.org/keys.html) ·
[Autenticación de índices](https://manpages.debian.org/trixie/apt/apt-secure.8.en.html).

El contexto está en
`.tools/backup-source-build/immich-server-v336-debian-update-v1/transport/context.tar.gz`.
Mide 3.968.757 bytes y tiene SHA256
`f2dde9109a5d935b8c061ed652ee7bce0ebcf75338c1ee0cb6ffc29b2c975988`.
`prepare_debian_update.py` permite regenerarlo desde el OCI local y la
captura de firmas e índice preservada. `--output` debe ser una ruta nueva.

En el builder Linux existente, verificar los digests del TAR y de la
referencia local `base_tag` antes de construir. Usar un `candidate_tag`
nuevo. El instalador usa `dpkg` sin red dentro de la capa, registra sus
versiones en el gestor Debian, compara el inventario completo y comprueba
Node, FFmpeg, FFprobe y el SONAME OpenEXR. Mantener el OCI anterior.

```bash
docker --context default buildx build --builder default \
  --platform linux/amd64 --network none --pull=false --provenance=false --sbom=false \
  --build-arg "BASE_IMAGE=$base_tag" --tag "$candidate_tag" "$context_dir"
```

El candidato Debian `12ba8283…` se construyó y pasó el smoke nativo de
Sharp, bcrypt y audio. Trivy bajó de 4 a 1 CRITICAL y de 150 a 132 HIGH
en paquetes Debian. Sigue pendiente la prueba con una imagen OpenEXR
sintética en los consumidores que usan esa biblioteca.

## Cierre npm y runtime restantes

1. La imagen `50ed464e…` conserva el `node_modules` oficial. El lock
   3.3.6 ya resuelto `53a7d839…` y su grafo de 650 identidades del
   servidor sí incluyen `handlebars 4.7.10`, `proxy-addr 2.0.8` y
   `tar 7.5.21`. Los tres tarballs se conservan en la cápsula Linux.
   Reutilizar ese lock, su almacén Linux y los recibos de integridad.
   [Handlebars](https://github.com/handlebars-lang/handlebars.js/security/advisories) ·
   [proxy-addr](https://github.com/advisories/GHSA-jqcg-44mw-7w3h) ·
   [tar](https://github.com/advisories/GHSA-23hp-3jrh-7fpw).
2. La DB del 13 de noviembre encontró 0 CRITICAL, 0 HIGH, 18 MEDIUM y 3
   LOW en el grafo congelado de 650 identidades. El informe
   `phase4-immich-server-v336-frozen-npm-trivy-20261113-v1.json` tiene SHA
   `bd30204f7096f40a816d059d933d4097577275f4ef7be4bc3383a5e106a7406f`.
   La aceptación requiere además verificar la proyección física y escanear
   su imagen. Los pasos siguientes conservan la versión 3.3.6.
3. Las seis filas HIGH de npm de la imagen combinada pertenecen al npm
   global 11.19.1. Node 24.22.3 aún empaqueta ese npm, así que el
   incremento conserva los comandos npm y npx mediante npm 11.21.2
   verificado por separado. El escaneo de los 144 manifiestos del tarball
   dio cero CRITICAL o HIGH y dos MEDIUM en glob y minimatch.
   [Node 24.22.3](https://nodejs.org/en/blog/release/v24.22.3) ·
   [npm 11.21.2](https://github.com/npm/cli/releases/tag/v11.21.2).
4. El tracker Debian aún marca `libxml2` y varias incidencias de Kerberos
   como vulnerables en Trixie. Queda evaluar backports mantenidos o una
   base con el cierre nativo reconstruido, incluyendo glibc, FFmpeg/audio,
   libvips y el contrato ML. Los paquetes de otra suite no se instalan
   directamente sobre Trixie. [libxml2](https://security-tracker.debian.org/tracker/CVE-2026-6653) ·
   [Kerberos](https://security-tracker.debian.org/tracker/source-package/krb5).

### Exportar y empaquetar las dependencias del servidor

Ejecutar `export_server_node_modules.sh` con `/bin/sh` y UID/GID 1000:1000
en el SDK Linux existente con `--network none`. Montar el directorio de trabajo
original en `/work` y usar un directorio de salida nuevo dentro de esa ruta.
El script exige los hashes del lock, pnpm, Node y del índice SQLite de 1.251
entradas. Usa `deploy --prod --no-optional` para generar un lock derivado del
servidor. Pasar el espejo local `pnpm-v11-metadata.tar.gz` como segundo
argumento. Está en `.tools/backup-source-build/immich-server-v336-npm-metadata-v2`
y tiene SHA `5936efa42bfbc0fe2b2f181ad1805187d6e5e33b48c79f2f5edf55a35ef1f3dc`.
Incluye 617 archivos con el cotejo de SRI para las 650 identidades del grafo
y la metadata completa oficial necesaria para verificar sus fechas. El
script restaura el espejo en el directorio del intento y conserva el
registro de política del lock original.
[pnpm deploy](https://pnpm.io/cli/deploy).

El intento Linux v4 pasó la política del lock derivado para 650 paquetes
de registro y materializó 651 entradas con el SDK local incluido y cero
descargas. El recibo está en
`.tools/evidence/phase4-immich-server-npm-deploy-20261113-v4.json`.

```bash
docker exec -i --user 1000:1000 "$sdk_cid" /bin/sh -s /work/immich-npm-deploy-v1 \
  /work/pnpm-v11-metadata.tar.gz < export_server_node_modules.sh
python3 verify_server_node_modules.py "$export_dir/deploy/node_modules" \
  production-graph.json > npm-tree-receipt.json
mkdir -p "$context_dir/payload"
cp Dockerfile.npm-update "$context_dir/Dockerfile"
cp install_server_node_modules.sh "$context_dir/payload/install.sh"
cp immich-plugin-sdk-dist.sha256 "$context_dir/payload/SDK.sha256"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
  -C "$export_dir/deploy" -cf - node_modules | gzip -n > "$context_dir/payload/node_modules.tar.gz"
(cd "$context_dir/payload" && sha256sum node_modules.tar.gz > TREE.sha256)
```

El verificador comprueba el grafo `production-graph.json` de
`.tools/backup-source-build/immich-server-js-lock-v1`. Informa las
identidades omitidas para revisar las exclusiones opcionales. La imagen
Debian derivada `sha256:12ba8283bbc40394a16152c2530d58e5d2260890befacaabe2d18bfed62aa25a`
retiene el ELF Sharp 0.35.5 que cargó libvips 8.18.7 en el smoke previo y
los siete archivos generados de `@immich/plugin-sdk`. La capa npm preserva
esos archivos y comprueba el prebuild glibc de bcrypt del propio paquete.
Usar el comando Docker del incremento Debian con el nuevo contexto y un
tag nuevo. Repetir el smoke como UID 1000, Trivy sobre la imagen y la
aceptación de carga, WebSocket, búsqueda ML y arranque.

### Actualizar Node y npm de la imagen combinada

Usar como base el manifiesto combinado
`sha256:5717c3b6ca3e0acf78a5a3bb6f42f679b5a0c74170151a8b35b2c197f334b9c6`
y config `sha256:3b55f31114e1bdc1624b54cbcdeb0cdd6fcd97b8979804ae40b3bed39d848c25`.
El contexto está en `.tools/backup-source-build/immich-server-v336-node-update-v1/context.tar`.
Verificar su SHA256 con el recibo de la misma carpeta y aplicar el comando
Docker del incremento Debian con el contexto y un tag nuevo.

Se proyectan el binario Linux amd64 y las cabeceras de Node de su release
firmado por Antoine du Hamel. npm 11.21.2 se verifica con SRI y las dos
firmas ECDSA del registro. Se mantiene Corepack 0.36.0 y se verifica ABI
137 y N-API 10, las versiones de npm/npx, el import de SDK y Sharp/bcrypt.
El arranque y la CLI de Immich ejecutan Node directamente. Mantener npm
preserva su interfaz dentro de la imagen.
[Claves de releases de Node](https://github.com/nodejs/node#release-keys) ·
[Firmas del registro npm](https://docs.npmjs.com/about-registry-signatures/).

En el contenedor nuevo, ejecutar `verify_compiled_runtime.cjs` como
UID/GID 1000:1000 con el WAV conocido y `v24.22.3` como argumento final.
Escanear el OCI exacto, probar CLI y ejecutar aceptación funcional con
PostgreSQL, Valkey y ML. El informe previo sobre npm es
`.tools/evidence/phase4-immich-npm-tool-11.21.2-trivy-20261113-v1.json`.
