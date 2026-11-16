# Segundo intento de compilación tras reanudar el SDK

Esta unidad nueva conserva las cinco fuentes, perfil, fallo y parciales de
compile V1. No se ejecutó SSH, Docker, SDK o compilación al implementarla.
Su único CID sigue siendo
`f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d`.

## Cambios cerrados respecto de V1

- Se quita la exportación `PROFILE=release` y se ejecuta `unset PROFILE`
  inmediatamente antes de `make`. PostgreSQL añade PROFILE a los flags C;
  el fallo real de V1 fue `gcc: error: release: linker input file not found`.
- Cargo sigue recibiendo `--profile release`; conserva los mismos RUSTFLAGS,
  dos jobs, fat LTO, `.symtab`, target musl y presupuesto de 1800 s con reserva
  de limpieza de 20 s. No se amplían memoria, CPU, procesos o red.
- WORK pasa a `/work/.compile-agency-v2`, outputs a `/outputs/compile-v2`,
  SQLite a `/opt/agency-sqlite-v2` y target a
  `/work/.compile-agency-v2/target`. No reutiliza objetos o biblioteca de V1.
- El prefijo de bóveda es `MEDIA_POSTGRES_IMMICH_COMPILE_V2_`; no sustituye
  los campos anteriores ni el start histórico del SDK V4.
- Exige exactamente la instancia del recibo RESTART respaldado por raíz.
  Los cinco recibos SDK antiguos siguen comparándose literalmente y son
  historia. El PID/StartedAt vivo debe coincidir con el reinicio nuevo.

Se mantiene el origen SQLite 3.54.1 y la demostración de enlace dinámico:
`LIBSQLITE3_SYS_USE_PKG_CONFIG=1`, `SQLITE3_DYNAMIC=1`, STATIC sin definir,
sourceid/opciones de la biblioteca realmente cargada, build-script output
exclusivo del prefijo nuevo, SONAME `libsqlite3.so.0` junto a vchord.so,
RUNPATH literal `$ORIGIN`, símbolos SQLite indefinidos en vchord y ninguno
definido. Se conservan pgvector 0.8.5, VectorChord 1.1.1, sus controles/SQL y
las licencias, igual que en V1. No cambia ninguna fuente upstream ni checksum.

## Reanudación requerida y plan local

La unidad resume V1 está fijada por perfil
`ad82bc28d9b5770ecfb1c2753de16177e4058ffd9a924a7671d1e211a1b4f939`
y ejecutor
`f878b9dc1f9fdfec0bf61d53ed1210a0a3bbb713ee4e8c759aae29c4943755fc`.
Compile V2 rechaza un RESTART ausente, fallido, de otro CID, de otra fuente,
sin prueba instalada o que reutilice el StartedAt antiguo.

Raíz ya ejecutó la reanudación de forma separada y respaldó su envelope;
esta unidad sólo lee ese respaldo. El host debe conservar los bytes de
`resume-v1-restart.json`, `resume-v1-stopped.json` y
`resume-v1-sdk-proof.json`. Se comparan con el RESTART_B64 literal y sus SHA
antes y después de compile; el fallo V1 debe seguir teniendo SHA canónico
`5878733d8f7d09e3f1e7a2ff65df6f8a6c7d1318b74442da77672492b0a99d6d`.

El modo predeterminado sólo lee metadata/bóveda local. Tras el reinicio
respaldado, genera un perfil nuevo EXCL:

```powershell
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/compile_local_v2.py plan --output 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\compile-v2-profile.json'
```

Antes de aplicar, raíz respalda con el prefijo nuevo `PROFILE_B64`,
`PROFILE_SHA256`, `LOCAL_SHA256`, `REMOTE_SHA256` y `RECIPE_SHA256`.
El perfil liga fuentes, plan SDK, cinco recibos históricos, wrapper/raw de
features y el RESTART_B64 actual. No imprime ni transmite credenciales.

Sólo raíz podrá ejecutar el comando siguiente tras revisar esos pins:

```powershell
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/compile_local_v2.py apply --profile 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\compile-v2-profile.json' --expected-profile-sha256 SHA_RESPALDADO --root-update-vault
```

No se ejecutó apply al desarrollar esta unidad. El CLI conserva y relee el
envelope en `MEDIA_POSTGRES_IMMICH_COMPILE_V2_RECEIPT_B64`. Si falla el proceso,
la verificación del recibo o su respaldo, intenta detener sólo el CID propio
mediante NoForeign, sin depender de outputs escribibles. No reintenta,
reinstala, crea otro contenedor ni avanza a SQL o a imagen final.

## Verificaciones y límites

Las pruebas heredadas conservan los rechazos ELF, SQLite/sourceid, pérdida
de proof, timeout/limpieza, protocolo fragmentado y plan sin SSH. La regresión
ejecuta únicamente declaraciones de entorno con Bash: V1 filtra `release` y
V2 lo retira. No ejecuta make, Cargo ni el cuerpo de ninguna receta. También
exige el reinicio respaldado y rechaza la instancia antigua.

La compilación real, clausura ABI de la imagen base, carga SQL y SCA completa
del runtime continúan pendientes. La lectura estática o un resultado limpio
de los paquetes Alpine no acreditan por sí solos las bibliotecas custom.
