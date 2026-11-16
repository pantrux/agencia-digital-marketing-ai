# SDK aislado para OpenEXR 3.5.3 externo

## Estado y alcance

Este lanzador prepara un CID nuevo para compilar OpenCV mínimo de Zou. El código
y sus pruebas locales no acreditan una ejecución Linux ni una rueda aceptada.
Raíz ejecuta las transiciones tras revisar y respaldar los pins. No se modifica
el CID anterior `1be1ec…`, sus parciales, la receta original ni el kit `755ea0…`.

La base es Wolfi `ce3d4630…`, configuración `b70d6183…`, con `--pull=never`.
El SDK corre como UID/GID 0 exclusivamente dentro del contenedor temporal:
2 CPU, 6 GiB, swap adicional cero, 128 procesos, sin red, puertos ni dispositivos,
`cap-drop=ALL`, `no-new-privileges` y tmpfs `/tmp` de 64 MiB. No es el runtime de
la aplicación. La configuración Docker propia no usa credenciales del host.

## Entradas y cinco montajes

El prefijo preparado tiene 248 entradas y 15 paquetes autenticados, TAR
`7ef4c7b3ade622abf647e96e4b276396f7a34e50dbb4d373048500d820b5a53c`
de 11.448.320 bytes. Su recibo es `3ab93de9…`. Se transfiere como datos junto con
seis fuentes/atestaciones públicas, mediante staging UID/GID 1000 exclusivo.
La promoción crea directorios root 0700 y archivos 0600 por FD, sin seguir enlaces
ni sobrescribir. Los once enlaces internos del prefijo sólo admiten sus destinos
basename fijados. Los CMake, headers, bibliotecas y la receta conservan sus bytes.

| Host | Contenedor | Acceso |
| --- | --- | --- |
| Kit original `755ea0…/inputs` | `/inputs` | RO |
| Fuente original `755ea0…/source` | `/source` | RO |
| `/opt/media-platform/opencv-openexr3-sdk-v1/prefix` | `/overlay` | RO |
| Headers originales `755ea0…/numpy-headers` | `/numpy-headers` | RO |
| `/var/lib/media-platform/opencv-openexr3-sdk-v1/outputs` | `/work` | RW |

No se filtra `inspect` para aparentar el contrato anterior: se validan los cinco
binds declarados y efectivos, labels, imagen, entorno, usuario, namespaces,
capabilities, tmpfs y límites. Cualquier sexto volumen o montaje escribible no
previsto se rechaza. Fuente, kit original y NumPy conservan identidades y hashes
del prepare `83b4ed68…`; sólo se reutilizan esos datos, nunca resultados antiguos.
La comprobación actual de hashes y el preflight host RSA `b7e79bb8…` permanecen
ligados a la atestación. No se incorpora `cryptography` al SDK.

## Orden de ejecución por raíz

Usar el Python aislado con Paramiko 5.1.0. El comando sin argumentos genera un
plan público local y no lee la bóveda ni establece conexión:

```powershell
python docker/kitsu-zou/run_openexr3_sdk.py plan
```

Después de respaldar rutas, fuentes y SHA del perfil, raíz liga el plan y ejecuta
cada transición en una llamada distinta. El CLI de raíz es el único que invoca
el actualizador canónico de la bóveda:

```powershell
$python = '.tools/venvs/synology-audit/Scripts/python.exe'
& $python docker/kitsu-zou/run_openexr3_sdk.py plan --root-update-vault
& $python docker/kitsu-zou/run_openexr3_sdk.py prepare --root-update-vault
& $python docker/kitsu-zou/run_openexr3_sdk.py create --root-update-vault
# El recibo con CID completo queda respaldado antes de start.
& $python docker/kitsu-zou/run_openexr3_sdk.py start --root-update-vault
# El nacimiento, PID, StartedAt y cgroup quedan respaldados antes de sdk.
& $python docker/kitsu-zou/run_openexr3_sdk.py sdk --root-update-vault
# Raíz emite la aprobación SCA externa descrita abajo.
& $python docker/kitsu-zou/run_openexr3_sdk.py approve --root-update-vault
& $python docker/kitsu-zou/run_openexr3_sdk.py build --root-update-vault
& $python docker/kitsu-zou/run_openexr3_sdk.py stop --root-update-vault
```

`start` rechaza cualquier otro SDK/builder propio activo y exige al menos 8 GiB
de memoria disponible. Es una comprobación de ventana, no una reserva global
atómica: raíz serializa los SDK de PostgreSQL y OpenCV. No se detiene otro recurso
para liberar memoria. No se reinicia un CID que haya arrancado antes.

`sdk` instala únicamente los 70 APK originales offline y sin scripts; acredita
19 de base + 70 = 89 paquetes exactos y los seis SHA de herramientas. Se coteja
la DB física `ae74c96e…`, interfaces `lo`, ausencia de Git y cuatro mounts RO.
Los tres APK nuevos sólo aportan datos parciales del prefijo; no se instalan.

