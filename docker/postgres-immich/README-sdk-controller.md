# Controlador por etapas del SDK PostgreSQL Immich

Esta unidad prepara el controlador local, el receptor/ejecutor remoto y sus
pruebas. La verificación Linux usa SSH fijado y un directorio temporal propio,
sin ejecutar acciones del controlador, Docker o SDK. Las acciones reales
quedan a cargo de la raíz después de congelar fuentes y respaldar sus hashes.

El alcance es exclusivamente el constructor temporal de PostgreSQL Immich en
`vm-media-01`, `192.168.0.85`. No despliega aplicaciones ni modifica el builder
global, `sre_control.py`, NAS, `.7`, Compose o datos PostgreSQL de producción.

## Contrato fijo

Se conserva el plan histórico con SHA-256
`c77cdcbc4c327db69fd72eeaa01ef73a77fb333fa199b2f049129e6dcfe0899e` y el kit
de 78 archivos cuyo `SHA256SUMS` tiene SHA-256
`d3f427770834e2d2c20337a9a3d3c8161a191a16d2cb174ea2d66cdfab4b03a0`.
La revisión 2 conserva ambos documentos sin modificar sus bytes. Sobre ese
histórico aplica dos ajustes aprobados: la máscara tmpfs para el VOLUME
heredado `/var/lib/postgresql/data` (1 MiB, nosuid, nodev y noexec), y la imagen
propia PostgreSQL para aplicaciones con override explícito `--user 0:0`
exclusivamente para este SDK. No genera un volumen anónimo. Los límites
externos de las etapas se reducen a SDK 600 s y grafo 180 s.

La imagen propia fijada es
`sha256:c908ada03479b540f808ca3d3bf426ae012cae8097c355b3d672d066465fb54a`.
El Dockerfile PostgreSQL aplicaciones con SHA-256
`ce37ad0eee4d2ef69197a6ecaba25e6720fbfefa79e616f19dbffb3bcdfdf652`
conserva las once capas originales, los 45 paquetes Alpine, PostgreSQL 16.15,
sus headers y ambos scripts oficiales; únicamente elimina los archivos reales
gosu y su enlace su-exec, añade etiquetas y fija `USER 70:70`. El recibo real
runtime2 acredita scripts conservados, gosu ausente y arranque como UID 70.
El informe Trivy ya aceptado registra cero HIGH/CRITICAL para esta imagen;
esa auditoría de la base no acredita todavía el futuro SDK o extensiones.

Docker 29 presenta el digest del manifiesto en `container.Image`. El ID del
JSON de configuración observado por Trivy es otro identificador y no se usa
como identidad del contenedor. La imagen aplicaciones y su runtime conservan
`USER 70:70`; el SDK necesita UID 0 para instalar sus inputs offline dentro
del contenedor temporal. Ese override no modifica la imagen ni su runtime.

| Recurso | Ruta o valor |
| --- | --- |
| Propietario | `agencia-digital-marketing-ai:postgres-immich-builder:v1` |
| Staging UID/GID 1000 | `/home/jandrade/.agencia-pg-immich-stage-d3f427770834e2d2c20337a9a3d3c816` |
| Inputs root, sólo lectura | `/opt/media-platform/build/contexts/postgres-immich/d3f427770834e2d2c20337a9a3d3c816/inputs` |
| Outputs del SDK | `/var/lib/media-platform/build/postgres-immich/d3f427770834e2d2c20337a9a3d3c816/outputs` |
| Control host, fuera del contenedor | `/var/lib/media-platform/build/postgres-immich/d3f427770834e2d2c20337a9a3d3c816/control` |

Los cinco campos individuales `MEDIA_POSTGRES_IMMICH_SDK_REMOTE_STAGING_PATH`,
`MEDIA_POSTGRES_IMMICH_SDK_INPUTS_PATH`, `MEDIA_POSTGRES_IMMICH_SDK_OUTPUTS_PATH`,
`MEDIA_POSTGRES_IMMICH_SDK_CONTROL_PATH` y `MEDIA_POSTGRES_IMMICH_SDK_OWNER`
deben coincidir exactamente. También se exige el layout respaldado de las
raíces `/opt/media-platform` y `/var/lib/media-platform`, con sus dev/ino,
marcadores y recibos originales. Los ancestros compartidos del sistema de
builds deben existir ya y conservar sus marcadores; no se adoptan directorios
desconocidos ni se modifica la metadata de otros componentes.

