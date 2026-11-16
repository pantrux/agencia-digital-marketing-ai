# Reconstrucción PostgreSQL/VectorChord V5

## Estado y cambio autorizado

Esta unidad prepara cinco fuentes nuevas y un perfil pequeño. No ejecuta SDK,
SSH, Docker, SQL ni compiladores al generar el plan o importar los módulos.
V4, sus cinco fuentes, perfiles, positivos y fallos se conservan. Las copias
byte exactas previas están en `.tools/evidence/postgres-immich-security-before-v5`.
No se adopta ningún target ni resultado de la regresión fallida anterior.

El fallo V4 `d7c27f4d…` ocurrió antes de ejecutar los dos casos: crossbeam-epoch
0.9.20 se había declarado mediante `path` y Rust beta 1.100 convirtió su uso de
`usize::max_value()` en error por el `-Dwarnings` heredado. No se modifica esa
fuente, el vendor autenticado, el SDK ni la política de avisos del código propio.

V5 mantiene versiones exactas de registro resueltas exclusivamente desde el
vendor offline. Cargo distingue dependencias non-path y les aplica su política
normal de `--cap-lints allow`; el harness conserva `-Dwarnings`, sin cap global.
Esto está documentado en [niveles de lints de Rust](https://doc.rust-lang.org/rustc/lints/levels.html#capping-lints)
y [lints del manifest de Cargo](https://doc.rust-lang.org/cargo/reference/manifest.html#the-lints-section).

| Paquete | Versión | SHA256 del crate oficial |
| --- | --- | --- |
| anyhow | 1.0.103 | `2a4385e2e34eb35d6b3efe798b9eb88096925d87726c0798709bf56d9ed84af3` |
| crossbeam-epoch | 0.9.20 | `2d6914041f254d6e9176c01941b21115dcfb7089e55135a35411081bd106ef3f` |
| crossbeam-utils | 0.8.21 | `d0a5c400df2834b80a4c3327b3aad3a4c4cd4de0629063962b03235697506a28` |

El harness nuevo se llama `agency-native-security-regression-registry-v1` y
mantiene los bytes de los casos `src/lib.rs` (`78de1f00…`): Anyhow comprueba
comportamiento de context/downcast_mut; Epoch reproduce format_null. Esto no
equivale a Miri ni acredita ausencia de UB. El lock propio tiene cuatro paquetes:
harness y las tres filas de la tabla. No entran dev-dependencies upstream.

```toml
[dependencies]
anyhow = "=1.0.103"
crossbeam-epoch = {version = "=0.9.20", default-features = false, features = ["std"]}
```

Los dos nodos nuevos del lock incorporan su source de registro y checksum reales.
`registry+https://github.com/rust-lang/crates.io-index` es el identificador
canónico; no es una orden Git. La configuración propia sustituye crates.io por
`/work/.security-rebuild-v5/vendor` y fija `net.offline=true`. No hay path, patch,
replace ni allow nuevo. Cargo documenta el contrato de [source replacement](https://doc.rust-lang.org/cargo/reference/source-replacement.html)
y las [fuentes vendorizadas de sólo lectura](https://doc.rust-lang.org/cargo/commands/cargo-vendor.html).
Los hashes y firmas externos siguen siendo necesarios: `.cargo-checksum.json`
por sí solo no autentica una fuente maliciosa.

## Rutas y recursos nuevos

| Función | Ruta o identidad |
| --- | --- |
| Work | `/work/.security-rebuild-v5` |
| Target Cargo | `/work/.security-rebuild-v5/target` |
| Runtime proyectado | `/outputs/security-rebuild-v5/runtime-root` |
| Staging | `/home/jandrade/.agency-pg-security-rebuild-v5` |
| Control propio | `control/security-rebuild-v5` bajo el control SDK ya acreditado |
| Familia de bóveda | `MEDIA_POSTGRES_IMMICH_SECURITY_REBUILD_V5_` |
| Perfil local | `.tools/postgres-immich-security-inputs-v1/security-rebuild-v5.profile.json` |

El CID SDK existente es `f042555d…`, base propia `c908ada0…`. No se crea otro
contenedor ni se cambia su configuración: red none, 4 CPU, 12 GiB, 256 PID y dos
jobs. Rust/rustfmt y SQLite `/opt/agency-sqlite-v3` permanecen como SDK fijado.
V5 exige un restart propio nuevo y respaldado, con NoForeign, nacimiento,
PID/StartedAt y cgroups. Raíz serializa esta ventana con OpenCV; la preparación
de datos no autoriza arrancar PostgreSQL mientras otro SDK esté activo.

## Plan y transiciones de raíz

```powershell
python docker/postgres-immich/security_rebuild_v5.py plan
python docker/postgres-immich/security_rebuild_v5.py plan --output '.tools/postgres-immich-security-inputs-v1/security-rebuild-v5.profile.json'
# Sólo raíz, después de revisar y respaldar el perfil físico:
python docker/postgres-immich/security_rebuild_v5.py plan --root-update-vault
```

El plan por defecto sólo lee fuentes/evidencias públicas fijadas y sus hashes.
La publicación del perfil es EXCL, exige su raíz privada y no sustituye otro
archivo. `--root-update-vault` es una operación explícita de raíz; el agente no
la ejecuta. Los campos usan `OWNER`, `CID`, `PROFILE_PATH/SHA256`,
`LOCAL_SOURCE_SHA256`, `REMOTE_SOURCE_SHA256`, `RECIPE_SOURCE_SHA256`,
`STAGING_PATH`, `CONTROL_PATH`, `WORK_PATH` y `OUTPUT_PATH` bajo la familia V5.

Cada operación se invoca separadamente:

```text
python docker/postgres-immich/security_rebuild_v5.py <modo> --expected-profile-sha256 <SHA_RESPALDADO> --root-update-vault
```

| Modo | Requisito y efecto |
| --- | --- |
| `receive` | Once archivos fijados; staging UID1000 exclusivo, SHA/tamaño/EOF; sin SDK. |
| `prepare` | Promoción por FD y marcadores root0700/0600, host/layout y CID detenido; sin arranque. |
| `restart` | Sólo CID/creación/imagen propios detenidos y SCA SDK vigente; época/cgroups respaldados. |
| `materialize` | TAR original fijado y overlays exactos, 244 crates, source/target/harness V5 nuevos; sin adoptar V4. |
| `graph` | Dos árboles Cargo offline/frozen; vchord exige `--features pg16`, xtask conserva su argv. |
| `regression` | Metadata cerrada, pruebas con `-vv`, política de lints y dos resultados exactos. |
| `build` | Sólo xtask build; flags PG16/SIMD/cshim/Rust/SQLite intactos, output nuevo. |
| `catalog` | Coteja SQL/control/licencias y SQLite dinámica; sólo vchord.so puede cambiar. No SQL. |
| `stop` | Independiente de inputs/SCA/build; control, prepare respaldado, daemon/CID/NoForeign actuales. |

Raíz respalda cada recibo antes del siguiente. Graph exige después la revisión
literal `GRAPH_REVIEW_STATUS=PG16_SIMD_INIT_CONFIRMED_SECURITY_UPGRADES_ONLY`.
Receive/prepare no sustituyen ese gate. Todos los modos SDK requieren también
`SDK_SCA_APPROVED=true` y ruta/SHA de la evaluación específica vigente de 110 APK,
rustfmt y driver. Stop queda exento para no impedir limpieza por evidencia vencida.
El lector canónico y ACL están fijados por SHA y admiten hasta 8 MiB de `.env`.
No hay credenciales en perfil, argv, stdout ni recibos; SSH conserva Paramiko
5.1.0, pin Ed25519, RejectPolicy y ausencia de fallback/agente.

## Nuevas pruebas antes de ejecutar los casos Rust

La etapa regression conserva `RUSTFLAGS` exactamente:

```text
-Dwarnings -Ctarget-feature=-crt-static -Cpanic=unwind -Clink-arg=-Wl,-rpath,$ORIGIN
```

Primero corre `cargo metadata --frozen --offline --format-version 1
--filter-platform x86_64-unknown-linux-musl`. Debe identificar un único workspace
local y tres paquetes de registro; los paths físicos deben ser los del vendor
autenticado, con versiones/checksums del lock y aristas exactas, sin paquetes
extra ni dependencia path. El log JSON y el proceso se conservan.

Después corre `cargo test --frozen --offline --target
x86_64-unknown-linux-musl --lib -vv -- --test-threads=1`. El verificador del log
no ejecuta texto: analiza argv con `shlex` y exige las tres bibliotecas externas
con `--cap-lints allow`, y el harness test con `-Dwarnings` sin cap ni allow.
Si el SDK concreto produce un formato distinto se rechaza para revisión; no se
declara aprobado por inferirlo desde el manifest. Cargo 1.99 puede omitir flags
de lint redundantes de unidades ya capped, como indica su [changelog oficial](https://doc.rust-lang.org/stable/cargo/CHANGELOG.html).

Se reutiliza intacto `check_regression_output`: exactamente dos líneas completas
aprobadas y un solo resumen positivo; cero pruebas, FAILED, duplicados o texto
contradictorio bloquean. El recibo añade los SHA de metadata/lock/log y evidencia
de la política de lints. No se cambia el grafo de producción ni la compilación
de vchord por este ajuste de un harness independiente.

## Presupuestos y aceptación pendiente

Se mantienen los límites V4: receive180 s, prepare120 s, restart60 s,
materialize/graph/regression300 s, build1800 s, catalog180 s y stop20 s, con sus
reservas de limpieza, logs de 4 MiB, latido SSH y recolector WNOWAIT por grupo
propio. No se hace Git, red, SQL, buildx ni un upgrade del SDK. Fallos y parciales
se conservan sin reintento automático; sólo se señala o detiene el CID acreditado.

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -p test_postgres_immich_security_rebuild_v5.py -v
python -m ruff check docker/postgres-immich/security_rebuild_v5.py docker/postgres-immich/security_rebuild_remote_v5.py tests/test_postgres_immich_security_rebuild_v5.py
```

Las pruebas portables no ejecutan SDK ni servicios. Las tres pruebas nativas de
FD/EXCL/NoFollow/flock requieren Linux; usan TemporaryDirectory y ninguna
infraestructura de producción. La prueba de inside_write necesita UID/GID 0
porque el helper a13 conserva su guarda root; las otras dos admiten UID1000. El arnés Linux separado de raíz deberá
ejecutarlas sin omisiones. Compilación V5, catálogo, STOP positivo, SCA de la
imagen final y aceptación SQL siguen pendientes; conservar 96c y sus finalizadores.
