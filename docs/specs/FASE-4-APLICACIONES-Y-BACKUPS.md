# Especificación ejecutable de Fase 4: aplicaciones y copias 3-2-1

Estado: en implementación. Fecha: 2026-11-06. El cierre operativo de Fase 3 y el manifiesto declarativo `phase: 3` son la base; esta especificación no acredita aplicaciones ni respaldos todavía.

## Objetivo y límites

Desplegar el conjunto de aplicaciones de la arquitectura v4.1 en `vm-media-01`, `192.168.0.85`, y demostrar salud, acceso administrativo, integración GPU y una copia con restauración aislada. Código y configuración pertenecen a `/opt/media-platform`; las bases y estado de aplicaciones a `/var/lib/media-platform`, en el disco local. Medios y copias usan `/mnt/producciones`, cuyo origen exacto es `192.168.0.8:/volume2/producciones`.

Se conserva el runner, la configuración NFS, el datastore NAS heredado y cualquier recurso ajeno. En Windows siguen únicamente los cuatro demonios GPU nativos; no se instalan Docker ni bases de datos allí. No se ejecutan comandos Git, tampoco indirectamente mediante instaladores o Dockerfiles. Las fuentes se obtienen por artefactos HTTPS fijados, sin clonar repositorios.

El repositorio público conserva únicamente su licencia, por instrucción del usuario. Esta fase puede preparar y verificar fuentes locales y recursos propios; no autoriza publicación de código ni activa CD.

## Gobernanza y propiedad

Cada credencial e identificador operativo nuevo se respalda primero en `.env`, con los tres comentarios obligatorios, luego en `.env.example` como `CHANGE_ME` y finalmente se inyecta. El responsable principal es el único escritor de la bóveda. Los agentes proponen nombres y contratos sin generar ni instalar secretos por su cuenta.

La inspección inicial es de lectura: identidad SSH fijada, hostname, direcciones, espacio, memoria, montajes, servicios, contenedores, redes, puertos y rutas objetivo. La presencia de una ruta o nombre no demuestra propiedad. La instalación requiere marcadores y recibos propios y rechaza colisiones ajenas antes de mutar. No hay limpieza global Docker, eliminación de volúmenes ni modificación recursiva de rutas no acreditadas.

La configuración privada del destino se limita a las variables de cada servicio. Usar archivos secretos cuando el componente los admita y entorno privado cuando su contrato lo requiera. No imprimir configuraciones resueltas, contraseñas, cabeceras Bearer, volcados de bases o configuración OpenClaw. IDs devueltos por creación se respaldan inmediatamente antes de utilizarlos en operaciones posteriores.

## Aplicaciones y contratos

| Componente | Contrato de la fase |
|---|---|
| PostgreSQL Immich | Rama 16 con VectorChord compatible y pgvector, datos locales; imagen y extensiones fijadas y auditadas antes de crear la base. No aceptar un tag como prueba de versión PostgreSQL actualizada. |
| PostgreSQL Kitsu y aplicaciones | PostgreSQL 16, bases y roles separados por aplicación; sin publicar puertos de base al exterior. |
| Valkey | Instancias o límites independientes para evitar interferencias entre colas y cachés; persistencia según función y sin publicar Redis en LAN. |
| Kitsu/Zou | UI `8081`, API `5000`, base `zoudb`; cuenta administrativa y token funcional respaldados. Separar datos y servicios de producción del contenedor conjunto de demostración. |
| Immich server | Versión `3.3.6`, servidor `2283`; librería NAS y almacenamiento propio delimitados, login y API key MCP. ML exclusivamente en `.6:3003`. |
| n8n | UI `5678`, PostgreSQL, clave de cifrado respaldada y ejecución de tareas aislada; uso interno de la agencia. Su licencia SUL no se presenta como OSI ni se cambia la licencia del código propio por la de una dependencia. |
| SupoClip | UI `3080`, construcción CPU en `.85`; la transcripción debe reutilizar el servicio Whisper CUDA autenticado de `.6:8000`, sin descarga o fallback local silencioso. |
| MoneyPrinterTurbo | UI `8501`, API privada, configuración y medios separados del código. Verificar su contrato de transcripción antes de integrarlo con el puente remoto. |
| Consumidor R2 | API `3010`, autorización interna/HMAC, rutas NAS acotadas, checksum, publicación atómica e idempotencia. No aceptar URLs arbitrarias ni órdenes de sistema desde una petición. |
| Observabilidad | OTel, node-exporter y cAdvisor; receptor real del NAS y lectura de métricas comprobados, no presumidos por abrir un puerto. |

