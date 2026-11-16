# Constructor V2 del consumidor R2

Esta unidad prepara sólo `r2-sync-consumer`. Es una copia explícita y revisada
del controlador V1 `a5531346…` y helper `697aca93…`; no los importa, modifica ni
reemplaza. Conserva sus algoritmos de plan/hash, prueba de raíces, SSH fijado,
NoGit, publicación del contexto privado y plazo del build. No construye una
imagen por defecto ni crea contenedores de aplicación.

El responsable debe respaldar primero estas cuatro variables nuevas, con sus
comentarios españoles y plantilla correspondiente. El constructor sólo las lee;
nunca escribe la bóveda:

| Variable V2 | Contenido |
| --- | --- |
| `MEDIA_PLATFORM_BUILDER_V2_SOURCE_SHA256` | SHA256 de `docker/build_component_v2.py`. |
| `MEDIA_PLATFORM_BUILDER_V2_HELPER_SHA256` | SHA256 de `docker/component_build_remote_v2.py`. |
| `MEDIA_PLATFORM_COMPONENT_BUILDS_V2_B64` | Base64 del JSON cerrado descrito abajo. |
| `MEDIA_PLATFORM_BUILD_V2_APPROVED_PLAN_SHA256` | `plan_sha256` concreto revisado, antes de aplicar. |

Las variables V1 no autorizan V2. Se reutilizan únicamente la identidad SSH ya
respaldada de `.85:22`/`jandrade`, las raíces fijas `/opt/media-platform` y
`/var/lib/media-platform`, su propietario y `MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64`.
El parser de la bóveda sigue siendo el canónico de `scripts/ci/check_env.py`.
La clave cliente debe coincidir con su pública; el host usa la pública/huella
Ed25519 respaldadas y `RejectPolicy`, sin agente ni búsqueda de otras claves.

## Configuración y fuentes

El JSON V2 tiene exactamente `schema_version:2`, `layout_helper_sha256` y
`components`. Este último contiene exclusivamente `r2-sync-consumer` con:

```json
{
  "build_id": "fase4-r2-primero",
  "tag": "media-platform/r2-sync-consumer:fase4-r2-primero",
  "base": "python:3.12.16-alpine3.24@sha256:213242b537a1c166a0c53e903461fc18d5518e7196c93dbecda452660a848918",
  "context_root": "/opt/media-platform/build-v2/contexts",
  "logs_root": "/var/lib/media-platform/build-v2",
  "files": {
    "Dockerfile": "SHA256_RESPALDADO",
    "r2_client.py": "SHA256_RESPALDADO",
    "sync_http.py": "SHA256_RESPALDADO",
    "sync_ledger.py": "SHA256_RESPALDADO",
    "sync_protocol.py": "SHA256_RESPALDADO",
    "sync_runtime.py": "SHA256_RESPALDADO",
    "sync_storage.py": "SHA256_RESPALDADO"
  }
}
```

El fragmento representa la entrada del componente, no el JSON completo ni un
ejemplo ejecutable: se sustituyen todos los SHA por valores observados. El pin
del helper de layout debe ser el de `docker/host_layout.py` ya revisado. Ese
helper se valida, carga en memoria y contrasta hostname/IP/Linux/root reales y
dispositivo/inodo/SHA de marcador y recibo de ambas raíces antes de escribir.

El contexto contiene exactamente siete archivos regulares, sin enlaces ni
hardlinks, y suma como máximo 256 KiB. Se reconstruye desde la whitelist, sin
copiar recursivamente el workspace. README, `.env`, `.git`, cachés, secretos,
configuración y datos no se transfieren. El helper comprueba otra vez tamaño y
SHA de cada byte recibido. Un cambio posterior invalida el plan antes de SSH.

El plan y el recibo incluyen `source_manifest`: `schema_version:1`, una tabla
`files` de los seis `.py` y `root_sha256`, calculado sobre el JSON de esa tabla
ordenado por nombre, sin espacios. No incluye Dockerfile ni el propio campo
`root_sha256` en ese cálculo. El Dockerfile conserva su SHA independiente en
los siete archivos del contexto. No se genera un octavo archivo en la imagen
ni se incrusta un hash del propio script en él. La política externa del operador
debe fijar y verificar los seis SHA antes del arranque; el JSON cerrado del
servicio existente no recibe nuevos campos en esta unidad.

## Plan y aplicación posterior

Desde el workspace, con el entorno aislado que ya contiene Paramiko **5.1.0**:

```powershell
python docker/build_component_v2.py r2-sync-consumer
```

Sin `--apply` sólo valida archivos y configuración locales y emite JSON. No abre
SSH ni crea rutas. El hash del plan es SHA256 de su JSON canónico sin su propio
campo `plan_sha256`. El responsable guarda el plan privado y respalda ese campo
antes de solicitar la acción explícita:

```powershell
python docker/build_component_v2.py r2-sync-consumer --apply --plan RUTA_ABSOLUTA_PLAN_PRIVADO --expected-plan-sha256 HASH_RESPALDADO
```

