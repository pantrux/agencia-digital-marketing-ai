# Entradas APK y SDK, sin instalación

`stage_native_sdk.py` prepara archivos dentro de la raíz privada respaldada
`.tools/postgres-immich-build`. El modo predeterminado sólo muestra el plan:

```powershell
python docker/postgres-immich/stage_native_sdk.py
python docker/postgres-immich/stage_native_sdk.py --prepare
```

Exige el manifiesto `musl-build-inputs.json` con SHA-256
`8d5a521ef8b2827f5198b9dcb5148c05f0aad743c7ce39912c75491139f5f745`,
los verificadores locales fijados por SHA-256 y la raíz con propietario,
marcador y ACL privada ya acreditados. Reutiliza los dos índices congelados
sólo si sus hashes coinciden y vuelve a verificar sus firmas con la clave
pública Alpine fijada. La base instalada procede de la capa OCI ya verificada;
se exige su hash original antes de resolver dependencias.

La clausura conservadora contiene 65 APK nuevos y reutiliza 20 paquetes de la
base sin actualizarlos. `isl26=0.26-r2` es un pin complementario explícito para
resolver la ambigüedad de proveedores de `libisl.so.23`; no modifica el
manifiesto original. Cada APK debe coincidir con nombre, versión, tamaño,
identidad Q1 del control autenticado y SHA-256 del payload comprimido.

Los paquetes oficiales `build-base`, `fortify-headers`, `llvm21-test-utils-pyc`,
`pyc` y `python3-pyc` declaran `arch=noarch` en el control firmado, aunque el
índice del repositorio x86_64 declara `A:x86_64`. El verificador admite esa
combinación y conserva las otras comprobaciones; rechaza binarios de otra
arquitectura. El primer intento detectó esta diferencia y quedó preservado.

Los tres archivos oficiales Rust (`rustc`, `cargo`, `rust-std`) se descargan
por HTTPS normal desde sus URLs fechadas y se comparan con los SHA-256
respaldados. El catálogo TAR calcula hashes de todos los archivos y metadatos
ELF, sin extraerlos, ejecutar instaladores, Cargo, compiladores o scripts de
construcción. Las descargas no siguen redirecciones, no usan configuración
personal de curl ni desactivan TLS. El cliente HTTPS recibe sólo el entorno
de sistema mínimo.

## Reanudación explícita de descargas propias

Un intento real descargó los 65 APK completos pero curl de Windows terminó
con `3221225477` (`STATUS_ACCESS_VIOLATION`). Ese error impidió la publicación.
El directorio y su informe de fallo se conservan. Para reutilizar esos datos:

```powershell
python docker/postgres-immich/stage_native_sdk.py --prepare --reuse-staging .native-staging-<32-caracteres-hexadecimales>
```

El nombre debe identificar un staging dentro de la raíz privada, con marcador
de propietario y manifiesto exactos y sin enlaces. Se vuelve a verificar cada
APK firmado antes de copiarlo al nuevo staging. Los archivos Rust existentes
se reutilizan sólo tras comparar su SHA-256 respaldado. No se sobrescriben ni adoptan
destinos finales, y las descargas nuevas se realizan en secuencia. Un fallo no
elimina los archivos ni publica un recibo de éxito.

## Resultados y límites

El directorio final `native-inputs-8d5a521ef8b2827f` contiene los originales
descargados, `native-sdk-lock.json`, `sdk-files.json`,
`sdk-source-sbom.cdx.json` y `receipt.json`. El recibo registra los hashes de
los tres inventarios. El SBOM describe entradas de SDK y paquetes base
reutilizados; no representa una imagen final ni acredita su seguridad.

La preparación real verificó los 65 APK (60 x86_64 y cinco noarch), los tres
archivos Rust y sus 188 archivos internos, sin enlaces. Los 22 ELF catalogados
son amd64; sus intérpretes dinámicos son musl y sus tablas dinámicas no
declaran versiones GLIBC. Esto acredita metadatos, no funcionamiento del SDK.
La revisión léxica de los tres `install.sh` no encontró referencias a Git y
ninguno se ejecutó.

El análisis Trivy del inventario derivado con contexto Alpine 3.24.4 y origen
de cada paquete reconoció los 85 paquetes y no encontró HIGH ni CRITICAL.
Se preserva su advertencia sobre SBOM de terceros. El SBOM original de fuentes
no declara un sistema operativo y no basta por sí solo para esa auditoría;
el primer análisis sin ese contexto no detectó paquetes y no se considera
aprobado. Ninguno de estos resultados cubre completamente los binarios Rust
ni sustituye al análisis de la imagen final.

`sqlite-libs=3.53.4-r0`, incorporado por las utilidades LLVM/Python, permanece
visible en este inventario de construcción. Es distinto de la propuesta de
SQLite 3.54.1 para enlazar la extensión. La selección y presencia en el
enlace final todavía requieren comprobación, sin ocultar C vendorizado.

Antes de construir siguen pendientes: respaldo de los nuevos hashes, auditoría
completa del SDK, confirmación nativa de la clausura y firmas mediante APK, grafo Cargo
con destino musl, tratamiento explícito del `build.rs` que invoca Git y de los
procesos Cargo hijos de xtask, ABI, carga de extensiones y pruebas SQL. Ningún
recibo de esta preparación habilita construcción, instalación o producción.
