# Reconstrucción aislada de VectorChord, revisión V4

El controlador prepara un perfil pequeño por referencias para reutilizar el SDK propio detenido `f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d`. Corrige sólo anyhow 1.0.103 y crossbeam-epoch 0.9.20 mediante el vendor alternativo ya materializado por Root. No cambia los controladores congelados ni los árboles/output originales. SQLite 3.54.1 y pgvector se verifican y copian como bytes; no se vuelven a compilar.

Esta entrega contiene código y pruebas controladas. No acredita recepción remota, reinicio, compilación, pruebas Rust, imagen nueva ni SQL de aceptación. Root debe revisar el perfil/pins y reservar CPU/RAM secuencialmente antes de cualquier operación.

## Corrección de la sonda PG16

La materialización V3 pasó realmente con 260 fuentes, 244 crates y prueba histórica intacta. Su grafo posterior se rechazó antes de compilar: la sonda `tree -p vchord` omitía `--features pg16`. La captura `phase4-pg-security-graph-capture-v3-graph-vchord.log` tiene 19.794 bytes y SHA256 `984decfd0a148d4913433fdbd2014bb40b1e2471bf52515bd2c61ec9152b8370`; el fallo completo conserva SHA256 `7ab08f147100a776ac037a1725aba819c2aef997c5c91b7e5c3c3939dd3eb429`. Root detuvo el mismo CID con PID0 y doble inspección NoForeign.

El parser fijado encontró 178 identidades y 480 líneas, sólo las dos versiones aprobadas, sin paquetes o features adicionales. Faltaba únicamente pg16 en vchord, pgrx, pgrx-catalog y pgrx-pg-sys. El manifest del TAR declara `default=[simd/init]` y `pg16=[pgrx/pg16,pgrx-catalog/pg16]`; por eso default no selecciona PostgreSQL16. Las trece rutas sólo se trasladaron al workspace previsto.

V4 introduce un constructor cerrado de argv: añade `--features pg16` sólo para vchord y conserva la sonda xtask sin ese flag. Las pruebas comparan el argv xtask con el AST de V3 autenticado, adaptando únicamente el workspace de esa expectativa. Se conservan la comparación estricta, el parser y el build xtask existente, que ya deduce pg16 de PG_CONFIG y lo transmite internamente. No se modifica ni normaliza la captura real, el lock o el vendor; una nueva ejecución efectiva será necesaria para acreditar el resultado.

Los cinco archivos V4 son nuevos. Las cinco fuentes V3, perfil, planes, recibos y captura/fallo se preservan sin cambios y tienen copia sellada en `.tools/evidence/postgres-immich-security-before-v4/`. No se reusa el parcial, positivo o época anterior.

## Corrección de materialización y conservación del intento anterior

La revisión v2 falló realmente antes de Cargo: la lectura estricta del árbol histórico rechazó un ancestro de `/work/source/.github/dependabot.yml`. La lectura HEAD del daemon acreditó `/work/source` y `.cargo` en 0700, y `.github` en 0775. La prueba física está en `phase4-pg-security-source-ancestor-readonly-v1.json`, SHA256 `2b65a26b2ef15da093124725e0133e48f56a63b716b0897b8e52d2914d9d4234`; el CID quedó detenido con PID0. El TAR fijado contiene 56 directorios 0775, 257 archivos 0664 y dos ejecutables 0775. Esos modos pertenecen a la entrada, no son una política válida para el árbol nuevo.

Se conserva el preparador TAR corregido en V3. Esa revisión usó archivos nuevos y preservó el intento V2 en `.tools/evidence/postgres-immich-security-before-v3/`. Tampoco V4 relaja `inside_read`, hace chmod histórico o reutiliza un namespace previo.

La nueva copia procede exclusivamente de `vectorchord-source.tar.gz`, SHA256 `7cc81fe827f41e66dcca37dd4cb6dc171da9f6807ebf3d5892d72edda61fdf28`. El lector trata cada miembro como datos: rechaza enlaces, dispositivos, duplicados, escapes, raíces ajenas y colisiones archivo/directorio; impone los límites de inventario/tamaño. No invoca extract/extractall ni preserva UID/GID/modos del TAR. Escribe nuevos directorios root:0700 por descriptor, archivos 0600 y únicamente ejecutables originales en 0700.

Deriva xtask desde el miembro original usando los bytes verificados de `/inputs/offline_recipe.py` (`c0518a09…`), función `frozen_xtask` y resultado exacto `9055ec9b…`. Sustituye Cargo.lock por el derivado ya acreditado y añade la configuración offline del namespace nuevo. Se verifican los 260 archivos resultantes. La comprobación de integridad histórica por SHA continúa antes/después; la copia y su selección de ejecutables ya no leen ni hacen stat de `/work/source`.

