# Rueda local de Zou con Psycopg del sistema

Esta unidad prepara `zou==1.1.1+agency1` a partir de la rueda oficial
`zou==1.1.1`, commit `8f92792de9faeeb623e383311fa80828492bac37`.
El código, la licencia AGPL-3.0-only, los recursos y los entry points se
conservan byte a byte. No instala paquetes, importa Zou, carga libpq ni consulta
una base de datos. El plan es el comportamiento predeterminado.

## Cambio cerrado

Sólo cambian tres cabeceras de `METADATA`:

| Original | Distribución local |
| --- | --- |
| `Version: 1.1.1` | `Version: 1.1.1+agency1` |
| `Requires-Dist: psycopg[binary]==3.3.4` | `Requires-Dist: psycopg==3.3.4` |
| `Requires-Dist: psycopg[c]==3.3.4; extra == "prodpg"` | `Requires-Dist: psycopg==3.3.4; extra == "prodpg"` |

Se renombra el directorio `.dist-info` a la versión local y se regenera
`RECORD` con SHA256, tamaños y cobertura exacta. Los demás bytes de `METADATA`,
incluida su descripción, permanecen intactos. Se conservan todos los extras,
incluido el marcador `prodpg`; ese extra ya no vuelve a pedir la implementación C.
No se añade `psycopg_pool`: Zou usa `postgresql+psycopg` y el pool de SQLAlchemy.
La versión que un módulo pudiera declarar dentro de su código permanece sin
modificar; la identificación `+agency1` corresponde a la distribución local.

