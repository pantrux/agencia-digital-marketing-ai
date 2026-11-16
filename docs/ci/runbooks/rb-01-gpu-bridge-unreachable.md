# Procedimiento rb-01: puente GPU inaccesible

**Responsable:** equipo de plataforma / SRE · **Severidad:** alta si se detiene la producción audiovisual.  
**Última revisión:** 2026-11-16 · **Frecuencia:** ante alerta · **Última recuperación de PCI passthrough:** 2026-11-16

## Propósito y síntomas

Recuperar la comunicación desde `.85` y `.7` hacia los cuatro servicios de `192.168.0.6`: aprendizaje de Immich `:3003`, transcripción `:8000`, render `:8090` y métricas `:9835`. Una conexión TCP abierta o una respuesta `401` confirma acceso al servidor, pero no confirma CUDA, NVENC ni autenticación correcta.

## Prerrequisitos

- Acceso LAN a `.85`, `.7` y a Windows `.6`, sesión Windows del SID propietario y cola de trabajos detenida antes de una parada.
- Token `GPU_BRIDGE_BEARER_TOKEN` vigente en la bóveda y en el entorno protegido del verificador; no incluirlo en capturas o argumentos de consola.
- Instalación propia `C:\GPU-Bridge`, configuración privada `runtime.json` y recibo canónico `supervisor-start.json` respaldados antes de uso. El recibo de arranque no prueba salud.
- Revisar la [evidencia de cierre de Fase 3](../evidence/2026-11-05-fase-3.md) y el [procedimiento del puente](../../../gpu-bridge-win11/README.md). El control declarativo admite Fase 3; CD sigue deshabilitado y no sustituye las comprobaciones operativas.

## Diagnóstico rápido

1. Desde `.85`, comprobar alcance TCP sin enviar credenciales:

   ```bash
   python3 - <<'PY'
   import socket
   for puerto in (3003, 8000, 8090, 9835):
       try:
           with socket.create_connection(("192.168.0.6", puerto), timeout=3):
               print(puerto, "TCP accesible")
       except OSError:
           print(puerto, "TCP inaccesible")
   PY
   ```

   **Resultado esperado:** los cuatro puertos accesibles. Repetir desde `.7`; si solo falla un origen, revisar su ruta y la regla de Windows Firewall para ese origen.

2. En PowerShell sobre `.6`, comprobar procesos y tarjeta sin modificar servicios:

   ```powershell
   Get-NetTCPConnection -State Listen | Where-Object LocalPort -In 3003,8000,8090,9835 | Select-Object LocalAddress,LocalPort,OwningProcess
   pwsh -NoProfile -File 'C:\GPU-Bridge\Stop-GpuBridge.ps1'
   pwsh -NoProfile -File 'C:\GPU-Bridge\Configure-GpuFirewall.ps1'
   nvidia-smi
   ```

   **Resultado esperado:** cuatro listeners IPv4, `IdentityVerified=true` en Stop Plan, auditoría de reglas propias y RTX 3060 Ti observada. Ninguno de estos comandos detiene procesos ni cambia reglas. Los PIDs mostrados sirven para diagnóstico; no reutilizarlos para terminar procesos. Si falta CUDA o hay memoria agotada, conservar el diagnóstico antes de intervenir.

3. En `.6`, consultar salud local cargando el secreto solo en memoria, sin proxy:

   ```powershell
   @'
   import json, urllib.error, urllib.request
   from pathlib import Path
   config = json.loads(Path(r"C:\GPU-Bridge\runtime.json").read_text(encoding="utf-8-sig"))
   opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
   for port in (3003, 8000, 8090, 9835):
       request = urllib.request.Request(f"http://127.0.0.1:{port}/health",
           headers={"Authorization": "Bearer " + config["GPU_BRIDGE_BEARER_TOKEN"]})
       try:
           with opener.open(request, timeout=5) as response:
               body = json.load(response)
               print(json.dumps({"port": port, "http_status": response.status,
                   **{key: body.get(key) for key in ("service", "status", "ready",
                       "cuda_available", "nvenc_available")}}))
       except urllib.error.HTTPError as error:
           print(json.dumps({"port": port, "http_status": error.code}))
       except OSError as error:
           print(json.dumps({"port": port, "error_type": type(error).__name__}))
   '@ | & 'C:\GPU-Bridge\metrics\venv\Scripts\python.exe' -I -
   ```

   **Resultado esperado:** HTTP 200, servicio correcto y `ready=true` en los cuatro puertos; CUDA/NVENC según el backend. Un 503 en métricas puede reflejar un hijo degradado aunque NVIDIA responda. Esta consulta local no acredita acceso LAN ni el archivo final de render.

## Mitigación

