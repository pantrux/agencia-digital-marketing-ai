# Build controlado de componentes propios

Unidad de preparación de Fase 4. Sólo admite `immich-ml-auth` y `postgres-apps`; no ejecuta contenedores de aplicación ni declara aprobada la seguridad de una imagen construida. Los 21 tests de la unidad utilizan fuentes temporales, clientes SSH y ejecutores simulados. Después, el responsable verificó el cierre POSIX real de un hijo con stdout heredado y construyó ambos componentes en `.85`, con pins y planes previamente respaldados. Los IDs se respaldaron antes de exportar o ejecutar las imágenes; sus auditorías finales y aceptaciones se registran en los README de cada componente. Esto no acredita el despliegue del conjunto de aplicaciones.

## Configuración respaldada antes de aplicar

El controlador `build_component.py` lee `.env` local con el parser canónico de `scripts/ci/check_env.py`, sin eval ni sustituciones. No transfiere la bóveda ni usa credenciales ajenas. Exige los valores existentes `MEDIA_VM_IP=192.168.0.85`, `MEDIA_VM_SSH_USER=jandrade`, las claves Ed25519 cliente/host y su huella, las dos raíces `MEDIA_PLATFORM_INSTALL_ROOT`/`MEDIA_PLATFORM_DATA_ROOT`, `MEDIA_PLATFORM_OWNER_MARKER` y `MEDIA_PLATFORM_LAYOUT_IDENTITIES_B64`.

La identidad de layout debe ser el JSON de resultado real de `host_layout.py`, codificado como Base64: propietario, `success=true` y las dos identidades con ruta/rol, dispositivo/inodo, SHA256 de marcador y recibo. El helper carga en memoria la fuente fijada de `host_layout.py`, comprueba Linux/root/hostname/IP reales y contrasta ambas raíces antes de cualquier escritura. No modifica esa fuente ni los marcadores originales.

El responsable debe añadir, con los tres comentarios obligatorios y plantilla `CHANGE_ME`:

- `MEDIA_PLATFORM_COMPONENT_BUILDS_B64`: Base64 del JSON descrito abajo, con pins reales revisados y `build_id`/tag elegidos y respaldados antes de transferir o usar el contexto.
- `MEDIA_PLATFORM_BUILD_APPROVED_PLAN_SHA256`: hash del plan concreto revisado. `plan` no lo requiere ni lo incorpora al hash; `--apply` exige que coincida con el argumento y el plan reconstruido.

Esquema del JSON anterior; los textos `SHA256_...` son espacios para los valores revisados, no un ejemplo ejecutable:

```json
{
  "schema_version": 1,
  "helper_sha256": "SHA256_DE_COMPONENT_BUILD_REMOTE_PY",
  "layout_helper_sha256": "SHA256_DE_HOST_LAYOUT_PY",
  "components": {
    "immich-ml-auth": {
      "build_id": "fase4-proxy-primero",
      "tag": "media-platform/immich-ml-auth:fase4-proxy-primero",
      "base": "python:3.12.16-alpine3.24@sha256:SHA256_INDICE_REVISADO",
      "files": {"Dockerfile": "SHA256_DOCKERFILE", "proxy.py": "SHA256_PROXY"},
      "context_root": "/opt/media-platform/build/contexts",
      "logs_root": "/var/lib/media-platform/build"
    },
    "postgres-apps": {
      "build_id": "fase4-postgres-primero",
      "tag": "media-platform/postgres-apps:fase4-postgres-primero",
      "base": "docker.io/library/postgres:16.15-alpine3.24@sha256:SHA256_AMD64_REVISADO",
      "files": {"Dockerfile": "SHA256_DOCKERFILE_POSTGRES"},
      "context_root": "/opt/media-platform/build/contexts",
      "logs_root": "/var/lib/media-platform/build"
    }
  }
}
```

Se puede respaldar sólo un componente a la vez. La whitelist es exacta: proxy `Dockerfile` y `proxy.py`; PostgreSQL sólo `Dockerfile`. El contexto se reconstruye desde esos archivos regulares sin enlaces ni hardlinks; no se envía el directorio del workspace. README, caches, `.git`, la bóveda y otros archivos quedan fuera de la transferencia. No usar una operación de copia recursiva para reemplazar este contrato. El tope total es 256 KiB.

La base debe coincidir exactamente con el único FROM del Dockerfile y tener digest SHA256 del repositorio oficial Python/Postgres correspondiente. No se admiten ADD, frontend remoto ni contextos adicionales. La seguridad del contenido del Dockerfile procede de su revisión y hash respaldado; las opciones NoGit no hacen seguro un Dockerfile arbitrario ni evitan un comando Git añadido en futuras instrucciones. Cada cambio de fuente/base/helper requiere pins, plan y revisión nuevos.

## Plan y aplicación del controlador

Usar el entorno aislado del proyecto que contiene **Paramiko 5.1.0** fijado. No instalar ni actualizar el Python global de Windows para esta operación. Desde la raíz del workspace:

```powershell
python docker/build_component.py immich-ml-auth
python docker/build_component.py postgres-apps
```

Sin `--apply` sólo hay lectura local, comprobación de configuración y SHA256 y salida JSON. No conecta SSH ni crea rutas. El plan incluye destinos derivados, base/tag/build_id, hashes/tamaños de los archivos y helpers, identidad pública del host y las dos raíces. El campo `plan_sha256` es SHA256 del JSON canónico sin ese propio campo; no es el hash del archivo formateado. Guardar el JSON en un archivo privado propio y respaldar ese campo en la bóveda antes de continuar. No hay autorreferencia al escribir `MEDIA_PLATFORM_BUILD_APPROVED_PLAN_SHA256`.

