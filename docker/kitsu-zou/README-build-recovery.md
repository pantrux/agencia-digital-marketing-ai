# Recuperación OpenCV: preflight criptográfico en el host

El primer build se detuvo en 0,319 s antes de CMake: el packager congelado
necesitó `cryptography` para verificar RSA y ese módulo no forma parte del SDK.
El recibo fallido `52a01b20…`, su log `228c01bf…`, la receta `fc3341fa…`, el
kit `755ea032…`, el auditor y los recibos anteriores permanecen intactos.

Root ejecutó el packager `831a2804…` con `--verify-inputs` en el host contra
el manifiesto aprobado `1207f584…`, con `Store.verify` antes y después. La
evidencia `phase4-zou-opencv-inputs-host-verification-real-v1.json`, SHA
`b7e79bb8…`, acredita RSA-SHA256 del índice, Q1 del control, SHA256 del payload
y de los inputs, 70 APK, dos APK de headers y 271 aristas de clausura. Esta
unidad adopta sólo esos bytes fijados y no repite RSA ni añade dependencias.

## Archivos y alcance

La unidad tiene cuatro archivos nuevos: `recover_opencv_build.py`,
`opencv_build_recovery_remote.py`, `tests/test_opencv_build_recovery.py` y este
README. El perfil pequeño conserva referencias físicas y hashes; no duplica
el plan SDK. Importar ambos módulos no abre conexiones ni ejecuta acciones.
Los soportes congelados se cargan desde bytes verificados, sin usar `.pyc`.

El único CID admisible es `1be1ec1148e2a85b638f1c8f9003726acde7f65a1421e220930aa3c494a9907b`.
Se conservan imagen, nacimiento, cinco binds, red aislada, capacidades,
2 CPU, 6 GiB y 128 procesos mediante el contrato NoForeign original.
No se instala, actualiza ni resuelve ningún paquete. No hay SQL, NAS,
puertos nuevos, eliminación de datos ni reconstrucción del SDK.

## Etapas y respaldo

`plan` es local, no lee `.env` y no conecta. Root revisa el perfil, respalda
sus pins y ejecuta cada etapa por separado. Cada recibo nuevo se conserva
como JSON local EXCL y se agrega mediante el helper de bóveda fijado a
`MEDIA_ZOU_OPENCV_BUILD_RECOVERY_V1_RECEIPTS_B64`. La siguiente etapa exige
el mismo envelope canónico positivo en host y bóveda. Un fallo también se
conserva; no habilita replay ni se transforma en éxito.

| Etapa | Condiciones y resultado |
| --- | --- |
| `verify-host` | Exige fallo original respaldado con cleanup detenido, CID parado/PID 0, inputs y raíces físicos originales, aprobación y SCA vigentes. Adopta la prueba host por SHA, verifica sus campos y vuelve a verificar Store. Crea sólo namespaces propios EXCL, conserva dev/ino y marcadores en su recibo. |
| `observe-stopped` | Exige `verify-host` positivo respaldado, NoForeign y nacimiento original. Comprueba que `build`, `stage`, `wheels`, `home` y `tmp` sigan ausentes; un enlace con cualquiera de esos nombres también bloquea. |
| `restart` | Exige ambas etapas anteriores respaldadas. Inicia únicamente el mismo CID detenido y acredita PID/StartedAt nuevos, cgroups y atestación de lectura de 89 APK y seis herramientas. No reescribe `sdk-receipt.json`. Root respalda este recibo antes de build. |
| `build` | Exige los tres recibos positivos respaldados y la misma instancia nueva. Revalida prueba, receta, identidades, inputs, SDK y SCA. Compila con límite y heartbeat; registra logs y wheel. No acredita importación nativa o SCA final de la rueda. |
| `stop` | Sólo requiere prepare/create originales, layout/control propio y NoForeign. No abre inputs ni depende de prueba host, DB SCA, SDK o build. Detiene sólo el CID exacto. Puede crear su namespace privado de recibo si todavía no existe. |

Los namespaces nuevos son `<control-original>/build-host-verify-v1` y
`<outputs-original>/.build-host-verify-v1`, root:root 0700, archivos 0600.
No se cambian mounts ni se incorporan esos datos al kit readonly. Los recibos
originales y `.build-started` permanecen donde estaban.