1. Mantener las órdenes en cola; no enviar renders nuevos a un puente que falla salud.
2. Si falla red, auditar el cortafuegos propio y sus almacenes persistente/efectivo. El recibo del wrapper elevado debe acreditar `Success=true`, `Verified=true`, `ReceiptWritten=true`; un PID de elevación no basta. No repetir Apply mientras exista una aplicación coordinada en curso, ampliar la lista de clientes ni desactivar el firewall. El [README](../../../gpu-bridge-win11/README.md) detalla las cinco reglas y su procedimiento elevado.
3. Con la RTX disponible en Windows, el supervisor reinicia sus hijos con Jobs propios y espera creciente. Si se necesita reiniciar el puente, esperar o cancelar de forma coordinada los trabajos en curso y revisar primero Stop Plan:

   ```powershell
   pwsh -NoProfile -File 'C:\GPU-Bridge\Stop-GpuBridge.ps1' -Stop -WhatIf
   pwsh -NoProfile -File 'C:\GPU-Bridge\Stop-GpuBridge.ps1' -Stop
   pwsh -NoProfile -File 'C:\GPU-Bridge\start-gpu-bridge.ps1' -Start
   ```

   **Resultado esperado:** Stop verifica identidad y espera únicamente sobre el handle retenido; el inicio devuelve un nuevo recibo del Python real. `Readiness=pending` obliga a repetir salud. La parada cancela trabajos activos y cierra sus hijos propios; no afecta procesos de otros proyectos.

   **Si falla:** conservar recibo y salida saneada. No usar `taskkill`, terminar por PID/nombre, editar el recibo para forzar aceptación ni reiniciar Windows/GPU para ocultar la causa. Un error de fecha exige revisar la versión: la corrección conserva DateTime/DateTimeOffset en UTC y admite solo diez ticks de diferencia con el handle.

## Recuperación de la RTX por PCI passthrough

1. En PowerShell de Windows `.6`, comprobar si Plug and Play detecta la RTX y si NVIDIA puede consultar la tarjeta asignada. El comando verifica la huella del ejecutable y muestra el estado, el modelo y la versión del controlador.

   ```powershell
   $rtx = Get-PnpDevice -Class Display -PresentOnly |
       Where-Object { $_.InstanceId -like 'PCI\VEN_10DE&DEV_2486*' }
   $rtx | Select-Object FriendlyName,Status,ConfigManagerErrorCode
   $gpu = Get-Content -LiteralPath 'C:\GPU-Bridge\runtime.json' -Raw | ConvertFrom-Json
   $huella = (Get-FileHash -LiteralPath $gpu.GPU_SMI_PATH -Algorithm SHA256).Hash
   if ($huella -ne $gpu.GPU_SMI_SHA256) { throw 'nvidia-smi difiere del runtime autorizado' }
   & $gpu.GPU_SMI_PATH "--id=$($gpu.GPU_UUID)" '--query-gpu=name,driver_version' '--format=csv,noheader'
   if ($LASTEXITCODE -ne 0) { throw "nvidia-smi devolvió $LASTEXITCODE" }
   ```

   Con código 0 (`CM_PROB_NONE`) y `nvidia-smi` correcto, seguir la mitigación del supervisor. Si aparece Código 43 o falta el dispositivo, capturar el diagnóstico antes de cambiar el controlador. Comparar la hora del último arranque con el log System de `nvlddmkm` y el registro de instalación en `C:\Windows\INF\setupapi.dev*.log`. Si cambió la huella, cotejar la firma de catálogo, el paquete y su versión con el responsable de plataforma.

