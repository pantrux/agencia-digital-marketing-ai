# Vendor alternativo para las dos correcciones Rust

Este preparador sólo deriva datos. Conserva la fuente VectorChord, Cargo.lock, vendor y archivos crate originales. No ejecuta Cargo, Rust, build.rs, Git, subprocesos, conexiones de red, SQL ni contenedores. La imagen actual conserva sus avisos y no se declara corregida por preparar estos archivos.

La propuesta aprobada cambia `anyhow 1.0.101 → 1.0.103` y `crossbeam-epoch 0.9.18 → 0.9.20`. Los archivos oficiales y sus checksums están fijados en [security-vendor-inputs.json](security-vendor-inputs.json). El lock fuente tiene 258 bloques package; 244 pertenecen al registro. Sólo se sustituyen version y checksum de esos dos bloques, conservando comentarios, bytes ajenos y la semántica restante. El vendor resultante mantiene 244 crates: los otros 242 conservan exactamente sus archivos y modos ejecutables.

## Plan y respaldo previo

Desde la raíz del proyecto, el modo predeterminado lee los inputs, verifica la identidad privada respaldada y calcula el plan en memoria:

```powershell
python docker/postgres-immich/prepare_security_vendor.py
```

Para guardar únicamente el plan, sin crear el derivado:

```powershell
python docker/postgres-immich/prepare_security_vendor.py --write-plan
```

Publica `derived-v1.plan.json` por creación exclusiva en `.tools/postgres-immich-security-inputs-v1`. Su SHA256 se calcula sobre los mismos bytes canónicos guardados. No sobrescribe un plan anterior. El operador debe respaldar ese SHA, el preparador, el manifiesto y los nombres siguientes antes de aplicar:

| Campo propuesto | Valor |
| --- | --- |
| `MEDIA_POSTGRES_IMMICH_SECURITY_DERIVED_ROOT` | `<workspace>/.tools/postgres-immich-security-inputs-v1/derived-v1` |
| `MEDIA_POSTGRES_IMMICH_SECURITY_DERIVED_OWNER` | `agencia-digital-marketing-ai:postgres-immich-security-derived:v1` |
| `MEDIA_POSTGRES_IMMICH_SECURITY_DERIVED_PLAN_PATH` | `<workspace>/.tools/postgres-immich-security-inputs-v1/derived-v1.plan.json` |
| `MEDIA_POSTGRES_IMMICH_SECURITY_DERIVED_PLAN_SHA256` | SHA exacto emitido por `--write-plan` |
| `MEDIA_POSTGRES_IMMICH_SECURITY_DERIVED_PREPARER_SHA256` | SHA de `prepare_security_vendor.py` congelado |
| `MEDIA_POSTGRES_IMMICH_SECURITY_DERIVED_INPUTS_SHA256` | SHA de `security-vendor-inputs.json` congelado |

La unidad actual entrega el plan para revisión. La materialización requiere respaldo y autorización de Root. El comando posterior es:

```powershell
python docker/postgres-immich/prepare_security_vendor.py --apply --plan-sha256 <SHA_RESPALDADO>
```

El SHA es un identificador público de integridad, no una credencial. El preparador no lee ni escribe `.env`. Recalcula el plan completo, exige igualdad byte a byte con el plan guardado y valida el hash externo antes de crear el directorio derivado.

## Salidas y guardas

`derived-v1` debe estar ausente. Su creación privada usa el helper nativo ya congelado, cuyos bytes y los del verificador ACL se comprueban antes de importarlo. La raíz superior debe conservar el dispositivo/inodo y marcador respaldados; las rutas rechazan symlinks, reparse points, hardlinks de archivos, escapes y componentes ambiguos. Los padres del workspace son administrados por el operador; no se promete aislamiento frente a otra identidad privilegiada que pueda reemplazarlos.

Dentro del nuevo directorio se publican por creación exclusiva:

- `.owner.json`: propietario lógico, identidad física y hash del plan.
- `Cargo.lock`: nueva copia con los dos bloques corregidos.
- `vendor.tar.gz`: TAR para Linux, con fechas/propietarios normalizados y gzip determinista; no se extrae el árbol de fuentes en Windows.
- `vendor-lock.json`: inventario completo y hashes de todos los archivos de los 244 crates.
- `build-script-audit.json`: inspección de los dos candidatos y hallazgos de procesos/Git, como datos.
- `receipt.json`: éxito sólo después de comprobar las salidas y revalidar las entradas originales.

Cada `.cargo-checksum.json` contiene los hashes de sus archivos y el checksum del archivo crate oficial. Los miembros TAR rechazan nombres duplicados, colisiones de mayúsculas, enlaces, tipos especiales, rutas de escape y límites excesivos. El total descomprimido se limita a 2 GiB y cada archivo a 64 MiB. Se conservan las dos licencias MIT y Apache-2.0 de cada candidato.

Un fallo deja el directorio propio y sus archivos para diagnóstico; no borra ni adopta salidas parciales y no emite recibo de éxito. Una segunda aplicación contra un directorio existente se rechaza. La recuperación requiere otra revisión del operador, sin sobreescrituras automáticas.

## Build scripts y límites pendientes

La inspección del `build.rs` fijado de anyhow **sí identifica dos lanzamientos de rustc**: un probe de capacidades y `rustc --version`. Usa `RUSTC` y contempla `RUSTC_WRAPPER`, `RUSTC_WORKSPACE_WRAPPER`, `CARGO_ENCODED_RUSTFLAGS` y `RUSTC_BOOTSTRAP`. No se observó Git ni acceso de red en ese script. Una futura compilación debe fijar el compilador autorizado y limpiar/fijar esas variables; el controlador acotado tendrá que incluir dichos procesos en su presupuesto.

El `build.rs` fijado de crossbeam-epoch consulta `CARGO_CFG_SANITIZE` y emite configuración; no lanza procesos. Ninguno de los scripts se ejecuta aquí. El informe automatizado no sustituye esta revisión manual ni la revisión de todos los build scripts del proyecto.

Se requieren después un lock/grafo efectivo confirmado, SCA completo actualizado, pruebas explícitas, compilación acotada, imagen nueva y verificación operativa. Los avisos de mantenimiento y la clasificación particular de rand/SQLite del recibo nativo siguen conservados. No se declara todavía producción, imagen corregida ni SQL aceptado.

## Verificación local de la herramienta

```powershell
python -m unittest discover -s tests -p test_postgres_immich_security_vendor.py -v
python -m ruff check docker/postgres-immich/prepare_security_vendor.py tests/test_postgres_immich_security_vendor.py
python -m ruff format --check docker/postgres-immich/prepare_security_vendor.py tests/test_postgres_immich_security_vendor.py
```

Las pruebas usan archivos temporales propios. No requieren `.tools` preexistente, credenciales, SDK, red ni infraestructura. Los helpers privados sólo se requieren para la operación real de plan/aplicación en Windows.
