# Finalización de imagen existente sin reconstruir

Unidad de cinco archivos: controlador local/remoto, observaciones, pruebas y
este procedimiento. Sólo raíz podrá apply tras revisar y respaldar sus pins.
El agente sólo hizo las lecturas autorizadas y preparó/probó estas fuentes.
No construyó, arrancó, cambió permisos, ejecutó SDK/SQL o escribió la bóveda.

## Causa y estado real conservado

Runtime prepare aee542c7… fue positivo: 66 archivos proyectados, 23 excluidos,
45 APK y clausura ABI de 26 ELF. El build produjo la imagen y después falló al
leer build-metadata.json: su modo real es0644, mientras el lector genérico
congelado exige0600. El fallo5c92cd51… y su perfil E27 permanecen intactos.

Además, Buildx no escribió containerimage.config.digest en ese metadata. En
ese caso su getImageID usa containerimage.digest como fallback del iidfile;
no es un config ID. Se conserva el JSON original sin inventar esa key.

La inspección y log acreditan como datos observados, aún no aprobación de SQL:

- Manifiesto96c6cce5d1efe176fe5b22e17c3c4c7bca926c94c403096bce9aa663df04a953.
- Config760d54774499a5871dd92c62de5a64cb0cb31efc0acefeeb20e48cfa4d628033.
- UID/GID70, entrypoint/CMD oficiales y owner runtime; 13 capas.
- Metadata root:root/0644/nlink1,2150bytes, dev2050/inode2530291,
  SHA7054686af6df047f0da36cf497b7d0c579451e65d5a998b44fdf720f845cbde9.
- IID root:root/0600/nlink1,71bytes, dev2050/inode2530290,
  SHAd31cfa60b0b58fd725ae37a95cf710e5e4f9eb7e479692350b5dbe5ce53409b8.
- Log root:root/0600/nlink1,1263bytes, dev2050/inode2530287,
  SHAcfd97597e684354859ffe1c0a35e3bff98734b06975cea806aa97bdf174ea0ac.

finish-image-observations.json fija esos datos, las identidades de context y
record y las huellas de las auditorías. La excepción0644 sólo corresponde a
ese nombre/inodo/tamaño/SHA; no cambia el conjunto de permisos de otros
lectores. Se usan FD NoFollow, archivo regular, UID/GID/nlink y comprobación
antes/después. Nada se chmod/chown. No se depende de image ls, que no mostró
el dangling; inspect del ID exacto sí confirmó su existencia, sin recoger Env.

## Perfil de referencias y gates

El perfil nuevo es pequeño: sólo SHA/path del histórico en RECORD/profile.json,
referencias E27/AEE/5c92, ID observado, observaciones y fuentes. No vuelve a
incluir el perfil histórico329KiB dentro de otro B64. La bóveda ya conserva
el histórico durable y el lector local8MiB se reutiliza con pin4369a61b….

Antes de exportar se revalida físicamente el histórico canónico E27, prepare
AEE, fallo5c92, marcas/devino de los dos namespaces y los tres archivos exactos.
Se cargan sólo los helpers públicos congelados runtime9e0a2829…, SDK V4,
compile V3, RECOVER y layout por sus SHA. Se usan sus validadores de host .85,
SDK detenido/NoForeign, catálogo, proyección, blobs base y ABI. Ninguna función
apply/build del runtime se invoca. No hay docker build, pull, run o start.

La identidad se resuelve sin modificar metadata: si config está presente,
iidfile debe ser ese config; si falta, iidfile debe ser el manifiesto. Inspect
Descriptor.annotations[config.digest] y las líneas únicas del log deben
converger. Después, el config se toma del OCI realmente exportado y debe
coincidir; sólo entonces se acredita la estructura final. Hay regresión de
ambos casos, incluida la forma real sin config.

## Escrituras y validación final

Sólo crea EXCL un subdirectorio propio dentro del record ya respaldado:

/var/lib/media-platform/build/postgres-immich-runtime/fase4-20261106-01/finish-image-v1

Root0700, marcador ligado a dev/inode, profile.json y observations.json nuevos.
Exporta con image save sólo el manifiesto96c a ese directorio/image.tar; no toca
el image.tar original ni adopta parciales. El proceso de exportación tiene
120s; helper total600s/cliente610s y captura4MiB. El recolector POSIX congelado
cierra sólo su grupo propio, sin señales por nombre o PID reutilizado.

Analiza el OCI completo, todos los blobs/DiffIDs y config; exige preservación
de las capas/nodos de la base c908/22fdd, 45 APK, scripts oficiales, UID70,
labels y sólo la proyección root0444 aprobada. Repite cierre ABI y hashes del
contexto, imagen, SDK detenido y todos los originales antes de receipt.json.
El código nunca añade un proveedor del SDK para ocultar una biblioteca ausente.

Un corte/fallo deja evidencia propia; no hay borrado ni replay. El recibo
incluye paso/error/proceso si existe y conserva el fallo original. El proceso
original de build se registra como desconocido, no se inventa a partir del
log; build_reexecuted=false. El éxito nuevo tampoco modifica BUILT_B64 fallido.
SQL y SCA final permanecen false, con aprobaciones separadas pendientes.

## CLI de raíz

Plan por defecto local sin bóveda/red. El segundo comando sólo genera el
perfil canónico EXCL después de comprobar los respaldos existentes:

```powershell
python -X utf8 -B docker/postgres-immich/finish_image.py plan
python -X utf8 -B docker/postgres-immich/finish_image.py plan --output '.tools/postgres-immich-build/finish-image-v1-final-profile.json'
```

Raíz respalda bajo MEDIA_POSTGRES_IMMICH_RUNTIME_FINISH_ los campos PROFILE_B64,
PROFILE_SHA256, LOCAL_SHA256, REMOTE_SHA256 y OBSERVATIONS_SHA256 antes de usar:

```powershell
python -X utf8 -B docker/postgres-immich/finish_image.py apply --profile '.tools/postgres-immich-build/finish-image-v1-final-profile.json' --expected-profile-sha256 '<SHA respaldado>' --root-update-vault
```

Sólo el CLI de raíz añade y relee RECEIPT_B64 nuevo. Reutiliza Paramiko5.1.0,
la clave y pin existentes de .85; sin credenciales nuevas ni valores en logs.
La futura aceptación SQL deberá gatear este FINISH positivo y SCA OS/nativa
vigentes, conservando la historia fallida anterior.

Fuente primaria del fallback:
[Buildx0.38.1 getImageID](https://github.com/docker/buildx/blob/v0.38.1/commands/build.go),
[metadata-file](https://docs.docker.com/reference/cli/docker/buildx/build/#metadata-file).
El caso real se contrasta como datos con las auditorías locales107af00b… y
7fc65399…; las pruebas no se presentan como un build o ejecución de imagen.
