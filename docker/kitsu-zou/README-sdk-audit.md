# Auditoría del SDK OpenCV instalado

Esta unidad lee la exportación real como datos. No instala paquetes, ejecuta
SDK/CMake/compiladores, modifica el CID o emite `SDK_SCA_B64`. Sus pruebas
locales no acreditan una exportación ni una ejecución Trivy reales. Root
revisa y respalda los pins antes de usarla.

Las cinco fuentes son `audit_opencv_sdk.py`, `opencv_sdk_audit_remote.py`,
`opencv_sdk_sbom.py`, `tests/test_opencv_sdk_audit.py` y este documento. El
controlador SDK revisión 2, su receta, empaquetador y kit permanecen intactos.

## Datos observados y límites

El plan se liga a los recibos positivos respaldados `prepare/create/start/sdk/export`.
Una creación vacía o instalación fallida no permite generar el plan ligado.
El lector acredita host/layout, directorios, inputs y mounts con el controlador
congelado. Docker sólo se usa para `inspect` antes y después: nunca llama a
`exec`, `start`, `stop`, `export`, build o instalación.

Abre `control/sdk-rootfs.tar` por FD NoFollow, UID/GID 0, modo 0600 y enlace
único; verifica tamaño hasta 3 GiB y SHA de todos sus bytes contra el recibo.
Dev/inode, tamaño y tiempos deben permanecer estables. Analiza el TAR entero,
con límites de miembros/tamaños y terminación nula completa, sin extraerlo.
Sólo transporta DB APK (hasta 4 MiB), os-release (16 KiB), manifiesto y pruebas
de procedencia; respuesta máxima 8 MiB y plazo remoto 180 s, cliente 190 s.

`etc/os-release` puede ser regular o el enlace relativo exacto
`../usr/lib/os-release` a un miembro regular. Otros enlaces del sistema se
recorren como metadatos y no se ejecutan ni extraen. Se rechazan duplicados
de los tres miembros objetivo, enlaces objetivo distintos, truncamiento y
contenido no nulo posterior al final TAR. No se transfiere el TAR completo.

Para usrmerge, sólo se admite `usr/lib/apk/db/installed` cuando el propio TAR
acredita el enlace simbólico exacto `lib → usr/lib`. Se rechazan ambas DB a la
vez, duplicados de `lib`, enlaces diferentes o rutas físicas contradictorias.
No se presume usrmerge por el nombre de un paquete. La captura conserva la
ruta física y la prueba del alias.

La DB debe contener exactamente **89 instalados: 19 base + 70 managed**,
con nombre/versión/arquitectura/licencia coincidentes con el plan. Si la DB
omite `o`, el origen se conserva desde el registro autenticado del plan y se
marca esa procedencia; nunca se sustituye por el nombre del subpaquete.
Wolfi y `VERSION_ID` proceden del os-release real. Se agregan por separado **dos APK
de headers parciales** y **NumPy 2.4.6 parcial**, cuyos árboles RO se revalidan
contra prepare. El lector reautentica el índice Wolfi RSA256 y conserva los
registros de origen/licencia de los headers; no los presenta como instalados.
Se conservan selección, SHA de APK/wheel y aviso de licencia NumPy completo
en el manifiesto capturado. No se atribuyen archivos omitidos de PyImath.

El SBOM distingue `instalado-verificado`, `headers-parciales-verificados` y
`headers-numpy-parciales-verificados`. La cobertura Trivy exige una biyección
exacta de **91 APK + NumPy**, sin duplicados, omisiones o cero paquetes. Todos
los findings permanecen en el reporte; un HIGH/CRITICAL impide `verified=true`,
aunque sólo afecte herramientas o headers que no irán al runtime.

## Secuencia explícita de Root

El plan puro no lee la bóveda ni abre SSH/NativeJob:

```powershell
python docker/kitsu-zou/audit_opencv_sdk.py plan
```

Tras los cinco recibos positivos, Root respalda
`MEDIA_ZOU_OPENCV_SDK_AUDIT_ATTEMPT` (`v1`, por ejemplo) y
`MEDIA_ZOU_OPENCV_SDK_AUDIT_REPORT_PREFIX`: ruta absoluta de
`.tools/evidence/phase4-zou-opencv-sdk-installed-<kit_sha16>-<attempt>`.

```powershell
python docker/kitsu-zou/audit_opencv_sdk.py plan --bind-vault --output .tools/evidence/phase4-zou-opencv-sdk-audit-v1-profile.json
```

Root revisa el perfil canónico y respalda `..._AUDIT_PLAN_B64` y
`..._AUDIT_PLAN_SHA256`. Incluye las tres fuentes ejecutables, NativeJob
`dfe20e53…`, Trivy 0.76.2 `dd07adf8…`, plan SDK y recibos. No contiene secretos.
El acceso SSH usa Paramiko 5.1.0 y las identidades/pin canónicos, sin fallback.

```powershell
python docker/kitsu-zou/audit_opencv_sdk.py capture --profile .tools/evidence/phase4-zou-opencv-sdk-audit-v1-profile.json --expected-plan-sha256 $shaRevisado --root-authorized
```

Capture crea exclusivamente `<prefix>.capture.json`. Root respalda el SHA
devuelto en `MEDIA_ZOU_OPENCV_SDK_AUDIT_CAPTURE_SHA256` antes del scan.

```powershell
python docker/kitsu-zou/audit_opencv_sdk.py scan --profile .tools/evidence/phase4-zou-opencv-sdk-audit-v1-profile.json --expected-plan-sha256 $shaRevisado --root-authorized
```

Scan no abre SSH. Sólo ejecuta Trivy mediante NativeJob: 180 s, 2 GiB, ocho
procesos, 1 MiB stdout/stderr y cierre del árbol propio; entorno mínimo sin
credenciales. Usa `.tools/trivy-cache-current-20261106`, `--skip-db-update`,
`--offline-scan` y `--list-all-pkgs`, sin ignores/VEX ni filtros de severidad.
Root coordina la caché única antes de ejecutarlo.

Conserva `.cdx.json`, `.trivy.json`, `.stdout.log`, `.stderr.log` y `.receipt.json`.
Contrasta hash de DB y metadata antes/después y exige vigencia en ambos momentos.
Timeout, captura excesiva, cleanup incompleto, warning/error o cobertura distinta
rechazan la verificación. Los parciales quedan preservados sin sobrescribir ni
reintentar automáticamente; otro intento necesita otro prefijo revisado.

El recibo acredita auditoría por identidades instaladas y headers parciales,
sin aprobar una rueda o imagen final. Root revisa alcance, hashes y DB y emite
después, si corresponde, el objeto cerrado `SDK_SCA_B64` del controlador.
El auditor no escribe `.env`, no habilita el derivado y no inicia compilaciones.

## Pruebas locales

Los fixtures comprueban TAR completo, ambos os-release, duplicados/enlaces,
truncamiento/límites, 89 instalados exactos, procedencia/licencias parciales,
biyección, HIGH conservado y rechazo de Job/DB/captura inválidos. No invocan
Trivy ni presentan proveedores controlados como ejecución de infraestructura.

```powershell
python -m unittest discover -s tests -p test_opencv_sdk_audit.py -v
```
