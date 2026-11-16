# Inventario de seguridad del SDK detenido

Esta unidad prepara datos para revisar el SDK `f042…`; no lo inicia, no ejecuta Rust, Trivy, SQL, SSH ni contenedores y no lee la bóveda. La aprobación sigue pendiente de Root. El controlador de recompilación y las evidencias anteriores permanecen separados.

La captura real de sólo lectura `phase4-postgres-immich-stopped-sdk-apk-readonly-v1.json` acredita PID 0, estado detenido, identidad del contenedor y base APK de 594.176 bytes. Su SHA256 es `9dd4902449589bccb94034a7e5fd51d280ecd9bee7d7adb7521412b34c07aded`. El inventario observado contiene **110 APK**: coincide exactamente con los **45 paquetes completos de la base** más los **65 agregados** declarados. La comparación conserva nombre, versión, arquitectura, licencia literal, origen y Q1; no usa el subconjunto `base_reused` para inferir el total. No hubo paquetes faltantes, extras o metadatos diferentes.

El manifiesto [sdk-inventory-inputs.json](sdk-inventory-inputs.json) fija todas las entradas por ruta y SHA256. El preparador comprueba UTF8, cabeceras y paquetes únicos, tamaño, archivos regulares e identidad estable durante la lectura. Sólo analiza la cabecera previa al primer `F:` de cada registro; los campos repetidos del catálogo de archivos no son metadatos de un paquete nuevo. `.postgresql-rundeps` no declara licencia ni origen: ambos quedan vacíos. Un Q1 representa el control APK y no se presenta como hash del archivo APK completo. Las URL de descarga de los 65 APK y sus SHA256 permanecen distintas de las URL de sus proyectos.

## Identidad del sistema operativo

El TAR de la base `c908ada0…` se comprueba por SHA256, índice OCI, manifiesto, config `22fdd47f…`, los 12 blobs de capas y sus DiffIDs descomprimidos. De esos bytes se leen en memoria `etc/alpine-release` y `usr/lib/os-release`: **Alpine 3.24.4**. El enlace `etc/os-release` apunta a `../usr/lib/os-release`. El paquete `alpine-release=3.24.4-r0` de la captura coincide. La mayor capa descomprimida observada ocupa 286.311.936 bytes; el lector fija un máximo de 384 MiB por capa y no extrae archivos.

Esta prueba identifica los archivos de la base y el paquete instalado. La captura de la DB no es una nueva lectura de todos los archivos del SDK; si Root necesita acreditar que nadie modificó posteriormente `os-release`, debe recoger sólo esos archivos del contenedor detenido y cotejarlos. No se requiere iniciar el SDK para hacerlo.

## Componentes fuera de APK

