# Aceptación de las salidas Cargo1.100 observadas

Cuatro fuentes nuevas; las anteriores permanecen congeladas. No ejecuta SDK,
compilación, SQL, instalación, Docker exec/start/cp o imágenes. Sólo raíz podrá
aplicar, tras revisar y respaldar el perfil y las fuentes. sql_verified y
final_sca_verified siguen false.

## Historia que no se reescribe

Compile V3 terminó release pero su catálogo buscaba un layout antiguo; se
conserva el fallo b460fc78…, el perfil bffefb81…, el log b23d2c02… y STOP
7c2a9fa9…. El primer postverify (perfil25902d9d…/fuente54daf210…) también se
conserva fallido: recibo407e6eedb2d0b3a8242972f7b96e7c3e5334374677c01fdc8e1cd57027147d36,
detalle «Directorio auxiliar no permitido». Ninguna etapa se repite.

Raíz capturó sólo datos del CID detenido por Docker archive API y acreditó
NoForeign/PID0. TAR local1.152.512bytes, SHA
862868ba5205e9bc26af07116a443202e80a5aa7572d2469d484da1c88d444b9.
El inventario real tiene exactamente23 miembros y dos hashes de roles distintos;
no es una ambigüedad de dos ejecuciones de build-script.

## Contrato concreto de los roles

- Build-script: 9de2fceeb1acc82e, con fingerprint/run-build-script-build-script-build
  y su JSON, out/bindgen.rs y run/{invoked.timestamp,root-output,stderr,stdout}.
- Biblioteca: eef41604b3fa4ec0, con su fingerprint y los .d/.rlib/.rmeta.
- La dependencia build_script_build de la biblioteca referencia el fingerprint
  del run: entero4494643329962863696 representado little endian da
  `50ec83824b2e603e`, exactamente el fingerprint capturado.
- CheckDepInfo refiere al fingerprint de eef; sus tres targets .d/.rlib/.rmeta
  dependen del mismo bindgen.rs de 9de. OUT_DIR y run/root-output apuntan sólo
  a ese directorio. No se ejecutan ni importan .rlib/.rmeta/bindgen.
- Los dos JSON conservan los cuatro RUSTFLAGS originales. El run acredita
  LIBSQLITE3_SYS_USE_PKG_CONFIG=1, SQLITE3_DYNAMIC=1, SQLITE3_STATIC=null y las
  rutas de SQLite custom V3. Las features bundled permanecen visibles; no se
  interpretan por sí solas como compilación del C vendorizado. El stdout real,
  el ELF y la sonda sourceid acreditan la selección dinámica.

La lista exacta de23 path/tipo/modo/uid/gid/tamaño/SHA está embebida en el helper,
derivada del TAR autenticado. Un hash extra, archivo auxiliar desconocido,
enlace, duplicado o cambio de bytes se rechaza. No se amplía una allowlist
genérica. Se registra el SHA de cada GET nuevo; la igualdad exige los miembros
y contenidos exactos, sin suponer que metadatos de transporte TAR sean estables.

El parser real se probó contra el TAR capturado como datos, no mediante SDK.
También se prueban fixtures independientes que recalculan sus hashes y aun así
fallan si rompen la relación .d/fingerprint/OUT_DIR o seleccionan SQLite estático.

## Comprobación de salidas

Reutiliza los validadores congelados V3 para ELF64 musl, sourceid SQLite3.54.1,
FTS5, DT_NEEDED libsqlite3.so.0, RUNPATH $ORIGIN, símbolos/symtab y versiones de
las extensiones. Registra SHA/tamaños/modos de todo el árbol, sin modificarlo.
Se leen sólo archivos root ordinarios sin enlaces, de modo0600/0644/0755/0444,
y directorios root0700/0755 sin escritura ajena. El SDK debe permanecer detenido
con la misma identidad antes y después de todas las lecturas.

Se cotejan literalmente los controles/SQL de pgvector0.8.5, vchord.control,
install/vchord--1.1.1.sql y todos los SQL upgrade VectorChord1.1.1 contra sus
TAR fijados, además de las cinco licencias/avisos (incluido SQLite del ZIP).
Xtask1.1.1 copia esos SQL estáticos en esta versión; no se asumen generados.
El conjunto instalado debe coincidir exactamente; otros install históricos no
forman parte de esta instalación. Esto acredita bytes, no ejecución SQL.

## Transporte y escrituras nuevas

CID/ruta GET son fijos y el único acceso de archivo es
`/work/.compile-agency-v3/target/x86_64-unknown-linux-musl/release/build/libsqlite3-sys`.
Sólo inspect y GET Docker archive, socket Unix root acreditado, lectura3s,
límite4MiB y presupuesto180s; cliente190s. El framing success booleano fue
probado contra el collector V4 real para éxito y fallo.

Stage nuevo `postverify-v3-accept`; prefijo nuevo
`MEDIA_POSTGRES_IMMICH_POSTVERIFY_V3_ACCEPT_`. Antes de leer se cotejan el
perfil/recibo físicos del intento anterior contra sus B64 literales. Sólo se
añaden en control host `.postverify-v3-accept-started` y los archivos con ese
prefijo: -profile.json, -archive.tar, -build-output.txt, -artifact.json y .json.
Todos son EXCL/root privados. El TAR se guarda ANTES del parser, para conservar
un diagnóstico incluso si el análisis vuelve a rechazar los datos. Un fallo
incluye su SHA/tamaño cuando la captura ya existe. Nada se reescribe ni elimina.

## CLI de raíz

Plan predeterminado local, sin bóveda ni conexión; perfil ligado al fallo y
STOP ya respaldados, también local:

```powershell
python -X utf8 -B docker/postgres-immich/postverify_accept.py plan
python -X utf8 -B docker/postgres-immich/postverify_accept.py plan --output '.tools/postgres-immich-build/postverify-v3-accept-profile.json'
```

Raíz respalda PROFILE_B64, PROFILE_SHA256, LOCAL_SHA256 y REMOTE_SHA256 bajo el
prefijo nuevo antes de usar:

```powershell
python -X utf8 -B docker/postgres-immich/postverify_accept.py apply --profile '.tools/postgres-immich-build/postverify-v3-accept-profile.json' --expected-profile-sha256 '<SHA respaldado>' --root-update-vault
```

Sólo el CLI de raíz agrega y relee RECEIPT_B64 nuevo, sin tocar el campo previo.
Autenticación y pin SSH de .85 reutilizan el CLI V4 congelado y Paramiko5.1.0
existente. El recibo positivo deberá gatear la futura imagen final junto con
los dos fallos conservados y STOP, no reemplazarlos. La carga SQL, la imagen,
SCA completa y aceptación de producción siguen pendientes.