`--apply` requiere coincidencia entre argumento, plan reconstruido y variable
V2 respaldada. Los pins de controlador/helper y las siete fuentes también se
revalidan antes de conectar. `--vault` señala sólo el archivo local del operador;
su contenido, ruta y credenciales no se envían al remoto. El payload público
viaja por stdin y está acotado a 1 MiB; la fuente del helper viaja fijada en la
orden, sin instalar un ejecutor persistente.

V2 usa propietario y marcador propios y áreas separadas:

```text
/opt/media-platform/build-v2/contexts/r2-sync-consumer/BUILD_ID/
/var/lib/media-platform/build-v2/r2-sync-consumer/BUILD_ID/
```

No adopta áreas V1 ni recursos ajenos por su nombre. Antes de crear o reutilizar
valida ancestros, modos, propietario, dispositivo/inodo, marcador, contexto y
recibo. Directorios root `0700`; fuentes, configuración vacía de Docker/Buildx,
logs e identificadores root `0600`. No borra ni cambia permisos recursivamente.
Un tag sin recibo propio, build parcial o contexto/log/ID alterado se rechaza;
un recibo íntegro con tag coincidente devuelve `changed:false`.

## Build e imagen candidata

La orden remota coloca las opciones NoGit **después** de `sudo -n env`; el hijo
Docker tiene entorno mínimo y configuración privada vacía. Conserva
`BUILDX_GIT_INFO/LABELS/CHECK_DIRTY=false`, attestations/provenance deshabilitadas
y `DOCKER_BUILDKIT=1`. Exige Docker Engine **29.9.3**, Buildx **0.38.1** y driver
`docker` del builder `default`, sin `--bootstrap`. El build usa sólo contexto
local, `--platform=linux/amd64 --network=none --provenance=false --load`;
no Bake, contextos remotos, SDK, instalación, pip/npm ni comandos Git.
La descarga de la base por el daemon sigue siendo una operación de red distinta
de `--network=none` de las instrucciones RUN.

La receta está cerrada: único FROM exacto, COPY de los seis nombres explícitos,
permisos/propietario, WORKDIR, USER y entrada JSON. Se rechazan RUN, ADD, COPY
remoto, frontend/directivas externas o instrucciones nuevas aunque alguien
recalcule el hash del payload. Usa la misma base oficial Python Alpine fijada
del proxy ML; la auditoría de esa base no sustituye la SCA de la imagen final.

`COPY --chown=0:0 --chmod=0555 … /app/` aparece **antes** de `WORKDIR /app`:
la intención es crear el destino con permisos de sólo lectura y dejar
exactamente seis `.py`, todos root y sin escritura. No depende de `--exclude` ni
de un frontend remoto. La lectura estática de
[BuildKit](https://github.com/moby/buildkit/blob/v0.28.0/solver/llbsolver/file/backend.go)
y [fsutil](https://github.com/tonistiigi/fsutil/blob/a2aa163d723f/copy/copy.go)
explica el modo aplicado al destino nuevo; el `MkdirAll` posterior no modifica
un directorio existente. El primer build deberá acreditar `/app` y sus modos
mediante inspección real de la imagen. Véase también la
[referencia oficial de COPY](https://docs.docker.com/reference/dockerfile/#copy).

La imagen declara `USER 65532:65532` y entrada
`python -I -B -S /app/sync_http.py serve`; `CMD []` elimina argumentos heredados.
Sin un `--config` explícito del operador, el proceso falla antes de servir.
No se incorporan configuración, secretos, estado ni datos. Sus futuros binds,
propietario UID65532, presupuesto de contenedor, rootfs de sólo lectura y red
limitada siguen pendientes de otro incremento. Esta unidad no abre puertos.

El límite del build sigue siendo 600 s/4 MiB de log, consultas de metadatos de
20 s y controlador de 750 s. El cierre conserva el grupo del hijo propio con el
líder sin recolectar hasta terminar las señales, incluido stdout heredado por
un descendiente. No consulta ni termina procesos ajenos. Los errores públicos
son fijos; el log privado no se vuelca a stdout.

El recibo devuelve ID de imagen, tag, base, hashes de fuente/log/plan y rutas
propias. El responsable debe respaldar ID/recibo antes de exportar o arrancar.
Mantiene `containers_created:false` y `final_sca_verified:false`: faltan build,
SCA final y aceptación de la imagen Linux real; no son aprobaciones implícitas.

## Validación de esta unidad

```powershell
python -B -m unittest discover -s tests -p test_component_build_v2.py -v
python -m ruff check docker/build_component_v2.py docker/component_build_remote_v2.py tests/test_component_build_v2.py
python -m ruff format --check docker/build_component_v2.py docker/component_build_remote_v2.py tests/test_component_build_v2.py
```

Las pruebas usan contextos temporales y transporte/ejecución controlados. Cubren
autoridad V2, alteraciones, límites, enlaces, rutas, metadatos de fuentes,
aprobación anterior a SSH, tags/recibos y cierre por timeout. No abren SSH ni
Docker reales. Las 32 pruebas nativas del servicio ya aceptadas siguen
perteneciendo a sus bytes congelados; este constructor no los modifica.
