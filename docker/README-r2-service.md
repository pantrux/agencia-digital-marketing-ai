# Control del servicio R2

Esta unidad administra exclusivamente el consumidor R2 en `vm-media-01`,
`192.168.0.85`. El plan local no conecta ni modifica recursos. Cada acción real
exige un plan/hash respaldado y utiliza SSH Ed25519 fijado, Paramiko 5.1.0,
`RejectPolicy` y el Docker Engine API por `/run/docker.sock`; no usa shell para
Docker, SDK, Git ni comandos recibidos de una petición HTTP.

La imagen permanece fijada al manifiesto
`sha256:fcb07c34a7cbbefd831d29429b2dd6c1ad881f8310cd174df2b0a9804b0602ef`.
La inspección OCI enlaza su config `26889cdc…`, las seis fuentes y el root SHA
`c0a312232342d92f1f0067606d54d9c0662a95fab8795f6d637a930ee76f91ee`.
Su recibo estático `deb2693b…` acredita capas, modos y SCA HIGH/CRITICAL, no un
servicio arrancado. El informe bruto conserva cinco MEDIUM y un LOW en pip.

## Preparación del responsable

El responsable respalda las siguientes variables nuevas, manteniendo los tres
comentarios españoles y la plantilla. El controlador nunca escribe la bóveda:

| Variable | Contenido |
| --- | --- |
| `MEDIA_R2_SERVICE_SOURCE_SHA256` | SHA256 de `r2_service.py`. |
| `MEDIA_R2_SERVICE_REMOTE_SHA256` | SHA256 de `r2_service_remote.py`. |
| `MEDIA_R2_SERVICE_CONTRACT_SHA256` | SHA256 de `r2_service_contract.py`. |
| `MEDIA_R2_SERVICE_SPEC_B64` | JSON cerrado descrito abajo, codificado Base64. |
| `MEDIA_R2_SERVICE_APPROVED_PLAN_SHA256` | Hash del plan de la acción concreta. |
| `MEDIA_R2_SERVICE_ROOT` | `/var/lib/media-platform/r2-sync-consumer`. |
| `MEDIA_R2_SERVICE_MEDIA_ROOT` | `/mnt/producciones`. |
| `MEDIA_R2_SERVICE_PORT` | `3010`. |

También exige la política `MEDIA_R2_SYNC_MAX_OBJECT_BYTES`, inicialmente
**1073741824** (1 GiB), y las identidades existentes del host/layout. Es un límite
operativo explícito, sin garantía de transferir ese tamaño dentro de los 300 s
del worker; no es una cuota comercial ni un SLA. Su modificación requiere un
plan de configuración nuevo, sin reinicializar un ledger existente.

El JSON de `MEDIA_R2_SERVICE_SPEC_B64` contiene exactamente estos campos:

```json
{
  "schema_version": 1,
  "deployment_id": "UUID_V4_MINUSCULA_NUEVA_RESPALDADA",
  "image_id": "sha256:fcb07c34a7cbbefd831d29429b2dd6c1ad881f8310cd174df2b0a9804b0602ef",
  "image_receipt_sha256": "deb2693be56e1d9b0458bc8bafd38673bd2c5fdd716605d1652714e5d6e597ed",
  "source_root_sha256": "c0a312232342d92f1f0067606d54d9c0662a95fab8795f6d637a930ee76f91ee",
  "account_id": "CF_ACCOUNT_ID_RESPALDADO",
  "max_object_bytes": 1073741824,
  "media_before": {"device": "ENTERO_OBSERVADO", "inode": "ENTERO_OBSERVADO"},
  "share_evidence_sha256": "SHA256_DE_EVIDENCIA_F2_DEL_SHARE_PROPIO",
  "prepared": null,
  "init_container_id": null,
  "service_container_id": null,
  "init_result": null
}
```