2. En el ESXi `.3`, comprobar desde Host Client que vídeo `0000:03:00.0` y audio `0000:03:00.1` figuran activos para passthrough y asignados a Windows 11. La consola de ESXi permite cotejar los reinicios de ambas funciones:

   ```sh
   esxcli hardware pci pcipassthru list | grep -E '03:00\.[01]'
   grep -E 'PCIPassthru.*(03:00\.[01])' /var/log/vmkernel.log | tail -n 20
   ```

   `Reset device … done` registra el fin del reinicio PCI. La recuperación se comprueba volviendo a leer la RTX en Windows. Si las funciones siguen asignadas y Windows aún informa Código 43 o la tarjeta sigue ausente, guardar el trabajo abierto, confirmar acceso a la consola VMware y hacer un apagado ordenado de Windows con encendido desde vCenter. Registrar el resultado de Windows y la nueva asignación PCI. [Reinicio de funciones PCI en DirectPath I/O](https://knowledge.broadcom.com/external/article/312208/)

3. Si el ciclo de la VM acaba con la RTX ausente o en Código 43, escalar al responsable de ESXi. Acordar la interrupción del host y confirmar acceso a la consola física. Revisar las órdenes abiertas en Studio y coordinar cómo reanudar los trabajos que dependan de los montajes NAS. Registrar la configuración de arranque vigente. Apagar y confirmar en este orden `vm-media-01`, `Ubuntu-OpenClaw`, `vCenter`, `Windows 11` y `DSM` desde el Host Client de `.3`. Reiniciar ESXi desde el Host Client o su consola. Tras arrancar, verificar datastore y red, y recuperar `DSM → Ubuntu-OpenClaw → vCenter → vm-media-01 → Windows 11`. Si el NAS o NFS siguen indisponibles, esperar su recuperación antes de iniciar los consumidores. Restaurar la configuración de inicio si se ajustó para la intervención. [Caso de NVIDIA sin respuesta en ESXi](https://knowledge.broadcom.com/external/article/418517/passthrough-nvidia-gpu-becomes-unrespons.html)

4. Repetir el diagnóstico de Windows. Con la RTX en código 0 y las huellas del runtime verificadas, ejecutar la comprobación de CUDA y una codificación NVENC de ocho fotogramas:

   ```powershell
   $gpu = Get-Content -LiteralPath 'C:\GPU-Bridge\runtime.json' -Raw | ConvertFrom-Json
   & $gpu.GPU_WHISPER_PYTHON_PATH -I -c 'import ctranslate2; n = ctranslate2.get_cuda_device_count(); print(n); raise SystemExit(int(n != 1))'
   if ($LASTEXITCODE -ne 0) { throw 'Falla la prueba de CUDA de Whisper' }
   $huella = (Get-FileHash -LiteralPath $gpu.GPU_FFMPEG_PATH -Algorithm SHA256).Hash
   if ($huella -ne $gpu.GPU_FFMPEG_SHA256) { throw 'FFmpeg difiere del runtime autorizado' }
   & $gpu.GPU_FFMPEG_PATH -hide_banner -loglevel error -f lavfi -i 'color=c=black:s=640x360:r=30' -frames:v 8 -c:v h264_nvenc -f null NUL
   if ($LASTEXITCODE -ne 0) { throw 'Falla la prueba de NVENC' }
   ```

   Iniciar sesión con la cuenta propietaria del puente. Comprobar que `Agency-GpuBridge-AtLogon` reconstruyó `P:` con cifrado e inició el supervisor. Si Immich se adelantó al puente, comprobar el siguiente intento de su unidad en `vm-media-01` y la salud de sus contenedores. Completar la verificación de servicios y el smoke general antes de declarar recuperación.

## Verificación

Repetir la consulta autenticada de los cuatro `/health` desde `.85` y `.7`, con el token proporcionado únicamente por su entorno privado. Registrar las ocho respuestas, host de origen y hora. Exigir HTTP 200, `status=ok`, `ready=true` y capacidad del backend. Comprobar también rechazo de Bearer ausente/incorrecto y de un cliente no permitido; no atribuir un rechazo de socket a una prueba HTTP autenticada.

Después repetir una transcripción y un render sintéticos controlados, sus perfiles CUDA, artefactos NAS, hash, marca Free, dimensiones/color, sonoridad `-14 ±1 LUFS` y pico `<=-1 dBTP`. Si se evalúan 4K, ProRes alfa o concurrencia, exigir sus evidencias independientes; ProRes usa `prores_ks` en CPU. La salud NVENC y una captura no acreditan el render final.

El smoke general de diez servicios preparado en Fase 0 también depende de Edge, aplicaciones y agentes de fases posteriores. Su resultado global no sustituye estas comprobaciones ni permite cerrar Fase 3 mientras falten sus propias pruebas.

## Reversión

Si la falla comenzó con una actualización propia, detener admisión y coordinar Stop verificado, restauración del bundle y configuración cuyos hashes ya estén respaldados y arranque con recibo nuevo. El instalador exige que la fuente coincida con el bundle autorizado; no admite sustituir archivos a mano ni omitir esa comprobación. No se restaura la bóveda completa ni se borra NAS para limpiar una caché. Para cambios que involucren otros planos, consultar el alcance y los adaptadores de la [reversión coordinada](../recipes/sre-rollback.md).

## Resolución de problemas

| Síntoma | Causa probable | Acción |
|---|---|---|
| Cuatro puertos caídos | Host Windows, supervisor, red o firewall | Separar salud local de conectividad LAN y revisar el recibo actual. |
| RTX en Código 43 y falla `nvidia-smi` | El controlador no logra inicializar la tarjeta | Cotejar Plug and Play y PCI passthrough. Seguir el procedimiento de recuperación de la RTX. |
| `401` | Bearer ausente, incorrecto o desincronizado | Comparar versiones de la credencial sin imprimir su valor; sincronizar desde la bóveda mediante el responsable. |
| `403` | IP real del socket fuera de lista | Revisar el origen real; una cabecera X-Forwarded-For no habilita acceso. `127.0.0.2` se rechaza aunque el firewall preserve loopback. |
| Transcriptor sano y render caído | Servicio de render o NVENC | Revisar registro del render y sesiones NVENC; respetar concurrencia máxima 2. |
| Respuesta HTTP sana y trabajo fallido | Salud superficial o VRAM | Ejecutar trabajo controlado y revisar telemetría; no declarar recuperación solo por HTTP. |
| Lucas recibió HTTP 200 y falló al verificar NAS | NFS aún no ve la escritura por SMB | Conservar los recibos privados de capturas y render. El siguiente disparo reutiliza el recibo que corresponda al original, audio, brief y solicitud, y vuelve a comprobar tamaño y SHA-256 por NFS. |
| Se pierde la respuesta HTTP de capturas o render | Interrupción tras la publicación en el NAS | Repetir la misma solicitud con los mismos insumos. El puente devuelve el recibo persistido si su firma y las huellas de todos los archivos coinciden. |
| El puente devuelve 409 para una etapa | Trabajo aún en curso, solicitud distinta, recibo ausente o archivo que difiere del recibo | Revisar el estado antes de intervenir. Conservar las salidas existentes. Si falta un recibo válido, preparar otra revisión cuando no se pueda acreditar su procedencia. Los trabajos anteriores al cambio pueden carecer del recibo. |
| `/render` 502 `VIDEO_FUERA_DE_CONTRATO` | El archivo codificado no cumple una validación final | Conservar diagnóstico de ffprobe/audio y corregir la causa; no relajar el contrato para publicar. |
| 422 `AUDIO_FUERA_DE_OBJETIVO` con voz de 1 s | Sonoridad final fuera de `-14 ±1 LUFS` aunque el pico cumpla | Conservar medición y revisar preparación de audio; no atribuirlo a 4K ni inventar una duración mínima por plan. |
| Caché D: inaccesible o sin espacio | ACL/propiedad o presupuesto de temporales | Revisar exclusivamente la raíz privada y trabajos activos; no mover resultados a una caché ni borrar NAS. |
| Stop Plan rechaza PID, SID o fecha | Recibo obsoleto, contexto distinto o fuente anterior | Revisar identidad y bundle con el responsable; nunca pasar un PID desnudo como alternativa. |

## Registros, escalado e historial

La alerta `AgenciaWhisperLatenciaFueraObjetivo` usa el p95 estimado del histograma de Whisper de los últimos 30 días. Se activa si el endpoint está disponible, hay observaciones y el p95 permanece en 8 s o más por minuto de audio durante cinco minutos. La serie incluye ensayos. Revisar el recuento junto con el p95, los reinicios del proceso y la salud del puente antes de intervenir. [Cálculo de cuantiles de histogramas](https://prometheus.io/docs/practices/histograms/)

`AgenciaStudioAudioFueraObjetivo` se activa cuando un informe reciente con muestras del portal de producción registra menos del 99,5 % de másteres NVENC dentro del objetivo de sonoridad y pico. Para el cociente sólo se usan recibos firmados de esa cohorte. `AgenciaStudioMedicionAudioDesactualizada` indica que falta el informe o que su último registro tiene más de doce minutos. Revisar `agency-studio-render-audio-metrics.timer` y el journal de su servicio en `vm-media-01`. Si el informe está al día y el recuento es cero, el tablero debe mostrar «Sin muestras».

Consultar recibos saneados y métricas `:9835`; el panel de observabilidad previsto está en `.8`. No adjuntar `runtime.json`, bóveda, argv ni volcados de entorno. Escalar al responsable de plataforma si no vuelve salud en una intervención, falla una guarda de propiedad o `nvidia-smi` falla. Registrar hora de Chile, puerto, origen, versión/hash y resultado en el registro de incidente; el responsable mantiene el Estado.

| Fecha | Ejecutor | Resultado |
|---|---|---|
| 2026-11-05 | Preparación de Fase 0 | Procedimiento documentado; ejecución del puente pendiente. |
| 2026-11-05 | Responsable de implementación | Certificado funcional 23:59:28 UTC−3: Plan propio, inferencia CUDA, Whisper final, firewall y ocho consultas LAN aprobados; Bearer ausente/incorrecto y socket no permitido rechazados. Color y orden del audio corregidos; MP4/ProRes 1080p/4K de 1 s/3 s, alfa muestreado, QA y concurrencia final 200/200/429 aprobados. Integridad de 17 fuentes nativas/20 renderer y 14 hashes NAS revalidada. Fase 3 certificada. |
| 2026-11-16 | Responsable de implementación | La RTX conservó Código 43 tras el ciclo de Windows. Se recuperó tras reiniciar ESXi con el mismo controlador 621.01. Se verificaron CUDA, NVENC, los cinco invitados y las diez sondas. [Incidente y evidencias](../../../ESTADO.md#arranque-de-windows-y-recuperación-del-puente-gpu-del-15-de-noviembre) |