Los pins y fuentes candidatas constan en la investigación local `.tools/evidence/fase4-research-plan.md`. Son candidatos de identidad, no certificados de seguridad. PostgreSQL, extensiones e imágenes se seleccionan tras comprobar vulnerabilidades y compatibilidad. Se conservan fuente, revisión, hash, plataforma y licencias de cada artefacto.

### Adaptador privado para Immich ML

El cliente Immich fijado no permite configurar el Bearer del puente. Incorporar un proxy privado dentro de la red Compose del servidor, sin puertos publicados. Su único destino es `192.168.0.6:3003`, con secreto leído de un archivo privado. Sustituye cualquier cabecera de autorización entrante por la propia y no actúa como proxy abierto.

Admitir únicamente el protocolo requerido: salud local, `GET /ping` hacia salud real y `POST /predict` multipart. Preservar estado HTTP y cuerpo de inferencia, con tamaños, plazos y concurrencia acotados. No seguir redirecciones ni confiar en cabeceras de destino/proxy del cliente. Probar rutas y métodos rechazados, secreto ausente, desconexión, límites y fallo upstream, además de una prueba real desde `.85`.

La configuración Immich de solo lectura fija CLIP `ViT-B-32__openai`, rostro `agency_yunet_auraface_v1` y OCR deshabilitado mientras no exista soporte. La nueva base no reutiliza embeddings Buffalo. Los umbrales faciales requieren comprobación con datos públicos de control antes de incorporar medios de clientes; dimensión 512 no acredita compatibilidad ni precisión.

### Adaptadores de transcripción

Verificar los contratos originales de SupoClip y MoneyPrinterTurbo antes de alterar sus proveedores. La petición a Speaches usa el modelo fijado, `verbose_json`, `timestamp_granularities[]=word` y `stream=false`. Mantener tiempos de palabra y errores explícitos; no inventar segmentos, descargar modelos o cambiar a CPU para ocultar indisponibilidad. Los adaptadores se desarrollan y verifican en unidades separadas de los manifiestos de despliegue.

## Copias y restauración

El procedimiento `sre/sre-backup-321.sh` crea volcados coherentes de PostgreSQL y copia cifrada de configuración y memorias OpenClaw propias, más D1 cuando exista. Un recurso todavía ausente se registra como dependencia, no como copia aprobada. Resolver la dependencia D1 antes de acreditar el cierre completo; no inventar una exportación de una base inexistente.

Destinos: áreas propias bajo `/mnt/producciones/_backups` y repositorio cifrado en el bucket R2 configurado. La contraseña restic se respalda antes de inicializar el repositorio. Separar credenciales, marcadores y prefijos propios de datos ajenos; no ejecutar `forget`, `prune` o restauraciones sobre rutas activas sin un procedimiento específico y propiedad acreditada.

La bóveda Windows requiere copia cifrada con cada cambio según la arquitectura; resolver y verificar ese flujo, sin declarar protección offsite solo por tener `RESTIC_PASSWORD`. Programar las tareas en `America/Santiago`, incluyendo corrida diaria `03:30`, y documentar cómo se tratan cambios horarios y ejecuciones solapadas.

La aceptación incluye una corrida real, integridad de archivos y restauración aislada: validar bases en una instancia temporal propia, comparar datos y retirar solo los recursos de esa prueba. Un listado de snapshots o salida cero no demuestra recuperación. Respetar RPO/RTO y registrar los límites que no hayan sido medidos.

## Seguridad, CI y aceptación

Imágenes fijadas por digest, sin `latest`; revisar también la versión real dentro de cada imagen. Ejecutar SCA de dependencias e imágenes y análisis de configuración. Los hallazgos no se ocultan con exclusiones generales. Documentar y restringir los accesos al host que requiere cAdvisor, incluyendo el riesgo de la API Docker; no confundir un montaje de socket `ro` con autorización de solo lectura de esa API.

Aplicar mínimos de privilegios, redes separadas, límites de recursos, salud real y almacenamiento delimitado según los contratos de cada componente. Verificar que el conjunto cabe en 6 vCPU/16 GB/80 GB, dejando margen operativo. No alterar el sistema o ampliar recursos antes de observar una necesidad real.

