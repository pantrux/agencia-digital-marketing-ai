# Plan musl nativo para PostgreSQL de Immich

Este incremento contiene un plan y sus entradas fijadas. No contiene un Dockerfile ejecutable ni acredita una imagen construida. Mantiene PostgreSQL **16.15**, VectorChord **1.1.1** y pgvector **0.8.5**. La raíz debe respaldar los pins y el SHA-256 de `musl-build-inputs.json` antes de ejecutar SDK, instaladores o compiladores. La construcción y la aceptación posteriores pertenecen exclusivamente a Linux en `vm-media-01`, `.85`.

El inventario general y la receta `postgres-apps` permanecen congelados. Este plan candidato no autoriza sustituir la base Debian del inventario ni habilita despliegues. `build_enabled`, `runtime_verified`, `security_clean` y `production_ready` siguen en `false`.

## Evidencia de compatibilidad y límites

El mismo commit de VectorChord `1.1.1`, `f9b5e234c44cee69b266b8431f92b7deed4e99d3`, define un [trabajo `psql_alpine`](https://github.com/supervc-stack/VectorChord/blob/f9b5e234c44cee69b266b8431f92b7deed4e99d3/.github/workflows/check.yml#L491) para PostgreSQL 15/16/17 y arquitecturas x86_64/aarch64. Incluye compilación, creación de la extensión y suites SQL. Usa Alpine 3.22, Clang 18, Rust beta y opciones Cargo para desactivar el CRT estático también en los artefactos del host. Esto documenta una ruta musl upstream; no demuestra que nuestra combinación nueva de Alpine 3.24, Clang 21 y SDK Rust fijado haya pasado. No se verificó una ejecución de ese trabajo remoto.

El plan conserva `pgrx=0.17.0` con `cshim`, `mimalloc=0.1.48` y la licencia elegida **AGPL-3.0-only**. No usa `gcompat`, un loader GNU o enlaces falsos de SONAME. El `.deb` oficial GNU no se traslada a Alpine.

El intento de resolver Trixie mediante purge queda descartado: el ELF real de PostgreSQL necesita bibliotecas afectadas y varios paquetes afectados son Essential. La evidencia de sus 63 HIGH y un CRITICAL del SO, los 30 paquetes exactos y sus dependencias se conserva en `.tools/evidence/phase4-postgres-immich-trixie-conclusion.md` y los JSON vinculados allí. No se ejecutó apt/dpkg ni se retiraron archivos de esa imagen.

## Entradas fijadas

Consulta primaria de entradas realizada el 6 de noviembre de 2026. El manifiesto adjunto contiene los hashes completos, URLs, los **244 crates de registro**, los **14 crates locales** y los paquetes directos del SDK. No tiene dependencias Cargo con fuente Git.

| Entrada | Pin |
| --- | --- |
| Base `linux/amd64` | `docker.io/library/postgres:16.15-alpine3.24@sha256:ac838714b7d794185a866e5a6b479b954e7d8a070568aa0acac6fe07c3070bf5` |
| VectorChord fuente | Commit `f9b5e234c44cee69b266b8431f92b7deed4e99d3`, archivo SHA-256 `7cc81fe827f41e66dcca37dd4cb6dc171da9f6807ebf3d5892d72edda61fdf28` |
| Cargo.lock original | SHA-256 `dae0885db162d70f216360330c235669e9dda610a93b31eb593c5755fde856ab` |
| pgvector fuente | `0.8.5`, commit `159b79aaad5983fb7459c1e3df2897fbb2d11788`, SHA-256 `9a483fad70ae2e0a50b3dccb6c4b4931d9a07375a1d5815e82b57870448a7d52` |
| Rust beta fechado | Manifiesto oficial `2026-11-05`: rustc `1.100.0-beta.10`, Cargo publicado como paquete `0.101.0-beta.10`; host y target `x86_64-unknown-linux-musl` |
| SDK PostgreSQL | `PG_CONFIG=/usr/local/bin/pg_config`, headers `PG_VERSION_NUM=160015`, PGXS de la propia base, sin instalar otro servidor PostgreSQL |
| C/C++ y LLVM | `build-base=0.5-r4`, GCC/G++ `15.2.0-r5`, `clang21=21.1.8-r3`, `clang21-dev=21.1.8-r3`, `llvm21-dev=21.1.8-r1` |
| SQLite corregida candidata | Amalgamación oficial `3.54.1`, SHA-256 `0a9c8e34352dad46006aa82836d5383bff9dc6bc9ce5a60553c05aa30e6ad1f9`, licencia `blessing` |

El [manifiesto Rust fechado](https://static.rust-lang.org/dist/2026-11-05/channel-rust-beta.toml) fija por separado los tarballs musl de `rustc`, `cargo` y `rust-std`. Instalar únicamente esos componentes verificados dentro del constructor aislado; no ejecutar un instalador descargado por tubería ni `rustup update`. La elección beta reproduce el requisito de Cargo experimental del trabajo upstream, con fecha y hashes concretos; sigue necesitando verificación real.

La inspección de las capas de la base confirma que ya contiene `pg_config`, los encabezados de servidor y PGXS bajo `/usr/local`. También confirma `USE_LLVM=1`, `CLANG=clang-21` y LLVM 21. No instalar `postgresql16-dev` de Alpine: aunque también anuncia 16.15, usa otro prefijo/configuración y depende de Clang 16. No usar los alias flotantes `clang` o `llvm-dev`, que no garantizan el SDK de la base.

Además de los compiladores, el constructor prevé `pkgconf`, `icu-dev`, `libxml2-dev`, `libxslt-dev`, `lz4-dev`, `zstd-dev`, `openssl-dev`, `readline-dev` y `zlib-dev`, con versiones exactas en el manifiesto. Estos headers cubren las opciones presentes en el PostgreSQL inspeccionado. Permanecerán en la etapa de construcción.

Los índices oficiales [Alpine 3.24 main](https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/APKINDEX.tar.gz) y [community](https://dl-cdn.alpinelinux.org/alpine/v3.24/community/x86_64/APKINDEX.tar.gz) están fijados por SHA-256 de los bytes consultados. **Falta resolver y descargar la clausura APK completa**, verificar firmas con claves oficiales y registrar SHA-256 por archivo antes de instalar. El campo `Q1` de APKINDEX no es un SHA-256. Si cambia el índice, detenerse y revisar un manifiesto nuevo; no resolver versiones distintas silenciosamente ni hacer un upgrade global.

## Cargo sin red, sin Git y sin cambios de lock

La fuente llega por HTTPS y commit exacto. Para cada crate de Cargo.lock, descargar el `.crate` desde su URL de `static.crates.io`, comprobar el checksum SHA-256 del lock y extraer de forma acotada sin rutas absolutas, escapes o enlaces. Construir el árbol vendor y su inventario sin invocar Git. Antes de ejecutar build scripts, completar también la revisión de sus comandos: que el lock no tenga fuentes Git no acredita por sí solo que ningún script pueda llamar al ejecutable.

Configurar source replacement de `crates-io` hacia el directorio vendor y `CARGO_NET_OFFLINE=true`, además de red de construcción `none`. El texto `registry+https://github.com/rust-lang/crates.io-index` del lock es un identificador heredado; no debe provocar acceso al índice Git. No ejecutar `cargo install`, `cargo pgrx init`, `apk add git` o `actions/checkout` como parte de esta receta.

El [xtask original](https://github.com/supervc-stack/VectorChord/blob/f9b5e234c44cee69b266b8431f92b7deed4e99d3/crates/xtask/src/main.rs) crea procesos `cargo metadata`, `cargo rustc` y `cargo clippy` sin `--frozen`. Una opción en el proceso exterior no los cubre. Antes de construir falta preparar un parche mínimo, ligado al hash del archivo original, que añada `--frozen` a esas llamadas. No modificar las versiones ni los checksums del lock. Comprobar su SHA antes y después; cualquier diferencia detiene la operación.

Reproducir las opciones upstream en `.cargo/config.toml`, únicamente dentro del constructor:

```toml
unstable.host-config = true
unstable.target-applies-to-host = true
host.rustflags = ["-Dwarnings", "-Ctarget-feature=-crt-static", "-Cpanic=unwind"]
build.rustflags = ["-Dwarnings", "-Ctarget-feature=-crt-static", "-Cpanic=unwind"]
```

Usar `RUSTC_BOOTSTRAP=1` sólo en esa etapa, como requiere la ruta upstream observada; no copiarlo al runtime. Fijar `PG_CONFIG=/usr/local/bin/pg_config`, `TARGET=x86_64-unknown-linux-musl`, `PROFILE=release`, `CC=clang-21` y `CXX=clang++-21`. El xtask deriva `pg16` de ese `PG_CONFIG` y configura `PGRX_PG_CONFIG_PATH` para sus hijos. No usar `target-cpu=native`; para pgvector desactivar su `OPTFLAGS=-march=native` mediante `OPTFLAGS=` para conservar un objetivo de CPU explícito y portable.

## SQLite: selección por fuentes y comprobación pendiente

La dependencia `rusqlite 0.38.0` con feature `bundled` está en `[dependencies]` de la biblioteca `vchord`; no sólo en herramientas o tests. `src/lib.rs` incluye `recorder` sin una condición de compilación, y `recorder/worker.rs` usa `rusqlite::Connection`, transacciones y WAL. `libsqlite3-sys 0.36.0`, fijado por el lock, incorpora SQLite 3.51.1 y su ruta bundled habilita FTS5. Esto acredita la selección esperada a partir de los manifiestos y fuentes; el grafo Cargo del SDK elegido y el enlace del nuevo `.so` todavía no se ejecutaron ni verificaron. Tampoco acredita por sí solo exposición a entradas SQL de un atacante.

El [tracker de CVE-2026-119373](https://security-tracker.debian.org/tracker/CVE-2026-119373) identifica un problema FTS5 antes de SQLite 3.54.0. La [página primaria SQLite sobre vulnerabilidades](https://sqlite.org/cves.html) documenta otros problemas FTS5 corregidos después de 3.51.1. Mantener ese C fuera del inventario de una imagen daría una falsa impresión de cobertura.

La salida propuesta conserva los crates y Cargo.lock sin falsificar checksums. El `build.rs` verificado de `libsqlite3-sys` admite `LIBSQLITE3_SYS_USE_PKG_CONFIG=1`, que elige `build_linked` incluso cuando `bundled` está activo. Compilar aparte la [amalgamación oficial SQLite 3.54.1](https://sqlite.org/2026/sqlite-amalgamation-3540100.zip), con PIC, SONAME estándar y las opciones funcionales equivalentes a la ruta bundled; crear su metadata pkg-config propia y fijar `SQLITE3_LIB_DIR`, `SQLITE3_INCLUDE_DIR` y `SQLITE3_STATIC=0`. El SHA3-256 de su `sqlite3.c` coincide con el publicado en el [historial oficial 3.54.1](https://sqlite.org/changes.html). Su [licencia primaria](https://sqlite.org/copyright.html) declara dominio público.

No reescribir archivos dentro del vendor ni su `.cargo-checksum.json`, ni sustituir la biblioteca por un alias de otra ABI. La biblioteca corregida deberá ser un artefacto propio identificado, con fuente, opciones, versión y hash incluidos en el SBOM final. La ruta necesita pruebas con los bindings existentes de rusqlite; no basta cambiar una variable. Si no se acredita esa compatibilidad, detener la construcción y presentar un parche o actualización de dependencia explícitos con un lock nuevo revisado.

## Diseño del siguiente incremento, todavía sin ejecutar

Después del respaldo de pins y de completar el staging firmado, preparar como máximo cinco archivos por unidad: Dockerfile de etapas separadas, script de construcción y parche acotado de xtask, además de los cambios necesarios a este manifiesto/README. La descarga/verificación de entradas puede ser una unidad anterior independiente; no mezclarla con la compilación.

La etapa builder parte del mismo digest PostgreSQL Alpine y contiene SDK/APK/vendor fijados, sin credenciales ni acceso a red durante compilación. La etapa final parte de esa base exacta, conserva su entrypoint y retira el `gosu` real y su alias verificando los mismos hashes de la receta de aplicaciones. Copia únicamente los módulos, SQL/control, SQLite corregida, licencias y recibos propios. Ejecuta como `70:70`; deja compiladores, headers nuevos y cachés de Cargo en la etapa builder. No modifica metadatos de paquetes para alterar el escaneo.

Las rutas de instalación se obtendrán del `pg_config` respaldado: módulos bajo `/usr/local/lib/postgresql` y SQL/control bajo `/usr/local/share/postgresql/extension`. VectorChord requiere `shared_preload_libraries=vchord`. No asumir que las rutas GNU `/usr/lib/postgresql/16` valen para esta base.

## Aceptación real obligatoria

| Control | Evidencia requerida antes de aceptar |
| --- | --- |
| Pins y staging | SHA de fuentes/lock/SDK/APK/vendor; firmas APK correctas; sin Git ni llamadas a instaladores flotantes; clausura exacta respaldada. |
| Features | Tras respaldar el SDK, ejecutar `cargo metadata` y `cargo tree -e features` offline/frozen para `vchord/pg16`. Conservar qué selecciona rusqlite/libsqlite3-sys y qué pertenece sólo a dev/test/xtask. |
| Compilación | Red `none`; inner Cargo también frozen; logs sin fallback; lock original idéntico; versiones efectivas del SDK y hashes de outputs. |
| ABI | `readelf`/herramienta equivalente sobre ambos módulos y SQLite: arquitectura amd64, libc musl, todas las `DT_NEEDED` resueltas; sin símbolos `GLIBC_*`, loader GNU o gcompat. Exigir que `vchord.so` referencia la SQLite corregida prevista y comprobar su archivo real. |
| SQLite | Helper de aceptación enlazado a la misma biblioteca: versión 3.54.1, opciones FTS/JSON/metadata previstas, transacciones, WAL y persistencia tras cierre/reinicio. Verificar biblioteca cargada en el proceso propio y ausencia de la amalgamación 3.51.1 enlazada. |
| PostgreSQL | Inicialización vacía como UID/GID 70, SQL SCRAM autenticado y rechazo sin contraseña, versión 16.15, checksums activos y reinicio conservando una fila centinela. Contrato de archivos privados igual al de `postgres-apps`. |
| Extensiones | Precarga real; `CREATE EXTENSION vector VERSION '0.8.5'` y `CREATE EXTENSION vchord VERSION '1.1.1'`; comprobar `pg_extension`, crear índice vectorial y ejecutar búsqueda real con resultado esperado. |
| Recorder | En datos sintéticos propios, habilitar `vchordrq.query_sampling_enable` y tasa 1; ejecutar consultas que usen el índice; verificar `vchordrq_sampled_queries` y repetir tras reinicio. El source test upstream `tests/vchordrq/recall.slt` proporciona el contrato SQL. No dejar GUC de prueba en datos de clientes. |
| Errores | Dependencia ausente o permisos insuficientes detienen la prueba sin fallback GNU, cambio a root ni relajación de autenticación. |
| SCA | Escaneo real HIGH/CRITICAL cero de imagen final, fuentes Cargo y componentes C identificados. Guardar SBOM completo con SQLite y source receipts. No usar filtros por paquete, `--ignore-unfixed`, borrado de metadatos o invisibilidad del C embebido para aprobar. |

No hay resultados de estos controles ABI/runtime/SQL todavía. El escaneo estático Cargo registrado dio cero HIGH/CRITICAL: el análisis habitual del workspace cubrió 224 crates de registro más 14 locales; un inventario CycloneDX adicional cubrió los 244 crates de registro del lock. Las advertencias del escáner sobre SBOM de terceros están preservadas. Esos resultados no acreditan el C de SQLite, el SDK instalado o la seguridad de una imagen final.

Evidencia de investigación recuperable: `.tools/evidence/phase4-vectorchord-musl-source-inputs.json`, `phase4-vectorchord-postgres-native-sdk.json`, `phase4-vectorchord-alpine-sdk-packages.json`, `phase4-vectorchord-bundled-sqlite-inspection.json`, `phase4-vectorchord-sqlite-corrected-source.json` y los informes `phase4-vectorchord-*-trivy*`. La raíz conserva la decisión de respaldo, construcción y publicación.
