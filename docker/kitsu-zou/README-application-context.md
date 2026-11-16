# Contexto Zou completo para prueba funcional aislada

Cinco fuentes preparan datos locales y una receta finita. No leen `.env`, conectan
por SSH, ejecutan Docker/SDK, instalan dependencias en Windows ni aprueban producción.
Los originales y sus recibos permanecen intactos. No es otro controlador remoto.

## Entradas y plan

`prepare_application_context.py` verifica las 142 ruedas originales por SHA e
identidad del inventario, excluye sólo `psycopg-binary` y sustituye Zou, Flasgger y
OpenCV por sus derivados respaldados `41ead8…`, `db3bb5…` y `1c03ca…`. Las 141
ruedas elegidas se cotejan por ZIP/CRC, METADATA y RECORD. La clausura Linux
CPython3.12.16 usa el validador congelado `e7fba271…`, incluidos extras activos y
`prodpg`. Rechaza dependencias por URL, paquetes ausentes/sobrantes y driver C/binary.
El nuevo lock mantiene hashes y versiones locales; no modifica el lock histórico.

Los ocho APK del bundle `ce08d9…` y gdbm/sqlite de Python se reautentican con el
índice Wolfi9b27 intacto, clavef003, RSA-SHA256/Q1/datahash y los helpers congelados.
Las diez identidades, licencias, fuentes y cantidades de payload figuran en el plan.
No se ejecutan scripts de APK durante esa preparación.

```powershell
python -B -X utf8 docker/kitsu-zou/prepare_application_context.py
```

El plan predeterminado devuelve `ready=false`: falta la captura física **actual**
`image-security-inputs.json` de la factory Python y su SHA externo respaldado por
Root. No acepta la base ce3 como si contuviera Python, IDs de ejemplo o una
aprobación declarada. El documento debe vincular manifest/config, TARbase/final,
19 APK instalados+14 componentes manuales e intérprete/libpython exactos. Puede
mantener `runtime_approved=false` y `sca_approved=false`: es una referencia física
para una prueba parcial explícita, no un certificado de producción.

```powershell
# Root fija y respalda ambas referencias antes de usar este plan:
python -B -X utf8 docker/kitsu-zou/prepare_application_context.py --runtime-capture RUTA_FISICA --runtime-capture-sha256 SHA_FISICO
# Después de respaldar el hash del plan, las fuentes y la nueva ruta/owner:
python -B -X utf8 docker/kitsu-zou/prepare_application_context.py --runtime-capture RUTA_FISICA --runtime-capture-sha256 SHA_FISICO --apply --expected-plan-sha256 SHA_PLAN
```

La publicación usa parent privado `backup-source-build` y su marcador789258,
raíz nueva EXCL, marker/devino, plan y recibo. Rechaza enlaces/reparse/hardlinks,
cambios físicos, destinos existentes y bytes fuera del plan. Un fallo conserva
el parcial propio. No adopta ni sobrescribe otro contexto.

## Build que hará Root en Linux

Root enlazará el alias BuildKit `pythonruntime` a un OCI **local** con el mismo
manifest/config de la captura. El preparador no crea/exporta ese OCI ni hace pull.
Usar rednone/pullfalse, sin push, y `BUILDX_GIT_INFO`, `BUILDX_GIT_LABELS` y
`BUILDX_GIT_CHECK_DIRTY` en `false` antes de invocar Docker. Los límites y timeout
del build se fijan en la operación revisada de Root; esta receta no los simula.

El Dockerfile usa un bind de contexto RO durante RUN: APK, ruedas y catálogo de
build no quedan copiados como archivos finales. Instala nativamente desde el
repositorio local firmado, con selectors `nombre=versión`, `--no-network`,
`--no-scripts`, `--no-commit-hooks`, keys/repositories propios y sin
`--allow-untrusted`/force. La DB física debe pasar de exactamente19 a29 identidades;
gdbm/sqlite dejan de ser sólo payload manual. Permanecen12 componentes Python
manuales: no se inventan filas instaladas para ellos.

