# Archivo local de candidatas confirmadas V2

`archive_vault_changes_v2.py` libera cuota de la cola Windows moviendo generaciones
completas a `C:\GPU-Bridge\vault-changes-v1\quota-archive-v2` en el mismo volumen.
El movimiento conserva los bytes y los permisos privados, y consume el mismo
espacio físico en C:. El operador utiliza el runtime sellado y los recibos
locales de las subidas. No necesita credenciales ni acceso a R2.

## Archivo preventivo con el observador activo

`Agency-Vault-Queue-Capacity` ejecuta el mantenimiento cada minuto con la misma cuenta del observador. La tarea usa `pythonw.exe` para iniciar sin ventana el intérprete cuya huella comprueba el archivador. El archivador comprueba el 90 % del límite de bytes y del número máximo de generaciones. Si se alcanzó uno de los dos umbrales, toma los bloqueos del escritor y del cargador, coteja el acuse de una captura y la mueve mediante un plan de una entrada. Cada invocación procesa hasta cuatro entradas y suelta ambos bloqueos entre movimientos. Si encuentra un bloqueo ocupado, libera el que haya tomado y reintenta cada 200 ms durante los primeros 20 s del lote.

La última captura sigue disponible para deduplicar y las pendientes conservan su ubicación. El registro `operations/quota-live-plan-v2.json` identifica la operación pendiente de confirmar si un movimiento se interrumpe. La siguiente ejecución verifica el mismo plan y completa el recibo. El archivo se conserva en el mismo volumen y libera cuota de la cola activa.

Al seleccionar una captura para mantenimiento preventivo, una prueba de subida incompleta deja esa captura y sus comprobantes en la cola. La tarea continúa buscando otra captura con prueba completa. Una diferencia de huella, permisos o estructura sigue deteniendo la operación. La aplicación y reanudación de un plan ya fijado exigen verificar toda su prueba.

Para instalar la tarea, usa el SHA-256 del archivador documentado en `BACKUP_VAULT_QUOTA_LIVE_ARCHIVER_SHA256`.

```powershell
& 'sre/Register-VaultQuotaMaintenance.ps1' -ArchiverSha256 $archiverSha256
```

El Programador fija esa huella en su acción y comprueba los bytes del archivador antes de ejecutarlo. Una operación con los bloqueos ocupados se vuelve a evaluar en el siguiente disparo.

Publica `archive_vault_changes_v2.py` junto a `pause_vault_quota_archive_v2.py` cuando cambien los bytes del archivador. La utilidad de pausa y reanudación verifica la misma huella antes de actuar.

## Archivo manual

Pausa `Agency-Vault-Changes-V1` con el SID de la tarea. Obtén primero el PID de
la acción `watch` que coincide con el plan sellado y coordina la pausa de los
escritores V2 manuales. El archivador comprueba procesos conocidos y ambos
locks antes de empezar.

```powershell
$python = 'C:\Users\Jorge Andrade\AppData\Local\Programs\Python\Python312\python.exe'
& $python -I -S -B sre/pause_vault_quota_archive_v2.py pause --pid $watcherPid
& $python -I -S -B sre/archive_vault_changes_v2.py plan --limit 16
& $python -I -S -B sre/archive_vault_changes_v2.py apply --operation '<operation>' --plan-sha256 '<plan_sha256>'
& $python -I -S -B sre/pause_vault_quota_archive_v2.py resume --operation '<operation>' --plan-sha256 '<plan_sha256>'
```

El plan escribe un manifiesto privado y muestra sólo su identificador, SHA-256,
el número de generaciones y sus bytes. Selecciona hasta 32 capturas `OBSERVED`
con ACK automático, cotejado con el intento, el snapshot y los cuatro jobs.
Preserva las pendientes, la última captura usada para deduplicar y las
generaciones que requieren otra prueba de reconocimiento. Los logs de subida
se conservan en `operations`.

Si la ejecución se interrumpe, vuelve a usar el mismo `operation` y
`plan_sha256`. El operador comprueba ambas ubicaciones y completa el recibo de
cualquier movimiento ya verificado. Si informa un conflicto, conserva ambas
ubicaciones y el manifiesto para revisarlos. Reanuda la tarea cuando `apply`
termine con `success=true`. Un segundo `apply` sobre el mismo plan informa
`newly_moved=0` tras verificar el archivo.

La pausa espera hasta 450 segundos por ambos locks para que termine la subida
en curso. `WATCHER_LOCK_BUSY` indica que el plazo de adquisición venció.
Si la pausa devuelve `TASK_STOP_RESULT_UNVERIFIED`, comprueba que la tarea
esté `Ready`, que el PID anterior haya salido y que ambos locks se hayan
liberado antes de archivar. Si la reanudación devuelve
`TASK_STARTED_CAPTURE_UNVERIFIED`, comprueba el estado y la captura antes de
repetir un arranque. Una solicitud que supera el plazo puede haber surtido efecto.
