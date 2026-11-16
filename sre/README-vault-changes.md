# Respaldo de cambios de la bóveda Windows

## Coordinación de la tarea de Windows

El observador de `Agency-Vault-Changes-V1` comparte un bloqueo de proceso con
el cargador. La observación y la reserva de una subida se turnan para acceder
a la cola local. El cargador libera ese bloqueo al registrar el intento y deja
que el observador capture otras versiones durante la conexión a R2. Los
bloqueos en disco coordinan la tarea de archivo y los escritores externos.

El modo continuo sigue cotejando el contenido de la bóveda cada segundo. En
reposo reutiliza hasta por diez segundos el resultado de una auditoría de la
cola y conserva las dos lecturas que confirman que el archivo es estable. Un
cambio de huella o en el directorio de capturas fuerza un recorrido inmediato.
Cada recorrido completo valida las identidades, los permisos y los recibos
antes de reservar una subida. El modo de ejecución única coteja toda la cola.

Cuando detecta una versión nueva, el observador usa la misma lectura de la
cola para deduplicar la huella y verificar la cuota antes de conservarla.
Durante una ráfaga, el cargador alterna entre la última versión observada o
publicada y la pendiente más antigua. Mantiene hasta tres subidas simultáneas
bajo un único bloqueo de cargador y vuelve a reservar cuando queda un espacio
libre. Cada captura conserva su snapshot, intento y acuse. La selección
coteja los intentos en curso con las reservas del cargador. Si falla una subida,
las demás conservan su resultado y el cargador detiene las nuevas reservas
hasta la conciliación. El modo de ejecución única
selecciona sólo la pendiente más antigua.

La comprobación operativa coteja los tiempos de captura, reserva y acuse con
el arranque de la tarea. El inventario de acuses verifica también el cierre
del proceso de subida y los tres comandos de Restic. El procedimiento de
pausa usa el plan sellado que corresponde a la acción registrada.

## Restauración con una caché vacía

Desde V10, el cargador publica un acuse inmutable en
`recovery-receipts-v1/` del prefijo protegido del repositorio. Escribe el
acuse después de verificar el listado del snapshot y usa el instante de
captura para ordenarlo. Ante un fallo de conexión, vuelve a intentar la
publicación del acuse del mismo snapshot. Los acuses cubren las capturas
observadas y las publicaciones confirmadas. Su contenido incluye el ID del
snapshot, el tamaño y la huella SHA-256, autenticados con una clave derivada
de `RESTIC_PASSWORD`. Si el segundo intento falla, la cola conserva el ID del
snapshot y requiere conciliación.

Para seleccionar la última captura desde R2, usa
`latest_recovery_record(client, expected_owner, password, repository_id)` en
`vault_change_worker.py`. Proporciona el cliente R2 y el marcador del perfil
verificado, la contraseña de Restic y el ID registrado del repositorio. La
función coteja el marcador, consulta una sola entrada del prefijo y autentica
su contenido. Usa el ID completo, la generación y la huella devueltos para
restaurar en un destino privado vacío. Después de rotar la contraseña,
verifica el acuse de una nueva captura con la credencial vigente.

Para recuperar una captura conocida, toma el ID completo del snapshot, la
generación y el SHA-256 esperado de un acuse verificado. Con el perfil y las
credenciales de recuperación en un entorno privado, coteja por GET el
marcador del repositorio y usa `restic cat config` para confirmar el ID
registrado. Comprueba con `restic cat snapshot <ID>` que el host, las
etiquetas y la ruta de la candidata corresponden al acuse.

Prepara una caché nueva y un destino privado vacío. Usa la subcarpeta que
contiene `candidate.env` dentro del snapshot, por ejemplo
`/C/GPU-Bridge/vault-changes-v1/captures/<generación>`. El ensayo de
restauración desde R2 usó esta operación:

```powershell
& $Restic --cache-dir $CacheVacia -o s3.region=auto -o s3.bucket-lookup=path `
    -o s3.connections=16 -o s3.retries=2 restore "${Snapshot}:$Subcarpeta" `
    --target $DestinoPrivado --verify
```

Exige salida cero, un único `candidate.env` y coincidencia de su SHA-256 con
el acuse. Mide desde el cotejo del marcador hasta esa coincidencia. La prueba
con el snapshot identificado alcanzó 32,328 s con 16 conexiones. Este ajuste
se aplicó a la restauración de ensayo. [Restauración medida](../.tools/evidence/phase7-vault-restore-new-cache-direct-16-20261116-v1.json)

