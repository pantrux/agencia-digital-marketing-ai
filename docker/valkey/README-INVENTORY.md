# Inventario estático de Valkey 9.1.3

Esta unidad tiene cuatro fuentes: CLI, núcleo puro, pruebas y este README. Lee
el TAR local como datos; no ejecuta Valkey, herramientas dentro de la imagen,
Trivy, Docker, SSH, compiladores o SDK. No consulta `.env` ni la red. El modo
predeterminado calcula el plan y los hashes previstos sin publicar artefactos.

## Entradas conservadas y confianza

Se verifican por bytes/SHA antes de interpretar:

- Candidato `phase4-valkey-alpine-candidate.json`: `74decf8f…`.
- Freeze de captura V2 `phase4-valkey-oci-capture-v2-final-freeze.json`: `743580ca…`.
- Perfil V2 `79fa1543…`, las cuatro fuentes congeladas que referencia y los
  envelopes positivos de capture/fetch. Se cotejan etapa, owner, identidad,
  marcador, perfil, hash y encadenamiento de recibos, sin reescribirlos.
- TAR `.tools/cache/phase4-built-images/valkey-de6fbe9626fea9f6-v2.tar`,
  18.843.648 bytes, SHA256
  `046dc47ec1d369d3d916190ff3b62e91a45585833faf1fddb141372813bb904e`.
  El lector exige archivo regular, nlink1, sin enlaces/reparse en la ruta y
  misma identidad/bytes antes y después. El campo histórico `directory` del
  recibo fetch corresponde aquí al dev/inode del archivo: no se adopta como carpeta.

La identidad OCI debe coincidir exactamente con manifiesto
`de6fbe9626fea9f6f297b6867d2c8cf8f0394ee5ed171613166fc093cb49d0a6`
y config `631d75bac338990caa2d3c1c9c7ba5f4c38584f7a4c04cdfb363d861a33fb692`.
Se conservan el digest del índice multiarch declarado y sus límites: ese índice
no está dentro del TAR, por lo que no se inventa una verificación independiente
del vínculo índice/plataforma ni una attestation de construcción.

El parser puro reutilizado es `final_image_remote.py`, SHA256
`9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49`.
Se cargan exclusivamente sus bytes verificados y se usan `json_object`,
`safe_name`, `apply_layer`, `elf_info` y `follow`; ninguna operación de imagen,
SDK, PostgreSQL o escritura de ese módulo se invoca. Tampoco se modifican sus
globals. El lector/hash seguro `recover_opencv_sdk_scan_v2.py` permanece fijado
en `b15f8ed20e19b0b1b19dfdd5ffd81ea60667175a014ab63409b29012fa6f253c`.
El núcleo propio también se verifica por SHA antes de compilar sus bytes;
no se permite escoger un `.pyc` distinto.

## Parser y presupuesto de datos

El contenedor exterior sólo admite metadatos OCI y blobs SHA256 ordinarios.
Se rechazan duplicados canónicos, escapes, enlaces exteriores, miembros sparse,
descriptores/tamaños/digests inconsistentes, blobs extra y trailer no nulo.
Se verifica config linux/amd64, cada capa y cada DiffID descomprimido.

Límites cerrados: TAR de 256 MiB, capa expandida de 64 MiB, expansión acumulada de 128 MiB,
miembro interno de 32 MiB y 20.000 nodos/miembros. Las capas vacías son válidas. Se
leen miembros mediante streams en memoria; no se usan extract/extractall ni se
materializan enlaces, dispositivos o rutas de la imagen en el host.

Whiteouts y directorios opacos se aplican en un filesystem virtual. Cada nodo
final conserva su capa/digest/DiffID, nombre/posición de header y PAX observados.
Los payloads eliminados se descartan del modelo; no se atribuyen archivos de una
capa oculta al estado final. Los alias se resuelven sólo dentro del modelo,
incluido `/etc/os-release` hacia `usr/lib/os-release`.

El plan real observado contiene siete capas, 45.945.344 bytes expandidos,
1.175 nodos, 23 APK, tres ELF propios y nueve alias. Estos datos no acreditan
funcionamiento del servicio ni seguridad binaria.

## Dos coberturas separadas

`os-packages.cdx.json` contiene un OS Alpine 3.24.3 y 23 bibliotecas APK. Conserva
P/V/A, origen `o`, versión fuente, commit `c`, licencia declarada y archivos
atribuidos por la DB. Los PURL preservan arquitectura y distro; SrcName,
SrcVersion y Arch quedan explícitos. Se registra que las firmas APK no se
verificaron independientemente dentro de este inventario. No se ejecuta SCA.