Los nombres operativos son nuevos: owner `agencia-digital-marketing-ai:postgres-immich-security-rebuild:v3`, staging `/home/jandrade/.agency-pg-security-rebuild-v3`, control `<CONTROL_SDK>/security-rebuild-v3`, workspace `/work/.security-rebuild-v3` y salida `/outputs/security-rebuild-v3`. Los recibos locales usan `security-rebuild-v3.<modo>.<uuid>.json`. El prefijo de bóveda V4 es independiente: se requieren receive/prepare/restart nuevos y sus respaldos; ningún positivo ni fallo anterior habilita esas etapas.

## Plan sin leer la bóveda

Desde el workspace:

```powershell
python docker/postgres-immich/security_rebuild_v4.py plan
```

Lee únicamente código y evidencia fijados. No lee `.env`, conecta por SSH, ejecuta SDK ni crea directorios remotos. El perfil incluye paths/SHA/tamaños y referencias a los recibos previos; conserva el historial grande en sus archivos originales.

Publicación exclusiva del perfil en la raíz local privada ya respaldada:

```powershell
python docker/postgres-immich/security_rebuild_v4.py plan --output '.tools/postgres-immich-security-inputs-v1/security-rebuild-v4.profile.json'
```

El archivo debe estar ausente. Después de revisar código/perfil, Root puede respaldar los campos del perfil mediante la opción explícita:

```powershell
python docker/postgres-immich/security_rebuild_v4.py plan --root-update-vault
```

Ese modo exige que el perfil físico coincida antes de actualizar punteros. El agente que prepara esta unidad no invoca el actualizador ni el CLI operacional.

Los campos usan el prefijo `MEDIA_POSTGRES_IMMICH_SECURITY_REBUILD_V4_`: `OWNER`, `CID`, `PROFILE_PATH`, `PROFILE_SHA256`, `LOCAL_SOURCE_SHA256`, `REMOTE_SOURCE_SHA256`, `RECIPE_SOURCE_SHA256` y `STAGING_PATH`, `CONTROL_PATH`, `WORK_PATH`, `OUTPUT_PATH`. No se guarda una copia completa del perfil ni credenciales en esos recibos. El updater original, fijado por SHA, conserva los tres comentarios por variable y la comparación concurrente del master.

## Transiciones explícitas

Cada llamada operacional requiere perfil fijo, SHA externo y `--root-update-vault`. Sustituir el hash público por el respaldado:

```text
python docker/postgres-immich/security_rebuild_v4.py <modo> --expected-profile-sha256 <SHA_RESPALDADO> --root-update-vault
```

| Modo | Acción y requisito |
| --- | --- |
| `receive` | Recibe 11 archivos declarados como UID1000 en staging nuevo; valida tamaños/SHA/EOF antes de escribir. No arranca el SDK. |
| `prepare` | Verifica recibo de recepción, host/layout y todos los bytes; promueve copias a nuevo control SSD root:0700. Captura daemon e identidad del CID detenido. |
| `restart` | Exige prepare respaldado y mismo CID/creación/imagen/configuración detenido. Arranca sólo ese SDK; registra época y cgroup real. |
| `materialize` | Exige restart respaldado. Crea `/work/.security-rebuild-v3` ausente, deriva `source/` desde el TAR fijado con overlays exactos y materializa vendor verificado en `vendor/`. Crea cargo-home/home/tmp/target/harness privados nuevos. |
| `graph` | Ejecuta árboles Cargo cerrados offline/frozen. Exige misma selección/features que el baseline autenticado, salvo las dos versiones corregidas. |
| `regression` | Exige graph respaldado y revisión literal de Root; ejecuta harness funcional de dos pruebas y comprueba nombres, contador y salida real. |
| `build` | Exige graph/regression respaldados y revisados. Ejecuta sólo xtask build, con target y salida nuevos. |
| `catalog` | Verifica artefactos generados, SQLite dinámica y SQL/control, y produce runtime-root nuevo reutilizando archivos anteriores verificados. No ejecuta SQL. |
| `stop` | Independiente de inputs, derivado y recibos de build. Exige control SSD/prepare respaldado, daemon y nacimiento del CID propio. Parada por CID completo, sin búsquedas por nombre/PID ni eliminación de contenedores. |