## Plan y persistencia por la raíz

El modo predeterminado lee sólo JSON y hashes de fuentes locales. No lee
`.env`, abre SSH ni inspecciona o ejecuta SDK/Docker:

```powershell
python docker/postgres-immich/run_offline_sdk.py
```

La raíz usará el Python aislado con Paramiko **5.1.0** para las transiciones.
El CLI exige `--root-update-vault` en las acciones reales. Sólo entonces carga
la bóveda con el parser canónico e invoca el helper privado `vault_updates`;
el agente implementador no utiliza esa opción ni escribe `.env`.

El plan ligado a la bóveda fija los hashes del controlador, ejecutor remoto,
helpers, host, claves públicas/huella existentes y layout. Antes de autenticar
se verifican los 78 archivos locales por tamaño, tipo y SHA-256. No sigue
enlaces ni reparse points. SSH exige únicamente la clave cliente Ed25519
respaldada, correspondencia de su pública y el pin exacto del servidor:
RejectPolicy, sin agente SSH, búsqueda de claves o contraseñas alternativas.

Los documentos que el CLI de raíz respalda son:

| Campo | Transiciones conservadas |
| --- | --- |
| `MEDIA_POSTGRES_IMMICH_SDK_CONTROLLER_B64` | Plan canónico, hashes, rutas, layout y destino fijado. |
| `MEDIA_POSTGRES_IMMICH_SDK_PREPARED_B64` | Transferencia y prepare: staging, directorios, dev/ino, marcadores y recibo. |
| `MEDIA_POSTGRES_IMMICH_SDK_CONTAINER_B64` | Create, start y stop: CID completo, imagen, estado, PID observado y límites. Un CID devuelto con fallo se conserva como no verificado. |
| `MEDIA_POSTGRES_IMMICH_SDK_STAGE_RECEIPTS_B64` | SDK y grafo: proceso cliente, límites efectivos, hashes de logs y recibos observados. |

Cada actualización se hace antes de permitir la etapa siguiente. El servidor
compara el documento respaldado con su recibo host protegido; una bandera o
autorreporte de entorno no sustituye esa igualdad. Ninguna acción llama
automáticamente a la siguiente.

## Secuencia explícita de operaciones

Los comandos siguientes describen el uso futuro por la raíz; no se ejecutaron
como parte de esta preparación:

```powershell
python docker/postgres-immich/run_offline_sdk.py plan --root-update-vault
python docker/postgres-immich/run_offline_sdk.py prepare --root-update-vault
python docker/postgres-immich/run_offline_sdk.py create --root-update-vault
python docker/postgres-immich/run_offline_sdk.py start --root-update-vault
python docker/postgres-immich/run_offline_sdk.py sdk --root-update-vault
python docker/postgres-immich/run_offline_sdk.py graph --root-update-vault
python docker/postgres-immich/run_offline_sdk.py stop --root-update-vault
```

`prepare` transmite mediante un receptor Python como jandrade. No usa SFTP
con resolución de enlaces implícita: crea el staging exclusivamente, retiene
descriptores de directorio y abre cada archivo con NOFOLLOW y EXCL. La
transferencia está limitada a 256 MiB por archivo, 768 MiB totales y el tamaño
exacto autenticado de cada entrada. Rechaza escapes, enlaces, tipos especiales,
datos adicionales, archivos alterados y destinos existentes.

El CLI respalda el recibo del staging antes de pedir promoción root. El helper
root valida hostname/IP, layout, UID/GID, permisos, whitelist, inodos y hashes
antes de copiar a su namespace. Las nuevas carpetas son root:0700 y los
archivos root:0600. La copia se hace por descriptores, sin sobrescribir, borrar
o mover archivos ajenos. Luego se respalda prepare completo **antes de
create**. El staging original se conserva para auditoría; no se limpia de
forma recursiva ni se reutiliza automáticamente un estado parcial.