Los siete controles CI siguen bloqueantes, con CD deshabilitado. Al existir Compose, exigir su validación real con datos sintéticos en CI y configuración privada sin salida sensible en destino. Los validadores de fase se amplían de forma acumulativa; declarar Fase 4 solo después del cierre operativo.

Para cerrar: contenedores propios saludables, login Kitsu/Immich, API keys respaldadas y funcionales, prueba ML autenticada desde el servidor, adaptadores de audio verificados, consumidor R2 con checksum/idempotencia, observabilidad recibida en NAS, corrida y restauración de las copias, controles de código/seguridad/documentación y Estado actualizado. Ninguna comprobación se sustituye por una simulación presentada como producción.

## Unidades verificables

Cada incremento comprende hasta cinco archivos de implementación, pruebas y documentación; se divide si mezcla responsabilidades o supera ese límite. Propietarios separados y sin edición concurrente del mismo archivo.

1. Inventario fijado, validador y preflight de lectura.
2. Directorios propios, redes, PostgreSQL y Valkey.
3. Proxy ML privado y pruebas HTTP.
4. Immich, configuración y bootstrap administrativo.
5. Kitsu/Zou, UI y bootstrap administrativo.
6. n8n y ejecutor aislado.
7. Adaptador remoto SupoClip y, después, imagen/Compose.
8. Adaptador remoto MoneyPrinterTurbo y, después, imagen/Compose.
9. Colectores y receptor real NAS.
10. Consumidor R2: autorización, transferencia, idempotencia y pruebas.
11. Copias: volcados, repositorios, programación y restauración aislada.
12. Integración final, controles CI/SRE, documentación y cierre.

La autorización de implementación deriva del objetivo original y de las decisiones vigentes del usuario; no se solicita permiso de nuevo para tareas reversibles dentro de este alcance. Un dato imprescindible no disponible se consulta solo después de revisar las fuentes conectadas y la configuración existente.

## Contrato puro del consumidor R2 v1

La primera unidad del consumidor implementa `src/media/sync-contract.ts`, sus pruebas `tests-ts/sync-contract.test.ts` y el protocolo detallado en `docker/r2-sync-consumer/README-protocol.md`. Es una biblioteca pura compartible entre Worker y Node; no tiene HTTP, filesystem, descarga, persistencia ni servicio desplegado. Se mantienen fase declarada 3 y CD deshabilitado.

El JSON cerrado ordena exactamente job_id, asset_id, project_slug, extension, bytes, sha256 y object_key. Los identificadores son UUIDv4 lowercase; project_slug usa ASCII seguro con máximo 63 caracteres y rechazo de dispositivos DOS. La extensión es un token alfanumérico lowercase de 1..10 caracteres. bytes exige un entero positivo seguro y una política maxObjectBytes obligatoria, sin fallback. SHA256 es lowercase de 64 caracteres. La clave exacta es `ingest/<job_id>/<asset_id>.<extension>` y sólo se deriva el destino relativo `proyectos/<project_slug>/01_raw/<asset_id>.<extension>`; no se aceptan URL ni ruta final del cliente.

El cuerpo está limitado a 4096 bytes y debe llegar ya canónico: UTF8 estricto, parseo, validación y recodificación byte a byte rechazan duplicados, orden/whitespace alternativos, escapes equivalentes y exponentes numéricos. La firma usa WebCrypto HMAC/SHA-256 con clave de 32..128 bytes no extraíble, sobre `v1\n<timestamp>\nPOST\n/v1/sync\n<sha256-del-cuerpo>`. timestamp es decimal canónico de segundos Unix; reloj inyectado y tolerancia inclusiva de ±300 segundos. signature es `v1=` más hexadecimal lowercase64; se usa verify nativo para el MAC. El resultado devuelve el trabajo congelado, la huella del cuerpo y el destino derivado.

Un reintento conserva cuerpo/hash y renueva timestamp/firma. La huella permite una futura clave de idempotencia durable, pero la biblioteca no implementa replay ni almacenamiento en memoria volátil. El siguiente incremento debe verificar montaje NFS y ancestros sin symlinks, descargar S3 con autenticación y límites, comprobar tamaño/checksum y publicar atómicamente sin sobrescribir recursos ajenos. Se requerirán persistencia, recuperación tras caída y pruebas HTTP antes de una transferencia real; los tests puros no acreditan esas operaciones.
