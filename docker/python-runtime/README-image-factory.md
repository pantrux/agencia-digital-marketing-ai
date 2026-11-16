# Factory de imagen Python sin arranque

Esta unidad construye y captura una imagen propia a partir del contexto del [preparador congelado](README-runtime-python.md). El agente sólo implementa y prueba contratos locales. Las operaciones remotas las ejecuta Root después de revisar y respaldar fuentes, perfil, identidades y recibos. No arranca contenedores, Python de la imagen, aplicaciones, SDK o scanners.

Son cinco fuentes: [controlador](build_runtime_image.py), [remoto](build_runtime_image_remote.py), [gate de datos](runtime_image_inventory.py), [pruebas](../../tests/test_python_runtime_image.py) y este documento. Las cinco fuentes originales del preparador quedan intactas.

## Identidad y entradas

- Host canónico: `vm-media-01`, `192.168.0.85`, operador SSH `jandrade`.
- Namespace remoto nuevo: `/var/lib/media-platform/build/python-runtime-image-v1`.
- Owner: `agencia-digital-marketing-ai:python-runtime-image:v1`.
- Raíz de captura local nueva: `<workspace>/.tools/backup-source-build/python-runtime-image-v1`.
- Prefijo único: `MEDIA_PYTHON_RUNTIME_IMAGE_V1_`.

El padre remoto `/var/lib/media-platform/build` debe existir, ser root:root, modo 0700, sin enlaces ni ancestros modificables por grupo/otros. No se crea/adopta ese padre automáticamente. El layout existente se coteja mediante el helper `3c986…`, incluyendo marcador, recibo y dev/ino de `/opt/media-platform` y `/var/lib/media-platform`. La recepción exige el namespace nuevo, root:root 0700, marcador propio y salidas EXCL 0600. No se elimina una imagen, archivo o parcial ante fallos.

Los inputs son los cinco outputs auténticos de la preparación Python: `context.tar`, `inventory.json`, ambos CDX y `runtime-image-gate.json`. El controlador comprueba el recibo físico, marcador/dev/ino, ACL privada y cada tamaño/SHA antes de usarlos. El recibo no se inventa si falta. El plan puede presentar `inputs_ready=true` después de esa preparación, pero conserva `ready=false` mientras falten las evidencias físicas, ABI y seguridad; no contiene IDs finales ficticios.

## Construcción sin consultas al registro

La base es el manifiesto `sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72`, config `sha256:b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7`. Debe estar presente en Docker. Si no existe, la etapa falla: no hace pull ni consulta un candidato alternativo.

El TAR original permanece intacto. Se crea un TAR derivado nuevo cuyo único cambio de contenido es la línea `FROM …` por `FROM pythonbase`. Se conservan el catálogo de `rootfs`, bytes, modos, propietarios y enlaces; ambos TAR y Dockerfiles tienen SHA propios. El alias se resuelve mediante `--build-context pythonbase=oci-layout://<namespace>/base-oci@sha256:ce3d…`, después de verificar el OCI exportado y sus blobs. No se confía en `--pull=false` como aislamiento suficiente por sí solo.

El comando cerrado usa Buildx `default`, `linux/amd64`, `--network none`, `--pull=false`, `--provenance=false`, `--sbom=false`, `--load` y contexto por stdin. El Dockerfile sólo tiene FROM/COPY/configuración/USER/WORKDIR/CMD. No tiene RUN, frontend remoto, APK, scripts, compiladores o SDK. Se usa configuración Docker privada vacía y socket Unix literal; no se hereda entorno del operador.

`image-id.txt` se reserva EXCL antes del build. `build-metadata.json` es la única excepción que puede ser 0644, en el directorio privado, regular, root:root y nlink1. Los demás archivos deben seguir en 0600. No se acepta un archivo de metadatos previo. El manifiesto, config e IID se conservan antes de comprobaciones posteriores: IID debe corresponder a `containerimage.config.digest`, mientras `containerimage.digest` identifica el manifiesto. Docker 29 puede mostrar cualquiera de esos dos tipos exactos en inspect; eso no permite confundirlos al leer el OCI. Una salida fallida puede contener IDs conocidos con `verified=false`; no acredita imagen válida.

