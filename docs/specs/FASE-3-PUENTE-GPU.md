# Especificación ejecutable de Fase 3: puente GPU de Windows

## Objetivo y alcance autorizado

Ejecutar en `W11-HOMELAB` (`192.168.0.6`) únicamente los cuatro servicios GPU sin estado de la arquitectura v4.1. Las aplicaciones, bases de datos y datos de negocio permanecen en `.85` y Synology. La autorización proviene del objetivo original y del plan por fases; la Fase 2 está cerrada con evidencia real.

La fuente del puente está en `gpu-bridge-win11/` y su instalación propia en `C:\GPU-Bridge`. Los medios persistentes se leen y escriben en `P:\`, correspondiente a `/volume2/producciones` por ADR-001. Los modelos y el navegador permanecen bajo la instalación privada; `GPU_RENDER_CACHE_ROOT` separa el bundle y temporales reconstruibles en `D:\Proyectos\Agencia Digital de Marketing AI\.tools\gpu-render-cache`, cuya propiedad y ACL se verificaron antes de inyectarla. Ninguna de esas cachés contiene bases de datos de negocio ni sustituye los medios finales en NAS.

## Hechos y decisiones de implementación

- Lectura preparatoria: RTX 3060 Ti, 8192 MiB, controlador 621.01; 3686 MiB usados en ese momento. Python 3.12.1, Node 24 y FFmpeg disponibles. NVCC ausente; su instalación no se presupone necesaria para ruedas CUDA precompiladas.
- La memoria libre se vuelve a consultar antes de inferencia; no se reserva la GPU completa ni se alteran procesos ajenos.
- Los componentes oficiales fijados se ejecutan mediante adaptación nativa de portabilidad y entornos propios; sus pruebas locales se registran por backend. No se instala Docker/WSL ni se alteran componentes por un fallo sin revisar el alcance congelado.
- Cada servicio de inferencia mantiene su entorno aislado. Las versiones, commits, archivos HTTPS y SHA256 se fijan antes de instalar. No se utilizará un instalador que invoque Git.
- La disponibilidad de una biblioteca o de `CUDAExecutionProvider` no acredita inferencia GPU: se requieren pruebas reales del proveedor utilizado y de sus resultados.

## Contrato común y protección

Los cuatro puertos escuchan IPv4 en `0.0.0.0` mediante `GPU_BRIDGE_BIND_ADDRESS`, con identidad LAN `192.168.0.6` y acceso loopback del supervisor. Toda ruta, incluidas salud y métricas, exige `Authorization: Bearer` con `GPU_BRIDGE_BEARER_TOKEN` ya respaldado. La comparación del secreto es de tiempo constante; las salidas y registros nunca incluyen el token.

Solo se admiten clientes IPv4 `192.168.0.85`, `.7`, `.8` y comprobaciones locales por `127.0.0.1`. Se utiliza la dirección real del socket, sin confiar en cabeceras de proxy. El cortafuegos usa cuatro Allow y un Block del complemento IPv4 en esos puertos exclusivos, `LocalAddress=Any`, preservando `127.0.0.0/8`; esto no amplía la lista del servidor a otras IP loopback. Comprueba propiedad en almacenes persistente y efectivo antes de escribir. El wrapper elevado exige hash del script canónico y devuelve un recibo privado; iniciar su elevación no acredita Apply ni acceso LAN. Los servicios aplican también la lista de clientes.

Todos los servicios ofrecen `GET /health` y alias `/healthz`. La respuesta del adaptador es JSON con `service`, `status`, `ready`, `version` y evidencia resumida del backend. Un backend no comprobado o degradado devuelve 503; no se sustituye por una simulación ni por CPU silenciosa. Las rutas originales necesarias de los componentes se conservan detrás de la misma protección.

Los medios se identifican con rutas relativas a la raíz NAS. Se rechazan rutas de otro disco, UNC ajenas, `..`, dispositivos de Windows, flujos alternativos y enlaces o puntos de reanálisis que salgan del ámbito permitido. Ninguna petición puede ejecutar órdenes arbitrarias ni proporcionar código de composición remoto.

## Servicios y aceptación

| Puerto | Servicio | Condición de aceptación real |
|---|---|---|
| 3003 | Immich machine-learning CUDA | Protocolo original compatible con la versión de servidor fijada; embeddings CLIP y procesamiento facial con CUDA acreditados. `/ping` original sigue disponible con autenticación. |
| 8000 | Speaches/faster-whisper CUDA | `POST /v1/audio/transcriptions` acepta el contrato OpenAI compatible, `response_format=verbose_json`, `timestamp_granularities[]=word`, `stream=false`; devuelve palabras con inicio/fin y usa CTranslate2 CUDA. |
| 8090 | Remotion y FFmpeg NVENC | `/stills`, `/inspect` y `/render` operativos; MP4 H.264 NVENC real y BT.709, 4K para Ultra/Ilimitado, MOV ProRes 4444 alfa para Ilimitado mediante `prores_ks` en CPU, sonoridad `-14 ±1 LUFS`, pico verdadero `<= -1 dBTP`, marca Free y máximo de dos trabajos propios simultáneos compartidos entre operaciones/formatos. |
| 9835 | Métricas y supervisión GPU | `/metrics` presenta memoria, temperatura y sesiones de codificación observadas; supervisión reinicia únicamente procesos propios del puente ante fallo comprobado. |

### Contrato de medios del servicio 8090

La identificación mínima de una solicitud usa `project_id` y `job_id` como nombres cortos seguros. El proyecto inicial es `sansevieras`; no se infiere autorización de un nombre proporcionado por un usuario público. Los consumidores internos autenticados son responsables de la identidad y cuota del pedido.

- `/stills`: composición local permitida y propiedades validadas; captura inicial en el segundo 2 o en los fotogramas solicitados y devuelve rutas NAS relativas de imágenes QA, dimensiones y duración observada. La latencia de captura se mide en el equipo real.
- `/inspect`: archivo NAS permitido; devuelve datos de ffprobe, análisis ebur128 y una tira de contactos con regla vertical visible `Y=0..1920` para revisar posiciones. La regla usa lienzo normalizado de 1080×1920 también para fuentes 4K y conserva dimensiones originales, escala y desplazamientos. Los artefactos quedan en el área QA del trabajo.
- `/render`: composición local y propiedades validadas, plan `Free|Pro|Ultra|Ilimitado`, formato y límites permitidos. Los archivos finales quedan bajo `proyectos/<project_id>/06_renders/<job_id>/render/`. Free incorpora marca de agua; un solo proceso Node comparte dos slots entre renders, capturas e inspecciones y rechaza el tercero con HTTP 429. La respuesta incluye artefactos, códec y verificación de audio; ProRes declara `hardware_acceleration=false` y debe acreditar alfa por decodificación, no solo por etiqueta del códec.

El límite operativo común es 600 segundos por pieza y 20 minutos de procesamiento; no se inventan duraciones comerciales diferentes por plan. El consumidor interno controla cuotas y cuentas en Fase 6. El bundle se compila una vez por proceso y los assets de cada solicitud se aíslan por UUID; no se expone el token al navegador ni se borra recursivamente en NAS. El [contrato exacto del renderer](../../gpu-bridge-win11/render-worker/README.md) documenta formatos, validación de alfa y límites.

Los esquemas exactos de composición y respuestas se incorporan junto a sus implementaciones antes de conectar los MCP de Fase 5. Las pruebas positivas no utilizan medios privados del usuario.

## Estructura y estilo

- `gpu-bridge-win11/`: configuración, protección común, adaptadores de inferencia, métricas, renderizador, instalador y supervisor.
- `tests/`: contratos y lógica Python sin GPU, con transportes o proveedores controlados.
- `tests-ts/` o pruebas propias del renderizador: tipos, rutas y reglas de composición.
- `docs/ci/evidence/`: resultados reales y límites de la Fase 3.

Se mantienen comentarios, documentación y errores propios en español. Los identificadores de protocolos y componentes oficiales conservan su forma original. Los scripts se importan sin actuar sobre infraestructura; auditoría y aplicación son modos explícitos.

## Plan por unidades verificables

1. **Protección y configuración común.** Validar host, puertos, token persistido, rutas NAS y clientes. Pruebas de rechazo de token/IP/ruta y de importación sin efectos. Dependencia: ninguna.
2. **Métricas y supervisión propia.** Lectura real de NVIDIA, contratos de salud y reinicio acotado. Pruebas de parser/propiedad y medición real sin alterar otros procesos. Dependencia: 1.
3. **Transcripción CUDA.** Instalar artefactos fijados en entorno propio y ejecutar audio sintético con palabras temporizadas. Pruebas de contrato, rechazo de CPU y consumo real de VRAM. Dependencia: 1.
4. **Immich ML CUDA.** Adaptar únicamente los puntos no portables del componente fijado y acreditar inferencia original. Pruebas de protocolo y ejecución CUDA real. Dependencia: 1.
5. **Inspección y render.** Incorporar composición local permitida, capturas, regla QA, NVENC, mezcla y límite de concurrencia. Verificar un video sintético y medir sus artefactos. Dependencia: 1.
6. **Instalación y acceso LAN.** Publicar archivos propios con recibos y secretos mínimos; configurar inicio, supervisión y reglas de red propias. Los pasos que requieran elevación de Windows se dejan preparados y revisables antes de solicitar la intervención necesaria. Dependencias: 2–5.
7. **Verificación distribuida.** Consultar los cuatro `/health` desde `.85` y `.7` con el token persistido; comprobar denegación no autorizada y pruebas GPU reales. Actualizar mínimos declarativos, controles, evidencia y Estado. Dependencia: 6.

Las unidades de inferencia y render pueden desarrollarse en paralelo después de fijar el contrato común, con propietarios de archivos separados. Cada unidad se divide si supera cinco archivos de implementación o mezcla responsabilidades independientes.

## Comandos de verificación comunes

```powershell
python scripts/ci/check_env.py
python -m unittest discover -s tests -p 'test_*.py'
python -m ruff check scripts tests sre vcenter storage gpu-bridge-win11
npm run lint
npm run typecheck
npm run test
python scripts/ci/check_docs.py
```

Los siete controles CI conservan sus umbrales y CD deshabilitado. Tipos y pruebas Node incluyen el renderer; Ruff y PowerShell incluyen el puente. SCA separa métricas (4), Immich runtime (69 consultas OSV y una fuente fijada excluida explícitamente), build (8) y Whisper (94); ORT del feed Microsoft sí se consulta por identidad/versión en OSV. El recibo renovado tras ArgMax acredita 175 consultas exactas y hashes actuales de informes/diagnósticos; el renderer tiene auditoría npm propia. La [receta CI](../ci/recipes/developer-push-flow.md) reproduce los comandos. El control declarativo admite Fase 3; las pruebas CI no acreditan GPU ni instalan servicios en `.6`. El cierre operativo se certificó por separado.

## Operación propia y cierre verificado

El supervisor mantiene sus hijos con Jobs Windows propios y sirve métricas en `9835`. Reserva ese socket exclusivamente antes de publicar el recibo del Python real; el PID del redirector del venv se registra aparte como lanzador. `Started=true` significa identidad e inicio, con `Readiness=pending` hasta comprobar los cuatro servicios.

`Stop-GpuBridge.ps1` usa Plan por defecto. Antes de admitir Stop compara recibo privado, host, SID, propiedad, rutas, bundle, argv en memoria, PID, FILETIME exacto y fechas UTC con tolerancia de diez ticks; termina solo mediante un SafeProcessHandle retenido. No busca por nombre, mata PIDs descendientes ni admite PID desnudo. Plan/Stop y nuevo arranque reales ya están registrados; el [runbook rb-01](../ci/runbooks/rb-01-gpu-bridge-unreachable.md) explica diagnóstico, drenado y recuperación coordinada.

La última prueba instalada acreditó CLIP y reconocimiento facial CUDA con `agency_yunet_auraface_v1` (YuNet + solo AuraFace glintr100), un rostro en 8,594 s. Whisper verificó 28 palabras con tiempos monótonos en 5,359 s; se conservan 3,281 s con caché caliente y 17,079 s fría coincidente con render concurrente como mediciones históricas, sin atribuir p95. El firewall propio aplicó cinco reglas con recibo verificado. Desde `.85` y `.7` —hostname real `openclaw-ia`— se observaron ocho respuestas de salud 200/ready y ocho rechazos 401 con Bearer incorrecto. Las pruebas locales también acreditaron cuatro rechazos 401 sin Bearer y cuatro 403 por socket `127.0.0.2` pese a cabecera de proxy suplantada; no se presentan como un cuarto cliente LAN.

Tras corregir BT.709 y preparar estéreo/48 kHz antes de ambas pasadas loudnorm, el endpoint final aprobó MP4 NVENC 1080p de 3 s en 20,313 s (`-14,00 LUFS`/`-1,50 dBTP`) y 4K de 1 s en 20,859 s (`-14,00 LUFS`/`-2,85 dBTP`), con marca Free cuando corresponde y contactos QA normalizados. MOV ProRes 4444 de 1 s aprobó 1080p/4K en 16,547/41,485 s, CPU `prores_ks`, PCM 24 bits/48 kHz, audio `-14,00 LUFS`/`-2,84 dBTP` y alfa decodificado mínimo 0 en muestra 64×64 a 0,5 s; también aprobaron las capturas transparentes. Se conservan los renders anteriores de 3 s archivados, el 502 inicial de color y el rechazo 422 previo de audio de 1 s; este último no fue un fallo 4K ni se convirtió en duración mínima comercial. La [evidencia de cierre](../ci/evidence/2026-11-05-fase-3.md) registra artefactos, pruebas y límites. MercadoPago sandbox autentica un usuario de prueba; la integración de pagos sigue en Fase 6 y no acredita un cobro.

## Límites

Siempre: respaldar cualquier credencial o identificador operativo antes de inyectarlo; comprobar procesos propios, autenticación, recursos y evidencias reales. Nunca: comandos Git sin petición explícita, contenedores de aplicaciones/DB en Windows, cambios a equipos de agentes ajenos, exposición WAN de los puertos GPU o reinicio general de la GPU/Windows para ocultar errores.

La concurrencia del renderer final aprobó dos archivos HTTP 200 en 29,672/29,797 s y un tercero 429 en 0,719 s; 36 muestras globales NVIDIA observaron máximo dos sesiones. El responsable certificó la **Fase 3 verificada operativamente**, con cierre documental de 2026-11-06 y certificado funcional de `2026-11-06T02:59:28.129562+00:00` —2026-11-05 23:59:28 UTC−3 en Chile—: 17 fuentes nativas y 20 del renderer verificadas, 14 hashes NAS revalidados, cuatro salud 200/ready, tres hijos sanos e identidad propia comprobada sin detener. El certificado de lectura no es un snapshot de disco. No quedan verificaciones pendientes del cierre de esta fase.

Las mediciones puntuales no son SLO p95. Arranque tras reiniciar Windows, workflows remotos, CD, integración Immich server de Fase 4, backups futuros y pagos productivos permanecen fuera de lo acreditado. El responsable registra por separado el avance a Fase 4 y la declaración de capacidades, conservando CD deshabilitado.
