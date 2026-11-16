# Restic: inventario y cierre estático de metadatos

Esta unidad prepara requisitos de módulos y archivos `.mod`/`.info` de restic 0.19.1. **No es un resolvedor MVS ni un build**, no ejecuta Go y no descarga ZIP. Un cierre completo de estos metadatos tampoco acredita SumDB, fuentes compilables, licencias o ausencia de vulnerabilidades.

## Entradas y cambios propuestos

`restic_module_inventory.py` comprueba el tamaño y SHA-256 del archivo oficial congelado y los hashes individuales de `go.mod` y `go.sum` antes de consultar la red. No modifica el archivo comprimido ni el árbol de fuente original. El Plan observado encuentra **93 requisitos** actuales y conserva 93 raíces propuestas, con estos cinco cambios:

| Módulo | Candidato |
|---|---|
| `github.com/klauspost/compress` | v1.18.7 |
| `golang.org/x/crypto` | v0.55.0 |
| `golang.org/x/net` | v0.60.0 |
| `golang.org/x/text` | v0.41.0 |
| `google.golang.org/grpc` | v1.83.2 |

Son candidatos respaldados, no una afirmación de que sus transitivas estén limpias. El archivo `go.sum` original se registra como declaraciones `h1:`: el script valida su formato y contradicciones, **sin presentar una comprobación criptográfica de SumDB realizada en Python**. Tampoco equipara un `h1:` con el SHA-256 crudo de un ZIP.

## Almacenamiento y comandos

Root respalda primero `BACKUP_RESTIC_MODULE_WORK_ROOT`, `BACKUP_RESTIC_MODULE_OWNER_MARKER`, `BACKUP_RESTIC_MODULE_INVENTORY_PATH` y `BACKUP_RESTIC_MODULE_PROXY_ROOT`. El destino propio es `BACKUP_BUILD_ROOT/restic-modules-v1`, con `proxy/` e `inventory.json` derivados. La raíz se crea sólo si está ausente, acreditando previamente ACL privada e identidad del padre; el marcador `.agency-restic-modules-owner.json` y `initialization-receipt.json` deben existir antes de recopilar metadatos. El helper no certifica ACL por sí solo: son una precondición del operador.

```powershell
python sre/restic_module_inventory.py
python sre/restic_module_inventory.py --collect-metadata
```

El primer comando sólo lee entradas y devuelve Plan, sin red ni creación de carpetas. El segundo consulta metadatos y publica el inventario en la raíz respaldada. No permite cambiar repositorio, proxy, upgrades o staging mediante argumentos. Lee la bóveda literalmente mediante el parser canónico, sin ejecutar valores ni imprimirlos.

La escritura conserva los bytes recibidos y añade tamaño/SHA-256. No sobrescribe un archivo distinto ni un inventario previo. Una operación fallida puede dejar archivos propios parciales y un recibo nuevo `incomplete-<id>.json`; no ocupa el nombre del inventario completo. El operador debe conservarlos e investigar. Un reintento compara los metadatos existentes con los bytes recién recibidos y no sustituye diferencias. No se borran caches ni se adopta otro proyecto para reintentar.

## Alcance del cierre

La recopilación sigue **todos los requisitos declarados** de cada par nombre/versión visitado, conserva versiones distintas y elimina ciclos por identidad exacta. No selecciona la versión máxima ni reproduce graph pruning, lazy loading, búsquedas de paquetes o resolución MVS. Las directivas `replace`/`exclude` del módulo principal se rechazan en este incremento; las de dependencias se registran sin aplicarlas, conforme a su alcance en Go. Un formato no soportado causa rechazo, nunca una normalización silenciosa.

El parser acepta formas canónicas de `go.mod` usadas por esta entrega. Strings que requieran escapes Go, directivas desconocidas, identidad duplicada o requisitos repetidos se rechazan para revisión. El helper de versiones acepta versiones **canónicas de módulos**, no todas las abreviaturas del paquete semver general de Go. Prueba orden de prereleases/pseudoversiones, major de ruta, `+incompatible`, escape de mayúsculas y la excepción histórica `gopkg.in/check.v1` con `v0.0.0-*`. La validación autoritativa de versiones, revisiones y selección sigue pendiente de Go.

