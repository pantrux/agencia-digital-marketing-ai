# Recuperación v2: sistema operativo del SBOM

La recuperación v1 ejecutó Trivy y fue rechazada: el reporte identificó OS
`none` y sólo NumPy, sin los 91 APK requeridos. No se aprobó el SDK. Su código
`9ce903e3…`, referencias, perfil, intención, CDX, reporte y logs permanecen
intactos, con sus hashes en `opencv-sdk-scan-recovery-v2-refs.json`.

Esta revisión usa cuatro archivos nuevos y un perfil pequeño por referencias.
Reutiliza la captura válida `fbdb485a…` y el plan original `747a33d0…`; no copia
ni repite la captura, no abre SSH y no ejecuta el SDK. No cambia la función
congelada `make_sbom` ni las fuentes del auditor o recuperación anteriores.

## Corrección y rechazo cerrado

El BOM físico conserva las 92 libraries autenticadas y añade exactamente:
`type=operating-system`, `name=wolfi`, `version=VERSION_ID` del os-release
capturado y `bom-ref=os:wolfi`. El descriptor no lleva PURL. La identidad se
lee del dato físico protegido por el SHA de captura, sin inferirla de un tag.

Antes de NativeJob se exige un solo descriptor con esos cuatro campos y un
total de 93 componentes: OS + 91 APK + NumPy. BOM sin OS, duplicado, identidad
divergente o PURL de paquete en el descriptor producen rechazo.

Trivy recibe y se calcula el SHA del BOM físico completo. Sólo para
`assess_report` y el cotejo de origen/PURL se crea una vista nueva de componentes
`type=library`; el OS no se trata como un paquete ni se elimina del archivo.
El reporte debe acreditar `Metadata.OS.Family=wolfi` y `Name=VERSION_ID` real.
Otros campos informativos del OS reportado se conservan. OS `none`, versión
distinta, paquetes omitidos o fuentes/PURL divergentes impiden la aprobación.

Permanecen los mismos hasher de firmas separadas, lecturas acotadas, controles
de DB vigente, NativeJob 180 s/2 GiB/ocho procesos/1 MiB y flags offline. Sólo
se clasifican los tres WARN genéricos exactos, después de acreditar todas las
identidades. WARN desconocido/ERROR/FATAL bloquean; ningún finding o severidad
se elimina o modifica. HIGH/CRITICAL mantiene `verified=false`.

## Prefijo nuevo y respaldo de Root

El prefijo nuevo es la ruta absoluta de:
`.tools/evidence/phase4-zou-opencv-sdk-installed-755ea032b52a767f-v2`.
Se usa sólo cuando no tiene salidas anteriores. V1 no se limpia ni se reutiliza.
La captura sigue en su ruta v1 original; no se crea otra captura en el prefijo v2.
La intención y el recibo nuevos registran que el scan de recuperación v1 se
ejecutó y fue rechazado, además del fallo preflight del auditor original.

```powershell
python docker/kitsu-zou/recover_opencv_sdk_scan_v2.py plan --output .tools/evidence/phase4-zou-opencv-sdk-scan-recovery-v2-profile.json
```

Root revisa el perfil y respalda la familia
`MEDIA_ZOU_OPENCV_SDK_SCAN_RECOVERY_V2_` con los sufijos `PROFILE_PATH`,
`PROFILE_SHA256`, `LOCAL_SHA256`, `REFS_SHA256` y `REPORT_PREFIX` antes de usarla.
La unidad no escribe `.env` ni emite `SDK_SCA_B64`.

```powershell
python docker/kitsu-zou/recover_opencv_sdk_scan_v2.py scan --profile .tools/evidence/phase4-zou-opencv-sdk-scan-recovery-v2-profile.json --expected-profile-sha256 $shaRevisado --root-authorized
```

El recibo v2 conserva reporte/logs completos, hashes, clasificaciones de WARN,
orígenes/PURL y OS reportado. Un fallo deja sus parciales sin replay ni borrado
automáticos. Root decide la emisión posterior del recibo SCA del SDK; un
resultado de esta unidad no aprueba una rueda o imagen final.

## Verificación disponible

Los tests públicos sólo usan fixtures y no dependen de `.tools`. Cubren OS
ausente/duplicado/divergente, reporte OS none y la vista library sin mutar el
BOM físico; HIGH sigue presente. Los controles anteriores permanecen congelados.

Se ejecutó además una comprobación privada **sólo de datos**: BOM nuevo desde
la captura física contra el reporte histórico SDK-bash que sí reconocía
91 APK + NumPy. Pasaron OS, nombres/versiones, fuentes, arquitectura y PURL.
Evidencia: `.tools/evidence/phase4-zou-opencv-sdk-scan-recovery-v2-compatibility.json`,
SHA `008d70790c848193ffba830d9bc0baecabe5abe4c5e015e34da9e132eec8d8a8`.
Es una prueba de compatibilidad, sin NativeJob/Trivy actual ni aprobación del SDK.

```powershell
python -m unittest discover -s tests -p test_opencv_sdk_scan_recovery_v2.py -v
```