Después ejecuta `/usr/bin/ldconfig` explícitamente. pip se carga desde el wheel
Wolfi26.2.2 fijado `38f4aa…`, con Python `-I -S -B`, `--isolated`, `--no-index`,
`--no-deps`, `--require-hashes`, sólo ruedas y `--target /opt/zou/site`. No resuelve
nada por Internet ni procesa `.pth` del sitio destino. Se conservan metadata y
licencias reales. Estas acciones Linux todavía no se han ejecutado en esta unidad.

La política de repositorio local y verificación por identidad procede de
[apk-repositories](https://raw.githubusercontent.com/alpinelinux/apk-tools/v2.14.10/doc/apk-repositories.5.scd),
[apk.8: no-network/scripts/hooks](https://raw.githubusercontent.com/alpinelinux/apk-tools/v2.14.10/doc/apk.8.scd),
[app_add](https://raw.githubusercontent.com/alpinelinux/apk-tools/v2.14.10/src/app_add.c),
[database](https://raw.githubusercontent.com/alpinelinux/apk-tools/v2.14.10/src/database.c)
y [package](https://raw.githubusercontent.com/alpinelinux/apk-tools/v2.14.10/src/package.c).
La base acredita apk-tools Wolfi2.14.13-r0; el contrato se comprueba además durante
el build real. No se afirma haber ejecutado su ayuda o instalado esos APK.

## Lanzamiento y aceptación que falta

CMD llama a `launch_zou.py api` con UID/GID1000 y Python `-I -S -B`. Inserta
directamente `/opt/zou/site`, sin `site.addsitedir`, limpia el entorno y fuerza
`PSYCOPG_IMPL=python`. Gunicorn real usa un worker gevent en5000. `events` usa
un único GeventWebSocketWorker en5001 sobre `zou.event_stream:app`; no se replica
el estado de presencia en procesos separados. No hay servidor Flask de desarrollo.

Root debe proporcionar DB_HOST/PORT/USERNAME/DATABASE, KV_HOST/PORT,
DOMAIN_NAME/PROTOCOL y secretos montados como archivos legibles por1000:
`zou_db_password`, `zou_kv_password`, `zou_secret_key`. Se leen sólo esas rutas
fijas, con límites/propiedad/permisos y sin imprimir valores ni pasarlos en argv.
La configuración de esta prueba desactiva correo/SAML y conserva Redis real.
API/eventos necesitan sus directorios propios en `/var/lib/zou/{previews,tmp}`.
Root fijará red privada, recursos y rutas/credenciales antes de arrancar.

La base temporal debe existir antes de `init-db`, que llama a Alembic upstream.
`create-admin` conserva el callback original; recibe ZOU_ADMIN_EMAIL y contraseña
desde `/run/secrets/zou_admin_password`, nunca de argv. No usa contraseña default.
No ejecuta telemetry de `upgrade-db`. La inicialización y el admin no se ejecutan
automáticamente al arrancar API/eventos.

Aceptación mínima real: libpq resuelto por `ctypes.util.find_library('pq')`, SHA de
la biblioteca, Psycopg `python`/180006, importación Gunicorn/gevent y Zou; migración
de DB propia, login correcto/incorrecto, authenticated, refresh, logout y rechazo
del JWT revocado, con Valkey autenticado. Validar Swagger UI3/OpenAPI/OAuth/JWT
desde la app real junto con esos casos, evitando otro fixture Flask separado.

El bundle estático Kitsu1.0.77 ya está descargado; servirlo y fijar proxy `/api`
hacia5000, `/socket.io` hacia5001 y fallback SPA corresponde a la siguiente unión
HTTP. Esta imagen no inventa un proxy ni incorpora Node. No acredita todavía
import/ABI OpenCV: su rueda mínima necesita proveedores ELF externos de OpenEXR,
PNG y su clausura; FFmpeg/media también quedan pendientes explícitos. El import
cv2 de Zou está dentro de la función thumbnail, por lo que no se suplanta con un
mock para la prueba de login. Ningún cero SCA o permiso de producción se deriva
del plan/contexto.

## Verificación local

`python -B -X utf8 -m unittest discover -s tests -p test_zou_application_context.py -v`
usa fixtures ZIP/RECORD y archivos temporales. No instala o importa paquetes de
la aplicación. Las pruebas de ACL con callback controlado son sólo contratos de
publicación; no se presentan como una aceptación ACL real de Linux/Windows.
