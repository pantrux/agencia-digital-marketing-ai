# Reanudación explícita del SDK tras el fallo de pgvector

Esta unidad no ejecutó SSH, Docker, SDK o compilación durante su preparación.
Sólo admite el CID completo
`f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d`.
Conserva V4, compile V1, sus recibos, los 78 inputs y las salidas parciales.

El fallo real de compile V1 tiene recibo `5878733d8f7d09e3f1e7a2ff65df6f8a6c7d1318b74442da77672492b0a99d6d`
y log `224e13a408ee7773965da41f208fa72f1317acda574f0e2f52cc8bbb27b9fe40`.
SQLite custom pasó; `make` de pgvector recibió la palabra `release` como flag
por `PROFILE=release`. Cargo no llegó a iniciarse. PostgreSQL documenta este
consumo de PROFILE en su [Makefile 16.15](https://raw.githubusercontent.com/postgres/postgres/REL_16_15/src/Makefile.global.in).
La corrección de compilación pertenece a una unidad V2 separada.

## Plan local y respaldo

El modo predeterminado sólo lee evidencia/bóveda local mediante el parser
congelado. Genera un perfil nuevo EXCL dentro de la raíz privada existente:

```powershell
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/resume_sdk.py plan --output 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\resume-v1-profile.json'
```

Antes de contactar la VM, raíz respalda con prefijo
`MEDIA_POSTGRES_IMMICH_RESUME_V1_`: `PROFILE_B64`, `PROFILE_SHA256`,
`LOCAL_SHA256` y `REMOTE_SHA256`. El perfil incluye los cinco recibos SDK
históricos y el fallo compile V1 ya respaldado. No sustituye ningún B64 V4.

Sólo raíz ejecutará los siguientes comandos, con Paramiko 5.1.0, SSH/pin y
helper de bóveda existentes. Estos comandos no se ejecutaron al implementar:

```powershell
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/resume_sdk.py observe-stopped --profile 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\resume-v1-profile.json' --expected-profile-sha256 SHA_RESPALDADO --root-update-vault
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/resume_sdk.py restart --profile 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\resume-v1-profile.json' --expected-profile-sha256 SHA_RESPALDADO --root-update-vault
```

## Transiciones y comprobaciones

`observe-stopped` sólo consulta Docker y archivos propios. Exige NoForeign V4
completo, Estado exited/PID 0, parada limpia sin OOM, mismo Created y StartedAt
históricos, control/devino/Store78 y recibos SDK/grafo iguales, fuente/perfil
compile V1 y su log intactos. Escribe `resume-v1-stopped.json` nuevo en control
y raíz respalda/relee su envelope en `STOPPED_B64`. No inicia el contenedor.

`restart` rechaza la ausencia de ese respaldo y cualquier deriva del estado
observado. Marca la transición EXCL antes de `docker start` del único CID.
El hold original valida sus 78 inputs, rechaza Git y espera hasta 7200 s;
retorna antes de la rama de instalación. No se reinstala ni se vuelve a
ejecutar sdk/graph. Se observa un StartedAt nuevo y PID positivo, con límites
físicos de 4 CPU/12 GiB/swap adicional 0/pids 256 y red none. Se compara el
StartedAt, porque el sistema operativo puede reciclar un número de PID.

Una verificación Python fijada por SHA, dentro del CID, lee sin ejecutar:

- Las versiones exactas de los 110 APK frente a los 45 de la base y los 65
  APK declarados, con SHA de la copia de metadata inicial.
- Todo el árbol VectorChord original frente al TAR fijado, permitiendo sólo
  el parche xtask 9055 y la configuración Cargo offline literal ya aprobados.
- Los archivos vendor frente al TAR inmutable; faltantes, extras, enlaces y
  bytes cambiados impiden continuar.
- Cada payload instalado de rustc/cargo/rust-std frente a `manifest.in` y los
  tres TAR.XZ fijados. No ejecuta `rustc`, `cargo` ni scripts de construcción.
- Un catálogo de hash de los parciales V1 y del target antiguo, incluida la
  única referencia relativa SQLite permitida. Revalida la sonda SQLite V1
  completa sin volver a cargar o compilar esa biblioteca.

El inventario acredita payloads Rust declarados, no afirma que los metadatos
generados por el instalador formen parte de esos archivos originales. Los
paquetes APK se contrastan por inventario/versiones; no se afirma una nueva
auditoría de cada archivo ejecutable nativo de los 110 paquetes.

Guarda `resume-v1-sdk-proof.json` en outputs y su SHA/catálogos en el recibo
host `resume-v1-restart.json`. Los hashes anteriores no se reescriben. Raíz
debe respaldar y releer `RESTART_B64` antes de compile V2; el start V4 antiguo
permanece como historia, no se presenta como la instancia reiniciada.

## Límites y fallo

El verificador tiene plazo de 180 s; el helper completo se interrumpe a 240 s,
con margen para la parada propia y cliente limitado a 270 s. Capturas/logs
4 MiB, heartbeat/EOF y recolector POSIX son los congelados de V4. Ante fallo
posterior a iniciar la transición, se detiene sólo el CID con NoForeign;
la parada de emergencia necesita únicamente control protegido, no outputs.
No hay reintento, limpieza global, Docker create, SQL ni cambios en inputs.
El log privado `resume-v1-verification.log` conserva diagnóstico seguro.

Estas pruebas locales verifican guardas/bytes/transportes con fixtures; no
acreditan la reanudación ni el inventario real del recurso. Esa evidencia la
producirá raíz con los comandos anteriores después de revisar los pins.