Después de graph, Root debe respaldar `GRAPH_REVIEW_STATUS=PG16_SIMD_INIT_CONFIRMED_SECURITY_UPGRADES_ONLY` antes de regression/build. No existe avance automático de todas las etapas. Cada recibo local se publica EXCL y se respalda como `<MODO>_RECEIPT_PATH`/`<MODO>_RECEIPT_SHA256` antes de otra llamada.

Antes de restart y de todas las etapas posteriores del SDK, se exige además `SDK_SCA_APPROVED=true`, `SDK_SCA_RECEIPT_PATH` y `SDK_SCA_RECEIPT_SHA256`, bajo el mismo prefijo. El lector comprueba el archivo físico y su SHA antes de abrir SSH. El remoto vuelve a comprobar el recibo y su vigencia antes de actuar. Receive/prepare pueden preparar datos con el SDK detenido; stop queda exento para no impedir la limpieza por evidencia vencida.

El recibo SCA es JSON público de aprobación específica, máximo 64 KiB. Su esquema cerrado exige: `schema_version=1`, `root_approved=true`, `cid`, `created`, `apk_database_sha256`, `apk_package_count=110`, `apk_exact_coverage=true`, `formatter_sha256`, `driver_sha256`, `sdk_state_receipt_sha256` (RECOVER fc334…), `high_critical_count` real, `coverage_scope`, `reports` (lista de path/SHA), `scanned_at_utc` y `db_metadata` (Version2, UpdatedAt/NextUpdate). La fecha debe corresponder a esa DB y NextUpdate debe seguir vigente. No se sustituye una ausencia de CVSS por cero ni se decide automáticamente una excepción: la aprobación y evaluación específica de los resultados corresponde a Root.

La evidencia histórica vencida no habilita ese gate. Para preparar el inventario actual sin arrancar SDK, Root puede leer del CID detenido la DB APK y verificar su SHA `9dd4902449589bccb94034a7e5fd51d280ecd9bee7d7adb7521412b34c07aded`, enumerar exactamente los 110 paquetes y analizar ese inventario con la DB vigente. Debe cubrir por separado los bytes de rustfmt `bc01859…` y su driver `626e0c…`, con sus fuentes/procedencia y límites de cobertura explícitos. Un escáner que detecte cero paquetes del formatter no lo certifica. Esta unidad sólo impone el gate y propone esas entradas; no crea ni ejecuta un auditor nuevo ni afirma SDK limpio.

`stop` carga únicamente su perfil, código de control y prepare respaldado, sin revalidar NAS, DB, vendor o outputs exitosos. Puede coexistir con la operación detenida: no espera el flock de build. Las demás etapas usan un lock no bloqueante y marcadores exclusivos; no reintentan ni adoptan parciales.

## Fuentes, entornos y presupuestos

La carga de helpers congelados usa sus bytes después de verificar SHA; no ejecuta `.pyc` ni modifica globals/API de esos helpers. Se reutilizan el conector SSH, operaciones por descriptor, validador Docker/cgroup, recolector POSIX y prueba ELF/SQLite originales. Los verificadores históricos que escribían en namespaces viejos no se invocan.

El CLI operacional de Root lee directamente el master `.env` con parser canónico fijado, ACL nativa fijada y límite explícito de **8 MiB**. No genera otro archivo de credenciales. `connect(values)` es el original congelado: destino .85, identidad/pin Ed25519 exactos, Paramiko 5.1.0, sin agente ni fallback de claves. El secreto SSH queda en memoria del conector; no se serializa en perfil, argv, stdout ni recibos.

Los procesos Cargo reciben un entorno desde cero. RUSTC/CARGO/RUSTDOC apuntan al SDK fijo, RUSTFMT al aumento acreditado; wrappers, runners, flags heredados, proxies y credenciales están ausentes. Se conservan explícitamente `RUSTC_BOOTSTRAP=1`, flags PG16/musl/rpath del perfil anterior y SQLite dinámica. El CARGO_TARGET_DIR es nuevo y no reutiliza objetos compilados anteriores.

El nuevo `.cargo/config.toml` apunta exclusivamente a `/work/.security-rebuild-v3/vendor` y activa offline; las invocaciones usan `--frozen --offline`. El parser nuevo admite sólo `source/` bajo ese workspace exacto. Únicamente el baseline antiguo ya autenticado adapta su prefijo para compararse; el grafo real nunca se normaliza para esconder rutas ajenas.

