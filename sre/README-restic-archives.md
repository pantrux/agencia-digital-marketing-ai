# Staging estático de los módulos restic seleccionados

Esta unidad recibe ZIP como **datos**, sin ejecutar Go/SDK, extraer archivos, compilar ni modificar la bóveda. Usa exactamente los 284 módulos externos de la selección MVS v2 y su OSV de 286 identidades, cero avisos/omisiones y paginación agotada. No vuelve al cierre histórico de mil versiones. La fuente restic original, las propuestas y los cachés previos no se sobrescriben.

## Entradas y propiedad

El [controlador](stage_restic_archives.py) fija el inventario `restic-security-v2-selected-verified.json` SHA-256 `9e528979108edce274e45127ab60626cc9ccec003a2063162dce122b6e567029`, el OSV `90f6a068317789a6b36c7ec0783de29051ed43f0224e2e76ba54b74e4c456836`, el inventario actual de caché `f3a7432e…` y el go.sum posterior a MVS `3de5391b…`. Los 568 archivos `.mod`/`.info` elegidos se cotejan con esa caché verificada, se leen como UTF-8 y conservan sus bytes/hash en el plan. No se reconstruyen metadata ni checksums ausentes.

La única raíz es `BACKUP_RESTIC_SECURITY_V2_ARCHIVES_ROOT`, ya respaldada por raíz: `restic-security-v2/module-archives-v1`, owner `agencia-digital-marketing-ai:restic-module-archives:v1`. `--prepare` sólo admite crearla si está ausente, después de verificar el padre v2 y su ACL privada. Escribe primero el marcador de owner/dev/inodo y después el plan. No adopta otra raíz por nombre, no amplía permisos ni reemplaza archivos existentes. Los nuevos descendientes heredan la raíz privada. Un lock EXCL impide dos procesos de staging; un lock dejado por una interrupción requiere diagnóstico, nunca se borra por PID.

## ZIP, nombres y h1

El [validador](restic_archive_inputs.py) deriva HashZip/Hash1 de los bytes físicos del SDK Go 1.27.3, cotejados con SourceManifest `736791…`: `sumdb/dirhash/hash.go` SHA `24de6a…`, `zip/zip.go` `3a0fd2…` y `module/module.go` `aa2b67…`. Esa lectura no ejecuta el SDK ni lo declara limpio. HashZip incluye **todas** las entradas, también directorios vacíos; Hash1 ordena nombres completos por sus bytes UTF-8 y resume SHA256 de contenido, dos espacios, nombre y LF antes del SHA256/base64 final.

Se inspeccionan los nombres **RAW** centrales y locales con UTF-8 estricto y comparación literal; CP437 de `ZipFile` no se presenta como prueba. Se exige prefijo exacto `módulo@versión/`, sin escapes, ADS, rutas absolutas, enlaces/tipos especiales, nombres DOS, go.mod anidado o de otra capitalización, duplicados y colisiones archivo/directorio. `casefold` es conservador frente a SimpleFold de Go y puede rechazar nombres adicionales; tampoco se permiten espacios/puntos finales de componentes para conservar compatibilidad Windows. Una incompatibilidad legítima se reporta por input y **no se relaja automáticamente**.

El máximo es 500 MiB comprimidos y expandidos por ZIP, 100.000 entradas y 8 MiB de nombres. La lectura streaming comprueba tamaños reales y CRC, calcula SHA256 y valida completamente UTF-8 de `.go`, `.s` y `.S`; un archivo binario con otra extensión no se etiqueta como texto. No se llama a extract, extractall, ldd, Go ni herramientas de los archivos. Las licencias se registran por nombre, bytes, SHA y SPDX declarado; no equivale a revisión jurídica ni a una licencia única del grafo.

El h1 se compara con go.sum y notas lookup cacheadas **cuando existen**; ambas fuentes deben coincidir. El plan inicial observó h1 para 265 módulos: 101 en go.sum y 164 en lookup. Los otros 19 reciben cálculo sin comparación previa. Son comparaciones declaradas: Python no implementa firmas/Merkle ni acredita autenticación SumDB. El SDK deberá verificar los ZIP contra SumDB antes de cualquier build, incluidos los que ya tienen h1 declarado.

## Transferencia y publicación

Las URLs iniciales son GoProxy HTTPS con rutas escapadas del módulo/versión. Se aceptan hasta tres redirecciones HTTPS entre `proxy.golang.org` y `storage.googleapis.com`, sin usuario, puerto ajeno o fragmento. No se heredan proxies, auth o cookies. Las queries firmadas viven sólo en memoria de la petición; los recibos guardan la URL GoProxy original y el número de redirecciones. Los errores públicos tienen códigos fijos. La lectura usa un bloqueo de hasta 30 s y comprueba el plazo total de 300 s entre bloques; no se afirma cancelación instantánea de una syscall bloqueada.

El acumulado máximo es 3 GiB comprimidos y 8 GiB expandidos. El ZIP permanece en cuarentena durante la inspección. Una entrada válida se publica sin overwrite mediante hardlink en el mismo filesystem y se retira únicamente su nombre de cuarentena; los destinos son el proxy local `ruta/@v/versión.zip`, `.mod` y `.info`. La metadata se copia por hash desde el caché original, sin moverlo. Un ZIP rechazado queda fuera del proxy con diagnóstico por input y hash cuando existe; los demás inputs pueden seguir. Un límite global detiene el lote. La cuota expandida de entradas inválidas usa una carga conservadora cuando no se conoce su estructura.

Cada entrada recibe un archivo nuevo EXCL que correlaciona plan, índice, módulo/versión, archivos e inodos. Al continuar se revalidan esos bytes y sólo se omiten entradas con recibo propio; un archivo preexistente sin recibo no se adopta. Un fallo entre publicación y recibo conserva evidencia y requiere diagnóstico, sin limpiar ni sobrescribir. El ledger versionado resume los recibos y distingue `complete` de preparación parcial o fallos.

## Operación local autorizada

```text
python -B sre/stage_restic_archives.py
python -B sre/stage_restic_archives.py --prepare
python -B sre/stage_restic_archives.py --apply --plan-sha256 HASH --limit 3
```

El primer comando sólo muestra plan local; no crea rutas ni usa red. Preparación no descarga. La muestra de tres se entrega a raíz con SHA/h1/UTF-8/CRC antes del lote completo. Se continúa con el mismo plan/hash y el límite aprobado; las filas fallidas se conservan y no se reintentan automáticamente. No existe opción para desactivar validaciones. Si cambia fuente/controlador, se revisa otro plan; no se reemplaza el existente.

Las pruebas portables están en `tests/test_restic_archives.py`; usan ZIP adversarios y transportes controlados sin red ni SDK. La aceptación real exige además los recibos. Staging no equivale a compilación, firma SumDB, seguridad del binario, restauración, backup o despliegue.
