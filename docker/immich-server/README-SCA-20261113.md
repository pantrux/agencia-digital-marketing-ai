# Candidatos Immich y remediación del servidor

La última publicación estable comprobada el 13 de noviembre es `v3.4.1`,
publicada el 11 de noviembre a las 18:27 UTC. El manifiesto GHCR de Linux
amd64 es `sha256:4351dd621899ecc8ed317ae87cdcaeb968ec8ba4f9d8053d913a73eb6f6dd7c2`,
con configuración `sha256:cad0dfbf5486695337c1072ba9e3846dd1b3d7c9da24640ba396e0a6bd4a52fd`.
Se descargaron y verificaron los 26 blobs y se reconstruyó el TAR local.
[Publicación oficial](https://github.com/immich-app/immich/releases/tag/v3.4.1).

Trivy 0.76.2 y su DB del 13 de noviembre dieron el siguiente resultado.
El informe y el recibo están en
`.tools/backup-source-build/immich-server-official-v341-sca-v1/`.

| Alcance | Inventario | CRITICAL | HIGH |
| --- | ---: | ---: | ---: |
| Debian 13.7 | 223 | 3 | 150 |
| Node | 822 | 0 | 18 |
| Restic | 81 | 0 | 10 |
| Restic proxy | 11 | 0 | 0 |

`v3.4.1` conserva Node 24.22.1, `tini -- /bin/bash -c`, `start.sh` y la
ruta `/usr/src/app`. Sharp 0.35.5 y bcrypt 6.0.0 conservan la versión. La
imagen incluye el servidor, dependencias y recursos de su propia revisión.
El export compilado 3.3.6 sólo se aplica a la base exacta 3.3.6. El candidato
de la [receta compilada](README-COMPILED-SERVER.md) acredita por separado
el build, la ABI, el audio y el servicio ML privado.

## Próximas revisiones del candidato de producción

1. Preparar un candidato Debian basado en `v3.4.1` con
   `libopenexr-3-1-30` `3.2.12-1~deb13u1`, `zlib1g`
   `1:1.3.dfsg+really1.3.1-1+deb13u1` y el par `apt`/`libapt-pkg7.0`
   `3.0.4`. Descargar sólo el cierre desde `trixie-security` y acreditar
   `InRelease`, índice `Packages` y SHA256 por archivo. OpenEXR conserva el
   nombre del paquete, pero añade una dependencia de `libdeflate0 >= 1.19`
   y requiere comprobar los SONAME de los consumidores. Ejecutar el smoke
   de Sharp con PNG y OpenEXR y repetir FFmpeg/FFprobe. Los cuatro paquetes
   tienen corrección oficial. [OpenEXR](https://packages.debian.org/en/trixie/amd64/libopenexr-3-1-30) ·
   [zlib](https://security-tracker.debian.org/tracker/source-package/zlib) ·
   [APT](https://security-tracker.debian.org/tracker/DSA-6607-1).
2. Rehacer el cierre npm de la misma revisión con `fast-uri` `3.1.7`,
   `undici` `8.12.2`, `multer` `2.5.0` y `systeminformation` `5.33.16`.
   Resolver y probar las cuatro actualizaciones en el lock de `v3.4.1`.
   El npm de la base tiene sus propios avisos de `undici` y
   `brace-expansion`. Actualizar Node/npm desde material autenticado o
   prescindir de npm en el runtime tras verificar consumidores. Revalidar
   Node-API, Sharp, bcrypt y los workers.
3. Resolver el binario Restic con una publicación o build autenticado que
   cubra sus dependencias Go. Probar backup y restauración con una fixture
   privada si se activa ese componente. El recibo actual conserva sus
   10 HIGH. La aceptación del servidor y el reescaneo de ese binario son
   verificaciones independientes.
4. Tratar `libxml2` y Kerberos como bloqueadores de la imagen Debian. El
   tracker confirma que Trixie carece de solución publicada para
   `CVE-2026-6653` y varios avisos de Kerberos. La revisión debe evaluar
   backports de fuente mantenidos o reconstruir la clausura nativa completa
   en otra base compatible. Evitar instalar bibliotecas de otra suite sobre
   Trixie. La proyección Wolfi sigue requiriendo un build del servidor,
   Sharp, codecs y los ELF completos de FFmpeg con los ABI de esa base.
   [libxml2](https://security-tracker.debian.org/tracker/CVE-2026-6653) ·
   [Kerberos](https://security-tracker.debian.org/tracker/source-package/krb5).

La imagen del ensayo permanece privada. El candidato de producción deberá
acreditar sus propios inventarios, cobertura SCA, arranque, audio y vídeo,
migraciones, autenticación y búsqueda ML con el puente autorizado.
