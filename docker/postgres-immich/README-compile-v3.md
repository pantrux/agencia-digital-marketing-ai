# Compile V3 con el formatter oficial acreditado

Cinco fuentes nuevas, sin editar compile V1/V2, U1, el aumento, V4 o los 78
inputs. El agente sólo preparó y probó estas fuentes localmente; no ejecutó
SSH, SDK, Docker o compilación. Los dos fallos anteriores permanecen como
historia y sus objetos no se reutilizan.

## Cambio respecto de V2

V2 pasó pgvector y llegó a pgrx-bindgen, donde faltó rustfmt. V3 conserva los
flags y la corrección `unset PROFILE` antes de make; añade únicamente el
formatter oficial y su búsqueda de driver aprobada, más gates/namespace del
tercer intento. No se modifica pgrx, el grafo, vendor, Cargo.lock o checksums.

| Elemento | Valor de V3 |
| --- | --- |
| WORK | /work/.compile-agency-v3 |
| Outputs | /outputs/compile-v3 |
| Target Cargo | /work/.compile-agency-v3/target |
| SQLite custom | /opt/agency-sqlite-v3 |
| RUSTFMT | /opt/agency-rustfmt-v1/bin/rustfmt |
| LD_LIBRARY_PATH privado | /opt/agency-sqlite-v3/lib:/opt/agency-rust/lib |
| Prefijo de bóveda | MEDIA_POSTGRES_IMMICH_COMPILE_V3_ |

Mantiene dos jobs Cargo/make, 4 CPU/12 GiB/swap adicional 0/pids 256/red none,
1800 s con reserva de 20 s, flags Rust originales más RUNPATH literal
$ORIGIN, strip none y perfil release con LTO fat. Las rutas V1/V2 no se
adoptan ni se limpian. CARGO_HOME conserva sólo la configuración offline
fijada y el vendor previamente verificado; el target de compilación es nuevo.

Se comprueban el binario rustfmt SHA
bc01859cf8d5b93e912b62d711500a30a19da8ad2fe52ba7c2d1c5efb67b1c3f y el driver
SDK3 SHA 626e0c6efaac2ce87441a7fffa7ee53f66c3a90df282803420771698d33b2e2f
antes de ejecutar la receta. No se duplica el driver, no se usa cargo-fmt y
no se modifica el entorno del host.

## Aumento previo obligatorio

El perfil del aumento debe tener SHA
60f6d8b0db156abeb0fcd0c73e764484bb4a8530f7b1c9aa9a705eea55b28a80,
el ejecutor 997877a7ae20478b7c6cb6afd3f7f2432a325c7aa319d2004508fffdbf3724dc
y el manifiesto 58de0656a6f05b76f3845449b8ec9a946abf62485881f976df09f2bdd1dab64f.
V3 exige los envelopes RESTART_B64 y AUGMENT_B64 positivos de esa unidad,
respaldados por raíz. No basta con que exista el archivo rustfmt.

El aumento acredita la versión reportada
`rustfmt 1.11.0-beta (01ddda61fa 2026-11-04)`, paquete 1.11.0-beta.10, el
driver y una prueba real de formato con rechazo inicial/aceptación final.
Se rechazan versiones distintas aunque se recalculen hashes del envelope.

La instancia viva debe coincidir exactamente con el contenedor/PID/StartedAt
del aumento y su reinicio. Los start/SDK/grafo históricos siguen comparándose
literalmente. Antes y después del build se cotejan los recibos host del
aumento, su prueba instalada, la prueba del formatter y ambos fallos previos.
Todo se conserva; no se reescribe un B64 anterior para fingir el arranque.

## Perfil y aplicación de raíz

Una vez respaldado el aumento, este comando genera sólo un perfil local
canónico EXCL, sin SSH o ejecución de SDK:

```powershell
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/compile_local_v3.py plan --output 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\compile-v3-profile.json'
```

Raíz respalda PROFILE_B64, PROFILE_SHA256, LOCAL_SHA256, REMOTE_SHA256 y
RECIPE_SHA256 bajo el prefijo nuevo antes de aplicar. El perfil liga ambas
transiciones reales del aumento y todas las fuentes/recibos anteriores.

Sólo raíz, después de revisar los pins, podrá ejecutar:

```powershell
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/compile_local_v3.py apply --profile 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\compile-v3-profile.json' --expected-profile-sha256 SHA_RESPALDADO --root-update-vault
```

El CLI conserva y relee MEDIA_POSTGRES_IMMICH_COMPILE_V3_RECEIPT_B64. No
reintenta ni avanza a SQL, SCA o construcción de imagen. Ante fallo del
proceso, recibo o respaldo, sólo se detiene el CID propio con NoForeign,
independientemente de los outputs escribibles, y se conservan parciales.

## Evidencia requerida y límites

Conserva PostgreSQL 16.15, pgvector 0.8.5, VectorChord 1.1.1 y SQLite 3.54.1.
SQLite debe acreditar sourceid/opciones reales, build-script output del
prefijo V3 exclusivo, SONAME libsqlite3.so.0, DT_NEEDED dinámico, símbolos
SQLite indefinidos y ausencia de implementación C dentro de vchord.
Libsqlite3.so.0 queda junto a vchord.so; el RUNPATH es $ORIGIN. No se usa
SQLITE3_STATIC=0: las opciones estáticas se mantienen sin definir.

El catálogo verifica ELF amd64/musl y conserva .symtab, hashes, controles y
SQL de instalación, licencias y el log privado. Las pruebas locales cubren
los rechazos heredados, formatter/driver/formato y gating de recibos con
proveedores controlados. No acreditan un build real. La clausura ABI contra
la base runtime, carga SQL, pruebas con UID 70 y SCA completa siguen pendientes.
