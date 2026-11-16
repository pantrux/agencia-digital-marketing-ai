# Respaldo aislado de OpenClaw

`openclaw_backup.py` empaqueta un staging privado acreditado y restaura el
archivo resultante en un directorio nuevo. Prepara el staging con las rutas y
la propiedad verificadas de los recursos. El contrato siguiente
define nombres de intercambio, independientes del layout de OpenClaw.

## Staging y procedencia

Preparar un directorio privado nuevo bajo una raíz Linux propia. La raíz, el
staging y sus subdirectorios requieren modo `0700` y el UID/GID efectivos del
operador. Los archivos deben ser ordinarios, tener un solo enlace y modo
`0600`. La herramienta abre los directorios por descriptor, sin seguir
enlaces. Los nombres `STAGING`, `CAPTURA_NUEVA` y `RESTAURACION_NUEVA`
son nombres de un solo directorio directamente bajo esa raíz.

El staging contiene `manifest.json` y un `config.json` por cada ID exacto
`agency-account`, `agency-strategist`, `agency-director`, `agency-editor`
y `agency-critic`. Cada proyección contiene `id`, `model` en formato
`proveedor/modelo` y, opcionalmente, `name`. Puede incluir
`ID/MEMORY.md` y archivos Markdown de `ID/memory/` en esos mismos cinco
directorios. Revisar el contenido al preparar la proyección, incluido el
texto de las memorias, y excluir credenciales, sesiones, cachés y datos de
otros equipos. Usar sólo la proyección autorizada de cada agente.

El manifiesto declara `schema` igual a `1`, `owner` igual a
`agencia-digital-marketing-ai:openclaw-backup:v1`, `scope` igual a
`agency-state` o `synthetic` y `files` con el inventario exacto. Cada fila
tiene `path` relativo, `size` en bytes y `sha256` en minúsculas. Ordenar las
filas por `path` y serializar con
`json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)`
seguido de LF, en UTF-8. El operador acredita la procedencia del staging y
respalda el SHA256 de ese manifiesto por separado antes de pasarlo al
productor. El campo de ámbito sólo acredita la etiqueta validada del staging.

El límite es de 256 archivos de datos, 4 MiB por archivo, 32 MiB de carga
total y 64 KiB de manifiesto. Las rutas usan componentes ASCII portables
de hasta 100 caracteres por ruta. El TAR fija orden, metadatos y relleno.
Con la misma versión de Zstandard y los mismos bytes de entrada, la salida
es reproducible. El codec requiere Linux con `memfd_create` y los binarios
del sistema `/usr/bin/zstd` y `/usr/bin/prlimit`.

### Preparar la proyección de los agentes

