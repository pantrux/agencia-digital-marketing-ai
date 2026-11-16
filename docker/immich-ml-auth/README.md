# Proxy privado de Immich ML

Imagen construida y probada en una aceptación aislada el 2026-11-06 en `.85`. La imagen final pasó SCA con 38 paquetes Alpine y un paquete Python, sin HIGH/CRITICAL. Salud y una petición textual real devolvieron 200 desde un contenedor sin puertos publicados, con embedding de 512 dimensiones, en 5,514 s. El contenedor quedó detenido. La integración con Immich server y la precisión facial siguen pendientes; no cambia el manifiesto de fase.

El servidor Immich 3.3.6 utiliza multipart en `/predict` y no dispone de cabeceras Bearer configurables en su cliente ML. Este adaptador añade la autorización desde un archivo y sólo conecta a `192.168.0.6:3003`. La dirección es literal en el código: ninguna variable, URL, `Host`, `X-Forwarded-*` o cabecera de proxy puede cambiarla. `http.client` no utiliza proxies del entorno ni sigue redirecciones.

## Contrato HTTP

| Entrada privada | Operación |
|---|---|
| `GET /ping` | Consulta autenticada nueva a `GET /health` del puente; devuelve su JSON. Un HTTP 200 requiere `ready=true`, `cuda_available=true`, `service=immich-ml` y `status=ok`. |
| `GET /healthz` | Misma comprobación real, utilizada por el healthcheck local. No hay salud estática ni caché. |
| `POST /predict` | Copia multipart por bloques de hasta 64 KiB, sin analizar `entries`, imágenes ni texto. Sustituye la autorización entrante por el Bearer propio. Preserva estado HTTP y bytes de la respuesta, incluidas las cadenas JSON de embeddings originales. |
| Otras rutas, consultas, URL absolutas o métodos | Rechazo explícito sin contactar el upstream. |

El cliente fijado sólo evalúa `response.ok` en `/ping`; no compara con `pong`. Sus peticiones actuales usan `FormData` con texto o un Blob de tamaño conocido. La prueba Node real comprueba este transporte. Se exige exactamente un `Content-Length` decimal en POST y un único `Content-Type: multipart/form-data; boundary=...`; se rechazan `Transfer-Encoding`, longitudes duplicadas y `Expect`. No se admite upload chunked en esta unidad: cambiar el cliente requiere ampliar y probar ese contrato explícitamente.

Se reenvían únicamente el Content-Type necesario, la longitud reconstruida y la autorización propia. Las cabeceras entrantes de identidad o destino no se propagan. La respuesta conserva cuerpo/estado y Content-Type, con longitud reconstruida; no propaga `Location`, cookies ni otras cabeceras upstream. Un 3xx conserva su estado y cuerpo sin seguirlo. Un fallo de transporte o respuesta excesiva produce 502; agotar el plazo produce 504; la concurrencia de inferencia agotada produce 429.

## Límites y desconexión

| Recurso | Tope |
|---|---|
| Entrada multipart | 32 MiB; se rechaza antes de enviar si la longitud declarada excede el tope. |
| Respuesta de inferencia | 8 MiB, leída por bloques y retenida en memoria acotada antes de responder. Evita entregar un cuerpo parcial con un estado de éxito. |
| Respuesta de salud | 64 KiB. |
| Línea / conjunto de cabeceras entrantes | 4 KiB / 16 KiB. |
| Conexiones atendidas / cola TCP | 8 / 8; al agotar conexiones se devuelve 503 sin crear otro hilo. |
| Predicciones simultáneas | 2, sin cola de inferencia dentro del proxy. |
| Cabeceras / salud / inferencia | 5 s / 5 s / 120 s de plazo total. |
| Inactividad de lectura del cliente | 5 s; conexión inicial upstream hasta 3 s dentro del plazo total. |

Hay una petición por conexión y cierre explícito, sin keep-alive ni pipelining reutilizable. La vigilancia comprueba desconexión cada 25 ms después del upload y conserva el socket upstream incluso cuando `HTTPConnection` lo transfiere al objeto de respuesta. El plazo no se renueva por recibir bytes lentamente. La desconexión durante upload interrumpe su lectura; al terminar se cierran respuesta, transporte y vigilancia, y se libera el slot. Cerrar HTTP no garantiza detener una inferencia CUDA que el proceso remoto ya haya empezado: su semáforo y su política de cancelación siguen siendo responsabilidad del puente.

## Secreto y futura integración

El proceso lee únicamente `/run/secrets/gpu_bridge_bearer`, o la ruta absoluta indicada mediante `--secret-file`. No lee la bóveda maestra ni recibe el token en variables, argumentos o registros. Falla antes de escuchar si el archivo falta, no es regular, es un enlace, excede 4096 bytes o contiene un token inválido. Se permite un salto de línea final. La rotación exige sustituir el secreto y recrear el servicio después de respaldar la bóveda.

El responsable debe respaldar e inyectar el token existente antes del despliegue. El archivo debe ser legible por UID/GID `65532:65532`, con permisos privados compatibles con Docker Secret. Mantener el proxy sólo en la red privada del servidor Immich, sin sección `ports`, sin red de host ni acceso desde las aplicaciones ajenas. Esa red debe permitir la salida LAN hacia el destino fijo. Configurar la URL ML del servidor como `http://immich-ml-auth:3003`; el secreto se monta sólo en el proxy.