`create` exige ese prepare respaldado. Verifica la imagen propia local por
el manifiesto c908 fijado, `USER 70:70` y el entorno público exacto de la base.
Usa la referencia literal de ese manifiesto y `--pull never`; no descarga
una imagen. Devuelve el CID de 64 caracteres y lo registra; el CLI de raíz lo
respalda antes de aceptar `start`. El contenedor debe permanecer en estado
created. No se arranca para obtener su identidad.

`start` sólo admite ese CID creado y nunca iniciado. Antes y después comprueba
imagen, usuario exactamente `0:0`, etiquetas, entrada fija `/bin/sh ... hold`, entorno público exacto,
límites, namespaces y NoForeign. Exige network none, ausencia de puertos,
dispositivos, privilegios o capacidades añadidas, NNP, los dos binds exactos
y la máscara tmpfs, sin volúmenes anónimos. Se conserva la retirada NET_RAW del
plan histórico y las capacidades ordinarias del constructor. Docker puede
representar esa retirada como la lista `['NET_RAW']` o `['CAP_NET_RAW']`:
se aceptan exclusivamente esas dos listas de un elemento. No se normalizan
otras capacidades, prefijos arbitrarios, duplicados o listas combinadas. Después de
arrancar acredita en cgroup v2 los valores reales `memory.max=12884901888`,
`memory.swap.max=0`, `pids.max=256` y cuota CPU igual a cuatro periodos. Si no
se pueden acreditar, intenta detener el CID propio y no permite SDK.

`sdk` y `graph` vuelven a validar layout, directorios, inputs, recibos y la
misma instancia iniciada. La evidencia host vive en control, sin montar en el
contenedor. La configuración Docker es privada y vacía; no se heredan
credenciales del host. Logs y capturas se limitan a **4 MiB**; excederlos es un
fallo, nunca una aprobación truncada. El recolector POSIX conserva el líder
sin recolectar mediante WNOWAIT, cierra su grupo propio y sólo después hace
wait; no utiliza un PGID reciclable ni alcanza grupos ajenos.

## Timeout, cancelación y detención

Terminar el cliente `docker exec` no termina el proceso que el daemon ejecuta
dentro del contenedor. Por eso un timeout, error o cancelación activa una
segunda comprobación NoForeign y la parada por **CID exacto**. Se pide stop
con cinco segundos; si sigue vivo y continúa acreditado, se pide kill. Se
inspecciona de nuevo para informar si la detención se verificó. No se actúa
por nombre de contenedor, nombre de proceso o PID host.

Durante SDK/grafo el CLI envía un latido por segundo sobre stdin SSH. EOF o
cinco segundos sin latido interrumpen el recolector y activan la misma
detención del CID propio, incluso si desaparece el CLI mientras sigue la
etapa. Se reserva tiempo de limpieza dentro del presupuesto. Un resultado
normal conserva hold para la etapa posterior; la vida máxima adicional del
hold es de 7200 segundos. Los límites son mecanismos implementados y pruebas
locales; no constituyen un tiempo de producción medido.

`stop` usa la evidencia host y el CID previamente respaldado. No exige que el
SDK conserve intactos sus archivos de salida para poder detener su propio
contenedor, pero mantiene la validación de imagen, etiquetas y montajes. Si
la comprobación indica un recurso ajeno o ambiguo, no envía señales y reporta
el fallo. No elimina contenedores, imágenes o volúmenes. Un error de limpieza
queda explícito en el informe.

## Recuperación propuesta del create no iniciado

La raíz creó el CID
`1fc6479f4e27bd361b26579a7fb912be816c9c0ccc61f260eee04bbc36cde1d9`
y el guard anterior rechazó la representación `CAP_NET_RAW` antes de start.
El diagnóstico disponible registra estado created, PID 0 y StartedAt cero.
No contiene el objeto inspect completo, por lo que no basta por sí solo para
autorizar una recuperación. El CID quedó respaldado como create_unverified.

Se propone una migración explícita de recibos por equivalencia semántica,
mediante una unidad separada que la raíz revisará antes de ejecutarla. Este
controlador no implementa la migración ni la ejecuta automáticamente. No se
debe reintentar prepare/create sobre los namespaces existentes ni cambiar
únicamente el plan activo para evitar el rechazo.