Ejecutar `prepare_openclaw_staging.py` con el usuario propietario de los
archivos cuando estén configurados los cinco agentes. Valida un modelo
primario explícito por agente según el plan de implementación, para la
lista anterior `agents.list` o las entradas actuales `agents.entries`.
La proyección copia únicamente `id` y `model`. Verificar la ruta de
`openclaw.json` con el operador antes de ejecutar la herramienta. El
preparador requiere JSON estricto. Si OpenClaw usa JSON5, referencias
`$include` o sustitución, preparar una instantánea JSON redactada del
roster efectivo en un directorio privado separado y conservar el
nombre `openclaw.json`. La CLI actual permite exportar sólo el objeto
de agentes con `openclaw config get agents --json`. Encapsularlo bajo
`agents` en la instantánea. [Configuración de OpenClaw](https://docs.openclaw.ai/gateway/configuration) ·
[Entradas de agentes](https://docs.openclaw.ai/gateway/config-agents/entries-and-multi-agent) ·
[CLI de configuración](https://docs.openclaw.ai/cli/config).

Preparar un inventario JSON privado con `schema` igual a `1` y un objeto
`workspaces`. En cada agente con memorias a copiar, indicar `root` igual
a su workspace configurado explícitamente y `files` con rutas relativas
y SHA256 de los bytes revisados. El ejemplo usa una ruta ficticia.

```json
{
  "schema": 1,
  "workspaces": {
    "agency-editor": {
      "root": "/ruta-de-ejemplo/workspace-editor",
      "files": {"MEMORY.md": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
    }
  }
}
```

Revisar el texto de cada Markdown para excluir credenciales y datos de
otros equipos antes de calcular su SHA256. Con `workspaces` vacío se
proyectan sólo las cinco configuraciones. El preparador abre sólo los
archivos autorizados, por descriptor y sin seguir enlaces. Comprueba
propiedad, rutas, tamaños y huellas. El modo de planificación emite el
SHA256 del manifiesto y el conteo de archivos. Para publicar, repetir
con el SHA256 del plan revisado y un nombre de staging nuevo bajo una
raíz privada, fuera de la configuración y los workspaces activos.

```bash
python3 -I -B sre/prepare_openclaw_staging.py agency-state CONFIG_JSON INVENTARIO_JSON
python3 -I -B sre/prepare_openclaw_staging.py agency-state CONFIG_JSON INVENTARIO_JSON \
  --publish RAIZ STAGING SHA_MANIFEST
```

Las tres rutas se indican en forma absoluta. Usar el mismo manifiesto
para la captura de la sección siguiente. Este procedimiento se probó
con fixtures y con los cinco agentes registrados para el respaldo de Fase 4.
La primera copia real incluyó sus configuraciones sin archivos de memoria.

## Captura y verificación aislada

```bash
python3 -I -B sre/openclaw_backup.py capture RAIZ STAGING CAPTURA_NUEVA SHA_MANIFEST agency-state
python3 -I -B sre/openclaw_backup.py restore RAIZ CAPTURA_NUEVA RESTAURACION_NUEVA SHA_ARCHIVO agency-state
```

`RAIZ` es una ruta absoluta. Cada comando devuelve un recibo JSON de éxito
con el ámbito, tamaño y hashes. El primer comando escribe
`CAPTURA_NUEVA/openclaw.tar.zst` para `agency-state`. El segundo requiere el SHA256 del archivo
por canal independiente y valida todo el TAR antes de crear la raíz nueva.
Ante un fallo de escritura se conserva el destino parcial privado y se
devuelve error. Para una ejecución posterior se requiere otro nombre nuevo.
La restauración recupera archivos para inspección sin registrar agentes,
arrancar el gateway ni escribir en su configuración activa.

Para una corrida 3-2-1, el operador incorpora la captura `agency-state` al
staging raíz exigido por `sre/sre-backup-321.sh` y añade su SHA256 a
`SHA256SUMS`. El productor prepara el archivo local en permisos privados.
El cifrado de NAS y R2 corresponde a restic en la etapa 3-2-1.

## Reconstrucción de la configuración desde R2

1. Lee `SHA256SUMS` y `openclaw.tar.zst` del snapshot R2 identificado por el recibo de la corrida. Coteja el manifiesto y la huella del TAR con el diario de la corrida y con el recibo privado `capture.json`.
2. Restaura el TAR en un directorio privado nuevo con el comando anterior. Usa las cinco proyecciones `ID/config.json` como entradas de `agents.entries` e indica `agents.ownership` igual a `explicit` para esta instancia nueva. Genera el parche con `openclaw-agency-setup/patch_config.py`, el runtime MCP instalado y un directorio temporal de workspaces. Aplica el parche a la configuración temporal.
3. Fija `OPENCLAW_STATE_DIR` y `OPENCLAW_CONFIG_PATH` a la instancia temporal y ejecuta `openclaw config file`, `openclaw config validate --json` y `openclaw config get agents.entries --json`. Comprueba los cinco IDs, modelos, listas de skills y permisos MCP. [CLI de configuración](https://docs.openclaw.ai/cli/config).
4. Inicia el gateway reconstruido bajo una unidad temporal con `PrivateNetwork=yes`, `PrivateTmp=yes` y una credencial de ensayo respaldada en la bóveda. Usa `openclaw gateway run --bind loopback --port 19371` con la configuración temporal y pasa el token por `OPENCLAW_GATEWAY_TOKEN`. Completa las sondas HTTP `/healthz` y `/readyz` dentro de la unidad. Consulta `agents.list`, coteja las políticas individuales con `config.get` y verifica los registros del plugin con `tools.catalog`, pasando el `agentId` de un agente recuperado. Detén la unidad y coteja el PID y la huella de configuración del gateway activo. [Inicio del gateway](https://docs.openclaw.ai/cli/gateway/running) · [Métodos RPC](https://docs.openclaw.ai/gateway/protocol/operator-methods).

El ensayo con el snapshot cifrado del 14 de noviembre tardó 20,938 s desde el inicio de la recuperación hasta el cotejo. Reconstruyó las cinco entradas con los modelos esperados y nueve permisos de herramientas de cuatro servidores MCP. La configuración temporal exigió declarar explícitamente la propiedad de los agentes. Se retiró el árbol temporal y el gateway activo conservó su PID y su configuración. El alcance medido fue la reconstrucción y validación con el runtime existente. [Evidencia](../.tools/evidence/phase4-openclaw-r2-config-reconstruction-20261115-v1.json).

El inventario de los cinco workspaces encontró cero archivos `MEMORY.md`, cero notas Markdown de agente y diez informes generados bajo `memory/dreaming/`. El TAR cotejado contiene cinco configuraciones. [Informes de Dreaming](https://docs.openclaw.ai/concepts/dreaming).

El arranque aislado tardó 50,297 s de extremo a extremo desde R2. El proceso dio HTTP 200 en `/readyz` a los 16,708 s de su arranque. Los RPC devolvieron los cinco agentes, sus permisos por rol y las nueve herramientas registradas del plugin. El catálogo registra la oferta general del plugin y `config.get` permite cotejar las listas configuradas de cada rol. La unidad salió con código 0, conservó el gateway activo y retiró el estado temporal. La medición cubre el arranque y el acceso RPC local con el runtime existente. [Ensayo de arranque](../.tools/evidence/phase4-openclaw-r2-gateway-recovery-20261115-v2.json).

La copia automática de las 03:30 del 15 de noviembre recuperó las mismas cinco configuraciones y la misma huella del TAR. El ensayo aislado tardó 39,875 s desde R2 hasta completar el cotejo RPC y cerrar la unidad. Verificó los cinco modelos, sus listas de skills, las políticas de los cinco roles y nueve herramientas del catálogo. El endpoint de herramientas sin token devolvió 401. La unidad cerró con código 0 y se retiró el estado temporal. [Copia automática y arranque](../.tools/evidence/phase4-scheduled-openclaw-restore-20261115-v1.json).

## Copia independiente de las 04:00 de Santiago

`agency-backup-openclaw-clock.timer` activa la copia en `vm-media-01`. La unidad usa el transporte SSH fijado de la copia integral y el empaquetador de los cinco agentes. El archivo `openclaw.tar.zst` y su `SHA256SUMS` se guardan con Restic en `/volume2/producciones/_backups/openclaw` y en `studio-backups-bucket`, bajo `openclaw-restic-v1`. El inventario automático se limita a las memorias descritas en el contrato del transporte. [Transporte y selección de memorias](README-openclaw-transport.md)

El respaldo usa la etiqueta `agency-openclaw-clock` y la corrida `openclaw-clock-AAAA-MM-DD`. Para comprobar una corrida, coteja el timer y el diario privado de `/var/lib/media-platform/backup-openclaw-clock-v1/journal`. En `vm-media-01`, ejecuta el verificador con `--solo-recibos` y registra en la bóveda los dos IDs que devuelve. Después ejecuta la lectura completa. El verificador comprueba el repositorio y la etiqueta de cada snapshot, lee los manifiestos y el TAR y valida en memoria los archivos de los cinco agentes y sus modelos. Si una copia anterior quedó pendiente de conciliación, revisa los snapshots que ya tengan la etiqueta de esa corrida antes de volver a usar los destinos. El ensayo manual se identifica con `openclaw-clock-acceptance-2026-11-15`.

```bash
systemctl list-timers agency-backup-openclaw-clock.timer
journalctl -u agency-backup-openclaw-clock.service --since today
sudo /usr/bin/python3 -I -B /opt/media-platform/backup-321-runtime-v3b/backup_321_verify_openclaw_restore.py \
  --run-id openclaw-clock-AAAA-MM-DD --solo-recibos
sudo /usr/bin/python3 -I -B /opt/media-platform/backup-321-runtime-v3b/backup_321_verify_openclaw_restore.py \
  --run-id openclaw-clock-AAAA-MM-DD
```

Para verificar la reconexión de medios, conserva la escucha en loopback y configura el egreso de la unidad temporal a `192.168.0.6/32` y `192.168.0.85/32` además de loopback. Desde el gateway recuperado, invoca las herramientas del proyecto sintético con las asignaciones restauradas. Valeria busca la muestra B-roll en Immich, Sofía transcribe el WAV con Whisper, Elena consulta la revisión existente en Kitsu y Lucas genera un PNG con Remotion. Coteja el PNG y el recibo firmado en `05_revision` antes de retirar sólo la salida generada para el ensayo. Prueba la solicitud sin token y una herramienta fuera del rol. [Invocación HTTP de herramientas](https://docs.openclaw.ai/es/gateway/tools-invoke-http-api).

El ensayo de reconexión tardó 63,5 s desde R2 hasta completar las cuatro llamadas y apagar la réplica. Los controles devolvieron 401 sin credencial y 404 para B-roll desde el rol de Elena. El PNG de 1080 × 1920 coincidió por NFS y SMB. Se retiraron las dos capturas de ensayo y ambos originales conservaron sus huellas. [Ensayo de medios](../.tools/evidence/phase4-openclaw-r2-gateway-media-recovery-20261115-v5.json) · [Cotejo y retiro](../.tools/evidence/phase4-openclaw-r2-gateway-media-cleanup-20261115-v1.json).

### Recuperar el perfil OAuth de Sofía

En una instalación nueva, autoriza el mismo titular del perfil de OpenClaw registrado en la bóveda. Ejecuta el inicio de sesión con el gateway de destino aún detenido y con `OPENCLAW_STATE_DIR` y `OPENCLAW_CONFIG_PATH` apuntando al estado recuperado. El perfil queda en el almacén de OpenClaw. [OAuth de OpenAI en OpenClaw](https://docs.openclaw.ai/providers/openai/authentication).

```bash
openclaw models auth login --agent agency-account --provider openai \
  --method device-code --profile-id "$PERFIL_OAUTH_SOFIA"
```

Arranca el gateway de destino y ejecuta la [comprobación firmada del socket de Sofía](../openclaw-agency-setup/sofia-broker/openclaw-owner-plugin/README.md#contrato-local-del-socket). Exige HTTP 200 y texto `ready`. Después verifica un turno sintético con la atribución `openai/gpt-6.1-terra` y el harness `codex` antes de conectar el broker recuperado.

En el ensayo, el plugin reconoció el perfil declarado pero ausente del almacén recuperado. La comprobación firmada devolvió `503 SOFIA_OWNER_PREFLIGHT_OAUTH_TYPE` y el socket rechazó el turno siguiente. La misma comprobación en el gateway activo devolvió `200 ready` con su perfil vigente. El inicio de sesión interactivo y la inferencia desde una instalación nueva siguen pendientes. [Ensayo de recuperación](../.tools/evidence/phase4-openclaw-r2-oauth-recovery-20261115-v6.json) · [Control de producción](../.tools/evidence/phase4-openclaw-r2-oauth-production-control-20261115-v1.json).

## Sonda sintética

```bash
python3 -B -m unittest discover -s tests -p test_openclaw_backup.py -v
```

La sonda usa una raíz temporal, una proyección ficticia por agente y dos
memorias de prueba. El caso Linux ejerce la CLI y el Zstandard del sistema,
comprueba reproducibilidad, rechazo del SHA incorrecto y de un destino ya
existente, y conserva un archivo de otro equipo. En Windows, los casos de
archivos por descriptor se omiten y la prueba portable de Zstandard usa
Node.js con soporte de ese codec si está disponible. La sonda escribe
`openclaw.synthetic.tar.zst` y la restauración comprueba el ámbito incluido
en el manifiesto. El ensayo sólo acredita el flujo con la fixture.
