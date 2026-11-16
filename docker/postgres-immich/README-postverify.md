# Postverificación V3 sin recompilar

Esta unidad de cuatro archivos verifica datos ya producidos. No ejecuta un SDK,
compilador, instalador, SQL, Docker exec/start/cp ni crea imágenes. El fallo V3
original se conserva como fallo; un recibo separado acredita la verificación.
La imagen final y la aceptación SQL siguen pendientes.

## Motivo y gates

Cargo terminó release; el verificador V3 buscaba el layout antiguo
`libsqlite3-sys-<hash>/output`. Cargo 1.100 coloca el archivo bajo
`libsqlite3-sys/<hash>/output`. El fallo ocurrió antes de escribir el catálogo y
antes de normalizar permisos. Por eso se leen modos root 0600/0644/0755/0444 y
directorios root 0700/0755, sin enlaces, hardlinks o escritura ajena. Se registran
sus modos sin cambiarlos.

Se exigen literalmente el perfil compile V3 bffefb81…, el fallo
b460fc786cceadbfa92e84093acfeb15f1d7013d7dedb65a0b68690931051d0e,
el log b23d2c02948b0637e483ab9691f71e660d5196616fea1573f9becb0cfeccca54
y STOP 7c2a9fa9223d1d9b745fe648637273470fe351a17b1a6c26a637672c214cf95e.
STOP procede de `Controller.saved()['stop']`: el CLI V4 fusiona sus grupos y lo
lee de `MEDIA_POSTGRES_IMMICH_SDK_V4_CONTAINER_B64`. No se reconstruye el perfil
V3 después de añadir esta sexta etapa; se usa el B64 histórico aprobado.

El CID único es
f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d.
Antes y después se comprueba NoForeign V4 completo, detenido/PID0 y la misma
identidad. Se verifican los 78 inputs, directorios y recibos respaldados,
SDK/grafo/features, fuentes/flags del compile y log físico. No se reabre ni
modifica el contenedor.

## Lectura del build output

La única operación HTTP al daemon es GET del archivo del CID detenido, ruta
fija:

`/work/.compile-agency-v3/target/x86_64-unknown-linux-musl/release/build/libsqlite3-sys`

Se usa `/run/docker.sock` local acreditado, plazo de socket 3 s y presupuesto
externo de 180 s; el cliente espera como máximo 190 s. No hay endpoint, path o
comando libre en el perfil. No se acepta otro CID. El TAR completo se limita a
4 MiB y 128 miembros, todos datos root y sin enlaces, traversal, duplicados,
archivos especiales o auxiliares ajenos. Debe existir exactamente un hash y un
`output`. El parser acepta ambos layouts para regresión; el GET real usa el
nuevo directorio conocido y no busca rutas alternativas.

Se conserva el TAR y su inventario/SHA, y los bytes literales del build output.
El verificador V3 congelado exige `rustc-link-search` exclusivo al SQLite V3,
`sqlite3` dinámico y ausencia de `static=sqlite3`. Luego verifica ELF musl,
DT_NEEDED libsqlite3.so.0, RUNPATH $ORIGIN, símbolos SQLite importados/exportados,
symtab, sonda SQLite 3.54.1/sourceid/FTS5 y versiones vector0.8.5/vchord1.1.1.
SQL y control pgvector y las cinco licencias/avisos se cotejan contra los TAR/ZIP
originales fijados. Los SQL generados de VectorChord se inventarían por SHA y
se comprueban como texto/versiones; su ejecución se acredita en la futura
aceptación SQL, no aquí. Ninguna biblioteca adicional del SDK se promueve.

## Interfaz de raíz

El plan predeterminado es local y no abre la bóveda ni conecta:

```powershell
python -X utf8 -B docker/postgres-immich/postverify.py plan
```

Con fallo y STOP ya respaldados, generar perfil canónico EXCL en la raíz
privada existente. No ejecuta infraestructura:

```powershell
python -X utf8 -B docker/postgres-immich/postverify.py plan --output '.tools/postgres-immich-build/postverify-v3-framed-profile.json'
```

Raíz respalda ANTES de apply, con prefijo
`MEDIA_POSTGRES_IMMICH_POSTVERIFY_V3_`, PROFILE_B64, PROFILE_SHA256,
LOCAL_SHA256 y REMOTE_SHA256. Después revisa los hashes y ejecuta:

```powershell
python -X utf8 -B docker/postgres-immich/postverify.py apply --profile '.tools/postgres-immich-build/postverify-v3-framed-profile.json' --expected-profile-sha256 '<SHA respaldado>' --root-update-vault
```

Autenticación y host pin reutilizan exactamente el CLI V4 congelado y Paramiko
5.1.0 aislado. No se imprimen valores de bóveda. Sólo el CLI de raíz escribe y
relee `MEDIA_POSTGRES_IMMICH_POSTVERIFY_V3_RECEIPT_B64`, tanto para un resultado
positivo como para uno fallido, antes de avanzar.

## Escrituras nuevas y fallos

Sólo se añaden en el control host ya propio `.postverify-v3-started` y los
archivos `postverify-v3-profile.json`, `postverify-v3-archive.tar`,
`postverify-v3-build-output.txt`, `postverify-v3-artifact.json`,
`postverify-v3.json`, todos EXCL y root privados. No se reescribe la salida
compile-v3 ni ningún recibo anterior. Un corte deja evidencia propia y exige
revisión; no hay replay automático, borrado o adopción de parciales.

Un éxito indica `stage=postverify-v3`, conserva las referencias al fallo y STOP,
incluye catálogo/hash/modos y fija sdk_executed/compiler_executed=false. No fija
sql_verified o final_sca_verified. El futuro gate de imagen final debe exigir
los tres envelopes separados: fallo original, postverify positivo y STOP.

## Validación local

Pruebas de ambos layouts, raíz TAR literal, ambigüedad, extras, enlaces,
traversal, duplicados, límites/truncamiento HTTP, licencia/SQL desde fuentes y
plan sin bóveda/red. Proveedores de HTTP están controlados: las pruebas no
acreditan un GET real. Root revisará y ejecutará el controlador después de
respaldar sus pins. No se ha realizado infraestructura por el agente.

## Revisión de framing anterior a la primera aplicación

El perfil13897652… y la fuente d4ba60b5… se conservan como historia, sin aplicar.
Raíz detectó que faltaba success booleano de nivel superior para el collector
V4. La revisión cambia sólo ese campo en main y añade una prueba que pasa la
salida real de main por el collector V4 congelado, para éxito y fallo.
Ningún GET, catálogo, gate o escritura remota cambió. El nuevo perfil framed
debe respaldarse antes del primer apply; no hay intento remoto que recuperar.
