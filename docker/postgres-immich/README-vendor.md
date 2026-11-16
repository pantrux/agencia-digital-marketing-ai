# Preparación de fuentes y vendor Cargo

`prepare_vendor.py` prepara únicamente datos de entrada. No ejecuta Cargo, Rust, compiladores, build scripts ni contenido descargado. El manifiesto original SHA-256 `8d5a521ef8b2827f5198b9dcb5148c05f0aad743c7ce39912c75491139f5f745` permanece sin cambios.

```powershell
python docker/postgres-immich/prepare_vendor.py
python docker/postgres-immich/prepare_vendor.py --prepare
```

El primer comando sólo lee y verifica el manifiesto, sin red ni escrituras. El segundo exige Windows, la raíz `.tools/postgres-immich-build`, su marcador respaldado y una ACL protegida cuyo propietario sea el usuario actual y cuyas únicas concesiones heredables correspondan a ese usuario, SYSTEM y Administradores. Rechaza enlaces/reparse points en la cadena de la raíz y un marcador no ordinario.

El transporte real usa `C:\Windows\System32\curl.exe`, observado como **8.22.0 con Schannel**. Mantiene la validación TLS normal, limita HTTPS, no sigue redirecciones, no lee configuración curl del usuario y sólo hereda variables básicas del sistema. Cada descarga exige HTTP 200 y checksum exacto. No transmite credenciales. Las descargas se acotan a cuatro en paralelo, 30 segundos por transferencia y un presupuesto compartido de 20 minutos. Los dos intentos previos con urllib quedaron detenidos; el diagnóstico del segundo acreditó un error de validación del emisor TLS. Sus logs/stagings se conservan y no se añadió ninguna CA ni excepción.

Se descargan la fuente VectorChord fijada y los **244 crates** del lock. Los TAR de origen se inspeccionan como datos; rechaza escapes, duplicados, enlaces, tipos especiales, tamaños excesivos y un `.cargo-checksum.json` preexistente. Windows no extrae las fuentes. El preparador genera un TAR vendor para Linux que conserva bytes, directorios y bits ejecutables; normaliza propietario/fecha y añade checksums Cargo calculados sobre los archivos originales sin modificarlos.

La publicación ocurre al completar el staging, mediante renombrado dentro de la misma raíz privada. Un destino ya existente se rechaza; no se adopta ni sobrescribe. Ante un fallo se conservan los restos privados para diagnóstico, sin borrado recursivo ni reintento automático.

## Resultado real preparado

Ruta: `.tools/postgres-immich-build/vendor-8d5a521ef8b2827f`.

| Archivo | SHA-256 |
| --- | --- |
| `vendor.tar.gz` | `c0209f4b1b6fc31d56e495886cb4729c0c52634504c7a0c627b29d2859a0b2c1` |
| `vendor-lock.json` | `6bd7c4db4c0d297da3a073939f6e78c6d7837bc65d2a245053dac531f9199c57` |
| `source-audit.json` | `77cc172354345cfd1a2c833b83f123a1cd264b9528e0df94f9cb733b8b69f4af` |
| `source-sbom.cdx.json` | `65913e9d16566f4ea08dc152af3143f778c6d4fb43bd3a238b1b3ba2d7506b3d` |

Una segunda lectura completa del TAR comprobó **244 crates y 16.727 archivos fuente**, todos los checksums de archivos y archivos comprimidos, la ausencia de enlaces/tipos especiales y la metadata normalizada. Evidencia: `.tools/evidence/phase4-postgres-immich-vendor-validation.json`.

El SBOM es un inventario de **fuentes sin compilar**. Incluye los crates, las 177 fuentes C/C++ inventariadas y versiones embebidas observadas: mimalloc 2.2.4/3.1.5 y SQLite 3.50.4/3.51.1/3.51.2 en distintos subárboles. Esas versiones no se presentan como seleccionadas o enlazadas. SQLite 3.54.1 sigue siendo la propuesta corregida separada del plan; no se sustituyeron sus fuentes dentro de ningún crate ni se falsearon checksums.

## Revisión de llamadas y bloqueo pendiente

El análisis automático registra 53 archivos `build.rs`, 32 candidatos textuales Git y 157 sitios de creación de procesos. Una revisión más amplia añadió CMake, scripts C y workflows. Se clasificaron 44 sitios entre ambos reportes: datos/documentación, un parser de ejemplo de clap, scripts de CI/release/setup no ejecutados y rutas CMake que el `build.rs` de mimalloc observado no utiliza. Los informes originales se preservan.

Existe una llamada real que requiere resolver la selección: **`wasm-bindgen-shared 0.2.108/build.rs` ejecuta Git incondicionalmente si ese crate se compila**. No se ejecutó. Antes de compilar, con SDK ya fijado y auditado, la raíz deberá ejecutar únicamente metadatos Cargo offline/locked para el target musl y acreditar si queda seleccionado. Si queda seleccionado, se requiere un parche mínimo explícito fuera del vendor original, con origen, licencia, hashes y prueba de ausencia de esa invocación. No basta omitir el ejecutable Git del PATH.

La revisión recuperable está en `.tools/evidence/phase4-postgres-immich-git-manual-review.json`; los candidatos ampliados están en `phase4-postgres-immich-git-broad-candidates.json`. La revisión no habilita el build: todavía faltan el grafo target, el parche frozen de xtask, la clausura APK/SDK y las verificaciones SQLite/ABI/SQL/SCA. Todos los recibos mantienen `build_enabled=false`, `sdk_executed=false` y `compiler_executed=false`.

La unidad tiene pruebas con archivos sintéticos de hashes, rutas, tipos, límites, determinismo, permisos, SBOM y publicación. La aceptación real Linux de la imagen sigue pendiente. No se creó ningún contenedor ni se modificó infraestructura, `.env`, el inventario congelado o los datos de aplicaciones.
