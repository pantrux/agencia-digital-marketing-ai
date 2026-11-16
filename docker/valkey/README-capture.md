# Captura OCI de Valkey como datos

Esta unidad prepara una captura revisable del repositorio `valkey/valkey`, versión `9.1.3`, variante `alpine3.24`, en `vm-media-01` (`192.168.0.85`). No inicia contenedores, instala servicios ni ejecuta el binario Valkey. Obtener y verificar un TAR no acredita su seguridad: faltan la auditoría actual del sistema y la cobertura del binario incorporado. No hay captura real acreditada por estas pruebas locales.

El candidato físico es `.tools/evidence/phase4-valkey-alpine-candidate.json`, SHA256 `74decf8f583aaf3ba0ca6f4c2aa708c6147c863fb1f5ab2f75fc25ac5e813096`. Se conservan por separado:

| Identidad | Digest SHA256 |
| --- | --- |
| Índice upstream | `bf09d58e96d6b4524a15396b65b242bbe18beebbcc15a04d8ee75ddac62fae0b` |
| Manifiesto Linux amd64 | `de6fbe9626fea9f6f297b6867d2c8cf8f0394ee5ed171613166fc093cb49d0a6` |
| Configuración | `631d75bac338990caa2d3c1c9c7ba5f4c38584f7a4c04cdfb363d861a33fb692` |

La referencia de los tres comandos Docker permitidos es literalmente `docker.io/valkey/valkey@sha256:de6fbe9626fea9f6f297b6867d2c8cf8f0394ee5ed171613166fc093cb49d0a6`. Docker 29 puede informar el manifiesto como `Id`; otros almacenes informan la configuración. El inspector admite únicamente esos dos valores fijados, Linux/amd64 y el `RepoDigest` exacto. El TAR acredita además la relación índice → manifiesto → configuración → siete capas, incluidos SHA256, tamaños y DiffIDs descomprimidos.

## Preparación por el responsable

El plan es local y no lee `.env`, importa Paramiko ni abre SSH:

```powershell
python -I -S -B docker/valkey/capture_image.py plan
```

La salida contiene `profile` y `profile_sha256`. El hash corresponde a los bytes JSON canónicos de `profile`, sin salto de línea; no al texto completo impreso. El perfil fija hashes de las dos fuentes, candidato y auxiliares. Cambiar sus bytes exige revisión y nuevos pins. Root conserva el plan y respalda primero estos campos de la única familia `MEDIA_VALKEY_OCI_CAPTURE_V1_`:

| Sufijo | Representación exigida |
| --- | --- |
| `ROOT` | `/var/lib/media-platform/valkey-oci-capture-de6fbe9626fea9f6-v1` |
| `OWNER` | `agencia-digital-marketing-ai:valkey-oci-capture:v1` |
| `IMAGE` | Referencia completa por digest indicada arriba |
| `MANIFEST_DIGEST` / `CONFIG_DIGEST` | `sha256:` seguido del valor correspondiente |
| `LOCAL_ARCHIVE_PATH` | Ruta absoluta exacta `destination` del plan, terminada en `.tools/cache/phase4-built-images/valkey-de6fbe9626fea9f6-v1.tar` |
| `PROFILE_SHA256` | Campo `profile_sha256` del plan |
| `CONTROLLER_SHA256` / `REMOTE_SHA256` | `source_sha256` / `remote_sha256` del perfil |

Se exige además la identidad existente `MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64`. El layout se comprueba con la fuente fijada `host_layout.py` (`3c986485…`), host `vm-media-01`, IPv4 `.85`, UID efectivo 0 y los dos directorios existentes con sus inodos/marcadores/recibos. El script no crea ni adopta el layout de otra aplicación.

El lector local admite una bóveda literal de hasta 8 MiB, rechaza enlaces/reparse/hardlinks y cambios de identidad; comprueba la ACL mediante el auxiliar fijado antes y después del análisis canónico. Compara identidad/tamaño/mtime/tipo entre `lstat` y `fstat`, conservando además ctime en comparaciones separadas `lstat`→`lstat` y `fstat`→`fstat`. Así admite la representación diferente de ctime entre APIs de Windows sin perder la detección de cambios dentro de cada una. No imprime valores. El conector se obtiene sólo de la función AST `connect_ssh` del archivo fijado `build_component_v2.py` (`9053e827…`), sin importar ese módulo completo. Exige Paramiko 5.1.0, clave de host Ed25519 exacta, `RejectPolicy`, `allow_agent=False` y `look_for_keys=False`. No solicita contraseña de sudo: utiliza sólo `sudo -n` y falla si no está autorizado.

## Etapas independientes

Root utiliza el Python aislado ya acreditado con Paramiko 5.1.0; las operaciones necesitan site-packages, por lo que no se usa `-S` en ellas. Se ejecuta una etapa por invocación:

```powershell
& '<Python aislado con Paramiko 5.1.0>' -I -B docker/valkey/capture_image.py prepare --execute --expected-profile-sha256 '<hash revisado>' --root-update-vault
& '<Python aislado con Paramiko 5.1.0>' -I -B docker/valkey/capture_image.py capture --execute --expected-profile-sha256 '<mismo hash>' --root-update-vault
& '<Python aislado con Paramiko 5.1.0>' -I -B docker/valkey/capture_image.py fetch --execute --expected-profile-sha256 '<mismo hash>' --root-update-vault
```

