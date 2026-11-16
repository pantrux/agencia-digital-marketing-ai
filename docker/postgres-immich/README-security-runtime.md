# Imagen PostgreSQL corregida desde catálogo V7

## Estado y frontera

Se preparan cinco fuentes nuevas, sin ejecutar SSH, SDK, Docker, SQL ni `.env`.
El plan por defecto devuelve `ready=false`, sin leer la bóveda: faltan catálogo
y STOP positivos de V7. El borrador anterior para V6 nunca se adoptó y queda
preservado en el checkpoint `bbe692f7…` y sus cinco copias byte exactas. V6 falló;
esta revisión exige explícitamente la familia V7 y no transforma aquel fallo en
un éxito. Cualquier cambio futuro de familia requiere otra revisión de gates.

La imagen 96c, su config 760d, sus namespaces y finalizadores anteriores se
conservan. No se declara una nueva imagen, un catálogo o un digest inexistente.
Incluso un build futuro positivo mantendrá `release_approved=false`,
`sql_verified=false` y `final_sca_verified=false`.

| Concepto | Contrato propuesto |
| --- | --- |
| Base local | Manifiesto `c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a`, config `22fdd47fd045fefc8c744ff00f801c6efe0e99cab9eb67e1751712760974316c` |
| Plataforma | Linux amd64; PostgreSQL 16.15; USER 70:70 y entrypoint oficial heredados |
| Owner nuevo | `agencia-digital-marketing-ai:postgres-immich-security-runtime:v1` |
| Context | `/opt/media-platform/build/contexts/postgres-immich-security-runtime/security-v7-20261106-01` |
| Record | `/var/lib/media-platform/build/postgres-immich-security-runtime/security-v7-20261106-01` |
| Catálogo fuente | `/outputs/security-rebuild-v7/runtime-root` del SDK propio detenido |
| Prefijo de bóveda | `MEDIA_POSTGRES_IMMICH_SECURITY_RUNTIME_V1_` |
| Perfil local fijo | `.tools/postgres-immich-security-inputs-v1/security-runtime-v1.profile.json` |

Raíz respaldará rutas, owner, perfil y fuentes antes de prepare/build. No se
crean recursos al importar las fuentes o consultar el plan.

## Parsers y datos preservados

La fuente histórica `final_image_remote.py` se autentica mediante SHA256
`9e0a282940bd7585b6953b43dbc3f02df9bf01bb9f775e5a7a4eb1172ed4fa49`.
Se compila sólo una selección AST cerrada de sus funciones de datos
OCI/capas/ELF/ABI, inventario APK y proyección, con constantes/imports necesarios.
No se carga apply, main, OWNER, LABELS ni ninguna operación del controlador
histórico; tampoco se sustituyen sus globals. Los parsers no reciben el
argumento publish. La publicación del layout OCI se hace por FD en el namespace
nuevo, después de que el parser haya validado todos los blobs.

Los siete helpers de IO identificados en el freeze se copiaron desde esa fuente
autenticada a este controlador, con sus propios OWNER/CONTEXT/RECORD. No se
modifica ni invoca una operación histórica. El SDK V4 `a13ab960…`, layout
`3c986485…`, contrato V7 `7206217b…` y conector `685bb8a5…` se cargan por bytes y
SHA, sin loaders `.pyc`. El CLI exige Paramiko/pin reales del conector existente;
no añade credenciales, fallback ni AutoAddPolicy.

El perfil es pequeño por referencias de ruta/SHA/tamaño: perfil V7, prepare,
restart, catálogo y STOP; compile V3 histórico, recuperación fc334, prepare y
finish de la imagen 96c. No vuelve a almacenar el perfil runtime histórico de
329 KiB en otro B64. Los bytes de las referencias se revalidan al transmitir y
el servidor verifica su conjunto, hash y tamaño. El máximo del perfil es 32 KiB;
la lectura local de `.env` admite 8 MiB con parser canónico y ACL privada.

## Gates de promoción

Todos los prerequisitos deben ser envelopes canónicos positivos de la etapa,
owner y perfil af3a correctos. El catálogo V7 debe acreditar sus archivos reutilizados
y la época de restart; STOP debe corresponder al mismo CID, nacimiento,
started_at, límites y NoForeign, con running false y PID 0. Se cotejan en disco
el perfil, prepare/restart/catalog y el único `stop-<timestamp>.json` cuyo
contenido coincide con el STOP respaldado. La inspección actual del CID f042 se
valida con el contrato NoForeign completo y debe igualar el STOP antes y después
de prepare/build. No se arranca ni se detiene ese contenedor desde esta unidad.
La identidad del daemon debe coincidir con la registrada por prepare V7 y se
vuelve a consultar físicamente en ambas comprobaciones. El nacimiento se coteja
también contra el perfil V7 fijado, no sólo entre recibos de la misma familia.

También se releen por FD las identidades, marcadores y recibos prepare/finish
físicos de la imagen 96c. Sus 66 archivos proyectados son el baseline. El
catálogo V7 tiene 89 entradas; se excluyen los 23 headers/bitcode del SDK. Las
otras 65 entradas de runtime deben conservar SHA/tamaño; sólo vchord.so puede
variar y se rechaza que permanezca idéntico a la candidata antigua.

La copia abre únicamente rutas del catálogo por FD/NoFollow, exige UID/GID 0,
fuentes 0600, ancestros privados y hash/tamaño. Crea destinos EXCL, directorios
0755 y archivos 0444. El snapshot posterior rechaza archivos y directorios
vacíos adicionales. No se usan librerías del SDK para completar dependencias:
el cierre ELF/SONAME/símbolos debe resolverse con los 45 APK de la base c908 y
los tres objetos del overlay, o falla explícitamente.

