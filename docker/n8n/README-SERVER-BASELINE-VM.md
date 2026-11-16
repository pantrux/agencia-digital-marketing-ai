# Transporte y contenedor del ensayo baseline n8n

`n8n_server_baseline_vm.py plan` verifica localmente las fuentes fijadas y produce el plan público. El plan contiene el comando de creación, el SDK musl ya usado por el runner y los SHA de la cápsula, el lock y los operadores. Respaldar el `plan_sha256` antes de cualquier etapa. El CLI usa el conector SSH fijado de la aceptación PostgreSQL, con Paramiko 5.1.0 y pin Ed25519 exacto. El CLI carece de lector o escritor de bóveda.

```powershell
python -I -B docker/n8n/n8n_server_baseline_vm.py plan
```

Cada acción recibe un objeto JSON por stdin desde el operador de raíz. Usar un pipe privado desde el proceso que mantiene las claves en memoria. Los campos comunes son `parent` con `device` e `inode` de `/opt/media-platform`, `daemon` con `ID`, `ServerVersion` y `DockerRootDir`, y `ssh` con estas seis claves del conector:

```text
MEDIA_VM_IP
MEDIA_VM_SSH_USER
MEDIA_VM_SSH_PUBLIC_KEY
MEDIA_VM_SSH_HOST_PUBLIC_KEY
MEDIA_VM_SSH_HOST_FINGERPRINT
MEDIA_VM_SSH_PRIVATE_KEY_B64
```

El cliente conserva el DTO SSH local. El host sólo recibe el plan y las fuentes verificadas, las identidades públicas y la cápsula cuando corresponde. Las respuestas se guardan en un archivo nuevo bajo un directorio privado que ya exista. Para las etapas operativas usar el Python con Paramiko fijado, el SHA del plan respaldado y un `--receipt` nuevo por invocación.

| Acción | Campos adicionales por stdin | Resultado y transición |
|---|---|---|
| `receive` | Ninguno | Tras el preflight de host, daemon, V5 y espacio libre, envía por SSH los 439.342.873 bytes en bloques. El receptor publica `received` con el recibo y la identidad de `/opt/media-platform/n8n-server-linux-transfer-v2/inputs`. Respaldar ese objeto antes de crear. |
| `create` | `received`, el objeto devuelto en la etapa anterior | Comprueba 5 GiB y 350.000 inodos libres. Crea `agency-n8n-server-baseline-v1` con `--pull=never`. El CID queda en `baseline-control-v1/cid`. Respaldar el resultado completo de `create` y el CID antes de arrancar. |
| `start` | `received` y `created`, el resultado completo de `create` respaldado | Compara `created` con el recibo físico, revalida los binds y los límites y exige estado `created` con PID 0. Reserva `start-intent.json` antes de iniciar. |
| `status` | `received` y `created` | Inspecciona exclusivamente el CID respaldado. Devuelve estado, PID, fechas, código y OOM. Sólo devuelve `baseline_verified=true` tras una salida 0 y un recibo de seed ligado a la raíz propia, con lock intacto y cero descargas offline. |

Por ejemplo, pasar el DTO privado al siguiente comando mediante stdin:

```powershell
python -I -B docker/n8n/n8n_server_baseline_vm.py receive --plan-sha256 <sha-respaldado> --receipt <ruta-nueva>
```

El wrapper no inicia contenedores durante `receive` o `create`. El CID usa red `none`, UID/GID 1000, raíz de sólo lectura, dos CPUs, 5 GiB de RAM sin swap adicional y 128 PID. V5, el delta y las fuentes se montan de sólo lectura. La etapa tiene 2.100 segundos más el margen de cierre de BusyBox. El contrato de `tmpfs` usa el campo `HostConfig.Tmpfs` del motor. [Montajes tmpfs de Docker](https://docs.docker.com/engine/storage/tmpfs/).

Si falla el transporte después de crear, consultar el `cid` y los recibos bajo `baseline-control-v1` por lectura acreditada del host antes de cualquier intervención. La raíz EXCL y el intento anterior se conservan. El wrapper deja pendiente cualquier arranque cuando falta un `create.json` positivo. Para espacio y alcance del ensayo ver [recepción y seed](README-SERVER-BASELINE-SEED.md).