Las consultas usan sólo `https://proxy.golang.org`, versiones exactas, TLS verificado, cliente nuevo sin autenticación ni proxies del entorno, y sin redirecciones o fallback directo. Se exige UTF-8 en metadatos, identidad exacta en `.mod`/`.info`, tiempo UTC y longitud declarada coherente. Límites: cuatro solicitudes simultáneas, 180 segundos compartidos, 2048 identidades, 16 MiB totales, 256 KiB por `.mod` y 64 KiB por `.info`. Se cancelan y recogen tareas hermanas si hay error. No se ejecutan procesos secundarios.

El diseño de carpetas sigue el protocolo de proxy Go para **versiones exactas**. Al faltar ZIP y listados de versiones, no se presenta como un proxy completo para build ni para consultas `latest`. Las siguientes unidades deben confirmar selección MVS con el Go aislado, verificar SumDB, prefetchear y validar ZIP/fuentes compilables, auditar el grafo efectivo y preparar una copia de build con sus pins. No se cambia el umbral de cero HIGH/CRITICAL del restic resultante.

## Verificación

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -p test_restic_module_inventory.py -v
python -m ruff check sre/go_module_semver.py sre/restic_module_inventory.py tests/test_restic_module_inventory.py
python -m ruff format --check sre/go_module_semver.py sre/restic_module_inventory.py tests/test_restic_module_inventory.py
```

Los tests usan transporte HTTP controlado y almacenamiento en memoria; no consultan el proxy real ni ejecutan SDK. Fuentes primarias de contrato: [referencia de módulos Go](https://go.dev/ref/mod) y [semver de x/mod](https://pkg.go.dev/golang.org/x/mod/semver). Las consultas reales se documentan mediante recibos locales separados, con su cobertura y límites observados.

## Resultado real de esta entrega

El 6 de noviembre de 2026 se completaron las entradas `.mod`/`.info` de las **93 raíces propuestas: 34 directas y 59 indirectas**, incluidos los cinco upgrades. Se conservaron y releyeron contra sus SHA-256 **1045 pares / 2090 archivos / 1 027 212 bytes**. El layout de todas las parejas coincide con `<módulo escapado>/@v/<versión escapada>.mod` y `.info`, apto para consultas de versiones exactas mediante `file://`.

La expansión conservadora se detuvo al alcanzar el límite de 2048 identidades. Al reconstruir únicamente las restricciones de los metadatos ya retenidos, aparecen 2050 pares conocidos y una frontera de **1005 pares pendientes**. No se amplió la búsqueda histórica. `BACKUP_RESTIC_MODULE_WORK_ROOT/frontier.json` contiene las listas completas, hashes, URI local y esa frontera; **no se publicó `inventory.json` como cierre completo**. Ambos intentos incompletos se conservan y la cache reutilizada se revalidó con el ledger propio.

El módulo principal congelado no tiene `replace` ni `exclude`. Los metadatos retenidos contienen 71 módulos con `replace`, 4 con `exclude`, 34 con `retract` y 1 con `tool`; sus bytes originales se conservan. Las directivas `replace`/`exclude` de dependencias no se trasladan al módulo principal. La propuesta siguiente debe copiar el `go.mod`/`go.sum` original, preservando `go`, `toolchain`, comentarios, flags indirectos y `godebug winsymlink=0`, y modificar únicamente las versiones acordadas antes de la selección autoritativa de Go.

Siguen pendientes MVS efectivo, SumDB, ZIP/fuentes compilables, SCA del grafo seleccionado y build. La cache contiene versiones históricas que no deben confundirse con dependencias seleccionadas de restic. Durante esta entrega no se ejecutó ningún SDK ni se modificaron el SDK, Job, instalador o fuente restic original.