La [especificación de ruedas](https://packaging.python.org/en/latest/specifications/binary-distribution-format/)
define `.dist-info` y `RECORD`; [PEP 440](https://peps.python.org/pep-0440/#local-version-identifiers)
define la versión local. Se rechazan firmas `RECORD.jws`/`RECORD.p7s` para evitar
presentar una firma upstream como válida después del cambio. No se añade una
firma ni una atribución de aprobación upstream.

## Entradas, raíz propia y plan

Los pins están en [zou-wheel-derivative-inputs.json](zou-wheel-derivative-inputs.json).
La rueda original conserva SHA256
`4f753a130dde563da7ef48d5ec4b403630740738a9451226e3d0687041a72455`.
Una copia intacta reside bajo `.tools/kitsu-zou-wheel-derivative-v1`, cuyo marcador
es `.agency-zou-derived-owner.json` y cuyo propietario lógico es
`agencia-digital-marketing-ai:zou-wheel-derivative:v1`. Root respalda su identidad,
el marcador, el recibo, los pins y el plan antes de publicar la salida.

El controlador verifica rutas y ancestros sin enlaces/reparse, archivos regulares
con un enlace, identidad dev/inode y ACL privada. Rechaza entradas ajenas en la
raíz. No crea ni adopta la raíz desde la CLI. Lee las 142 ruedas ya congeladas
por SHA, contrasta sus `METADATA` con el inventario y vuelve a evaluar los
marcadores para Linux amd64/CPython 3.12.16 usando `packaging==26.2`.

El helper de acceso se carga compilando los mismos bytes cuyo SHA256 acaba de
verificarse, con `__file__` explícito. No se vuelve a abrir mediante un loader ni
se consulta un `.pyc`. La regresión incluye un `.pyc` sintético válido por fecha
y tamaño que el loader anterior ejecutaba, y un loader sustituto que debe
permanecer sin uso. El freeze y el plan v1 quedan archivados antes de esta revisión.

La clausura propuesta contiene exactamente 141 distribuciones: sólo se excluye
`psycopg-binary`. Hay 249 aristas únicas activas sin extras y 250 con `prodpg`, con los
mismos 141 paquetes. Se conserva `typing-extensions`, solicitado por Psycopg
para Python 3.12, y los extras transitivos existentes de los demás paquetes.
Se coteja además la cobertura exacta del informe OSV histórico de 142 versiones,
sin omisiones ni hallazgos, para identificar su subconjunto de 141 versiones
upstream. No se consulta la versión local en OSV ni se presenta esa comprobación
como una auditoría nueva o de código nativo.
La revisión no modifica el lock ni la caché original de 142 ruedas. Tampoco
reemplaza todavía la rueda OpenCV pendiente por su variante nativa.

```powershell
# Inspección local: imprime el plan y su hash, sin publicar la rueda.
python docker/kitsu-zou/derive_zou_wheel.py

# Sólo Root, después de revisar y respaldar ambos hashes y la fuente:
python docker/kitsu-zou/derive_zou_wheel.py --operation apply --manifest-sha256 HASH_MANIFIESTO --plan-sha256 HASH_PLAN
```

La publicación usa creación exclusiva, un intent previo y un recibo posterior.
Un archivo sin intent coincidente se rechaza incluso si tiene el nombre esperado.
Un reintento con el mismo plan y bytes es idempotente; no sobreescribe una salida
distinta. Un fallo deja el parcial propio para diagnóstico, sin borrado por patrón.
El recibo se respalda antes de usar la rueda. Una salida sin recibo validado no
se considera aceptada. Un cambio de fuente, manifiesto, herramientas o clausura
cambia el plan y requiere un nuevo respaldo; no se amplía ni se fuerza el hash.

El ZIP fija fecha 1980, orden, permisos y compresión. El plan registra Python y
zlib: no promete idénticos bytes entre versiones diferentes de esas herramientas.
La rueda original y los informes históricos se conservan.

## Contrato Linux pendiente

La implementación [Python de Psycopg](https://www.psycopg.org/psycopg3/docs/basic/install.html#pure-python-installation)
requiere libpq del sistema. El despliegue futuro debe fijar `PSYCOPG_IMPL=python`
antes de importar Zou. Psycopg documenta que esa
[selección explícita](https://www.psycopg.org/psycopg3/docs/api/pq.html#pq-module-implementations)
falla si la implementación solicitada no puede cargarse. No se acepta sustituir
el paquete binario por una exclusión del scanner o un fallback silencioso.

El candidato nativo es Wolfi `libpq-18==18.6-r7`, APK SHA256
`4372b3013045fe0a347cefe77a818d84135d03e22155fa8cbcd741a6f0e0c4f9`,
SONAME `libpq.so.5`. La procedencia y clausura previa están en
`.tools/evidence/phase4-zou-psycopg-libpq-plan.json`. Ese informe usó una DB de
Trivy cuyo NextUpdate era 2026-11-06 13:09 UTC; constituye evidencia histórica,
no una aprobación vigente de imagen o runtime.

Antes de aceptar Linux faltan imagen y SCA finales, caché real de `ldconfig`,
`ctypes.util.find_library('pq')`, rutas/SHA de las bibliotecas cargadas,
`psycopg.pq.__impl__ == 'python'` y `psycopg.pq.version() == 180006`.
Después deben probarse import de Zou con gevent, dos conexiones propias,
autenticación SCRAM, tipos, commit/rollback y cancelación mediante el
[dialecto Psycopg de SQLAlchemy](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg)
contra una base temporal propia en PostgreSQL 16.15. Nada de eso queda acreditado
por modificar metadatos. El uso AGPL de Kitsu/Zou y sus obligaciones se conservan.

## Pruebas de esta unidad

```powershell
python -m unittest discover -s tests -p test_zou_wheel_derivative.py -v
python -m ruff check docker/kitsu-zou/derive_zou_wheel.py tests/test_zou_wheel_derivative.py
python -m ruff format --check docker/kitsu-zou/derive_zou_wheel.py tests/test_zou_wheel_derivative.py
```

Las pruebas usan ruedas sintéticas y directorios temporales propios. Cubren bytes
de código/licencia, marcador `prodpg`, clausura y extras, versión incompatible,
dependencia ausente, rutas inseguras, CRC/RECORD/cabecera local, duplicados,
enlaces, límites, pins y publicación exclusiva/idempotente. No ejecutan código
de paquetes descargados ni realizan HTTP, SDK, instalación, imagen o DB.
