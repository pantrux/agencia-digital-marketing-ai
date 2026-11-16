# Inventario operativo de la Fase 4

Ejecutar en `vm-media-01` con Python 3, acceso al socket Docker local y
permiso de lectura de systemd. El comando consulta los contenedores por
etiqueta y emite un recibo JSON con el CID, el digest de imagen, el estado,
el healthcheck Docker cuando existe y el código de salida. Exige los
roles esperados, detecta duplicados y revisa seis unidades activas y
habilitadas. La inspección selecciona campos y etiquetas de forma explícita.

```bash
python3 -I -B docker/verify_phase4_runtime.py
python3 -I -B docker/verify_phase4_runtime.py --pins IMAGENES.json
python3 -I -B docker/verify_phase4_runtime.py --pins - < IMAGENES.json
```

| Proyecto observado | Roles |
| --- | --- |
| `agency-media` | `postgres-apps`, `valkey`, `zou-api`, `zou-events` |
| `agency-media-ui-refresh-v1` | `kitsu-ui` |
| `agency-n8n-private` | `n8n`, `javascript`, `python` |
| `agency-media-immich-lan` | `postgres-immich`, `immich-valkey`, `immich-ml-auth`, `immich-server` |
| `agency-supoclip-trial-nov13` | `postgres`, `valkey`, `migrate`, `backend`, `worker`, `model`, `frontend`, `proxy` |
| `agency-moneyprinter-lan` | `api`, `ui` |
| `agency-observability-nas-v1` | `otel-collector`, `node-exporter`, `cadvisor` |
| Docker Engine, propietario R2 | `serve` y, si se conserva, `init` |

El migrador de SupoClip y el inicializador R2 deben haber salido con
código 0. El resto debe estar en ejecución. El PostgreSQL compartido por
n8n y Kitsu aparece una sola vez. Los supervisores comprobados son
`agency-immich-lan.service`, `agency-kitsu-zou.service`, `agency-moneyprinter-lan.service`,
`agency-n8n-private.service`, `agency-observability-nas.service` y
`agency-supoclip-private.service`.

El manifiesto opcional usa `schema: 1` y un mapa `images` con claves
`proyecto/rol`, `r2/serve` o `r2/init`. Fijar cada digest desde su recibo
acreditado, en el formato exacto `sha256:` que Docker devuelve en
`.Image`. Un manifiesto parcial fija sólo sus roles. El resultado
`images_pinned=0` indica que se registraron imágenes sin cotejarlas
contra una fuente independiente. Un pin para `r2/init` requiere que
ese contenedor siga presente.

## Comprobaciones relacionadas

[`docker-compose.yml`](docker-compose.yml) y su [verificador de CI](../scripts/ci/check_compose_base.py)
validan la base de cuatro roles PostgreSQL/Valkey/Zou de Kitsu. Los otros
proyectos conservan sus Compose y supervisores propios. R2 usa Docker
Engine con etiquetas propias. El inventario usa `docker ps`,
`docker inspect` y `systemctl show` sin leer el entorno de Compose.

Para comprobar redes, montajes, firewall y salud de aplicación, usar
las sondas operativas de [n8n](n8n/README-private-deploy.md),
[Immich](immich-server/README-LAN.md), [SupoClip](supoclip/README-BOOT.md),
[MoneyPrinter](moneyprinter/README-WOLFI-BUILD.md), [Kitsu](kitsu-ui/README.md),
[observabilidad](observability/README-nas.md) y [R2](README-r2-service.md).
Registrar por separado autenticación, inferencia, métricas, SCA y
restauración 3-2-1.

La comprobación en la VM acreditó 18 roles con estado Docker `healthy` y siete en ejecución con sondas de aplicación. Para SupoClip, ejecuta `/usr/bin/python3 -I -S -B /opt/media-platform/supoclip-boot-v1/health_readonly.py`. Comprueba PostgreSQL, Redis, el latido ARQ y el proxy con las identidades vigentes. Para R2, `GET /healthz` en `.85:3010` confirmó el ledger y `findmnt` en el namespace del consumidor comprobó el origen NFSv4.1 de `/media`. Prometheus recibió muestras recientes de `agency-node` y `agency-cgroups` por mTLS, con métricas de memoria y CPU presentes. El migrador de SupoClip y el inicializador R2 habían terminado con código 0. Recibos `54b124f8…` y `49df2ad2…`.

