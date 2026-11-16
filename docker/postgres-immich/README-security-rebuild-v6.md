# Reconstrucción PostgreSQL/VectorChord V6

## Estado y cambio autorizado

Esta unidad prepara cinco fuentes nuevas y un perfil pequeño. No ejecuta SDK,
SSH, Docker, SQL ni compiladores al generar el plan o importar los módulos.
V5, sus cinco fuentes, perfiles, positivos y fallos se conservan. Las copias
byte exactas previas están en `.tools/evidence/postgres-immich-security-before-v6`.
No se adopta ningún target ni resultado de la regresión anterior.

V5 ejecutó los dos casos y obtuvo resultado positivo en el log real
`phase4-pg-security-regression-capture-v5-cargo-regression.log`, SHA256
`97d8e3edb099fbc1402d9d1ed7ab8216c4d1825b746bcedc150e70e4065bdfd5`.
La etapa se rechazó después porque el verificador sólo admitía cap externo
`allow`, mientras el Cargo fijado emitió `warn`, que conserva los avisos. El
fallo `e15f5054…` y la parada limpia permanecen como historia; esta revisión no
los convierte en un recibo positivo.

V6 cambia únicamente esa condición: admite exactamente un cap externo `warn`
o `allow`. No admite `deny`, `forbid`, ausencia ni caps duplicados, y el harness
mantiene `-Dwarnings` sin cap ni allow. Los logs y advertencias se conservan
íntegros. No se modifica el código de los crates, vendor, SDK, grafo, versiones,
flags PG16/SIMD ni comandos de prueba. Las rutas, namespace y familia de bóveda
son V6 para obtener recibos nuevos revisados por raíz. La política general de
Cargo se documenta en [niveles de lints de Rust](https://doc.rust-lang.org/rustc/lints/levels.html#capping-lints)
y [lints del manifest de Cargo](https://doc.rust-lang.org/cargo/reference/manifest.html#the-lints-section);
la elección concreta `warn` se acredita con los bytes del log real, no se deduce
de una política genérica.

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
`/work/.security-rebuild-v6/vendor` y fija `net.offline=true`. No hay path, patch,
replace ni allow nuevo. Cargo documenta el contrato de [source replacement](https://doc.rust-lang.org/cargo/reference/source-replacement.html)
y las [fuentes vendorizadas de sólo lectura](https://doc.rust-lang.org/cargo/commands/cargo-vendor.html).
Los hashes y firmas externos siguen siendo necesarios: `.cargo-checksum.json`
por sí solo no autentica una fuente maliciosa.

## Rutas y recursos nuevos

| Función | Ruta o identidad |
| --- | --- |
| Work | `/work/.security-rebuild-v6` |
| Target Cargo | `/work/.security-rebuild-v6/target` |
| Runtime proyectado | `/outputs/security-rebuild-v6/runtime-root` |
| Staging | `/home/jandrade/.agency-pg-security-rebuild-v6` |
| Control propio | `control/security-rebuild-v6` bajo el control SDK ya acreditado |
| Familia de bóveda | `MEDIA_POSTGRES_IMMICH_SECURITY_REBUILD_V6_` |
| Perfil local | `.tools/postgres-immich-security-inputs-v1/security-rebuild-v6.profile.json` |

El CID SDK existente es `f042555d…`, base propia `c908ada0…`. No se crea otro
contenedor ni se cambia su configuración: red none, 4 CPU, 12 GiB, 256 PID y dos
jobs. Rust/rustfmt y SQLite `/opt/agency-sqlite-v3` permanecen como SDK fijado.
V6 exige un restart propio nuevo y respaldado, con NoForeign, nacimiento,
PID/StartedAt y cgroups. Raíz serializa esta ventana con OpenCV; la preparación
de datos no autoriza arrancar PostgreSQL mientras otro SDK esté activo.

## Plan y transiciones de raíz

```powershell
python docker/postgres-immich/security_rebuild_v6.py plan
python docker/postgres-immich/security_rebuild_v6.py plan --output '.tools/postgres-immich-security-inputs-v1/security-rebuild-v6.profile.json'
# Sólo raíz, después de revisar y respaldar el perfil físico:
python docker/postgres-immich/security_rebuild_v6.py plan --root-update-vault
```

El plan por defecto sólo lee fuentes/evidencias públicas fijadas y sus hashes.
La publicación del perfil es EXCL, exige su raíz privada y no sustituye otro
archivo. `--root-update-vault` es una operación explícita de raíz; el agente no
la ejecuta. Los campos usan `OWNER`, `CID`, `PROFILE_PATH/SHA256`,
`LOCAL_SOURCE_SHA256`, `REMOTE_SOURCE_SHA256`, `RECIPE_SOURCE_SHA256`,
`STAGING_PATH`, `CONTROL_PATH`, `WORK_PATH` y `OUTPUT_PATH` bajo la familia V6.

Cada operación se invoca separadamente:

```text
python docker/postgres-immich/security_rebuild_v6.py <modo> --expected-profile-sha256 <SHA_RESPALDADO> --root-update-vault
```

| Modo | Requisito y efecto |
| --- | --- |
| `receive` | Once archivos fijados; staging UID1000 exclusivo, SHA/tamaño/EOF; sin SDK. |
| `prepare` | Promoción por FD y marcadores root0700/0600, host/layout y CID detenido; sin arranque. |
| `restart` | Sólo CID/creación/imagen propios detenidos y SCA SDK vigente; época/cgroups respaldados. |
| `materialize` | TAR original fijado y overlays exactos, 244 crates, source/target/harness V6 nuevos; sin adoptar V4. |
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
con un único `--cap-lints warn` o `--cap-lints allow`, y el harness test con `-Dwarnings` sin cap ni allow.
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
python -W error::ResourceWarning -m unittest discover -s tests -p test_postgres_immich_security_rebuild_v6.py -v
python -m ruff check docker/postgres-immich/security_rebuild_v6.py docker/postgres-immich/security_rebuild_remote_v6.py tests/test_postgres_immich_security_rebuild_v6.py
```

Las pruebas portables no ejecutan SDK ni servicios. Las tres pruebas nativas de
FD/EXCL/NoFollow/flock requieren Linux; usan TemporaryDirectory y ninguna
infraestructura de producción. La prueba de inside_write necesita UID/GID 0
porque el helper a13 conserva su guarda root; las otras dos admiten UID1000. El arnés Linux separado de raíz deberá
ejecutarlas sin omisiones. Compilación V6, catálogo, STOP positivo, SCA de la
imagen final y aceptación SQL siguen pendientes; conservar 96c y sus finalizadores.

## Regresión del formato real y límites de esta entrega

El test incorpora los 22.962 bytes exactos del log V5 como datos comprimidos,
con comprobación de SHA256 antes de invocar el parser. No ejecuta el texto ni
requiere `.tools` para ese fixture. También comprueba las dos variantes válidas,
las formas separada y con signo igual, caps múltiples y niveles no permitidos,
y rechazo de cualquier cap en el harness. El contenido de los dos casos Rust
sigue byte exacto y sus resultados completos se verifican con el gate anterior.

Las 43 pruebas descubiertas contienen 40 portables y 3 POSIX. En Windows las
3 POSIX se omiten explícitamente; eso no acredita su ejecución nativa. Un arnés
Linux43 separado se congelará para raíz antes de ejecutar infraestructura.
La generación del perfil y los controles locales de esta entrega no invocan
SSH, Docker, SDK ni `.env`. Los recibos reales V6 y el nuevo catálogo continúan
pendientes; no hay aprobación SQL, SCA final ni imagen de producción.

La corrección posterior de codificación sólo afecta a este README y al texto
de los tests; CLI, remoto, receta y perfil ya adoptados por raíz siguen byte
exactos. El freeze inicial d0ea5412 y los dos archivos previos se conservan en
`.tools/evidence/postgres-immich-security-v6-before-utf8`. El test histórico se
reconstruyó desde la copia V5 y el fixture nuevo y se cotejó contra su SHA
original `1ee57f1379f29bf79272cdb07d37f2129bdba69c1eb98c84acd40d73323e3977`.