1. `prepare` crea exclusivamente el namespace nuevo, con modo 0700, marcador 0600 ligado a owner/perfil/device/inode y configuración Docker propia vacía. No ejecuta Docker. El recibo `prepare.json` se conserva en el servidor. La siguiente etapa exige el mismo recibo físico y el respaldo de Root.
2. `capture` reserva un intento exclusivo. Sólo ejecuta `docker image inspect`, un `image pull --platform linux/amd64` si inspect devuelve exactamente el error único `No such image` para la referencia literal, y `image save`. Cualquier error de permisos, conexión u otro digest bloquea el pull/save. Después de pull vuelve a inspeccionar. Save escribe por stdout en el FD de `image.tar.partial`, creado con exclusión; no recibe una ruta de salida elegida por datos entrantes. El nombre remoto se conserva incluso cuando el recibo ya acredita una captura completa.
3. `fetch` exige el recibo de captura respaldado y vuelve a verificar propietario, permisos, identidad, tamaño y SHA del FD remoto. Transfiere cabecera → bytes exactos → recibo final. El receptor valida longitud, SHA, OCI, cierre de protocolo y exit code antes de publicar por enlace sin reemplazo. Un destino o parcial local previo impide empezar.

Cada resultado físico local queda en `.tools/evidence/phase4-valkey-oci-capture-v1-<etapa>.result.json`; hay un `*.intent.json` exclusivo previo a SSH. En éxito, `--root-update-vault` invoca únicamente el updater fijado de Root y respalda `PREPARE_RECEIPT_{PATH,SHA256,B64}`, después `CAPTURE_RECEIPT_{PATH,SHA256,B64}` y finalmente `FETCH_RECEIPT_{PATH,SHA256,B64}`. Todos tienen el prefijo común. `*_RECEIPT_SHA256` es el hash físico del JSON canónico completo; el campo interno `sha256` del envelope corresponde sólo a su `value`. Fetch añade `ARCHIVE_SHA256` y `ARCHIVE_SIZE`. Root debe comprobar que el respaldo terminó antes de invocar la etapa siguiente.

Si el updater falla después de guardar un recibo positivo, el archivo y el resultado remoto se conservan: Root inspecciona y respalda ese recibo existente. No se repite la operación para regenerarlo. Intentos, errores y parciales nunca se borran automáticamente. La unidad no implementa recuperación ni adopción de directorios, archivos o recibos previos.

## Límites y evidencia pendiente

El TAR está limitado a 256 MiB; el proceso save hereda `RLIMIT_FSIZE`. La inspección rechaza entradas TAR duplicadas, rutas inseguras, enlaces y tipos especiales. No se llama a `extractall`; cada blob se lee como datos. Se admiten únicamente metadatos OCI/Docker reconocidos y los blobs referenciados. Cada capa gzip tiene límite expandido de 512 MiB y el conjunto de 1 GiB. No se extraen ni ejecutan miembros internos de las capas. Una captura con blobs adicionales también se rechaza.

La captura tiene un presupuesto operativo de 300 s, con alarma externa a 305 s y hasta 2 s de recolección del proceso propio. Preparación/fetch tienen 120 s, alarma 125 s y canal local 150 s; captura usa canal 330 s. Docker recibe un entorno nuevo y configuración privada vacía, sin proxies ni credenciales heredadas. El canal envía latidos; su pérdida durante Docker cancela el proceso propio. Antes de cualquier señal al PGID, incluso tras un fallo previo al primer loop, `waitid(WNOWAIT)` debe acreditar que el hijo permanece retenido; `ChildProcessError` impide señalarlo. La limpieza bloquea SIGALRM, suspende su temporizador y restaura máscara y plazo restante descontando el tiempo consumido. No hay búsquedas por nombre, contenedores ni parada de otros procesos. La terminación del cliente Docker no promete deshacer bytes que el daemon ya hubiera incorporado al caché; no se borran imágenes. El cierre del canal rechaza cualquier byte stderr observado aunque exit sea 0; cuenta los bytes y descarta su contenido sin imprimirlo.

La copia local vive en un caché de datos del proyecto. No ofrece aislamiento frente a otro administrador o escritor autorizado de ese árbol; hashes y pins posteriores deben volver a comprobarse antes de cualquier uso. La publicación verifica el inodo original y conserva archivos si detecta sustitución, sin borrar un destino ya publicado. Nada de esta unidad autoriza ejecutar Valkey ni sustituye la SCA del TAR capturado.

Las pruebas locales usan TAR sintéticos, archivos temporales y dobles de transporte, sin `.env`, SSH, Docker, red ni SDK. Incluyen rechazos de hashes/DiffIDs, colisiones, truncado, cambio de archivo, recibos y conector alterados. La prueba nativa `openat`/`O_NOFOLLOW` se omite honestamente en Windows; Root debe ejecutarla en Linux antes de acreditar esa frontera. Tampoco se acredita todavía captura/fetch real, auditoría de la imagen o servicio Valkey.

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -p test_valkey_capture.py -v
python -m ruff check docker/valkey tests/test_valkey_capture.py
python -m ruff format --check docker/valkey tests/test_valkey_capture.py
```
