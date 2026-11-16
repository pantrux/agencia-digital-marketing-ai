# Arranque de SupoClip privado

`agency-supoclip-private.service` gestiona los ocho CID acreditados del
proyecto `agency-supoclip-trial-nov13`. Lee el recibo del ensayo de
reinicio de noviembre de 2026 y comprueba las identidades de imágenes,
contenedores y red Speaches. El migrador queda detenido con su código
0 y su `FinishedAt` original. PostgreSQL y Valkey arrancan primero.
Las consultas validan las 27 migraciones Prisma y las 26 de backend.
El modelo se une al namespace del worker. La API comprueba la cola y
confirma que no puede conectar con el puerto del modelo. El proxy
arranca al final y responde desde la IP local de la VM.

## Instalación en vm-media-01

Ejecutar como root con Bash. Se requiere Docker local con firewall de
iptables, `iptables-save`, `iptables-restore`, `curl`, Python 3 y systemd.
Conservar los directorios `/opt/media-platform/supoclip-trial-20261113-v1`
y `/opt/media-platform/supoclip-model-20261113-v1` con los montajes del
ensayo. El archivo de pesos sigue montado en sólo lectura. Mantener la
ventana libre de cambios concurrentes de Docker o del firewall.

Copiar a la VM `supoclip_boot.py`, `agency-supoclip-private.service` y
`.tools/evidence/phase4-supoclip-timestamps-restart-20261113-v1.json`.
Ejecutar los comandos desde el directorio de transferencia.

```bash
set -euo pipefail
boot_dir=/opt/media-platform/supoclip-boot-v1
receipt=phase4-supoclip-timestamps-restart-20261113-v1.json
printf '%s  %s\n' 21ec8c04123a85df99978ea9f1f74b9538608d1f21a2842ff009641eb1cfe22b \
  "$receipt" | sha256sum -c -
install -d -o root -g root -m 0700 "$boot_dir"
install -o root -g root -m 0444 supoclip_boot.py "$boot_dir/supoclip_boot.py"
install -o root -g root -m 0444 "$receipt" "$boot_dir/accepted.json"
boot=(/usr/bin/python3 -I -S -B "$boot_dir/supoclip_boot.py")
"${boot[@]}" preflight
systemd-analyze verify "$PWD/agency-supoclip-private.service"
```

`preflight` comprueba la identidad de los ocho CID, sus políticas y el
orden de las reglas. `firewall=false` indica que la transacción de
restauración o migración pasó `iptables-restore --test` y queda por
aplicar. En `FORWARD`, el salto `DOCKER-USER` debe ser el primero o
seguir a las dos guardas del bridge exclusivo de observabilidad. En
`INPUT`, la guarda de observabilidad, cuando existe, queda primera.
SupoClip coloca sus reglas tras esas guardas y las de MoneyPrinter
para los bridges `br-mpt-lan`, `br-mpt-fn` o el puerto 8501, si existen.
Los prefijos inesperados y las reglas con la etiqueta SupoClip ajenas
al plan bloquean el arranque. Las unidades de observabilidad y
MoneyPrinter tienen validadores históricos de posición absoluta que
pueden rechazar un reinicio simultáneo. `preflight` verifica el orden
efectivo y conserva las reglas de ambos. Si alguno rechaza su propio
orden tras reiniciar el host, comprobar su journal por separado.

El filtro permite al worker iniciar TCP hacia `192.168.0.6:8000`.
El proxy conserva `192.168.0.85:3080`, con conexiones nuevas externas
bloqueadas en `INPUT` o `DOCKER-USER` según el recorrido del paquete.
La red Speaches es IPv4 y el modelo sigue en el loopback del worker.

```bash
"${boot[@]}" adopt
install -o root -g root -m 0644 agency-supoclip-private.service \
  /etc/systemd/system/agency-supoclip-private.service
systemctl daemon-reload
systemctl enable --now agency-supoclip-private.service
"${boot[@]}" inspect
systemctl is-enabled agency-supoclip-private.service
systemctl is-active agency-supoclip-private.service
journalctl -u agency-supoclip-private.service --no-pager -n 25
```

`adopt` aplica la protección y fija `restart=no` en los CID existentes
de worker y modelo. SupoClip queda bajo un solo supervisor y conserva
sus CID. La unidad registra estado JSON en el journal. `inspect` debe
mostrar `success=true`, `firewall=true`, `local_http=200`, siete roles
en `running` y el migrador en `exited`. Usar `compose.boot.yml` al final
de los cuatro Compose si se prepara un reemplazo futuro y actualizar
el recibo acreditado antes de gestionar los CID nuevos con la unidad.

## Comprobar arranque y parada

En una ventana con la cola vacía, ejecutar `systemctl stop` para la
unidad. El proxy se detiene primero y el worker conserva PostgreSQL,
Valkey y el modelo mientras drena. El journal registra `drained` y
`drain_timed_out`. Si el drenaje supera 120 segundos, el worker se
detiene con una gracia de 30 segundos mientras el modelo aún está
disponible. Una tarea larga puede quedar pendiente de recuperación.
Verificar con `inspect` que la protección sigue instalada y que todos
los roles quedaron detenidos. Ejecutar `systemctl start`, comparar
identidades y comprobar nuevamente HTTP 200. Desde un origen LAN ajeno,
comprobar que no abre una conexión TCP nueva al puerto 3080.

Tras un reinicio completo ya programado de la VM, repetir `inspect` y
consultar `journalctl -b -u agency-supoclip-private.service`. El recibo
original sigue sirviendo para la sonda de modelo con `--worker-image`
y `--boot-managed`. Revisar el orden tras recargas manuales de reglas
del host y usar la unidad para restaurarlo. El reinicio de prueba del
par documentado anteriormente no equivale a reiniciar el host.

## Reversión

Detener y deshabilitar la unidad. `remove-firewall` retira únicamente
reglas de SupoClip y exige todos los CID detenidos. Los contenedores,
redes, bases y pesos quedan disponibles. Para volver a la operación
manual, aplicar el filtro del ensayo antes de iniciar el par o reactivar
la unidad mediante el procedimiento de instalación.

```bash
systemctl disable --now agency-supoclip-private.service
"${boot[@]}" remove-firewall
```