## Delta cerrado de la receta

La receta derivada es un dato generado desde el SHA original. Sólo cambia:

1. `recipe_dir=/inputs/code/docker/kitsu-zou`, para seguir usando el packager
   congelado aunque la receta nueva esté bajo `/work`.
2. La llamada `--verify-inputs` se sustituye por una lectura stdlib acotada
   de la prueba host y cotejo de su SHA externo `b7e79bb8…`, manifiesto,
   packager y modo de firma. El controlador exige además el recibo de adopción
   respaldado y las identidades actuales antes de reiniciar y compilar.

El bloque desde `# SDK ya preparado` hasta el empaquetado es byte idéntico:
se conservan todos los flags CMake, comandos build/install, validadores del
packager y la licencia. El wrapper constante mantiene `umask 077` y `exec`.
No se añade shim de cryptography ni se omite la verificación criptográfica.

El presupuesto de build es 1800 s; el timeout del proceso se calcula con el
tiempo restante y reserva 20 s para cleanup, con máximo 1780 s. El recolector
congelado usa sesión/grupo propios y heartbeat. Ante cierre del CLI, timeout
o error se vuelve a acreditar y detener sólo el CID; cancelar `docker exec`
no se considera suficiente para terminar sus procesos internos. Una alarma
de emergencia del proceso host añade un techo externo de 1820 s.
Los logs combinados se limitan a 4 MiB, sin aceptar salida truncada.

## Pins y comandos de Root

Root respalda previamente la familia `MEDIA_ZOU_OPENCV_BUILD_RECOVERY_V1_`:
`OWNER`, `PROFILE_PATH`, `PROFILE_SHA256`, `LOCAL_SHA256`, `REMOTE_SHA256`,
`CONTROL_PATH`, `DATA_PATH`, `HOST_PROOF_PATH`, `HOST_PROOF_SHA256`,
`FAILED_BUILD_PATH` y `FAILED_BUILD_SHA256`. Los dos últimos pares de evidencia
ya fueron respaldados por Root; el perfil debe usar exactamente sus rutas y
hashes. La CLI no escribe secretos nuevos y sólo Root habilita el escritor.

```powershell
python docker/kitsu-zou/recover_opencv_build.py plan --output .tools/evidence/phase4-zou-opencv-build-recovery-v1-profile.json
```

Las acciones usan el Python aislado con Paramiko 5.1.0, clave y pin existentes,
sin agentes ni fallback. `$shaRevisado` es el SHA del perfil que Root revisó:

```powershell
.tools/venvs/synology-audit/Scripts/python.exe docker/kitsu-zou/recover_opencv_build.py verify-host --profile .tools/evidence/phase4-zou-opencv-build-recovery-v1-profile.json --expected-profile-sha256 $shaRevisado --root-update-vault
```

Después de verificar cada recibo respaldado, se sustituye sólo `verify-host`
por `observe-stopped`, después `restart` y finalmente `build`. `stop` es una
acción separada con los mismos pins. Una etapa existente no se repite; los
parciales se conservan para diagnóstico y recuperación explícita.

## Pruebas y límites actuales

Las pruebas públicas usan fixtures, sin SSH, Docker ni SDK. Cubren prueba host
negativa/divergente, envelopes de etapa, parciales, nacimiento, SCA caducada,
89 APK/seis herramientas, deadline con reserva, timeout y cleanup del CID,
parada sin SDK, carga por bytes, EXCL y framing/exit coherentes. En Windows no
se presenta ese fixture como una ejecución POSIX real de reinicio/cleanup.
La receta derivada también se analiza con Bash/ShellCheck sin ejecutarla.

```powershell
.tools/venvs/synology-audit/Scripts/python.exe -W error::ResourceWarning -m unittest discover -s tests -p test_opencv_build_recovery.py -v
```

El fallo inicial y la verificación RSA del host son hechos de Root. Esta
unidad entrega fuentes y un plan; no afirma que el reinicio o build nuevos,
la rueda final, su importación o la imagen de Zou hayan pasado aceptación.