Los textos explicativos deben sustituirse; no son un ejemplo ejecutable. No se
reutiliza el root del fixture NFS v4. El responsable acredita la raíz real del
share por su evidencia de Fase 2 y dispositivo/inodo actual. Antes de escribir,
el helper verifica NFS4.1/rw, origen exacto `.8:/volume2/producciones` y el `.probe`
de 170 bytes/hash acreditado. Exige ausencia del marcador nuevo
`.agency-r2-service-root.json`, del root local y del proyecto
`proyectos/aceptacion_http_20261106`; rechaza colisiones y symlinks antes de crear.
No adopta ni altera proyectos existentes.

`prepare` crea sólo el marcador nuevo y proyecto/`01_raw` de aceptación en el
NAS, más el root local propio, `state` y archivos privados. NAS conserva la
proyección ACL de Synology: no se exige UID65532/modo0600 allí. Se mantienen
NoFollow, archivo regular/nlink1, dispositivo/inodo y SHA del marcador. En SSD,
root/state son UID/GID65532 `0700`, marcadores/recibos `0600`; configuración y
tres secretos `0400`, regulares/nlink1. No hay chown/chmod recursivo ni borrado.

La clave HMAC procede únicamente de
`STUDIO_EDGE_TO_HOMELAB_HMAC_SECRET` (64 bytes ASCII visibles). R2 usa sólo
`CF_ACCOUNT_ID`, `R2_PLATFORM_ACCESS_KEY_ID` y `R2_PLATFORM_SECRET_ACCESS_KEY`.
La clave canónica conserva el propósito ya respaldado en la arquitectura. Sólo
`prepare` inicial envía esos tres valores por stdin SSH, nunca argv/env/logs.
Se fijan permisos cuando el archivo aún está vacío, antes de escribir secretos.

## Secuencia explícita

Cada invocación sin `--apply` emite su plan JSON. Guardar ese plan privado y
respaldar su `plan_sha256` antes de aplicar:

```powershell
python docker/r2_service.py prepare
python docker/r2_service.py prepare --apply --plan RUTA_ABSOLUTA_PLAN --expected-plan-sha256 HASH_RESPALDADO
```

Repetir el mismo patrón de plan/apply para las acciones siguientes:

1. **prepare:** respaldar el DTO completo devuelto en `spec.prepared` antes de
   crear un CID. Contiene las identidades de root/state/media/proyecto/`01_raw`,
   identidad/mtime/ctime de los cuatro archivos privados y SHA de configuración.
   No devuelve hashes ni valores de secretos.
2. **create-init:** crea un contenedor detenido con entrada fija `init-db` y red
   `none`. Respaldar su `container_id` en `spec.init_container_id` **antes de start**.
3. **start-init:** exige ese CID respaldado y sus recibos propios; sólo arranca
   una DB ausente, espera exit 0 y salida fija `initialized`. Respaldar el DTO
   `init_result`. No hay adopción/reinicialización implícita de DB ni `--rm`.
4. **create-serve:** exige init acreditado. Comprueba antes que `.85:3010` esté
   libre mediante un socket propio sin listen; no termina procesos ajenos.
   Crea un CID detenido. Respaldarlo en `spec.service_container_id` antes de start.
5. **start-serve:** verifica de nuevo archivos, estado, CID, imagen, labels,
   configuración y todos los binds; arranca y consulta `/healthz` real.
6. **stop:** sólo controla el CID de servicio respaldado. Valida el control SSD,
   marcador/recibo e intención/created propios y configuración del daemon.
   **No abre NAS ni DB**; puede detener el servicio aunque fallen esos proveedores.
   No elimina el contenedor ni datos.

Un core estable enlaza deployment/configuración; los campos `prepared`, CIDs e
`init_result` se actualizan externamente sin alterar ese core. Cada acción sí
tiene su propio plan/hash. Los recibos e intenciones son EXCL: no se sobrescriben.
Una creación previa sólo se reconoce por su recibo protegido y CID acreditado,
nunca por coincidencia del nombre. La retirada del init detenido es una acción
posterior del responsable, después de conservar su recibo.