Zou API y eventos montan `/mnt/producciones/_kitsu/previews` sobre `/var/lib/zou/previews`. El estado y los temporales de Zou están en su volumen local. `agency-kitsu-zou.service` requiere `mnt-producciones.mount`. Su comprobación previa coteja la exportación NFSv4.1, los inodos, permisos y marcador, las dependencias saludables y los binds de los dos contenedores existentes. Para verificar la preparación, ejecuta como root `/usr/bin/python3 -I -S -B /opt/media-platform/kitsu-base-v1/preview_mount_guard.py`. La activación comprobó escritura y lectura con UID `1000` dentro de los dos contenedores. Kitsu respondió HTTP 200 desde Windows. Recibos `99dcc487…` y `c8498fe0…`.

Para revisar capacidad local en `vm-media-01`, ejecuta `/usr/bin/python3 -I -B /opt/media-platform/phase4-verifier-v2/verify_phase4_capacity.py`. El informe coteja `/`, el directorio Docker y el staging diario con sus montajes efectivos y mide cada dispositivo una vez. Registra bytes e inodos disponibles. Comprueba el montaje NFS desde el inventario local del kernel. La primera lectura confirmó un solo dispositivo ext4 para los tres directorios, con 2,32 GB y 3.367.034 inodos disponibles. Recibo `fa66755b…`.

## Plan reproducible de configuración

El [manifiesto](phase4-runtime.json) registra las siete identidades Compose,
los archivos locales y sus copias instaladas, el orden de overlays por rol,
el entorno acotado y los supervisores. Docker Engine conserva el consumidor
R2 como servicio con etiquetas propias. Los [recibos de etiquetas](../.tools/evidence/phase4-runtime-compose-paths-20261113-v1.json)
y de [huellas instaladas](../.tools/evidence/phase4-runtime-compose-sha-20261113-v4.json)
permiten cotejar ese mapa.

```bash
python3 -I -B docker/verify_phase4_compose.py plan
```

El plan local coteja los archivos fuente y los recibos y devuelve los ocho
comandos `config --quiet`, agrupados por proyecto e historial de rol. No
requiere Docker local. Mantener `docker/docker-compose.yml` como base de
Kitsu conserva su contrato de CI. El plan de implementación pide ese archivo
como punto de partida para el conjunto de la Fase 4. El manifiesto agrega las
invocaciones independientes porque los contenedores vigentes usan distintas
etiquetas de proyecto. [Proyectos de Compose](https://docs.docker.com/compose/intro/compose-application-model/)

En `vm-media-01`, instala juntos `verify_phase4_compose.py`,
`phase4-runtime.json` y `verify_phase4_runtime.py`. Desde una sesión root,
con un JSON privado `root:root/0600`, ejecuta la comprobación de configuración.

```bash
python3 -I -B /ruta/verify_phase4_compose.py check --variables /ruta/variables.json
```

La versión instalada en `/opt/media-platform/phase4-verifier-v4` cotejó las ocho configuraciones tras el cambio de PostgreSQL de Immich. Su JSON de interpolación queda bajo el mismo directorio privado. Recibo `8a74a7ca…`.

El JSON tiene exactamente dos objetos, `agency-observability-nas-v1` y
`agency-supoclip-trial-nov13`. Cada objeto contiene sólo las variables de su
campo `variables` en el manifiesto. Usa las referencias privadas respaldadas
de `prepared.json` de observabilidad y del plan/recibo de SupoClip. Sus valores
son nombres de imagen, IP, subred, ruta, red o rol. Los otros cinco proyectos
usan el `compose.env` privado registrado en la VM. El de MoneyPrinter es
`root:root/0400` y los otros cuatro son `root:root/0600`. La comprobación coteja los
bytes y modos de los Compose instalados y devuelve únicamente el resultado por
grupo. Para inspección de contenedores y unidades usa también
`verify_phase4_runtime.py`.

SupoClip conserva dos historiales instalados. Los seis roles base usan
`compose.functional.yml` y `compose.integration.override.yml`. `model` y
`worker` añaden `compose.local-model.yml` y
`compose.transcript-timestamps.yml`, en ese orden. La copia instalada del
primer archivo carece de la sobrescritura local de la imagen de `migrate`
y hereda la imagen de `x-node`. El manifiesto fija ambas huellas.
`compose.boot.yml` se reserva para una sustitución futura. El supervisor
actual adoptó los CID existentes.
