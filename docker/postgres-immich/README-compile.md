# Compilación acotada sobre el SDK V4 existente

Esta unidad prepara plan/controlador/receta y pruebas. No se ejecutó apply,
Docker, SDK, compilador o SQL durante su implementación. No modifica las
fuentes V4, los 78 inputs, Cargo.lock, vendor ni el estado de la VM/bóveda.

El único CID admitido es
f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d.
Debe conservar la misma instancia running acreditada por start: Created,
PID y StartedAt. Se reutilizan por SHA el controlador V4 685bb8a5 y el
ejecutor a13ab960; NoForeign y los límites físicos siguen siendo 4 CPU,
12 GiB, swap adicional 0 y 256 procesos. Se usan dos jobs Cargo/make para dejar
margen a LTO fat. No se amplían recursos ni se reintenta automáticamente.

## Plan y pins previos a apply

El modo predeterminado sólo lee metadata local y la bóveda mediante el parser
congelado. No abre SSH ni ejecuta SDK. Opcionalmente escribe un perfil nuevo
con EXCL bajo la raíz privada .tools/postgres-immich-build:

```powershell
python -B docker/postgres-immich/compile_local.py plan --output "D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\compile-v1-profile.json"
```

El perfil conserva el plan SDK ligado, los cinco recibos exitosos anteriores,
source SHA, configuración exacta y wrapper completo de features. Exige la
revisión MEDIA_POSTGRES_IMMICH_SDK_V4_FEATURE_REVIEW_STATUS con valor
PG16_SIMD_INIT_CONFIRMED_SQLITE_CUSTOM_LINK_REQUIRED_COMPILE_PENDING.

El wrapper tiene SHA
8e74968c1d6cf30668db4207b27d28432ef6f9cbf81c2d62fc1ed75d18deb8ca;
sus bytes de features tienen SHA independiente
21034e95a883742864ca39d5fa594435ba2a9223e70bdf79eff429cf1d9201c0.
Se comprueban ambos, pgrx0.17 cshim+pg16, simd init y la ausencia de
wasm-bindgen-shared/wasm-bindgen/git2 seleccionados en target.

Antes de apply la raíz respalda estos campos con prefijo
MEDIA_POSTGRES_IMMICH_COMPILE_:

- PROFILE_B64: bytes canónicos íntegros del perfil codificados como Base64.
- PROFILE_SHA256: hash externo de esos mismos bytes.
- LOCAL_SHA256, REMOTE_SHA256 y RECIPE_SHA256: las tres fuentes congeladas.

La aplicación futura corresponde sólo a raíz y exige el entorno aislado
Paramiko 5.1.0, SSH y pin existentes, flag explícito y SHA externo:

```powershell
python -B docker/postgres-immich/compile_local.py apply --profile "D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\compile-v1-profile.json" --expected-profile-sha256 SHA_COMPLETO_RESPALDADO --root-update-vault
```

No se ejecutó este comando. Antes de conectar se exige igualdad completa de
perfil, fuentes, revisión y documentos SDK actuales con la bóveda. Ninguna
credencial se transmite al contenedor ni se imprime.

## Preparación dentro del recurso propio

El helper root verifica layout, dev/ino y marcadores, 78 SHA, artefactos SDK y
grafo, y recibos prepare/create/start/sdk/graph idénticos a los respaldados.
Se archivan perfil y las dos fuentes en el control host, sin sobrescribir.
El directorio .compile-v1-started impide replay. Crea sólo el directorio
/work/.compile-agency-v1 del CID y copia recipe.sh y verifier.py desde control.
Comprueba ambos SHA dentro del CID antes de ejecutar cualquier receta.

La copia usa el CID completo después de NoForeign; no cambia los binds ni
escribe en inputs. El verificador Python sirve también para validar fuentes,
SQLite cargado y ELF dentro del contenedor. Sólo se habilita ese modo cuando
está en root Linux Docker y su propio SHA coincide con el pin transmitido.
Las comprobaciones finales se repiten desde el host sobre los bytes reales.

## Receta y demostración SQLite

La receta sólo admite compile, fija PATH/flags/target/CC y rechaza un
ejecutable Git. No añade paquetes ni usa red. Extrae los tres archivos SQLite
permitidos por sus SHA; no usa extractall. Conserva versión 3.54.1, número
3054001 y SOURCE_ID
2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76.
Produce sólo SQLite compartido en /opt/agency-sqlite con SONAME
libsqlite3.so.0 y las opciones de la rama bundled, incluida FTS5.

