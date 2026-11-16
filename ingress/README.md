# Ingreso Synology para la Fase 6

El renderizador prepara tres hosts nuevos para el plano de datos del NAS `192.168.0.8`. En el inventario activo, el conector Cloudflare llega por socket Unix al relay privado de `.85`. El relay presenta su certificado cliente a BunkerWeb `9443`. BunkerWeb autentica con otro certificado a Nginx Proxy Manager (NPM) `nginx-proxy-manager:443` y NPM conecta al servicio LAN. El alias Docker resolvió desde BunkerWeb a la IP dinámica `192.168.48.4` en `monitoring-stack_monitoring_network`. La API privada de BunkerWeb `1.6.9` escucha en `bw-api:8888` en `bw-universe`. Las interfaces de administración son `.8:7000` para BunkerWeb y `.8:81` para NPM `2.13.5`. [Relay de origen](README-origin-relay.md)

| Host público | Servicio LAN | Alcance |
|---|---|---|
| `api-studio.e-scale.cl` | `http://192.168.0.85:3010` | `POST /v1/sync` exacto, 4096 bytes, HMAC del consumidor R2. `GET /healthz` sigue en LAN. |
| `api-studio.e-scale.cl` | HTTPS al listener dedicado `192.168.0.7:<puerto confirmado>` | Opcional. `POST /v1/sofia/chat` exacto, 12288 bytes, HMAC propio. Con `sofia_tls: null` ambos proxies devuelven `404` y omiten este upstream. El proxy dedicado delega al broker en loopback `127.0.0.1:3107`. |
| `kitsu.e-scale.cl` | `http://192.168.0.85:8081` | Aplicación en `/`, incluida API y WebSocket `/socket.io/`. |
| `immich.e-scale.cl` | `http://192.168.0.85:2283` | Aplicación en `/`, incluido `/.well-known/immich`, API y WebSocket. |