Para snapshots anteriores a V10 sin acuse, el ensayo consultó Restic en R2 con
`snapshots --host <host> --group-by host --latest 1 --tag vault-changes-v1,OBSERVED --tag vault-changes-v1,COMMITTED`.
La agrupación por host devuelve un único candidato para el host solicitado,
aunque cada captura se guarde con una ruta distinta. Verifica el resultado
contra una huella independiente de la versión que se requiere recuperar.
Restic ordena por la hora del snapshot. El cargador puede iniciar la subida
de una captura antigua después de una más reciente durante una ráfaga.
En el ensayo, el candidato recuperado coincidió con la bóveda vigente y el
ciclo completo tardó 48,594 s. [Ensayo desde el catálogo](../.tools/evidence/phase7-vault-restore-new-cache-cloud-discovery-20261116-v1.json)

## Diseño de la entrega inicial

Esta unidad entrega código para conservar versiones y cargarlas cifradas al
repositorio R2 existente. **No activa una tarea, crea una raíz operativa, accede
a R2 ni modifica la bóveda durante su entrega.** El plan por defecto declara
`ready=false`; Root debe revisar, fijar las fuentes y completar identidades.
No sustituye ni modifica el writer histórico `0fc75c6d…` ni sus consumidores.

## Qué conserva

`vault_updates_v2.update()` es un writer público para los futuros operadores que
opten explícitamente por V2. Antes de reemplazar `.env`, guarda una candidata
inmutable mediante creación exclusiva, SHA-256, ACL privada y `fsync`, junto con
su recibo `PREPARED`. Publica una copia temporal en el mismo volumen de la bóveda;
la candidata original permanece en la cola. Después de releer los bytes
publicados crea `COMMITTED`. La plantilla contiene únicamente `CHANGE_ME`; un
fallo al publicarla conserva la candidata y el recibo de la bóveda ya publicada.

El lock coordina a los participantes de esta unidad y compara contenido e
identidad antes del reemplazo. **No es un CAS atómico frente a editores que no
respeten ese lock**: queda una ventana entre comparación y reemplazo. Tampoco
`fsync` acredita supervivencia ante cualquier fallo eléctrico o durabilidad del
directorio Windows. Estas limitaciones no se presentan como garantías cumplidas.

El watcher captura al arrancar y luego cada segundo las versiones manuales o
de writers históricos que resulten iguales en dos lecturas separadas por
100 ms. Valida la sintaxis literal, sin `eval`; registra por separado el estado
de los comentarios. Una escritura parcial inválida no se encola. Una versión
parcial que ya sea sintácticamente válida sí puede observarse: no se promete
conservar cada pulsación ni versiones que existan menos que el intervalo de
observación. Deduplica únicamente la última huella consecutiva; A→B→A conserva
tres generaciones. El watcher **nunca escribe `.env` ni su plantilla**.

## Configuración y activación reservadas a Root

El comando de plan sólo lee las tres fuentes públicas para obtener sus hashes:

```powershell
python -I -S -B sre/vault_change_worker.py
```

La propuesta fija estas rutas, todavía no creadas por esta entrega:

| Uso | Ruta / propietario |
| --- | --- |
| Código sellado | `.tools/backup-source-build/vault-changes-code-v1`, `agencia-digital-marketing-ai:vault-changes-code:v1` |
| Cola privada | `C:\GPU-Bridge\vault-changes-v1`, `agencia-digital-marketing-ai:vault-changes:v1` |
| Temporales de publicación | `.tools/backup-source-build`, marcador existente SHA `78925844…` |

Root debe preparar el directorio de código con el `Store` congelado, conservar
su marcador/identidad y copiar exactamente `vault_updates_v2.py`,
`vault_change_queue.py`, `vault_change_worker.py` y la copia sellada de
`scripts/ci/document_vault.py`. Su SHA completo está en el plan; se carga sólo
el AST de `GROUPS` y `documentation`, nunca su rutina de actualización.
Las tres fuentes nuevas se cargan por bytes fijados, sin adoptar `.pyc` vecinos.

Root completa un JSON privado a partir de `config_template`: identidad externa
del código, ruta y SHA del **Python real** aprobado, y posteriormente identidad
externa de la cola. Conserva el perfil/SHA en la bóveda antes de usarlo. El
ejecutable no debe ser un redirector que haga salir al padre antes que su hijo.
El SID debe ser el mismo que acredita las raíces y los seis helpers existentes;
no se supone que ejecutarlo como SYSTEM funcione con esas identidades.

Secuencia de revisión y activación futura:

1. Fijar fuentes/configuración y ejecutar explícitamente `--mode prepare` con
   `--config <ruta>` y `--config-sha256 <SHA>`. Sólo crea la cola propia con
   marcador privado y devuelve su identidad; no usa R2.
2. Root respalda esa identidad y el nuevo JSON/hash antes de cualquier writer
   V2 o carga. Un cambio de JSON requiere actualizar el pin externo.