LIBSQLITE3_SYS_USE_PKG_CONFIG=1 fuerza build_linked antes de bundled en
libsqlite3-sys 0.36. Se quitan SQLITE3_STATIC y PKG_CONFIG_ALL_STATIC; se fija
SQLITE3_DYNAMIC=1. No se usa STATIC=0 porque pkg-config0.3.32 interpreta la
presencia como enlace estático. Se verifica sqlite3.pc, versión y libdir
custom; no existe libsqlite3.a. Los bindings pre-generados del crate se
conservan intactos; su C SQLite 3.51.1 no se compila ni se copia al runtime.

RUSTFLAGS conserva -Dwarnings, -Ctarget-feature=-crt-static y -Cpanic=unwind;
añade únicamente -Clink-arg=-Wl,-rpath,$ORIGIN. RELEASE_STRIP=none conserva
.symtab; opt3/LTO fat/codegen1 permanecen. No se aplica stripping posterior.
Durante schema generation sólo LD_LIBRARY_PATH privado permite resolver el
SQLite custom. En la salida SQLite se coloca junto a vchord.so para RUNPATH.

pgvector usa make all/install, PG_CONFIG16.15 y OPTFLAGS vacío; no se invocan
dist, Docker o installcheck. xtask se lanza mediante cargo run --frozen y
mantiene sus cuatro llamadas hijas --frozen. Cargo.lock y xtask9055 se
verifican antes y después. No se edita ninguna fuente upstream o checksum.

Se exige prueba de versión/sourceid y opciones del SQLite realmente cargado,
build-script output con búsqueda exclusiva en /opt/agency-sqlite/lib y enlace
sqlite3 dinámico, DT_NEEDED libsqlite3.so.0 en vchord, RUNPATH literal $ORIGIN,
sqlite3_* indefinidos y ninguna implementación SQLite definida. El ELF debe
ser amd64/musl, sin dependencias/símbolos de versión GLIBC. No basta una
feature o cadena de versión en el binario.

## Artefactos, timeout y recibo

Las nuevas salidas viven sólo en /outputs/compile-v1: staging runtime-root,
probe SQLite, build-script output y recibo canónico con todos los hashes,
ELF/símbolos, versiones, flags y proof. Se conservan las licencias SQLite,
pgvector y las dos alternativas VectorChord, seleccionando AGPL para uso.
El staging incluye los archivos de make install (también headers/bitcode);
el futuro runtime deberá seleccionar/poner root:RO las bibliotecas y SQL
necesarios. Esta unidad no construye imagen ni ofrece archivos a usuarios.
Los modos0600/0700 del staging protegen su transporte privado; no son la
política final del directorio de bibliotecas PostgreSQL.

Los controles deben declarar vector 0.8.5 y vchord 1.1.1 una sola vez, con
sus SQL de instalación presentes y no vacíos. La sonda SQLite física debe
coincidir con el recibo. Estas comprobaciones no sustituyen la carga SQL real.

El presupuesto es 1800 s e incluye reserva de 20 s para parar el CID propio. El
watchdog del helper interrumpe a 1780 s; stop de 5 s/kill y comprobaciones tienen la
reserva restante. Docker exec usa los límites de cgroup ya acreditados y
señales sólo a su cliente POSIX; el CID se detiene explícitamente al fallar.
Logs/capturas 4 MiB, heartbeat cada 1 s y EOF/5 s sin latido activan la misma
limpieza. No se mata por PID/nombre ni se confía en que cerrar docker exec
termine el compilador dentro del contenedor.

El control conserva compile-v1.log y compile-v1-receipt.json. La raíz respalda
el envelope completo en MEDIA_POSTGRES_IMMICH_COMPILE_RECEIPT_B64 y lo relee;
no se alteran los B64 SDK ni se ejecuta la etapa siguiente. Cualquier fallo
mantiene parciales y detiene el CID acreditado; repetir requiere revisión.
El stop de emergencia sólo necesita el control protegido, sin confiar en
los outputs escribibles por el SDK.

Pruebas locales cubren ELF sintético, sourceid/opciones, rechazo de C
embebido/enlace erróneo, pérdida de proof, jobs4 rechazado, frame fragmentado,
plan sin SSH, recibo inválido sin escritura de bóveda y limpieza por CID con
proveedor controlado. No acreditan una
compilación real o los resultados SQL/SCA. SQL/PGDATA y la SCA del runtime
final permanecen explícitamente pendientes.