Si Docker creó un CID y falla la inspección o comprobación posterior, se conserva
la intención privada y se devuelve `created_cid`, `verified:false` y
`requires_cid_backup_before_any_action:true`. **También aparece con exit code 2**:
el responsable debe leer y respaldar ese CID antes de evaluar el fallo o realizar
otra acción. No lo arranca automáticamente. Un resultado así exige revisión y
un plan nuevo; el CLI no oculta el identificador tras un error genérico.

## Restricciones del contenedor

El API v1.54 se utiliza sólo si la versión del daemon admite esa API; el host
revisado es Engine29.9.3/API1.40–1.56. La entrada usa `python -I -B -S`, imagen
fijada, UID/GID65532, rootfs RO, capDrop ALL, no-new-privileges, 1 CPU, 768 MiB,
sin swap adicional, pids64 y tmpfs `/tmp` de 16 MiB/noexec/nosuid/nodev.
El init tiene red none/restart no; serve usa bridge con DNS/TLS de salida para
R2 y restart unless-stopped. Crear un contenedor nunca implica arrancarlo.

Sólo state es bind local RW; los cuatro archivos privados son binds RO. Serve
añade el NFS como `/media` RW y publica únicamente
`192.168.0.85:3010 → 3010/tcp`, sin red host, volúmenes anónimos o montaje del
root completo de control. La comparación de mounts contempla campos false/default
omitidos por Docker, rechaza opciones/montajes extra y contrasta la proyección
real `Mounts`. CapDrop/SecurityOpt siguen su forma canónica observada.

La salud sólo contiene ready/busy; busy con HTTP200 es válido y no provoca
reinicio/parada. La autenticación POST y los límites del worker siguen el
[runtime congelado](r2-sync-consumer/README-runtime.md). El controller tiene
presupuesto local120 s, alarma remota absoluta110 s y peticiones Docker de hasta
20 s con presupuesto global100 s. Una syscall bloqueada en D-state no puede ser
terminada inmediatamente; la alarma actúa cuando el kernel devuelve control.
No usa señales, nombres o grupos de procesos ajenos.

## Arranque ordenado en vm-media-01

El contenedor `serve` puede quedar bajo `agency-r2-sync.service` con `restart=no`.
El timer intenta iniciarlo al arrancar y una vez por minuto mientras esté inactivo.
La unidad exige `mnt-producciones.mount`, espera a Docker y se detiene si pierde
el montaje. Comprueba el origen NFSv4.1 en el host y en el namespace del CID,
la identidad de `/media` y su marcador antes de señalar disponibilidad. Un estado
`busy` válido mantiene el servicio disponible. El supervisor espera la salida de
Docker y detiene sólo su CID, incluso cuando el NAS no responde.

Prepara un `container.json` sin credenciales a partir del recibo del contenedor
`serve` y de `spec.prepared`. Usa `service_container_id` como `cid`, la `.Image`
exacta de Docker como `image`, `deployment_id` como `deployment`, y
`prepared.config_sha256` como `config_sha256`. Toma `media_device`, `media_inode`
y `media_marker_sha256` de `prepared.media_proof.device`, `.inode` y
`.marker_sha256`. Conserva los tipos enteros del dispositivo e inodo.

Desde root, instala `r2_service_boot.py` (`0444`) y `container.json` (`0400`) en
`/opt/media-platform/r2-service-boot-v1/` (`0700`), todos `root:root`. Instala
[`agency-r2-sync.service`](r2-sync-consumer/agency-r2-sync.service) y
[`agency-r2-sync.timer`](r2-sync-consumer/agency-r2-sync.timer) en
`/etc/systemd/system/` (`0644`). Usa el CID previamente acreditado en `cid`.

```bash
script=/opt/media-platform/r2-service-boot-v1/r2_service_boot.py
python3 -I -S -B "$script" preflight
mount_unit=$(systemctl show mnt-producciones.mount --property=FragmentPath --value)
test -n "$mount_unit" && systemd-analyze verify "$mount_unit" /etc/systemd/system/agency-r2-sync.{service,timer}
docker inspect --format '{{.Id}} {{.State.StartedAt}} {{.HostConfig.RestartPolicy.Name}}' "$cid"
docker update --restart=no "$cid"
systemctl daemon-reload
systemctl start agency-r2-sync.service
systemctl enable --now agency-r2-sync.timer
python3 -I -S -B "$script" check
docker inspect --format '{{.Id}} {{.State.StartedAt}} {{.HostConfig.RestartPolicy.Name}}' "$cid"
```