`build` ejecuta la receta preparada `fc77fe4d…` desde el prefijo RO con umask 077,
work nuevo `/work/openexr3-attempt-v2`, dos jobs y presupuesto total de 1.800 s,
incluida reserva de 20 s para cierre. El probe compartido no se ejecuta: se
acreditan targets upstream 3.5.3 y `DT_NEEDED libOpenEXR-3_5.so.34` antes del
CMake principal. El guard rechaza fallback a `3rdparty/openexr` y enlace estático
de `libdeflate.a`. El empaquetador congelado conserva su verificación final.
La aceptación de la rueda y su SCA final siguen pendientes después de compilar.

## Aprobación externa SCA de 95 componentes

El scan de entradas `42b066e1…` cubre 89 APK del snapshot, cinco identidades APK
parciales y NumPy 2.4.6; conserva diez MEDIUM y cero HIGH/CRITICAL/UNKNOWN.
Por sí solo no aprueba el CID nuevo. El recibo histórico `2ce0…` no cubre los
tres inputs nuevos y no se acepta como sustituto.

Raíz emite JSON con estos campos, respaldado por ruta/SHA antes de `approve`:

- `kind: openexr3-sdk-sca-approval-v1`, `verified: true`, `runtime_approved: false`.
- `cid`, `created`, `pid`, `started_at`, `plan_sha256` de la instancia respaldada.
- `prefix_archive_sha256`, `sdk_receipt_sha256`, `sdk_artifact_sha256`,
  `database_sha256` y los seis registros `tools` con `path`/`sha256`.
- `input_sca_receipt_sha256: 42b066e1…`, `input_sca_report_sha256: 8f76eafb…`,
  `sbom_sha256: 9538e956…`.
- `coverage` exacto: `installed_apk: 89`, `partial_apk: 5`, `numpy: 1`, `total: 95`,
  `high_critical: 0`, `unknown_or_unscored: 0`.
- `db` con `sha256: 7520aa80…`, `updated_at` y `next_update` ISO 8601 con zona.
  La hora actual debe estar entre ambas fechas; una DB vencida bloquea.

Se verifica el SHA físico del JSON externo y se conserva además su forma
canónica dentro del recibo approve. Build vuelve a cotejar esa prueba, los hashes
del prefijo y la época actual. Se rechaza una aprobación sólo textual de CID.
La igualdad DB/6 herramientas vincula la instalación nueva con el snapshot
auditado sin afirmar que los headers parciales estén instalados como paquetes.

## Bóveda, recibos y parada

Prefijo exclusivo `MEDIA_ZOU_OPENCV_OPENEXR3_SDK_`. Antes del plan ligado se
requieren `OWNER`, `CODE_PATH`, `PREFIX_PATH`, `CONTROL_PATH`, `OUTPUTS_PATH`,
`WORK_PATH`, `REMOTE_STAGING_PATH`, `LOCAL_SOURCE_SHA256`, `REMOTE_SOURCE_SHA256`,
`PROFILE_SHA256`, `PREFIX_TAR_PATH`, `PREFIX_TAR_SHA256`, `PREFIX_RECEIPT_SHA256`.
El perfil se respalda en `CONTROLLER_B64`; las transiciones agregan
`PREPARED_B64`, `CONTAINER_B64` y `STAGE_RECEIPTS_B64`. La aprobación usa
`SDK_SCA_RECEIPT_PATH` y `SDK_SCA_RECEIPT_SHA256`. No se imprimen credenciales.

Todo prerequisito exige envelope canónico de la etapa correcta, `success is True`
y igualdad con su respaldo. Las etapas existentes no se vuelven a ejecutar.
Cada intento conserva marcador y logs, incluso si falla; no se limpian parciales.
Los logs están acotados a 4 MiB, sin truncar. La pérdida del latido mata el grupo
CLI propio y detiene el CID acreditado; no se usa `taskkill`, nombre ni PID ajeno.

`stop` lee el plan ya respaldado, fuentes y control propios; no necesita TAR,
inputs, compiladores, SDK, SCA ni rueda. Valida el CID actual antes de `stop 5`
y de un eventual `kill` del mismo CID si aún está vivo. Un create no verificado
sólo admite limpieza con intención física y CID fallido respaldado idénticos.
No se elimina el contenedor ni sus datos.

## Verificación local y límites

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -p test_openexr3_sdk.py -v
python -m ruff check docker/kitsu-zou/run_openexr3_sdk.py docker/kitsu-zou/openexr3_sdk_remote.py tests/test_openexr3_sdk.py
```

Las pruebas usan fixtures para NoForeign, corrupción de TAR, etapa/respaldo,
aprobación por época/DB/cobertura, timeout y parada independiente. No ejecutan
Docker, SSH, CMake, compiladores ni una bóveda real. La promoción por FD,
los límites del kernel y la instalación Linux necesitan ejecución real de raíz;
los fixtures no la sustituyen. La SCA del SDK tampoco acredita el runtime final.