La integración Compose deberá usar usuario sin privilegios, sistema de archivos de sólo lectura, `cap_drop: [ALL]`, `no-new-privileges`, límites de memoria/CPU/PIDs y ninguna ruta de Docker/host montada. La biblioteca estándar HTTP tiene comprobaciones básicas y no es un servidor para exposición pública; este adaptador implementa límites adicionales para el único protocolo privado. No publicarlo mediante ingress ni proxy inverso de clientes.

## Runtime y comprobaciones

El Dockerfile propone la imagen oficial `python:3.12.16-alpine3.24` fijada al índice OCI `sha256:213242b537a1c166a0c53e903461fc18d5518e7196c93dbecda452660a848918`. Se verificó `linux/amd64`, manifiesto `sha256:a61cbd76a882645de7f91423329fe43a67eed60d7da24f9e5331414bb3618b20`, mediante Registry API anónima el 2026-11-06. La unidad sólo usa biblioteca estándar y no requiere glibc ni ruedas nativas adicionales; el cambio a musl debe confirmarse en la prueba Linux del responsable. No hay paquetes pip adicionales ni comandos Git en esta unidad.

Trivy 0.76.2 escaneó las dos bases por registro remoto, sin Docker ni contenedores, con umbral bloqueante HIGH/CRITICAL y sin ignorar hallazgos. La base Bookworm anterior produjo **37 HIGH y 2 CRITICAL** en 105 paquetes Debian, por lo que se descartó. La alternativa fijada detectó Alpine **3.24.4**, **38 paquetes del sistema y un paquete Python**, con **cero HIGH/CRITICAL** y salida 0. Los informes conservan tanto paquetes de sistema como bibliotecas; no se excluyó Python del análisis. La base de vulnerabilidades estaba actualizada el `2026-11-05T13:09:41Z`, con próxima actualización `2026-11-06T13:09:41Z`, posterior al escaneo.

Los reportes locales son `.tools/evidence/phase4-immich-ml-auth-bookworm-trivy.json` y `.tools/evidence/phase4-immich-ml-auth-alpine-trivy.json`, con logs y recibos de salida por variante. El reporte Alpine tiene SHA256 `db4d46a649bbbf3473099f6d7ea0b698e6d572b500c70f1d3b7331000b6eb51c`. El resultado acredita el umbral y los paquetes detectados en esa consulta, no ausencia absoluta de vulnerabilidades ni ejecución del intérprete Linux. El responsable debe respaldar el pin antes de usarlo, construir y auditar también la imagen propia, y comprobar en `.85` usuario, secreto, salud y protocolo real. La aceptación posterior construyó la imagen propia; el resultado operativo se registra a continuación y no convierte el escaneo inicial de base en una prueba de aplicación completa.

Para repetir la auditoría remota desde la raíz Windows, con caché en la unidad del workspace:

```powershell
& .tools/bin/trivy.exe image --image-src remote --platform linux/amd64 --cache-dir .tools/trivy-cache --scanners vuln --severity HIGH,CRITICAL --exit-code 1 --format json --output .tools/evidence/phase4-immich-ml-auth-alpine-trivy.json --timeout 5m docker.io/library/python@sha256:213242b537a1c166a0c53e903461fc18d5518e7196c93dbecda452660a848918
```

Desde la raíz, ejecutar los contratos locales sin GPU ni laboratorio:

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -p test_immich_ml_auth_proxy.py -v
python -m ruff check docker/immich-ml-auth/proxy.py tests/test_immich_ml_auth_proxy.py
```

Las pruebas levantan sockets loopback propios con un backend controlado y comprueban bytes/status, autorización sustituida, salud real, rechazo de rutas/métodos/tamaños/framing, plazos y cancelación durante cabeceras y cuerpo de respuesta. Incluyen `fetch(FormData)` del Node instalado. Nunca contactan `.6` ni utilizan secretos reales. La imagen propia ya pasó build, SCA y petición real desde un cliente aislado en `.85`. Evidencias privadas: `phase4-immich-ml-auth-build-result.json`, `phase4-built-images-sca-receipt.json`, `phase4-immich-ml-auth-runtime-acceptance.json` y `phase4-immich-ml-auth-acceptance-stopped.json`. Falta conectar Immich server con CLIP/rostros según ADR-003 y OCR deshabilitado; los tests del proxy no acreditan precisión facial ni cambian modelos.

Fuentes primarias: [cliente ML fijado de Immich](https://github.com/immich-app/immich/blob/587f549f46f1cd1822a06e16dcccecef04ea4565/server/src/repositories/machine-learning.repository.ts), [cliente HTTP estándar](https://docs.python.org/3.12/library/http.client.html), [servidor HTTP y límites de uso](https://docs.python.org/3.12/library/http.server.html), [Docker Secret por servicio](https://docs.docker.com/compose/how-tos/use-secrets/), [Python 3.12.16 de seguridad](https://blog.python.org/2026/10/python-3152-31410-31318-31216-31118/), [imagen Python oficial y variantes Alpine](https://hub.docker.com/_/python) y [análisis remoto de imágenes con Trivy](https://trivy.dev/docs/latest/target/container_image/).