El SDK conserva red none, 4 CPU/12 GiB/256 PID y jobs=2. Presupuestos por modo: receive180 s, prepare120 s, restart60 s, materialize/graph/regression300 s, build1800 s, catalog180 s, stop20 s. La alarma de operación reserva veinte segundos de limpieza tras las etapas que pueden ejecutar el SDK. El recolector POSIX congelado limita cada salida a 4 MiB, conserva el grupo propio hasta recogerlo y mata sus hijos al terminar; un latido SSH perdido interrumpe la operación. La limpieza del contenedor usa el CID acreditado y verifica stopped, sin certificarlo si falla.

La recepción y el reempaquetado/hash local no son pruebas de velocidad de compilación. Los límites no prometen resolver esperas de disco/kernel imposibles de interrumpir instantáneamente; cualquier fallo conserva evidencia propia y no produce aceptación ficticia.

## Alcance del harness

Se generan fuentes/manifest/lock propios bajo `regression/`, con **cuatro paquetes**: harness, anyhow 1.0.103, crossbeam-epoch 0.9.20 y crossbeam-utils 0.8.21 ya vendorizado. Los dos crates corregidos son dependencias por path fuera del workspace del harness, de modo que sus dev-dependencies no se añaden a la clausura. No se ejecuta cargo update, tidy ni descarga de faltantes; si el lock propuesto no es aceptado por Cargo frozen, se detiene para revisar, sin inventar un resultado positivo.

El caso epoch reproduce `format_null` oficial con Atomic/Shared, tanto sized como slice. El caso anyhow prueba mutación del contexto mediante downcast_mut y su resultado observable. **No demuestra ausencia de UB de aliasing ni rojo/verde Miri**: no hay Miri acreditado y no se instala. El recibo conserva `anyhow_ub_miri_proven=false`, aunque la prueba funcional pase. La corrección de esa propiedad se apoya además en el parche/fingerprint oficial y el aviso de versión, con revisión específica de Root.

Se exige exactamente una línea completa para cada una de las dos pruebas aprobadas y un único resumen positivo completo, con cero fallidas/ignoradas. Se rechazan prefijos, sufijos, duplicados, pruebas adicionales, controles, UTF-8 inválido y cualquier registro FAILED o de fallos contradictorio. Se admiten saltos LF/CRLF y los mensajes normales de compilación. No basta exit0 ni un harness de cero pruebas. Las licencias MIT/Apache-2.0 de los crates quedan en el vendor; los casos derivados del upstream conservan su atribución en la fuente generada.

V4 conserva también la corrección del validador de registros de V2 y el materializador V3. Los perfiles anteriores permanecen intactos y el CLI nuevo publica exclusivamente `security-rebuild-v4.profile.json`. Root debe revisar y respaldar el nuevo perfil/SHA, namespace y gate SCA actual antes de cualquier operación; no existe fallback hacia campos del prefijo anterior.

## Verificaciones de esta entrega

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -p test_postgres_immich_security_rebuild_v4.py -v
python -m ruff check docker/postgres-immich/security_rebuild_v4.py docker/postgres-immich/security_rebuild_remote_v4.py tests/test_postgres_immich_security_rebuild_v4.py
python -m ruff format --check docker/postgres-immich/security_rebuild_v4.py docker/postgres-immich/security_rebuild_remote_v4.py tests/test_postgres_immich_security_rebuild_v4.py
```

Las pruebas portables usan fixtures/mocks sin conexiones. Las pruebas dir_fd/flock se omiten honestamente en Windows y requieren Linux real antes de certificar esa frontera. Ninguna prueba Python ejecuta el harness Rust o un SDK. La aceptación SQL y la nueva imagen siguen pendientes de compilación, catálogo, SCA y aprobación posteriores.

La suite contiene 38 métodos: 35 portables y tres POSIX. Mantiene los casos TAR/EXCL previos y agrega la selección PG16 de vchord, el contrato xtask inalterado y la llamada efectiva al constructor conservando el rechazo de un grafo sin PG16. Las funciones de xtask se usan sólo como transformaciones de bytes; ningún test Python ejecuta Cargo ni compila Rust.

Para Linux/root hacen falta ocho entradas fijadas: los dos módulos nuevos, su test, `scripts/ci/check_env.py`, `offline_sdk_remote_v4.py`, `offline_recipe.py`, el remoto V3 como dato para comparar argv y el TAR original en su ruta relativa de fixture. Se propone un namespace nuevo `<CONTROL_SDK>/security-rebuild-tests-v4`, Python host, bytecode desactivado y ResourceWarnings estrictos; no requiere credenciales, `.env` real, contenedor, SDK o restart. El inventario/plan separado exige 38/38, cero skips, antes de acreditar la frontera POSIX. Las tres pruebas POSIX permanecen pendientes tras la corrida Windows.
