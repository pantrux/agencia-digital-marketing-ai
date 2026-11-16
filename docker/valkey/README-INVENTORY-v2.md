# Publicación V2 del inventario de Valkey

Esta revisión corrige únicamente la guardia de nombres del publicador y usa una
raíz y un propietario nuevos. Conserva el núcleo, los lectores, los contratos y
los datos de origen descritos en el [inventario original](README-INVENTORY.md).
No ejecuta Valkey, Trivy, Docker, SSH ni SDK; tampoco consulta `.env` o la red.

La publicación V1 real rechazó `os-packages.cdx.json` porque su expresión regular
sólo admitía una extensión simple. La raíz V1 y su `failure.json` quedan como
evidencia histórica: esta revisión no los modifica, elimina, adopta ni reutiliza.
No hubo un inventario V1 publicado con éxito.

## Conjunto de salidas cerrado

La entrada de publicación debe ser un diccionario con exactamente estas cinco
claves de texto, respetando mayúsculas y puntuación:

- `inventory.json`
- `file-catalog.json`
- `os-packages.cdx.json`
- `binary-materials.json`
- `provenance.json`

Faltantes, extras, alias, nombres DOS, separadores, escapes, sufijos alternativos
y flujos ADS se rechazan antes del marcador o los archivos de datos. No se
interpreta una ruta aportada externamente. Un intento inválido conserva sólo
su `failure.json` en la carpeta nueva del intento; no se reintenta esa carpeta.
Los hashes y tamaños deben coincidir además con el plan antes de escribirlos.

El núcleo continúa fijado por SHA256 en
`2d1f335fbb458d34c80b3a2c0fa993d75bc5cf0858377ac9bb1a25966976ee6d`.
Se cargan sus mismos bytes verificados, sin elegir `.pyc` ni alterar sus
variables globales. La composición de los cinco documentos permanece igual;
la procedencia y las limitaciones de cobertura siguen explícitas.

## Plan y publicación por Root

El comando predeterminado sólo calcula el plan y los documentos en memoria:

```powershell
python -I -S -B docker/valkey/inventory_image_v2.py
```

Root debe revisar y respaldar las huellas de fuente y plan, la ruta y el
propietario antes de ejecutar la publicación explícita:

```text
python -I -S -B docker/valkey/inventory_image_v2.py --publish --expected-source-sha256 SHA_CLI_V2 --expected-plan-sha256 SHA_PLAN_V2
```

La única raíz de publicación es `.tools/evidence/phase4-valkey-inventory-v2`.
El propietario es `agencia-digital-marketing-ai:valkey-inventory:v2`. La raíz
debe estar ausente: se crea de forma exclusiva, sin adoptar recursos existentes.
Se conservan marcador, identidad de dispositivo/inodo, plan, cinco salidas y
recibo. Los archivos son nuevos, se sincronizan y se releen con el lector
congelado para verificar sus bytes. No hay eliminación de intentos anteriores.

El recibo certifica la publicación de documentos. `binary_accepted`,
`sca_executed` y `runtime_executed` permanecen false. Los 23 APK y los tres
binarios propios siguen como coberturas separadas; ningún escáner se ejecuta.

## Regresión y comprobaciones

Las pruebas portables no necesitan el TAR ni los recibos ignorados de `.tools`:

```powershell
python -I -S -B -W error::ResourceWarning tests/test_valkey_inventory_publish_v2.py -v
```

La regresión adicional real se incorpora explícitamente con `--real`, sin
omitir ningún caso. Requiere las entradas locales fijadas del inventario:

```powershell
python -I -S -B -W error::ResourceWarning tests/test_valkey_inventory_publish_v2.py --real -v
```

Esta variante llama al `prepare()` real en sólo lectura y publica sus cinco
salidas reales dentro de una `TemporaryDirectory` propia. Verifica el contenido
exacto, tamaños, hashes, marcador, plan y recibo, y rechaza una segunda
publicación en el mismo destino. La carpeta temporal se limpia al finalizar.
No publica la raíz de Root ni modifica la CLI V1 o el núcleo congelado.

Los demás casos comprueban la aceptación del CDX, el conjunto cerrado, los
nombres peligrosos, la manipulación de hashes, la preservación de directorios
ajenos y el modo predeterminado sin publicación. La primera ejecución roja
reprodujo el rechazo `OUTPUT_NAMES` de los cinco nombres previstos antes del
cambio de guardia.

```powershell
python -m ruff check docker/valkey/inventory_image_v2.py tests/test_valkey_inventory_publish_v2.py
python -m ruff format --check docker/valkey/inventory_image_v2.py tests/test_valkey_inventory_publish_v2.py
```