1. La raíz conserva los B64 activos completos del plan, preparación, CID y
   etapas en historia antes de sustituirlos. Guarda también los bytes y hashes
   de prepare.json, cid.txt, marcadores y configuración Docker privada; obtiene
   un inspect completo nuevo del CID exacto y respalda su hash antes de escribir.
2. La comparación de ambos planes canónicos debe permitir diferencias sólo
   en remote_sha256 y plan_sha256. Los hashes de ambas fuentes congeladas deben
   acreditar que el único cambio del ejecutor es la aceptación de las dos
   representaciones equivalentes de CapDrop. Imagen, argv, rutas, kit, usuario,
   límites, propietarios, layout e identidades deben conservarse exactamente.
3. Se verifican de nuevo host/IP y SSH fijados, layout, dev/ino y marcadores de
   las tres raíces; staging e inputs deben contener sus whitelists y hashes
   originales. Outputs sólo debe contener sus marcadores, sin artefactos SDK.
   Control debe tener exclusivamente el estado de prepare y el intento create:
   prepare.json, cid.txt, directorio vacío .create-started, marcadores y
   docker-config/config.json con bytes exactamente `{}`. No puede haber recibos o intentos start,
   sdk, graph o stop. Todo se abre por descriptores sin seguir enlaces.
4. El inspect completo debe pasar NoForeign con el ejecutor corregido, tener
   Id y cid.txt iguales al CID respaldado, estado created, Running false, PID 0
   y StartedAt cero. Se fija Created y el hash de ese inspect para comprobarlos
   otra vez antes y después de modificar exclusivamente recibos. No se llama
   docker start, exec, stop, kill ni rm durante esta migración.
5. Un helper acotado conserva el prepare original y genera su versión migrada
   para el plan/verificador nuevos, con procedencia, hashes anteriores y hora
   de verificación explícitos. Mantiene intactos directories y el transfer
   original anidado: no finge otra transferencia ni cambia el marcador de
   staging. Sólo reemplaza prepare.json de control de forma atómica después
   de acreditar su inodo/hash originales. Inputs, outputs, sus marcadores y
   sus dev/ino no se modifican.
6. El helper crea create.json con EXCL/NOFOLLOW como recibo de **verificación
   recuperada del recurso existente**, con CID, inspect, plan/fuente nuevos y
   procedencia create_unverified. No inventa otro proceso docker create: si
   su registro original no existe, process queda nulo y la ausencia es expresa.
   Conserva .create-started y cid.txt; no recrea ni inicia el contenedor.
7. La raíz respalda los recibos resultantes antes de habilitar start. El B64
   preparado conserva transfer original y prepare migrado; el B64 del
   contenedor conserva create_unverified histórico y añade create verificado.
   Sólo entonces sustituye el plan activo y comprueba igualdad literal entre
   recibos protegidos y bóveda. Cualquier interrupción o diferencia mantiene
   start bloqueado hasta reconciliación explícita; no se rebajan sus guardas.

Esta propuesta conserva las copias de software y el contenedor nunca iniciado.
Su implementación, pruebas de rechazo y ejecución siguen pendientes; no se ha
migrado ningún recibo ni modificado infraestructura durante esta corrección.

## Verificación y límites actuales

Las pruebas locales cubren entrada alterada, inventario, transiciones con
respaldo de CID, NoForeign, rechazo antes de SSH, salida continua, timeout y
secuencia stop/kill del contenedor propio mediante proveedor controlado. En
Windows se omiten expresamente las pruebas nativas POSIX de recolección,
latido y NoFollow. El arnés Linux ejecuta los 15 casos, incluidos esos tres
casos nativos, con presupuesto externo de 40 segundos y limpieza de sus
descendientes propios; registra fuentes exactas y distingue proveedores
controlados de operaciones nativas. La evidencia de cada revisión queda
separada; no acredita operaciones Docker ni SDK.

No se ha acreditado aún el árbol musl real ni la exclusión de
wasm-bindgen-shared, tampoco compilación, SQLite enlazada, ABI, SQL o imagen
final. `compile` sigue fuera de los modos admitidos. Los scripts congelados y
sus recibos de preparación permanecen sin cambios.