`binary-materials.json` contiene tres ELF distintos: server, cli y benchmark;
los alias redis/check-aof/check-rdb/sentinel se conservan sin contarlos como
binarios adicionales. Incluye hash, tamaño, modo, UID/GID, origen, DT_NEEDED,
RPATH/RUNPATH, versiones ABI requeridas y proveedores virtuales con ownership
APK declarado. La resolución estructural no ejecuta el loader ni sustituye
una carga real, prueba de símbolos globales, TLS o instrucciones de CPU.

El usuario `valkey` observado es UID 999/GID 1000. Config.User está ausente; eso
no acredita identidad efectiva de un proceso. Se conservan entrypoint/CMD,
historial de construcción y etiquetas. El entrypoint y los directorios 1777
presentes en la imagen no se ejecutan, corrigen o consideran endurecidos.

El SPDX del proveedor declara sólo `valkey-server@9.1.3`, `filesAnalyzed=false`,
PURL genérico y CPE. No describe las bibliotecas embebidas ni demuestra que un
scanner las haya cubierto. No se inventa un paquete APK/Conan/npm equivalente
para convertir ese documento en una aprobación del binario.

Las cadenas Lua 5.1.5 y jemalloc 5.3.0 se registran por offset, texto y hash del
ELF como **candidatos**. Las referencias oficiales describen modificaciones de
seguridad/configuración en esas dependencias. No se les asigna automáticamente
la identidad o CVEs de una distribución stock; quedan pendientes la vinculación
binario/fuente y las identidades completas de otros materiales embebidos.

No se observaron nodos LICENSE/COPYING/COPYRIGHT/NOTICE en el TAR. La declaración
BSD-3-Clause del SPDX y los campos L de APK se conservan como declaraciones;
no sustituyen los textos ausentes. `licenses.complete`, `binary_sca_verified`,
`embedded_libraries_complete`, `os_sca_verified` y `binary_accepted` permanecen
false. El SCA OS y el análisis de binario/materiales requieren unidades y
reportes posteriores, sin usar el cero histórico de 23 APK como cobertura total.

## Plan y publicación exclusiva de Root

```powershell
python -I -S -B docker/valkey/inventory_image.py
```

El plan muestra SHA de CLI/núcleo/lectores, entradas, límites, conteos y SHA/tamaño
de cinco documentos calculados en memoria. Root debe revisar esos documentos
previstos y respaldar source/plan SHA, raíz y owner antes de publicar:

```text
python -I -S -B docker/valkey/inventory_image.py --publish --expected-source-sha256 SHA_CLI --expected-plan-sha256 SHA_PLAN
```

Destino nuevo: `.tools/evidence/phase4-valkey-inventory-v1`.
Owner: `agencia-digital-marketing-ai:valkey-inventory:v1`. Debe estar ausente;
mkdir EXCL y marker/devino rechazan cualquier directorio previo, aunque tenga
el nombre esperado. Los archivos se escriben nuevos, se fsync y se releen por
el lector congelado, sin chmod/chown de recursos ajenos. No se borran parciales.

Artefactos: `inventory.json`, `file-catalog.json`, `os-packages.cdx.json`,
`binary-materials.json` y `provenance.json`, más plan, marcador y recibo. La
publicación sólo certifica bytes de esos documentos; nunca un binario aceptado
o un scanner ejecutado. Un fallo conserva `failure.json` y no se reintenta.

## Verificación

```powershell
python -I -S -B -W error::ResourceWarning -m unittest discover -s tests -p test_valkey_inventory.py -v
python -m ruff check docker/valkey/inventory_core.py docker/valkey/inventory_image.py tests/test_valkey_inventory.py
python -m ruff format --check docker/valkey/inventory_core.py docker/valkey/inventory_image.py tests/test_valkey_inventory.py
```

Los fixtures ejercitan el parser real congelado con OCI/ELF sintéticos:
whiteout simple/opaco, origen por capa, duplicados/escapes/trailer, digest y
DiffID, plataforma, límites, aliases cíclicos, versiones contradictorias,
origen APK, proveedores ausentes y cobertura/licencias incompletas. La prueba
de publicación usa únicamente TemporaryDirectory propia y el lector real
congelado; ningún contenido ejecutable del fixture se inicia. El plan no puede
activar la publicación ni cargar un scanner.

Fuentes primarias de referencia: [release 9.1.3](https://valkey.io/download/releases/v9-1-3/),
[dependencias modificadas](https://github.com/valkey-io/valkey/blob/9.1.3/deps/README.md),
[targets de dependencias](https://github.com/valkey-io/valkey/blob/9.1.3/deps/Makefile)
y [COPYING de la fuente](https://github.com/valkey-io/valkey/blob/9.1.3/COPYING).
Las huellas blob de esas referencias están en el inventario; no se atribuyen
como materiales del build real sin la evidencia adicional pendiente.
