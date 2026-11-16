# Recuperación del scan del SDK OpenCV

Esta unidad recupera exclusivamente el preflight local del scan. Mantiene
intactos el auditor v2, su plan `747a33d0…`, la captura `fbdb485a…` y el diagnóstico
`86a602ab…`. No repite SSH, captura, exportación o ejecución del SDK. La captura
existente acredita el TAR `52ecd3e4…` de 680.871.936 bytes.

El fallo v2 ocurrió antes de crear SBOM, reporte, logs o NativeJob. Python
3.12.1 en Windows puede devolver dos `st_ctime_ns` diferentes para el mismo
archivo mediante fstat y stat, aunque ambos permanezcan estables dentro de su
API. El diagnóstico conservado contiene ese contraste para DB y ejecutable.

## Hasher separado y estricto

El scanner nuevo usa una firma fstat antes/después y una firma lstat
antes/después. Cada una conserva su propio ctime, modo y atributos. Entre APIs
sólo se comparan dev, ino, tamaño, mtime, nlink y tipo. No se elimina la
comprobación de ctime dentro de cada API ni se normalizan relojes diferentes.

Se exigen archivo regular, enlace único, ausencia de enlaces/reparse points,
límite de tamaño y conteo exacto de bytes. El descriptor permanece abierto
hasta cotejar el nombre. Truncamiento, crecimiento o cambios de cualquiera de
las firmas producen rechazo. Los resultados de DB y herramienta se comparan
de nuevo íntegramente después de Trivy, cada API con la misma API.

`recover_opencv_sdk_scan.py` carga exactamente los bytes fijados del auditor
v2 para sus validadores y SBOM, sin usar `.pyc`. No llama a `scan` v2 ni cambia
sus funciones. Copia explícitamente el bloque de ejecución necesario y cambia
el hasher, la procedencia y la clasificación acotada de mensajes. Conserva NativeJob 180 s/2 GiB/ocho procesos/1 MiB,
Trivy 0.76.2, la DB vigente, flags offline/sin actualización, cuatro límites
y biyección de 91 APK + NumPy. No cambia severidades ni oculta findings.

## Advertencias conocidas y pruebas de identidad

El log histórico contiene tres mensajes genéricos exactos: advertencia de SBOM
externo, recomendación de generarlo con Trivy y aviso de severidad tomada de
otro proveedor. Se clasifican únicamente esos tres textos literales; no se
silencian sus líneas ni se cambia una severidad. ERROR/FATAL o WARN desconocido
siguen rechazando el scan, igual que timeout o cleanup incompleto.

La clasificación sólo se acepta después de una biyección positiva y del cotejo
exacto de Name/Version/SrcName/SrcVersion/Arch/Identifier.PURL para los 91 APK
contra las propiedades autenticadas del SBOM. NumPy exige Name/Version/PURL,
los campos que esta versión realmente reporta. Omitir o alterar origen, versión,
arquitectura o PURL detiene la verificación. HIGH/CRITICAL se conservan y siguen
produciendo `verified=false` aunque las tres advertencias sean conocidas.

El recibo conserva clasificación, mensaje y hash de cada línea, hashes de
stdout/stderr completos y prueba de cobertura de origen. El reporte y los logs
brutos permanecen intactos. Esta comprobación responde al límite documentado
de [propiedades de SBOM externo](https://trivy.dev/docs/v0.76/guide/target/sbom/)
y conserva la [selección de severidad del proveedor](https://trivy.dev/docs/v0.76/guide/scanner/vulnerability/#severity-selection).
No equivale a una validación general de cualquier SBOM o aviso futuro.

## Referencias y autorización de Root

`opencv-sdk-scan-recovery-refs.json` fija rutas/SHA del plan, captura y
diagnóstico originales, hashes de los tres módulos v2 y SHA del TAR. El perfil
nuevo contiene sólo esas referencias, el SHA del archivo de referencias y el
del nuevo scanner; no duplica la captura de 1,12 MB ni el plan SDK.

```powershell
python docker/kitsu-zou/recover_opencv_sdk_scan.py plan --output .tools/evidence/phase4-zou-opencv-sdk-scan-recovery-v1-profile.json
```

Plan no abre la bóveda o red y no ejecuta Trivy/SDK. Root revisa y respalda:

- `MEDIA_ZOU_OPENCV_SDK_SCAN_RECOVERY_PROFILE_PATH`
- `MEDIA_ZOU_OPENCV_SDK_SCAN_RECOVERY_PROFILE_SHA256`
- `MEDIA_ZOU_OPENCV_SDK_SCAN_RECOVERY_LOCAL_SHA256`
- `MEDIA_ZOU_OPENCV_SDK_SCAN_RECOVERY_REFS_SHA256`

La ejecución posterior exige esos valores, el SHA externo revisado y los pins
de bóveda originales del auditor y captura, que se vuelven a comprobar.

```powershell
python docker/kitsu-zou/recover_opencv_sdk_scan.py scan --profile .tools/evidence/phase4-zou-opencv-sdk-scan-recovery-v1-profile.json --expected-profile-sha256 $shaRevisado --root-authorized
```

Se reutiliza el prefijo `phase4-zou-opencv-sdk-installed-755ea032b52a767f-v1`
sólo si contiene la captura y ninguna otra salida. No se adoptan ni sobrescriben
archivos de un intento anterior. Primero se crea exclusivamente
`.scan-recovery-intent.json`, ligado a los tres hashes históricos y al perfil
nuevo; después se crean SBOM, reporte y logs. El recibo `.receipt.json` declara
la recuperación y que el scan original falló en preflight, sin presentar el
intento v2 como una ejecución positiva.

Un fallo deja los parciales y su intención como evidencia. No hay replay o
limpieza automáticos. Root revisa el recibo y emite, si corresponde, el
`SDK_SCA_B64` del controlador. Este scanner no actualiza `.env`, no habilita
compilaciones ni aprueba una rueda o imagen final.

## Pruebas y límite actual

Las pruebas usan archivos temporales y firmas controladas. Reproducen el
rechazo real de la función v2 con ctime diferente por API, verifican que el
hasher nuevo lo acepta sin modificar v2 y rechazan cambios de las firmas,
puente, tipo/enlace y tamaño, incluida la segunda lectura acotada. También
prueban WARN nuevo, origen omitido/divergente y HIGH conservado. No ejecutan
Trivy, NativeJob, SSH o SDK.

```powershell
python -m unittest discover -s tests -p test_opencv_sdk_scan_recovery.py -v
```