## Etapas y respaldo

El plan es sólo lectura y no lee `.env`:

```powershell
python -I -B -W error::ResourceWarning docker/python-runtime/build_runtime_image.py
```

Antes de cualquier operación, Root respalda estos campos bajo el prefijo indicado:

| Sufijo | Contrato |
| --- | --- |
| `ROOT`, `OWNER`, `LOCAL_ROOT` | Rutas/owner literales anteriores |
| `PROFILE_SHA256` | SHA del objeto `profile` canónico impreso por plan |
| `CONTROLLER_SHA256`, `REMOTE_SHA256`, `INVENTORY_SOURCE_SHA256` | Tres fuentes productivas exactas |
| `INPUTS_RECEIPT_PATH`, `INPUTS_RECEIPT_SHA256` | Recibo Python preparado auténtico |
| `LAYOUT_B64` | Recibo existente del layout, con dos targets y sus identidades físicas |
| `DAEMON_B64` | JSON de sólo `ID`, `ServerVersion`, `DockerRootDir`; Docker 29 y raíz `/var/lib/docker`, valores reales previos |

La operación explícita de Root lee la bóveda con parser canónico fijado, ACL y límite 8 MiB mediante el lector congelado Valkey. Reutiliza únicamente `connect_ssh` AST del conector fijado: Paramiko 5.1, pin exacto, RejectPolicy, sin claves del agente o búsquedas implícitas. No crea otro JSON con credenciales ni las transmite en argv/entorno/logs. El actualizador congelado sólo se invoca con el flag de Root; los comentarios mantienen los tres prefijos canónicos.

```text
python -I -B docker/python-runtime/build_runtime_image.py ETAPA --execute --root-update-vault --expected-profile-sha256 SHA_REAL_RESPALDADO
```

Cada invocación tiene intent y resultado locales por exclusión. Una etapa fallida no se repite ni se adopta. Root conserva y respalda cada recibo positivo antes de pasar a la siguiente:

1. **prepare**: acredita host/layout, crea sólo el namespace nuevo y configuración vacía, coteja daemon real.
2. **receive**: recibe los cinco inputs por tamaño/SHA, genera el contexto derivado y comprueba su pin.
3. **base**: inspect/export de la base exacta, verificación OCI y publicación del layout local por FD/EXCL.
4. **build**: revalida base/layout OCI y contexto; construye sin RUN/red/pull y conserva logs/IDs.
5. **final**: inspect/export del manifiesto propio; coteja el config real contra BuildKit. No ejecuta la imagen.
6. **fetch**: transfiere los TAR base/final acotados, con hash, a raíz local nueva privada. Los parciales se conservan y no reemplazan destinos.
7. **gate**: relee las capturas locales, inputs y fuentes; publica `physical-verification.json` e `image-security-inputs.json` por exclusión.

El controlador respalda los campos `ETAPA_RECEIPT_PATH`, `ETAPA_RECEIPT_SHA256` y `ETAPA_RECEIPT_B64` tras cada éxito. No escribe esos campos el agente. Las etapas posteriores cotejan recibos respaldados con los físicos y el mismo perfil, directorio y marcador. Cambiar fuentes o preparación exige un perfil nuevo; no se sobrescribe la historia.

## Límites y cierre propio

Se admiten TAR OCI de hasta 256 MiB, capas expandidas de hasta 128 MiB, total expandido de 512 MiB, archivo de 32 MiB y 30.000 nodos. Se validan trailer/duplicados/case, descriptores, blobs SHA256, DiffID, alcance exacto y plataforma. No se extraen capas al host para ejecutarlas. La capa COPY no admite whiteouts ni dispositivos. El layout base se publica por FD y se revalida antes de Buildx, sin adoptar extras o enlaces.