## Etapas que sólo ejecutará raíz

```powershell
# Plan pendiente: no lee .env ni conecta.
python -B docker/postgres-immich/security_runtime.py

# Sólo cuando existan catálogo y STOP positivos y los punteros V7 estén respaldados:
python -B docker/postgres-immich/security_runtime.py plan `
  --output '.tools/postgres-immich-security-inputs-v1/security-runtime-v1.profile.json' `
  --root-update-vault

python -B docker/postgres-immich/security_runtime.py prepare `
  --expected-profile-sha256 SHA_RESPALDADO --root-update-vault

# Requiere el recibo prepare ya persistido en la bóveda:
python -B docker/postgres-immich/security_runtime.py build `
  --expected-profile-sha256 SHA_RESPALDADO --root-update-vault
```

El CLI sólo actualiza la bóveda cuando raíz proporciona el flag explícito y usa
el helper `0fc75c6d…`, con preservación de campos ajenos. Guarda el perfil y
todos los recibos locales con EXCL dentro de la raíz privada existente. Los
campos nuevos incluyen OWNER, CONTEXT_ROOT, RECORD_ROOT, BASE_OCI_ROOT,
PROFILE_PATH/SHA256 y LOCAL/REMOTE/DOCKERFILE_SHA256. Cada resultado conserva
PREPARE o BUILD_RECEIPT_PATH/SHA256; un fallo posterior al build también respalda
OBSERVED_IMAGE_ID/OBSERVED_CONFIG_DIGEST cuando están acreditados como datos.
Estos identificadores observados no implican aprobación de la imagen.

Prepare crea context/record nuevos, copia sólo la proyección y exporta la base
local a base.tar. Verifica manifiesto, config, capas, DiffIDs, 45 APK, UID70,
scripts oficiales y ausencia real de gosu/su-exec. Publica el layout local con
blobs verificados y conserva su inventario, hash TAR y cierre ABI. No hay pull,
push, compilación, instalación o ejecución de contenedores.

Build exige prepare físico/durable idéntico, fuentes/árbol/OCI sin deriva y
ningún archivo ajeno. Usa builder default/driver docker, Docker 29.9.3 y Buildx
0.38.1, contexto nombrado `oci-layout://...@sha256:c908…`, network none,
pull false, provenance false y load. El Dockerfile sólo tiene FROM local, COPY,
LABEL y USER; no RUN, frontend remoto ni instrucciones Git.

El plazo remoto total es 600 s, buildx 300 s y cada exportación 120 s, sujeto
siempre al plazo total. Captura hasta 4 MiB sin truncar; transporte hasta 2 MiB.
Los comandos Docker usan el recolector congelado que conserva líder con
WNOWAIT y limpia sólo su grupo. No se añaden contenedores BuildKit persistentes.

## Identidad y fallos posteriores al build

El lector propio acepta 0644 o 0600 únicamente para el nombre
`build-metadata.json`; IID y log requieren 0600. Siempre exige regular, UID/GID
0, nlink1, límite, descriptor e identidad estables. No altera permisos de origen.

Si metadata incluye config.digest, IID debe ser ese config. Si falta esa clave,
IID debe ser el manifiesto; config se obtiene del OCI real. Ambos IDs deben
coincidir con log e inspección de imagen. La anotación config.digest de Descriptor
es opcional; si existe se comprueba, nunca se inventa ni se exige su presencia.
Se conserva `observed-image-ids.json` antes de exportar/verificar ABI, por lo que
un fallo tardío conserva IDs, log, metadata y receipt negativo para recuperación
explícita sin rebuild. No se sobrescribe ni se adopta ningún intento previo.

La imagen final debe conservar todo config heredado salvo los labels nuevos,
sus capas/DiffIDs y cada nodo de la base. Sólo agrega la proyección y sus
directorios 0755; el overlay debe ser root0444 y la DB de 45 APK idéntica.
La validación de SQL, aceptación UID70 con DB temporal y SCA OS/nativa actuales
permanecen etapas posteriores independientes.

## Verificación local y límites honestos

```powershell
python -B -W error::ResourceWarning -m unittest discover -s tests -p test_postgres_immich_security_runtime.py -v
python -m ruff check docker/postgres-immich/security_runtime.py docker/postgres-immich/security_runtime_remote.py tests/test_postgres_immich_security_runtime.py
python -m ruff format --check docker/postgres-immich/security_runtime.py docker/postgres-immich/security_runtime_remote.py tests/test_postgres_immich_security_runtime.py
```

Las pruebas de identidad y formatos son datos/fixtures; incluyen el fallback
manifiesto/config observado en Docker29, negativos de fase/perfil/owner,
exclusión de operaciones históricas, proyección, referencias alteradas, JSON
ambiguo, plan sin bóveda y lector 8 MiB. Dos casos FD/UID0 se omiten en Windows y
deben ejecutarse en Linux real antes de usar el controlador. No se presentan
como casos nativos simulados ni como una imagen construida o limpia.

Las fuentes pueden congelarse antes del catálogo: el plan por defecto pendiente
no contiene hashes de recibos futuros. Sólo el plan operativo explícito de Root
resuelve y valida las cinco referencias V7 por PATH/SHA/tamaño, conserva los
envelopes físicos y publica el perfil listo en un destino EXCL. No hay fallback
a V6 ni a otro catálogo. Los fixtures de gates son datos sintéticos de tests,
no recibos de producción. La prueba de daemon divergente falla antes de abrir
el control remoto o continuar con una promoción.