El CID y `StartedAt` deben coincidir antes y después de la adopción en caliente.
Para la prueba de reinicio conserva el CID, registra el nuevo `StartedAt`, el
estado de la unidad y del timer y el resultado de `check`. En la verificación de
NAS tardío, comprueba que la unidad no inicia Docker ni el contenedor antes de
tener su montaje y que el siguiente disparo del timer recupera el mismo CID. El
timer no se habilita junto con la unidad. Para mantenimiento intencionado, detén
primero el timer. `docker.service` conserva sus dependencias originales.

Linux puede reasignar `st_dev` al montar de nuevo NFS. El supervisor coteja el
origen NFSv4.1, el inodo y la huella del marcador. Con el contenedor detenido,
verifica la huella de la plantilla y actualiza únicamente el dispositivo de la
prueba NFS en la configuración efectiva. El archivo conserva su propietario y
sus permisos. `config_sha256` y `media_device` del perfil corresponden a la
plantilla acreditada. Después de arrancar, el supervisor coteja el dispositivo
actual también dentro del contenedor.

El controlador `r2_service.py` y su ejecución remota `r2_service_remote.py`
verifican el contrato original mediante `r2_service_contract.py`. El inventario
general coteja la imagen y registra el CID. `r2_service_boot.py` valida también
el CID acreditado y su política de reinicio. Para devolver el control a Docker
en caliente, ejecuta en este orden y coteja CID y `StartedAt` al final.

```bash
systemctl disable --now agency-r2-sync.timer
python3 -I -S -B "$script" check
docker update --restart=unless-stopped "$cid"
systemctl stop agency-r2-sync.service
python3 -I -S -B "$script" preflight
docker inspect --format '{{.Id}} {{.State.StartedAt}} {{.HostConfig.RestartPolicy.Name}}' "$cid"
```

La parada del supervisor deja activo el CID cuando su política ya es
`unless-stopped`. Una parada ordinaria con `restart=no` detiene el contenedor.
`ExecStopPost` ejecuta esta decisión también si la preparación falla antes de
anunciar disponibilidad o termina `docker wait`. Para un reemplazo de imagen,
detén primero el timer y el supervisor y registra la transición de CID y perfil
antes de reactivar el timer. Usa el controlador anterior con el plan respaldado
cuando devuelvas el control a Docker.

## Pruebas y aceptación

```powershell
python -B -m unittest discover -s tests -p test_r2_service.py -v
python -m ruff check docker/r2_service.py docker/r2_service_contract.py docker/r2_service_remote.py tests/test_r2_service.py
python -m ruff format --check docker/r2_service.py docker/r2_service_contract.py docker/r2_service_remote.py tests/test_r2_service.py
```

Las pruebas portables usan Docker/SSH controlados, sin daemon/listener/R2 reales:
orden create/backup/start, rechazo de DB/nombre ajenos, CID tras fallo, health busy,
stop sin NAS, puerto ocupado, mounts y credenciales cerradas. Las pruebas Linux/root
usan únicamente TemporaryDirectory propias para NoFollow/UID65532/permisos y
colisiones; el tipo de montaje se inyecta, sin afirmar una prueba NFS nueva.
Windows omite esa parte explícitamente. El arranque y la aceptación HTTP/NFS
operativa corresponden al responsable después de respaldar los pins y DTO.

Primarias: [Docker Engine API](https://docs.docker.com/reference/api/engine/version/v1.54/),
[historial de API](https://docs.docker.com/reference/api/engine/version-history/)
y [perfil seccomp oficial](https://github.com/moby/profiles/blob/main/seccomp/default.json).
