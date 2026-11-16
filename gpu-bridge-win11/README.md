# Puente GPU nativo de Windows

Este directorio prepara los cuatro demonios sin estado de `W11-HOMELAB`, `192.168.0.6`, con RTX 3060 Ti de 8 GiB. Las aplicaciones y bases de datos se alojan en `.85`; el medio persistente está en Synology y se accede desde Windows por `P:\`, correspondiente a `/volume2/producciones`. La [especificación de Fase 3](../docs/specs/FASE-3-PUENTE-GPU.md) define los contratos y las pruebas de aceptación.

| Puerto TCP | Servicio | Prueba funcional requerida |
|---|---|---|
| 3003 | Immich machine-learning CUDA | Embeddings y procesamiento facial en CUDA, con protocolo original. |
| 8000 | Speaches/faster-whisper CUDA | Transcripción con palabras e intervalos temporales. |
| 8090 | Remotion + FFmpeg NVENC | Vídeo sintético, marca Free y audio final verificado. |
| 9835 | Métricas y supervisor propio | Métricas reales y recuperación acotada de procesos propios. |

La instalación propia se ubica en `C:\GPU-Bridge`; los modelos, entornos aislados y cachés son reconstruibles. No se despliegan aplicaciones ni DB en Windows. Cada servicio escucha IPv4 en `0.0.0.0`, conserva la identidad LAN `.6` y exige Bearer y cliente permitido en todas las rutas. Las direcciones IPv4 válidas son `.85`, `.7`, `.8` y `127.0.0.1`; permitir loopback en el firewall no amplía esa lista del servidor. Las cabeceras de proxy no alteran la identidad del socket. El [renderizador](render-worker/README.md) usa un único proceso Node para que su límite de dos trabajos sea global.

Las credenciales, rutas y recursos operativos se respaldan primero en la bóveda raíz y su plantilla; el instalador entrega un JSON privado mínimo mediante `GPU_RUNTIME_CONFIG_PATH`. No se introducen secretos en comandos, documentación o registros. La [evidencia de cierre](../docs/ci/evidence/2026-11-05-fase-3.md) acredita inferencia, formatos, concurrencia, firewall, ocho consultas LAN e integridad final. La Fase 3 está verificada operativamente; las aplicaciones e integración de Fase 4 continúan por separado.

## Instalación, caché y procesos propios

Los entornos de métricas, Immich y Whisper están separados. [Los manifiestos y sus instaladores](install_gpu_bridge.py) verifican versiones, procedencia y hashes antes de publicar; el entorno de construcción de Immich tampoco se fusiona con su runtime. El renderer mantiene su lock npm y navegador fijados. La configuración privada contiene únicamente las claves GPU necesarias, con propietario y ACL comprobados; no se carga ni copia la bóveda completa en los servicios.

| Ubicación | Uso y conservación |
|---|---|
| `C:\GPU-Bridge\runtime.json` | Configuración privada mínima, nunca imprimir ni adjuntar. |
| `C:\GPU-Bridge\services\` | Adaptadores y supervisor verificados contra el bundle respaldado. |
| `GPU_MODEL_CACHE_ROOT`, dentro de `C:\GPU-Bridge` | Modelos reconstruibles fijados por manifiesto. |
| `C:\GPU-Bridge\cache\remotion\` | Navegador reconstruible fijado por manifiesto. |
| `D:\Proyectos\Agencia Digital de Marketing AI\.tools\gpu-render-cache` | Bundle Remotion y temporales reconstruibles; propietario y ACL privada verificados. |
| `C:\GPU-Bridge\supervisor-start.json` | Recibo privado del proceso Python real, separado del PID del lanzador. |
| `P:\proyectos\<proyecto>\` | Medios persistentes, QA y entregables en NAS. |

`GPU_RENDER_CACHE_ROOT` selecciona la raíz D: sin mover modelos ni medios finales. El bundle se comparte mediante caché dentro del proceso y cada trabajo usa sus propios assets UUID; el navegador solo recibe el directorio público del trabajo. No borrar manualmente cachés en uso ni ejecutar limpiezas recursivas sobre NAS. Las guardas de propiedad deben seguir siendo válidas antes de cualquier limpieza.

El supervisor nativo mantiene sus tres hijos Immich, Whisper y render mediante Jobs de Windows; el cuarto listener sirve métricas y estado. Un proceso hijo caído puede reiniciarse; la salud fallida respeta una gracia de inicio de 180 segundos, tres fallos y espera creciente hasta 300 segundos. Windows inicia el supervisor mediante la tarea de inicio de sesión documentada abajo. El renderer se ejecuta en un solo proceso Node y comparte dos slots entre MP4, ProRes, capturas e inspección.

Las comprobaciones de salud ASGI disponen de cinco segundos para consultar el backend. La sonda local del supervisor espera ese plazo más un segundo para recibir la respuesta HTTP. Un HTTP 503 o el vencimiento de ese plazo siguen contando para la política de reinicio.

Las secuencias 1080p de al menos 30 segundos que incluyen vídeo de origen rasterizan hasta cuatro fotogramas en paralelo con Remotion. El MP4 de ese perfil usa JPEG intermedio de calidad 95 y convierte BT.601 de rango completo a BT.709 de rango limitado antes de NVENC. Las capturas y las secuencias ProRes con alfa usan PNG. El puente comprueba el audio del máster después de codificar. [Formatos intermedios de Remotion](https://www.remotion.dev/docs/renderer/render-frames#imageformat)

## Arranque y parada comprobados

Desde PowerShell 7.4 o posterior, la validación sin arranque es:

```powershell
pwsh -NoProfile -File 'C:\GPU-Bridge\start-gpu-bridge.ps1'
pwsh -NoProfile -File 'C:\GPU-Bridge\Stop-GpuBridge.ps1'
```

El segundo comando usa `Plan`: abre y retiene un handle, comprueba recibo, host, SID, propietario, rutas, bundle, argv privado, PID y fecha exacta. La identidad por FILETIME es exacta; CIM y la fecha JSON admiten solo diez ticks de diferencia. Las fechas tipadas por PowerShell se conservan en UTC, sin convertirlas a texto dependiente de cultura. Un recibo antiguo, otro propietario o un proceso reciclado se rechazan. Nunca se imprime el argv ni se admite un PID desnudo.

Para una intervención coordinada, detener primero la admisión de trabajos y esperar los trabajos propios en curso. Revisar `IdentityVerified=true` del plan y utilizar exclusivamente:

```powershell
pwsh -NoProfile -File 'C:\GPU-Bridge\Stop-GpuBridge.ps1' -Stop -WhatIf
pwsh -NoProfile -File 'C:\GPU-Bridge\Stop-GpuBridge.ps1' -Stop
pwsh -NoProfile -File 'C:\GPU-Bridge\start-gpu-bridge.ps1' -Start
```

Stop termina únicamente el handle retenido del supervisor y espera de forma acotada; al cerrarse sus Jobs terminan sus hijos propios. No busca procesos por nombre, no usa `taskkill` ni mata PIDs descendientes. La parada cancela trabajos activos; no sustituye a drenar la cola. El arranque reserva de forma exclusiva `9835` antes de escribir un nuevo recibo, espera su correspondencia con el lanzador o su descendiente validado y devuelve ambos PIDs por separado. `Started=true` y `Readiness=pending` acreditan inicio e identidad, no salud. No sobrescribir un recibo para adoptar un proceso sin la inspección controlada del responsable.

El responsable ya ejecutó Plan y Stop reales con identidad verificada y volvió a iniciar el supervisor; las evidencias saneadas son `gpu-stop-plan.json`, `gpu-stop-verified.json` y `gpu-supervisor-start-final.json` bajo `.tools/evidence/`. Una futura intervención debe verificar su propio recibo actual; esos PIDs históricos no son instrucciones.

La tarea `Agency-GpuBridge-AtLogon` usa [el controlador de inicio de sesión](Start-GpuBridgeAtLogon.ps1) instalado en `C:\GPU-Bridge` y PowerShell 7.6.10 en `C:\GPU-Bridge\tools\PowerShell-7.6.10`. Se dispara 20 segundos después del inicio de sesión de la cuenta que posee el mapeo de `P:` y espera hasta 75 segundos por el recurso cifrado. Si Windows recupera una reserva desconectada de `P:` sin política cargada, el controlador coteja el perfil persistente del usuario y rehace sólo el mapeo exacto con `RequirePrivacy=$true`. Lee la credencial privada `C:\GPU-Bridge\smb-logon-credential.xml`, protegida mediante DPAPI para esa cuenta y ese equipo. Se provisiona desde el `PSCredential` de `Get-AgencySmbConfiguration` y se renueva después de actualizar la credencial original en `.env`. Coteja el destino y el estado antes de consultar la carpeta de proyectos.

Con el puerto 9835 libre, la tarea consulta el estado de la RTX 3060 Ti. Si Windows informa Código 43, termina con ese código y conserva el último recibo de arranque correcto. En el flujo normal inicia el lanzador canónico con un registro de texto propio. Con un listener presente coteja el supervisor por su recibo. Escribe en `C:\GPU-Bridge\logon-start.json` las horas de inicio, si lanzó al supervisor, si acreditó cifrado y si reconstruyó el mapeo. Al recuperar el dispositivo, verifica que `nvidia-smi` reconoce la GPU registrada antes de actualizar la huella del binario de NVIDIA. [Export-Clixml y DPAPI](https://learn.microsoft.com/powershell/module/microsoft.powershell.utility/export-clixml) · [Reconexión de unidades de red](https://learn.microsoft.com/en-us/troubleshoot/windows-client/networking/mapped-drive-connection-to-network-share-lost)

Tras reiniciar Windows e iniciar sesión, cotejar el campo `ArranqueWindowsUtc` del recibo con `LastBootUpTime` de `Win32_OperatingSystem` y con el arranque anterior respaldado en `GPU_STARTUP_ATLOGON_BASELINE_BOOT_UTC`. Confirmar que la tarea terminó con código 0, que `SupervisorIniciadoPorTarea` es verdadero y que las diez sondas desde `vm-media-01` pasan. Recoger el recibo antes de volver a disparar la tarea manualmente.

## Cortafuegos: preparar y auditar

[Configure-GpuFirewall.ps1](Configure-GpuFirewall.ps1) es local y utiliza `Audit` por defecto. Importarlo solo define funciones. No solicita UAC ni eleva procesos automáticamente. Antes de cualquier `Apply`, el responsable debe respaldar estos nombres fijos en la bóveda:

| Nombre de regla | Función |
|---|---|
| `AgencyGPU-Block-IPv4-v1` | Bloqueo del complemento IPv4 en los cuatro puertos. |
| `AgencyGPU-Allow-TCP-3003-v1` | Clientes autorizados para Immich ML. |
| `AgencyGPU-Allow-TCP-8000-v1` | Clientes autorizados para transcripción. |
| `AgencyGPU-Allow-TCP-8090-v1` | Clientes autorizados para render. |
| `AgencyGPU-Allow-TCP-9835-v1` | Clientes autorizados para métricas. |

La propiedad exige a la vez el grupo exacto `Agencia Digital AI - Puente GPU v1` y la descripción exacta `Propiedad: Agencia Digital de Marketing AI; recurso: gpu-bridge-win11; contrato: 1.`. Una colisión de nombre, incluso variando mayúsculas, con propiedad distinta bloquea toda la aplicación antes del primer cambio. La comprobación previa incluye tanto `PersistentStore` como `ActiveStore`, incluidas reglas GPO/MDM del mismo nombre. No se eliminan reglas ni se modifican perfiles o reglas de otros proyectos.

Las cinco reglas son entrantes, TCP, persistentes, habilitadas en todos los perfiles y usan `LocalAddress=Any` sobre estos cuatro puertos exclusivos. Así cubren también adaptadores VPN u otras direcciones locales mientras los servicios escuchan en `0.0.0.0`; los demás puertos quedan fuera del contrato. Las cuatro reglas Allow admiten exclusivamente `192.168.0.85`, `192.168.0.7` y `192.168.0.8`. El Block se aplica primero y cubre estos intervalos inclusivos:

| Intervalo bloqueado | Excepción preservada a continuación |
|---|---|
| `0.0.0.0-126.255.255.255` | Todo `127.0.0.0/8`, incluido el supervisor por `127.0.0.1`. |
| `128.0.0.0-192.168.0.6` | Clientes `.7` y `.8`. |
| `192.168.0.9-192.168.0.84` | Cliente `.85`. |
| `192.168.0.86-255.255.255.255` | Fin del espacio IPv4. |

El validador numérico y las pruebas comprueban que no existen huecos ni solapamientos fuera de esas excepciones. El bloqueo explícito evita que reglas Allow ordinarias más amplias abran estos puertos exclusivos a otros clientes: Microsoft documenta su [precedencia sobre Allow](https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/windows-firewall/rules). Los [parámetros de NetSecurity](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule) admiten filtros de direcciones, intervalos y almacén persistente. Este diseño es IPv4 y depende de que los listeners continúen vinculados a IPv4; un cambio a IPv6 requiere revisar el contrato.

Desde la raíz del proyecto, auditoría y previsualización sin cambios:

```powershell
pwsh -NoProfile -File .\gpu-bridge-win11\Configure-GpuFirewall.ps1
pwsh -NoProfile -File .\gpu-bridge-win11\Configure-GpuFirewall.ps1 -Mode Apply -WhatIf
```

`Apply` real requiere una consola ya elevada y la revisión del plan anterior:

```powershell
pwsh -NoProfile -File .\gpu-bridge-win11\Configure-GpuFirewall.ps1 -Mode Apply
```

El script crea solo nombres ausentes y corrige únicamente reglas de propiedad acreditada que difieran del contrato. Una repetición conforme no escribe. Rechaza aplicar si algún perfil está deshabilitado o impide reglas locales; no cambia esas políticas. Después compara reglas persistidas y `ActiveStore`. Si la política efectiva no coincide, informa error y no acredita éxito. Un fallo parcial conserva lo ya escrito para auditarlo; no ejecuta una eliminación general ni modifica reglas ajenas como recuperación automática.

El [wrapper elevado](Apply-GpuFirewallElevated.ps1) exige host, SID, instalación privada, script canónico y su SHA256 respaldado. Se ejecuta desde una sesión ya elevada, sin solicitar UAC por sí mismo:

```powershell
pwsh -NoProfile -File 'C:\GPU-Bridge\Apply-GpuFirewallElevated.ps1' -RuntimeConfigPath 'C:\GPU-Bridge\runtime.json'
```

Solo un recibo privado con `Success=true`, `Verified=true`, `ReceiptWritten=true` y salida cero acredita la aplicación local. `Start-Process -Verb RunAs` devuelto o un PID de la ventana elevada todavía en ejecución no demuestran que las reglas se aplicaron. El responsable ya verificó el recibo `C:\GPU-Bridge\firewall-apply-receipt.json` con `Success=true`, `Verified=true`, `Changed=5`, fechado `2026-11-06T02:23:25Z` —2026-11-05 en Chile—. No hace falta solicitar otra elevación ni repetir Apply para completar las pruebas de render.

`Verified=true` acredita la comparación local de filtros y política efectiva. `NetworkTestPerformed=false` indica que no se han probado conexiones. La aceptación final exige salud desde `.85` y `.7`, rechazo de otro cliente y Bearer incorrecto, más las pruebas GPU de cada servicio. Reglas de bloqueo preexistentes pueden impedir clientes autorizados; este script no las elimina.

## Verificación local sin GPU ni red

```powershell
python -m unittest discover -s tests -p test_gpu_firewall.py -v
python -m ruff check tests/test_gpu_firewall.py
Invoke-ScriptAnalyzer -Path .\gpu-bridge-win11\Configure-GpuFirewall.ps1 -Severity Warning,Error
```

Las pruebas sustituyen los cmdlets de firewall con funciones en memoria. Cubren auditoría, WhatIf, falta de elevación, propiedad, rangos, idempotencia, deriva de reglas propias y ausencia de política efectiva. No aplican reglas ni acreditan conectividad. Las [recetas CI](../docs/ci/recipes/developer-push-flow.md) recogen tipos, pruebas y auditorías de los grafos separados; no instalan runtimes Windows ni cargan pesos en los runners públicos. El control declarativo admite Fase 3 y mantiene CD deshabilitado; el cierre real se acredita con el certificado operativo, no solo con esos archivos.

## Verificación operativa de Fase 3

Immich 3.3.6 verificó embeddings CLIP textual y visual, un rostro con YuNet + solo AuraFace `glintr100`, y perfiles de ejecución CUDA; la última prueba instalada aprobó en 8,594 segundos. Se conservan las mediciones anteriores de 14,047 y 10,000 segundos como historia, el ID `agency_yunet_auraface_v1` y el protocolo original. [ADR-003](../docs/decisions/ADR-003-modelos-faciales-comerciales.md) registra las licencias declaradas MIT/Apache-2.0; no convierte la restricción de Buffalo_L histórico en autorización comercial ni garantiza derechos sobre todos los datos de entrenamiento.

Whisper verificó HTTP 200 y 28 palabras con tiempos monótonos en 5,359 segundos en su última prueba instalada. Se conservan 3,281 segundos con caché caliente y 17,079 segundos en prueba fría coincidente con render concurrente. Son mediciones puntuales con distinta carga/caché, no p95 ni el SLO de un minuto. `/stills` generó una imagen 1080×1920 con marca Free en el fotograma 60, correspondiente al segundo 2, con 2,328 segundos de tiempo total.

El primer `/render` rechazó primarias/transferencia BT.709 ausentes; la corrección explícita por fotograma ya está instalada. Tras la corrección final de audio, MP4 NVENC aprobó 1080p de 3 s en 20,313 s (`-14,00 LUFS`/`-1,50 dBTP`) y 4K de 1 s en 20,859 s (`-14,00 LUFS`/`-2,85 dBTP`), con 30 fps, tres etiquetas BT.709 y marca Free cuando corresponde. `/inspect` produjo tres capturas y regla QA normalizada `Y=0..1920`, escala 0,5 para 2160×3840. MOV ProRes 4444 de 1 s aprobó 1080p en 16,547 s y 4K en 41,485 s: `prores_ks` en CPU, PCM 24 bits/48 kHz, audio `-14,00 LUFS`/`-2,84 dBTP` y alfa mínimo 0 en muestra decodificada 64×64 a 0,5 s. Las capturas PNG transparentes también aprobaron. Los hashes y tamaños figuran en cada evidencia; el muestreo alfa no examina todos los fotogramas. Las piezas anteriores de 3 s quedan archivadas como `*-pre-audio-order.json`.

Se conserva el intento previo de un segundo de voz que devolvió 422 `AUDIO_FUERA_DE_OBJETIVO`, con `-15,68 LUFS`, por debajo del mínimo `-15`. No era un fallo 4K/GPU ni establece duración mínima por plan. Preparar estéreo/48 kHz antes de ambas pasadas loudnorm corrigió la causa: ambos formatos aprobaron 48 pruebas Node, ocho controles CPU y las pruebas HTTP finales de 1 s/3 s descritas arriba, conservando el mismo contrato de aceptación.

La prueba distribuida `gpu-lan-health-real.json` acredita ocho `/health` HTTP 200 con `ready=true`, cuatro desde `.85` y cuatro desde `.7` —hostname real `openclaw-ia`—, más ocho rechazos HTTP 401 con token incorrecto. `gpu-denials-real.json` añade ausencia Bearer 401 y socket `127.0.0.2` con X-Forwarded-For suplantada 403 en los cuatro puertos. Esto acredita la decisión del servidor por socket, no una prueba de firewall desde un cuarto equipo LAN.

La concurrencia del renderer final aprobó dos archivos HTTP 200 en 29,672/29,797 s y rechazó el tercero 429 en 0,719 s; 36 muestras globales NVIDIA observaron máximo dos sesiones. La suite integrada aprobó 389 pruebas Python en 264,978 s con ResourceWarning estricto; SCA de desarrollo/pruebas cubrió 27 pins, el renderer 48 pruebas Node tras el último cambio y raíz TypeScript 12 con cobertura 100 %. Trivy no encontró altos/críticos en los dos locks npm; no analizó misconfiguraciones ni sustituye SCA Python. Los cuatro grafos GPU se renovaron tras ArgMax y aprobaron 175 consultas exactas, separando runtime/build y la excepción de fuente.

El responsable certificó el cierre operativo con `gpu-final-health-snapshot.json`: integridad de 17 fuentes nativas y 20 del renderer, 14 artefactos NAS con hash revalidado, cuatro salud 200/ready, tres hijos sanos e identidad propia acreditada por Plan sin detener. El certificado es una comprobación funcional, no un snapshot de disco ni una declaración automática de fase. No quedan pendientes del cierre de Fase 3.

Permanecen como trabajo posterior el SLO p95, arranque tras reinicio de Windows, workflow remoto, CD, integración Immich server de Fase 4 y backups futuros. El usuario sandbox de MercadoPago está autenticado; checkout, webhooks, suscripciones y pagos reales pertenecen a Fase 6.