3. Validar primero un ciclo acotado `--mode once` y sus recibos, incluida una
   restauración independiente posterior bajo autorización de Root. Una subida
   por sí sola no acredita restaurabilidad ni RPO sostenido.
4. Activar `--mode watch` sólo después de esa revisión. Task Scheduler es una
   opción de gestión SRE, sin puerto ni GPU. Root debe fijar nombre, SID, sesión,
   ejecutable, argumentos públicos y política de reinicio/múltiples instancias
   antes de registrarlo. Esta unidad no registra tareas ni evita UAC. La
   cobertura con sesión cerrada depende del principal finalmente elegido.

La API pública del writer exige siempre `config_path` y `config_sha256`; no
busca credenciales por argumentos o variables de proceso. Se ejecuta desde la
copia de código sellada. Los operadores históricos continúan usando 0fc hasta
que Root cambie cada futuro operador de forma explícita; sus versiones estables
quedan cubiertas por observación, con los límites anteriores.

## Carga, recursos y reconciliación

El cargador usa un bloqueo distinto del bloqueo corto de captura. Cada
generación reserva `attempt.json` y un nonce antes de arrancar su hijo. El hijo
crea `claim.json` exclusivo antes de la red, comprueba el marcador del repositorio
existente por GET y ejecuta únicamente `cat config`, `backup` y `ls` mediante los
helpers aprobados. No llama al controlador monouso anterior ni ejecuta `init`,
`unlock`, `forget` o `prune`.

Usa restic agency2 SHA `78735dd1…`, el perfil previo SHA `96d171fc…` y sus seis
fuentes fijadas. La identidad del repositorio existente se comprueba antes de
respaldar. Los secretos necesarios sólo pasan por el entorno mínimo privado del
proceso restic; no aparecen en argv, recibos públicos o logs de esta unidad.
La salida bruta de restic se conserva en la cola privada.

El hijo completo vive en el NativeJob congelado `dfe20e53…`: 390 s, 8 procesos,
2 GiB y 1 MiB de salida capturada. Verifica el Job inmediato antes de usar red;
`cat`, `backup` y `ls` tienen además 60/180/60 s respectivamente. El límite
exterior también cubre resolución DNS y comprobación del marcador. El resultado
se acepta sólo con cierre completo del Job, salida limpia, SHA/generación
correctos y listado de un único archivo del tamaño esperado. No se crean
procesos por nombre ni se matan PID ajenos.

Una candidata válida puede subirse como `PREPARED` si hubo una interrupción sin
prueba de publicación. `COMMITTED` exige su marcador; `OBSERVED` no demuestra
que el writer publicara esa generación. Se graba el snapshot privado tan pronto
como restic lo devuelve, antes de validar el listado. Un resultado incierto o
un intento existente impide el replay automático; la captura sigue funcionando
mientras el uploader queda pausado para revisión.

Un cierre abrupto puede dejar `.active.json` o `uploader.lock.json`. Al
arrancar en Windows, V5 consulta la hora del último arranque y toma un mutex
del sistema. Si la creación y la última escritura del archivo son anteriores
a ese arranque, coteja el propietario, el nonce y la identidad del archivo.
Conserva los bytes en `operations/recovered-boot-lock-*.json`.

Los bloqueos creados durante el arranque actual y los intentos de subida
sin acuse requieren revisión explícita. El operador coteja la candidata,
los registros, las etiquetas y los snapshots conocidos antes de reconciliar
un intento. Los IDs de snapshot se guardan en archivos privados de la cola
para evitar que la propia subida genere otro cambio en `.env`.

La propuesta permite 1024 generaciones y 2 GiB de **candidatas retenidas**; las
salidas/logs también tienen cotas por generación, pero no se incluyen en esos
2 GiB. No borra historial: Root debe definir retención y espacio disponible.
Al alcanzar la cuota, V2 falla antes de publicar nuevos bytes. El estado privado
registra errores de captura y pausa; `--mode status` no usa red y no revela
valores ni IDs de snapshot. Los tiempos de captura/subida permiten medir RPO;
esta entrega no afirma un RPO real hasta observarla funcionando.

## Validación de esta entrega

Las pruebas usan bóvedas sintéticas, filesystem temporal, S3/restic controlados
y el contrato real del validador NativeJob antes de crear procesos. Comprueban
persistencia previa al replace, ventana de crash, conflictos cooperativos,
cuotas, integridad, carga PREPARED, exclusión/replay y cierre antes de reconocer
un snapshot. No son una aceptación de ACL operativa, proceso Windows, Task
Scheduler, conectividad R2 o restauración nueva; Root debe verificar esas
fronteras al activar la unidad.