La distribución oficial fechada está ligada a `channel-rust-beta.toml` del **2026-11-05**, SHA256 `325df537…`, y al commit `01ddda61fada79bbc835476209518a6f68322984`. Las URL y SHA256 de los cuatro componentes se cotejan contra ese manifiesto local ya fijado. [El modelo de canales de Rust](https://rust-lang.github.io/rustup/concepts/channels.html) distingue beta de estable; no cambiamos esa clasificación.

| Componente | Versión declarada en distribución | Observación histórica fijada |
|---|---|---|
| rustc / rust-std | `1.100.0-beta.10 (01ddda61f 2026-11-04)` | rustc CLI coincide; payload de Rust: 152 archivos, fingerprint `d17dc661…` |
| Cargo | `0.101.0-beta.10 (72b77b8d9 2026-10-17)` | CLI: `cargo 1.100.0-beta.10 (72b77b8d9 2026-10-17)`; se conservan ambas representaciones |
| rustfmt-preview | `1.11.0-beta.10` | CLI: `rustfmt 1.11.0-beta (01ddda61fa 2026-11-04)`; binario `bc01859c…` |
| Biblioteca del compilador requerida por rustfmt | Mismo manifiesto/commit | `librustc_driver-03ff6c29172e9b06.so`, 310.364.368 bytes, SHA256 `626e0c6e…` |

El recibo de inspección conserva los archivos `LICENSE-APACHE` y `LICENSE-MIT` de rustfmt y sus hashes; esto no atribuye una licencia única a todas las bibliotecas de terceros de la distribución. La igualdad con la aceptación histórica del formatter se coteja en el perfil de compilación V3. Esta unidad no repite una consulta de versión ni una exportación de sus binarios actuales.

[RustSec describe su base como avisos sobre crates publicados en crates.io](https://rustsec.org/). Por ello no se consulta un crate ficticio `rustc`, `rustfmt` o `cargo` con la versión del CLI, ni se modela Rust como un APK de Alpine. Sus dependencias internas, LLVM, stdlib y ELF manuales requieren otra cobertura; cero resultados del escaneo APK no los certifica. [La política de Rust](https://rust-lang.org/policies/security/) cubre su distribución y limita sus actualizaciones a las líneas actuales; una beta fijada no garantiza mantenimiento indefinido.

La revisión de anuncios primarios del 2026-11-06 conserva tres avisos de Cargo con sus alcances:

| Aviso | Corrección publicada por upstream | Clasificación para esta distribución |
|---|---|---|
| [CVE-2026-33056](https://blog.rust-lang.org/2026/03/21/cve-2026-33056/) | Rust 1.94.1; extracción de crates y permisos | La línea 1.100 es posterior al piso corregido; inferencia de procedencia, sin reproducir el defecto |
| [CVE-2026-5222](https://blog.rust-lang.org/2026/05/25/cve-2026-5222/) | Rust 1.96; normalización de URL sparse, severidad oficial baja | Fuera del intervalo de distribuciones que el aviso identifica como afectadas |
| [CVE-2026-5223](https://blog.rust-lang.org/2026/05/25/cve-2026-5223/) | Rust 1.96.0; enlaces en crates de registros alternativos, severidad oficial media | Fuera del intervalo anterior a 1.96 indicado por el aviso |

Es una revisión específica de los avisos y procedencia descritos, no una afirmación de limpieza universal de Rust o rustfmt ni de todas sus dependencias. Root debe decidir la aprobación del uso cerrado de este SDK con esos límites y fingerprints. El runtime candidato `96c…`, sus crates originales y los árboles SQLite históricos no reciben aprobación de esta unidad.

## Preparación local y futura comprobación

Sin argumentos, el comando valida entradas y muestra el resumen, sin publicar datos:

```powershell
python docker/postgres-immich/prepare_sdk_inventory.py
python -W error::ResourceWarning -m unittest discover -s tests -p test_postgres_immich_sdk_inventory.py -v
```

Para publicar el inventario, el SBOM CycloneDX y el perfil propuesto como archivos nuevos:

```powershell
python docker/postgres-immich/prepare_sdk_inventory.py --output-prefix .tools/evidence/phase4-postgres-immich-stopped-sdk-inventory-v1
```

El prefijo produce `.inventory.json`, `.cdx.json` y `.profile.json` mediante creación exclusiva. No sobrescribe evidencia. El perfil liga el inventario/SBOM por SHA256, la captura y el estado del contenedor; todas sus aprobaciones están en `false`. El SBOM identifica al preparador propio, conserva la advertencia de SBOM de terceros y contiene 110 bibliotecas APK más la identidad del OS; no se hace pasar por un documento generado por Trivy. Los campos de origen se transmiten usando las propiedades que entiende [el decodificador oficial de Trivy 0.76.2](https://raw.githubusercontent.com/aquasecurity/trivy/v0.76.2/pkg/sbom/io/decode.go).

**El escaneo queda a cargo de Root después de liberar la caché compartida.** El perfil propone Trivy 0.76.2 y la DB vigente `trivy-cache-current-20261106`; se deben conservar su metadata, salida, errores y advertencias con hashes. Se requiere `--list-all-pkgs`, JSON y el umbral HIGH/CRITICAL vigente, sin ocultar avisos no corregidos. Debe comprobarse vigencia real de la DB en el momento de la ejecución y actualizar la propuesta si ha vencido. Esta unidad no utiliza esa caché ni ha ejecutado el escaneo.

Después del escaneo efectuado aparte, el mismo preparador acepta `--validate-report RUTA_JSON`: exige OS Alpine 3.24.4, biyección de los 110 nombres/versiones/arquitecturas, origen y versión fuente declarados. Rechaza cero paquetes, omisiones, extras, duplicados o distro diferente. Conserva el número de vulnerabilidades y HIGH/CRITICAL; su salida nunca emite aprobación `true`. La aceptación SDK de Root debe combinar el resultado físico del scan con esta cobertura y su revisión específica de los componentes no APK antes de autorizar restart/build.
