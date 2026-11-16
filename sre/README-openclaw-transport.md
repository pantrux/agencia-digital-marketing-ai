# Transporte diario de OpenClaw para la copia 3-2-1

Instala `backup_321_openclaw_remote.py`, `openclaw_backup.py` y `prepare_openclaw_staging.py` bajo `/home/jandrade/.openclaw-agency-backup/capture-321-v1` en `openclaw-ia`. La raíz es `0700` y los tres archivos deben pertenecer a `jandrade`. Las fuentes de captura se cotejan con las huellas incorporadas en el helper. El helper lee el JSON estricto de OpenClaw y proyecta únicamente las cinco configuraciones de la agencia. La selección automática incluye `MEMORY.md` y las notas de fecha `memory/AAAA-MM-DD.md` o `memory/AAAA-MM-DD-descripción.md` en los workspaces privados `0700` configurados para esos agentes. Para fijar una selección explícita, coloca un `memory-inventory.json` privado `0600` con rutas y hashes revisados. [Contrato de proyección y restauración](README-openclaw-backup.md) · [Memoria de OpenClaw](https://docs.openclaw.ai/concepts/memory)

Autoriza sólo la clave dedicada desde la IP de `vm-media-01`. Usa esta forma de `authorized_keys`, con la clave pública real al final de la línea.

```text
restrict,from="192.168.0.85",command="/usr/bin/python3 -I -B /home/jandrade/.openclaw-agency-backup/capture-321-v1/backup_321_openclaw_remote.py" ssh-ed25519 CLAVE_PUBLICA
```

En `vm-media-01`, prepara `/var/lib/media-platform/backup-321-openclaw-ingress-v1` con dueño root y modo `0700`. Instala `backup_321_openclaw_transport.py` junto a las dos fuentes de captura en una ruta administrada por root. El perfil JSON, la clave privada Ed25519 y `known_hosts` pertenecen a root y tienen modo `0600`, dentro del mismo directorio root `0700`. La única entrada de `known_hosts` identifica literalmente a `192.168.0.7` con la clave host verificada. El perfil fija los nombres y SHA-256 de ambos archivos.

```json
{
  "identity_file": "/var/lib/media-platform/backup-321-daily-config-v1/openclaw.identity",
  "identity_sha256": "SHA256_LLAVE",
  "known_hosts_file": "/var/lib/media-platform/backup-321-daily-config-v1/openclaw.known_hosts",
  "known_hosts_sha256": "SHA256_KNOWN_HOSTS"
}
```

El runner ejecuta el helper como root con `python3 -I -S -B RUTA/backup_321_openclaw_transport.py --profile RUTA_ABSOLUTA --profile-sha256 SHA256_PERFIL` y le entrega `{"run_id":"ID-NUEVO"}` por stdin. OpenSSH usa la clave dedicada hacia `jandrade@192.168.0.7:22` con el comando forzado. El receptor valida la ventana UTC, la huella del archivo y el manifiesto, y descomprime y verifica cada miembro antes de publicar. La ruta final `<run_id>` contiene `capture.json` y `openclaw.tar.zst`, ambos `0600`. El JSON de salida es la referencia que consume `backup_321_staging.py`. Si el helper rechaza una entrada, devuelve un código de error y el runner conserva sus otros recibos. Cada nueva captura usa un ID nuevo.

La ventana admite hasta cinco segundos de desfase entre los dos relojes. El transporte conserva las marcas remotas y, si la de fin todavía está por delante del reloj local, espera hasta alcanzarla dentro de esa cota antes de publicar. El ensamblador recibe una captura que ya figura como finalizada en la VM.

```bash
python3 -B -m unittest discover -s tests -p test_backup_321_openclaw_transport.py -v
```