Cada CLI Docker tiene hasta 180 s, salida diagnóstica conjunta de 1 MiB y, para exportación, RLIMIT_FSIZE de 256 MiB. El proceso CLI tiene entorno nuevo, core dumps deshabilitados y límite de espacio virtual de 512 MiB. El remoto tiene alarma de 240 s y el canal SSH 260 s. La limpieza puede añadir hasta dos segundos al CLI: el helper congelado `515f…` comprueba WNOWAIT, retiene el líder antes de señalar únicamente su PGID y suspende/restaura la alarma durante el cierre. No usa búsquedas por nombre, stop de contenedores ni podas generales. El límite del cliente Docker no es una promesa de limitar toda actividad interna del daemon; al no existir RUN no se inicia una carga de aplicación dentro del build.

## Gate físico y límites de la evidencia

El loader AST de `9e0…` selecciona exclusivamente funciones genéricas OCI/capas/ELF, `MAX_FILE` y `MAX_ARCHIVE` originales, con todos sus globals cerrados. No importa `apk_inventory` de 45 paquetes, `abi_closure` PostgreSQL ni constantes operativas PG. Sus salidas se cotejan con wrappers acotados propios; no se parchean globals de fuentes congeladas.

El gate exige **19 filas instaladas** en DB real base/final idéntica, y **14 componentes manuales** por catálogo físico. Son 33 componentes de cobertura, no 33 paquetes instalados. Se rechazan archivos base modificados, colisiones con archivos base, extras finales y faltantes. Directorios compartidos mantienen propiedad/modo; se cotejan cada archivo/enlace, intérprete/libpython, stdlib y paths ELF contra los inputs.

Las clausuras de carga se calculan por raíz ELF en contexto Python: intérprete, libpython, cargador PT_INTERP y dependencias concretas. No se toman exportaciones de otras extensiones no cargadas. Se comprueban SONAME, símbolos fuertes y nombres de versiones contra proveedores físicos. Es una comprobación estática; no prueba importaciones, dlopen posterior ni la asociación individual símbolo-versión. Esos límites siguen en el recibo y no se convierten en autorización de runtime.

El alias auténtico `usr/lib/terminfo -> ../share/terminfo` se observa por separado. Si su destino falta, el reporte lo mantiene pendiente de clasificación por Root; no lo repara ni lo presenta como resuelto. La configuración final exige Linux/amd64, USER 1000:1000, WORKDIR/CMD cerrados y entorno heredado más las tres variables fijadas, sin duplicados/LD_PRELOAD/PYTHONPATH/PYTHONHOME ni entrypoint/healthcheck/volúmenes/puertos activos imprevistos.

Las salidas para SCA ligan manifiesto, config, ambos TAR, inventario y CDX, componentes manuales y ELF físicos. No ejecutan Trivy/OSV ni emiten release. Los vendors de py3-pip-wheel, terceros estáticos y licencias conservan sus límites reales. Siempre se mantienen `runtime_approved=false`, `sca_approved=false` y `binary_executed=false`.

Root revisará SCA con cobertura exacta y emitirá después una autorización independiente. El consumidor Flasgger conserva una CPU, 512 MiB, **32 pids**, UID1000, raíz RO, tmpfs y red none; el runtime no arranca en esta unidad. OpenCV/NumPy son overlay posterior separado.

## Pruebas

```powershell
python -I -S -B -W error::ResourceWarning tests/test_python_runtime_image.py -v
python -m ruff check docker/python-runtime/build_runtime_image.py docker/python-runtime/build_runtime_image_remote.py docker/python-runtime/runtime_image_inventory.py tests/test_python_runtime_image.py
python -m ruff format --check docker/python-runtime/build_runtime_image.py docker/python-runtime/build_runtime_image_remote.py docker/python-runtime/runtime_image_inventory.py tests/test_python_runtime_image.py
```

Las pruebas portables leen fixtures TAR/OCI/ELF reales, sin ejecutar su contenido, y cubren 19+14, corrupción, contexto FROM, símbolos por contexto, IDs parciales, perfil/entorno y EXCL. Dos pruebas nativas de FD/NoFollow/layout OCI exigen Linux UID0 y se omiten honestamente en Windows. Root debe ejecutarlas en un directorio temporal propio antes de acreditar esa frontera; los dobles no sustituyen esa evidencia.