La autenticación de Kitsu e Immich permanece en sus aplicaciones. El límite propuesto en cada proxy para ambos hosts es 128 MiB por solicitud, incluida la sobrecarga multipart. Verificar los límites reales de Cloudflare y del backend antes de admitir cargas de clientes. Mantener las cargas mayores en los flujos LAN o R2 que correspondan. HTTP hacia `.85` viaja sólo por LAN y queda sujeto a comprobación de alcance antes de activar. [Reverse proxy de Immich](https://docs.immich.app/administration/reverse-proxy/) · [Tamaño máximo de carga de Cloudflare](https://developers.cloudflare.com/network/maximum-upload-size/).

## Generar la configuración

```text
python ingress/phase6_ingress.py
python ingress/phase6_ingress.py --inventory inventario-privado.json
python -m pytest -q tests/test_phase6_ingress.py
```

La primera orden lista precondiciones. La segunda lee un único JSON local de hasta 4096 bytes y entrega los fragmentos por stdout. `candidate_rendered` indica generación local. `activation_verified` indica si el propio renderizador acredita el estado operativo y siempre se emite como `false`. El inventario usa sólo metadatos de la revisión previa.

| Campo | Valor requerido |
|---|---|
| `schema` | Entero `2` para el relay con mTLS. El esquema `1` conserva el contrato anterior de conexión directa. |
| `observed_bunkerweb_version` | `1.6.9` comprobada sobre el NAS. |
| `observed_npm_image_id` | ID NPM instalado `sha256:90522c2d426d9b2a7ae02f60365e812e1990a0dd78870ceaf60186b05e05385b`. Cotejar el contenedor y confirmar que la API devuelve `2.13.5`. |
| `preflight_sha256` | SHA256 del recibo privado de revisión del NAS y de sus orígenes, sin prefijo. El renderizador valida formato. El operador coteja la huella con el archivo. |
| `tunnel_peer_ipv4` | IP privada literal. Usar el peer que observa BunkerWeb. En el esquema `2` Docker presenta el gateway `10.20.30.1` y el vhost exige también el certificado dedicado al relay. |
| `origin_client_auth` | En el esquema `2`, el valor fijo `studio-origin-relay-v1` activa la CA cliente de BunkerWeb. |
| `sofia_tls` | `null` para el alta inicial o un objeto exacto con `ipv4`, `port`, `server_name` tras verificar mTLS. IP `192.168.0.7`, puerto dedicado confirmado y SAN DNS exacto. Los puertos `3107`, `3108` y `18789` quedan excluidos. |

En BunkerWeb multisite, `service_draft_payload` corresponde a `POST /services` y `custom_config_payload` a `POST /configs`. Preparar cada servicio como borrador por la API privada autenticada o por la consola Services y Configs. Tras guardar, cotejar `GET /services/<host>` y `GET /configs/<host>/server_http/agency-phase6?with_data=true`. Cada `server_http_path` identifica la configuración generada. El fragmento de ubicación preserva la URI y valida la cadena del servidor NPM con SNI del host pedido. El resolver Docker se configura sólo en las ubicaciones nuevas. Mantener ModSecurity y CRS en bloqueo. El render desactiva el registro de auditoría de ModSecurity en estos hosts porque sus formatos habituales pueden contener cabeceras, datos de sesión y cuerpos. Revisar también los registros de errores y de reglas de bloqueo con datos sintéticos antes de enviar credenciales. [API BunkerWeb 1.6.9](https://raw.githubusercontent.com/bunkerity/bunkerweb/v1.6.9/docs/api.md) · [Plantilla de proxy](https://raw.githubusercontent.com/bunkerity/bunkerweb/v1.6.9/src/common/core/reverseproxy/confs/server-http/reverse-proxy.conf) · [Configuración ModSecurity](https://raw.githubusercontent.com/bunkerity/bunkerweb/v1.6.9/src/common/core/modsecurity/plugin.json).

En NPM, crear cada Proxy Host con `POST /api/nginx/proxy-hosts`, el bloque `npm` y el `certificate_id` real. `enabled: false` conserva la fila deshabilitada hasta la prueba. La consola SSL permite importar el par como Custom. Para automatizar por canal SSH privado a `127.0.0.1:81`, `POST /api/tokens` recibe `identity` y `secret`, y la sesión usa Bearer. `POST /api/nginx/certificates` recibe `provider: other`, `nice_name` propio y `domain_names: [host]`. Tras respaldar el ID, `/api/nginx/certificates/<id>/upload` recibe multipart `certificate`, `certificate_key` y, si corresponde, `intermediate_certificate`. Procesar sólo el estado de la respuesta de subida porque incluye los PEM privados. [API NPM 2.13.5](https://raw.githubusercontent.com/NginxProxyManager/nginx-proxy-manager/v2.13.5/backend/schema/swagger.json) · [Implementación de carga y persistencia del certificado](https://raw.githubusercontent.com/NginxProxyManager/nginx-proxy-manager/v2.13.5/backend/internal/certificate.js).

`advanced_config` gestiona WebSocket y las ubicaciones. Verificar sobre la imagen instalada que el bloque avanzado reemplaza la ubicación `/` predeterminada. El destino de formulario `127.0.0.1:1` sólo sirve como valor de reserva. Verificar que ese puerto carece de listener y que los upstreams LAN aparecen exclusivamente en los bloques avanzados. Comprobar que el Nginx efectivo del host API sólo enruta los POST admitidos. [Plantilla Proxy Host de NPM 2.13.5](https://raw.githubusercontent.com/NginxProxyManager/nginx-proxy-manager/v2.13.5/backend/templates/proxy_host.conf) · [Generador de NPM 2.13.5](https://raw.githubusercontent.com/NginxProxyManager/nginx-proxy-manager/v2.13.5/backend/internal/nginx.js).

`cloudflared_origin` es una entrada candidata por host, para incorporación individual por el titular del túnel. Incluye HTTPS, `httpHostHeader`, `originServerName` y verificación TLS. El conector debe alcanzar `.8:9443` y usar HTTP/1.1 con `Content-Length` fijo para los POST firmados. BunkerWeb acepta `CF-Connecting-IP` únicamente a través del peer TCP inventariado. Probar que un cliente LAN ajeno no puede usar el mismo peer aparente o falsificar la cabecera. [Parámetros de origen de Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/).

## Material TLS y restricciones de red

El responsable prepara los PEM y fragmentos en `/volume2/agency-studio-ingress-private-v2`, directorio nuevo de root `0700` verificado también por ACL DSM. Registrar marcador, identidades, caducidades y hashes. Copiar exclusivamente los materiales de cada proxy a sus directorios propios sin sobrescribir archivos. Acreditar ACL DSM y quién puede reemplazar un hijo antes de instalar o usar los archivos. Si el padre permite que otro usuario sustituya el hijo, el modo del subdirectorio por sí solo no protege la integridad. Las claves fuente quedan en la bóveda.

El API interno reemplaza el contenido de `/etc/bunkerweb/configs` y `/var/cache/bunkerweb` durante la reconciliación. La plantilla refiere a los binds independientes `/volume1/docker/bunkerweb/data/storage/agency-phase6` del scheduler y `/volume1/docker/bunkerweb/data/logs/agency-phase6` del proxy. Ambos procesos usan `101:101`. Acreditar un directorio `0750` de `root:101`, archivos `0440` y exclusión del subdirectorio de cualquier recopilador o rotación de logs. El bind `/volume1/docker/npm/data` se ve como `/data`. Colocar sus materiales operativos en `agency-phase6` con acceso restringido. Para cada certificado `other`, verificar que `/volume1/docker/npm/data/custom_ssl/npm-<id>` sea nuevo y tenga acceso restringido antes de cargar la clave, porque NPM 2.13.5 usa el modo por defecto al escribir el PEM. Confirmar owner/modo y legibilidad real por proceso, CA, CN, SAN, vigencia y correspondencia clave/par. Los certificados de BunkerWeb deben tener más de un día de vigencia para superar la validación del scheduler. [Validador `custom-cert` de BunkerWeb](https://raw.githubusercontent.com/bunkerity/bunkerweb/v1.6.9/src/common/core/customcert/jobs/custom-cert.py).

| Enlace | Material esperado |
|---|---|
| Conector → BunkerWeb | CA de servidor `/etc/agency-studio-tunnel-public/bunkerweb-server-ca.pem` en `.85`, legible para el conector. `<host>.pem` y `<host>.key` en `/data/agency-phase6` del scheduler. Un SAN por host público. |
| BunkerWeb → NPM | `npm-server-ca.pem`, `npm-client.pem`, `npm-client.key` e includes emitidos en `/var/log/bunkerweb/agency-phase6` del proxy. En NPM, certificado de servidor por host y CA cliente `/data/agency-phase6/bunkerweb-client-ca.pem`, dedicada al cliente BunkerWeb. |
| NPM → Sofía, cuando se configure | `sofia-server-ca.pem`, `sofia-client.pem` y `sofia-client.key` en `/data/agency-phase6`. El listener dedicado en `.7` acepta el SAN cliente y CA acordados para NPM, restringe el acceso a `.8` y sirve el SAN DNS inventariado. NPM usa ese DNS como SNI y `Host`, y conserva el host público en `X-Forwarded-Host`. |

Limitar los tres vhosts nuevos en `.8:9443` al peer del conector y los vhosts NPM al cliente mTLS de BunkerWeb. El salto BunkerWeb → NPM usa el bridge y el puerto interno `443`. NPM alcanza `.85:3010/8081/2283` por LAN y sólo usa `.7` si se configura el puerto dedicado. Mantener las consolas y puertos LAN fuera de los orígenes publicados.

## Comprobaciones y reversión

1. Cotejar alias DNS, CID, imágenes, ACL y ausencia de colisiones de nombres. Confirmar el peer observado por BunkerWeb para el conector de `.85`. La lectura desde NPM confirmó los tres upstreams `.85`. Guardar preimágenes de entradas administradas y huellas de los archivos y CIDs de terceros.
2. Preparar borradores y certificados. Mantener el túnel sin las rutas nuevas. Habilitar por ID cada host nuevo NPM con `POST /api/nginx/proxy-hosts/<id>/enable`, luego convertir sólo su borrador BunkerWeb por `POST /services/<host>/convert?convert_to=online`. El generador NPM prueba `nginx -t` antes de `nginx -s reload`. La recarga de BunkerWeb queda a cargo de su scheduler. Leer la configuración efectiva `nginx -T` de forma privada, revisar `meta.nginx_online` de NPM y cotejar ambos CIDs. Probar TLS, restricción de peer, mTLS, SNI/Host y los controles de terceros antes de abrir el túnel. Revisar los controles WAF y las IP de confianza heredadas. [Rutas NPM 2.13.5](https://raw.githubusercontent.com/NginxProxyManager/nginx-proxy-manager/v2.13.5/backend/routes/nginx/proxy_hosts.js).
3. Para el host máquina, comprobar health LAN y `404` para health público, raíz, sufijos y consultas. Con `sofia_tls: null`, comprobar también `404` en el POST exacto de Sofía. Para rutas habilitadas, probar métodos incorrectos, framing inválido, sobrepeso y la respuesta `401` del backend con JSON sintético sin firma. Una fixture autorizada debe comprobar conservación de `Content-Length`, `timestamp`, `signature` y bytes firmados, rechazo de duplicados, ausencia de reintentos y de secretos en registros. Si Sofía se añade después, probar también su respuesta firmada.
4. Para Kitsu e Immich comprobar login, ruta de descubrimiento Immich, reproducción, API y WebSocket con fixture de agencia. Comprobar reglas de caché de Cloudflare con sesiones. Publicar las tres rutas individualmente tras las pruebas de origen. Ante fallo, retirar las rutas y DNS nuevos, devolver sólo los servicios BunkerWeb nuevos a `draft` con `?convert_to=draft` y deshabilitar los IDs nuevos NPM con `POST /api/nginx/proxy-hosts/<id>/disable`. Confirmar la recarga y cotejar los controles previos. Conservar certificados y preimágenes mientras se concilia un resultado ambiguo.

El 14 de noviembre se verificaron el socket Unix, los certificados, la recarga de BunkerWeb y la ruta pública de sincronización. El primer video del portal llegó al NAS por el consumidor firmado. Las raíces públicas devolvieron 200 y las APIs restringidas de Kitsu e Immich devolvieron 200 con credencial y 401 sin ella. Quedan las pruebas públicas de reproducción y WebSocket y el listener de Sofía. [Sonda de API](../.tools/evidence/phase6-public-sync-origin-verified-20261114-v1.json) · [Lectura del NAS](../.tools/evidence/phase6-first-portal-video-nas-readback-20261114-v1.json) · [Sondas de aplicaciones](../.tools/evidence/phase6-kitsu-immich-public-auth-20261114-v1.json)

## Operador de NPM

`python3 -I -S -B npm_phase6_operator.py plan` muestra los modos sin acceder a NPM. El operador se ejecuta en el NAS con el renderizador fijado al lado y habla sólo con `127.0.0.1:81`. Cada orden consume un JSON por stdin con `host` y `token`, y los campos del modo indicado. Conservar el ID y `row_sha256` de cada salida saneada en la bóveda antes de la orden que los requiere.

| Modo | Campos adicionales y efecto |
|---|---|
| `inspect` | Lista las identidades y huellas saneadas del host solicitado para conciliación. |
| `certificate-create` | Reserva un registro `other` con nombre `agency-phase6-<host>`. |
| `certificate-upload` | `certificate_id`, `certificate_preimage_sha256`, `certificate`, `certificate_key` y `intermediate_certificate` opcional. Requiere el directorio `custom_ssl/npm-<id>` vacío, `root:root` `0700` y su ACL privada verificada antes de ejecutar. |
| `host-create` | `certificate_id`, `certificate_preimage_sha256` de la fila con materiales e `inventory` del renderizador. Guarda el Proxy Host deshabilitado. |
| `host-enable` y `host-disable` | `host_id`, `host_preimage_sha256`. Verifican la fila antes y después. La habilitación exige `meta.nginx_online=true`. |

Ante error o timeout, conciliar filas, archivos y estado real antes de repetir. Preparar y activar los hosts en serie bajo un único operador. La activación por API de NPM es una recarga y queda pendiente validar TLS, rutas HTTP, WebSocket y controles de terceros por separado.
