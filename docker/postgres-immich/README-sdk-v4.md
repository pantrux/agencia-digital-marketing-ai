# Intento independiente del SDK con XZ externo

El intento anterior conserva su CID detenido, recibo fallido, fuentes V3 y
kit original de 78 archivos. No se reanuda ni se adopta el SDK parcialmente
preparado. Esta unidad sólo prepara fuentes y planes nuevos; no ejecutó
Docker, SSH, APK o SDK ni modificó la bóveda.

## Causa y corrección de extracción

La fuente oficial BusyBox 1.37.0 fija 64 MiB en
archival/libarchive/decompress_unxz.c:70. Los paquetes Rust fijados rustc y
rust-std requieren diccionarios LZMA2 de 128 MiB; cargo requiere 48 MiB.
La biblioteca Python liblzma reproduce el rechazo con límite de 64 MiB.
Los tres archivos completos tienen su SHA esperado, alcanzan EOF XZ sin
bytes sobrantes y todos los checksums de cabeceras TAR son válidos. No hay
PAX; las extensiones de nombres largos son GNU. El flujo XZ declara check
NONE: la integridad del artefacto completo se acredita por el SHA fijado.

La fuente primaria es
https://busybox.net/downloads/busybox-1.37.0.tar.bz2, SHA-256
3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4.
Los informes completos permanecen en
.tools/evidence/phase4-rust-xz-busybox-source.json y
.tools/evidence/phase4-rust-xz-full-data-validation.json.

offline-container-v2.sh conserva todos los bytes del script original salvo
la línea de extracción dentro del bucle de los tres componentes Rust:

```sh
/bin/bash -o pipefail -c '/usr/bin/xz -dc "$1" | /bin/tar -xf - -C "$2" --no-same-owner' -- "/inputs/rust/$componente.tar.xz" /work/sdk-sources
```

Se usa el xz 5.8.4-r0 ya fijado en los 65 APK: SHA de archivo
547866cbe6f4a93efc9cee3bba03629d37f5590446677bb800cf4eada4b22c7c.
No se añade tar ni otro paquete. La ruta absoluta evita el decoder XZ de
BusyBox; pipefail conserva fallos de descompresor y extractor. VectorChord y
vendor mantienen sus flags gzip/strip originales. Los modos siguen siendo
hold/sdk/graph y no existe modo compile.

## Kit e histórico nuevos

El inventario derivado contiene exactamente 78 entradas. Sólo cambia el
origen físico, tamaño y SHA de la entrada lógica offline-container.sh para
apuntar al nuevo script. Las otras 77 entradas conservan sus registros y
bytes. Los documentos originales no se sobrescriben.

| Artefacto | SHA-256 |
| --- | --- |
| offline-container-v2.sh | 9799df3be3540225e7023fe7e41bc9cf4c31196b44a5cbf47c44d5254ce90cd5 |
| SHA256SUMS del kit nuevo | 03107096f926423b08a91f58269628bcd99232c5d89ba348246e65d3bd62bb4a |
| phase4-postgres-immich-offline-inputs-v2.json | a5994aeb2c0ca62d27a553912eb3bc41157555abeba90482159f2e2b34017384 |
| phase4-postgres-immich-offline-container-plan-v2.json | db933c4289ac90eae8f1c125132c63616e5e08d1c6ee776ee8abde40e93e69b5 |

El histórico nuevo se generó llamando solamente container_plan del helper
offline_recipe.py congelado c0518a09215262f9ab2c6daa1f75a35850abed946dfa3430bc745085f461d10f.
Recibe las nuevas rutas y el SHA del kit; no recicla argumentos de binds del
intento anterior. La revisión del controlador aplicará los ajustes aprobados
de imagen propia c908, UID0 del SDK y tmpfs PGDATA sobre ese nuevo histórico.

El ID nuevo es 03107096f926423b08a91f58269628bc. Sus rutas propias son:

- /home/jandrade/.agencia-pg-immich-stage-03107096f926423b08a91f58269628bc
- /opt/media-platform/build/contexts/postgres-immich/03107096f926423b08a91f58269628bc/inputs
- /var/lib/media-platform/build/postgres-immich/03107096f926423b08a91f58269628bc/outputs
- /var/lib/media-platform/build/postgres-immich/03107096f926423b08a91f58269628bc/control