Ejemplo de aplicación explícita con ruta y hash que el responsable ya haya respaldado:

```powershell
python docker/build_component.py immich-ml-auth --apply --plan RUTA_ABSOLUTA_PLAN_PRIVADO --expected-plan-sha256 HASH_RESPALDADO
```

`--vault` sólo permite indicar el archivo local de bóveda al controlador; nunca aparece en la orden remota. La fuente se vuelve a leer y comparar con el plan antes de conectar. SSH está fijado a `.85:22`, usuario `jandrade`, clave Ed25519 respaldada y `RejectPolicy`; se comprueba pública/privada y huella del host. No se carga el agente, no se buscan claves alternativas ni se usa autenticación por contraseña.

El controlador envía únicamente fuente pública fijada y un payload JSON acotado por stdin de la sesión autenticada. No instala el helper remoto. El código del helper viaja en la orden como Base64, sin credenciales; el payload contiene sólo fuentes, hashes e identidades del plan. La orden empieza por `sudo -n env` y después fija INFO/LABELS/CHECK_DIRTY en falso y procedencia deshabilitada, antes de ejecutar Python. La configuración y las claves de SSH permanecen locales.

## Operación remota acotada

La inspección del helper sin `--apply` valida identidad, payload y raíces sin crear rutas ni ejecutar Docker. La aplicación crea sólo áreas propias debajo de las dos raíces acreditadas. Los directorios intermedios reciben marcador JSON privado ligado a ruta/dispositivo/inodo. El contexto final contiene exclusivamente la whitelist; su identidad se enlaza desde el marcador del registro privado:

```text
/opt/media-platform/build/contexts/COMPONENTE/BUILD_ID/
/var/lib/media-platform/build/COMPONENTE/BUILD_ID/
```

La segunda ruta conserva marcador, `build.log`, `image.iid`, `receipt.json` y configuración privada vacía de Docker/Buildx. Todos los directorios son root `0700`; fuentes, logs y recibos, root `0600`. No hay borrado ni chmod/chown recursivo ni adopción de directorios existentes sin marcador exacto. No se utilizan credenciales, credential helpers ni configuración Docker global del operador: el entorno del hijo es mínimo y sus carpetas de configuración son propias. Los comandos de metadatos también heredan las opciones NoGit.

Se comprueba Docker Engine **29.9.3**, Buildx **0.38.1** y driver `docker` del builder `default`, usando su salida oficial de inspect sin `--bootstrap`. El build directo conserva `--builder=default --platform=linux/amd64 --network=none --provenance=false --load`, tag, iidfile y contexto locales fijados. No usa Bake, checkout, URLs Git ni builder `docker-container`. `--network=none` afecta a RUN, no a la descarga de la base por el daemon.

Un tag presente sin recibo propio se rechaza. Un recibo exitoso se reutiliza sólo si contexto/log/ID siguen íntegros y el tag apunta al mismo ID; devuelve `changed=false`. Un build fallido o incompleto no se reintenta automáticamente ni se sobrescribe: preservar sus archivos para diagnóstico, elegir y respaldar otro `build_id`/tag y generar un plan nuevo. La preparación de carpetas/configuración puede existir aunque una comprobación posterior de SDK/tag impida el build.

El build tiene presupuesto de 600 s y log máximo 4 MiB. El helper cierra únicamente el grupo del proceso hijo creado para su build, incluido el caso de un líder salido y un hijo con stdout heredado. Conserva al líder sin recolectar mediante `waitid(WNOWAIT)` hasta terminar las señales al grupo; no usa `poll` antes de ese cierre ni enumera procesos ajenos. Las consultas Docker tienen 20 s por llamada y salidas limitadas. El controlador admite hasta 750 s para metadatos/build/cierre y no imprime stderr ni el log crudo. Si se pierde SSH, la ejecución remota conserva su propio plazo; el responsable debe recuperar el recibo privado antes de usar una imagen cuyo resultado local no llegó.

La salida exitosa incluye ID de imagen `sha256:...`, tag, base, plan, hashes de fuente/log y ruta de recibo. El responsable respalda inmediatamente ID/recibo/hash antes de utilizar la imagen. El builder devuelve siempre `containers_created=false` y `final_sca_verified=false`: aún faltan SCA real de la imagen propia y sus verificaciones funcionales. No sustituirlos por la auditoría previa de la base.

## Pruebas locales y aceptación pendiente

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -p test_media_component_build.py -v
python -m ruff check docker/build_component.py docker/component_build_remote.py tests/test_media_component_build.py
python -m ruff format --check docker/build_component.py docker/component_build_remote.py tests/test_media_component_build.py
```

Los tests comprueban planes/pins/whitelist, rechazo de rutas/componentes/hardlinks y payload alterado, hash de aprobación antes de SSH, transporte simulado con pin y sin agente, contrato del SDK, NoGit después de sudo, tag ajeno, fallo e idempotencia del build simulado. No acreditan permisos/fsync POSIX, funcionamiento del plugin en `.85`, build real ni SCA final. El responsable revisa y ejecuta esas comprobaciones en la fase correspondiente. El manifiesto sigue `phase:3`, CD deshabilitado y publicación/workflow GitHub pendientes.

Fuentes: [identidad de raíces](README-host-layout.md), [especificación de Fase 4](../docs/specs/FASE-4-APLICACIONES-Y-BACKUPS.md), [variables oficiales Buildx](https://docs.docker.com/build/building/variables/), [inspect oficial sin bootstrap](https://docs.docker.com/reference/cli/docker/buildx/inspect/) y [build por contexto local](https://docs.docker.com/reference/cli/docker/buildx/build/). La revisión exacta NoGit de Buildx 0.38.1 se conserva en `.tools/evidence/phase4-build-without-git.md`.