La raíz respaldará rutas, hashes, owner y documentos del controlador nuevo
antes de preparar esos namespaces. Los ancestros compartidos conservan sus
marcadores anteriores; cada hijo nuevo tendrá su propio marcador/dev/ino.

## Verificación local

Tres pruebas acreditan la regresión de memoria XZ, la diferencia exacta del
script y la propagación de fallos de ambos lados de su pipeline mediante
sustitutos controlados. No se ejecutan xz, tar o componentes SDK candidatos
en esas pruebas. Sintaxis y análisis shell se comprueban separadamente.
La extracción real, instalación, grafo target y cualquier compilación siguen
pendientes de ejecución y respaldo por la raíz.

## Controlador del intento V4

run_offline_sdk_v4.py y offline_sdk_remote_v4.py son copias de las fuentes V3
con una diferencia cerrada: prefijo de bóveda, nombre del módulo remoto, rutas
de inventario/histórico, sus SHA y kit/build ID nuevos. El único cambio de
formato adicional divide una llamada read_source por la longitud de la ruta.
Una prueba reconstruye exactamente esos bytes desde las fuentes V3 fijadas.
No cambian NoForeign, capacidades normalizadas, presupuestos, proceso/grupo,
latido, transferencia por descriptores, comprobación cgroup o respaldos.

| Artefacto | SHA-256 |
| --- | --- |
| run_offline_sdk_v4.py | 685bb8a54fc7d65fd25710b5ea317f8eda239b57aa2978a0ff5e33b8aabba608 |
| offline_sdk_remote_v4.py | a13ab96098b5f739ed88cbacaffc5bed521825aeb033d428bddbf93be57137ed |
| Plan local sin bóveda | f1888a6ee04b8a93b23340e4f1b6c9b53c8d44866945fb0df5d38b74c850f62b |

El plan local está en
.tools/evidence/phase4-postgres-immich-sdk-controller-plan-v4.json. El esquema
del plan heredado conserva plan_revision 2; V4 identifica este intento y su
prefijo independiente, no un cambio de algoritmo o formato del plan.

El prefijo nuevo es MEDIA_POSTGRES_IMMICH_SDK_V4_. Antes de usar la bóveda,
la raíz respalda REMOTE_STAGING_PATH, INPUTS_PATH, OUTPUTS_PATH y CONTROL_PATH
con las cuatro rutas anteriores; OWNER conserva exactamente
agencia-digital-marketing-ai:postgres-immich-builder:v1. Ese owner identifica
el rol y permite verificar los ancestros compartidos ya acreditados. Los
labels build-id, kit y rutas nuevas separan las instancias y los hijos propios.

Los cuatro documentos activos usan exclusivamente los campos V4_CONTROLLER_B64,
V4_PREPARED_B64, V4_CONTAINER_B64 y V4_STAGE_RECEIPTS_B64 bajo el prefijo
MEDIA_POSTGRES_IMMICH_SDK_. Los cuatro campos anteriores y el CID detenido
permanecen intactos; no se migran ni se limpian. NoForeign rechaza que un
controlador adopte los montajes/labels del otro intento.

La raíz ejecutará explícitamente cada transición con su entorno Paramiko 5.1.0:

```powershell
python docker/postgres-immich/run_offline_sdk_v4.py
python docker/postgres-immich/run_offline_sdk_v4.py plan --root-update-vault
python docker/postgres-immich/run_offline_sdk_v4.py prepare --root-update-vault
python docker/postgres-immich/run_offline_sdk_v4.py create --root-update-vault
python docker/postgres-immich/run_offline_sdk_v4.py start --root-update-vault
python docker/postgres-immich/run_offline_sdk_v4.py sdk --root-update-vault
python docker/postgres-immich/run_offline_sdk_v4.py graph --root-update-vault
python docker/postgres-immich/run_offline_sdk_v4.py stop --root-update-vault
```

Sólo se ejecutó el primer comando local para verificar el plan. Cada paso real
exige el respaldo anterior, incluido CID antes de start. Los límites SDK600 s,
grafo180 s y capturas4 MiB se mantienen. No se inicia automáticamente la etapa
siguiente y no se ejecuta compile. Cinco pruebas V4 cubren diferencia cerrada,
separación de campos y recursos, generación de binds nuevos y plan sin SSH.
