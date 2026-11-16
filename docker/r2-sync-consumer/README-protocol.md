# Protocolo R2 v1: contrato puro

Estado: contrato implementado y probado localmente. No hay servidor HTTP, descarga S3, acceso al filesystem, contenedor ni transferencia desplegada en esta unidad. La fase declarada sigue en 3 y CD permanece deshabilitado.

El módulo `src/media/sync-contract.ts` prepara validación y WebCrypto portables para el productor Worker y clientes Node. Las pruebas están en `tests-ts/sync-contract.test.ts`. No importa módulos de Node ni mantiene estado de replay/idempotencia en memoria. El consumidor Linux usa Python estándar para acceder a `dir_fd`, `linkat` y `flock`: su [protocolo interoperable](README-protocol-python.md), [publicador](README-storage.md) y [ledger local](README-ledger.md) conservan estos mismos bytes y reglas. Sus aceptaciones individuales no acreditan aún un servidor integrado.

## Mensaje y rutas derivadas

El cuerpo admite exactamente siete campos, en este orden:

| Campo | Restricción |
| --- | --- |
| job_id | UUIDv4 lowercase, con variante RFC. |
| asset_id | UUIDv4 lowercase, con variante RFC. |
| project_slug | 1..63 caracteres ASCII; primero `[a-z0-9]`, resto `[a-z0-9_-]`. Rechaza dispositivos DOS completos CON/PRN/AUX/NUL/COM1..9/LPT1..9, además de puntos, separadores y caracteres fuera del alfabeto. |
| extension | Token lowercase alfanumérico de 1..10 caracteres, sin punto. |
| bytes | Entero positivo seguro para JavaScript y menor o igual a `maxObjectBytes`. |
| sha256 | Hexadecimal lowercase de 64 caracteres, checksum esperado del objeto. |
| object_key | Exactamente `ingest/<job_id>/<asset_id>.<extension>`. |

`SyncPolicy.maxObjectBytes` es obligatorio, entero positivo seguro. No existe límite predeterminado ni fallback ante configuración ausente o inválida. Esa política es operativa; no introduce cuotas comerciales.

El destino se deriva exclusivamente como `proyectos/<project_slug>/01_raw/<asset_id>.<extension>`. Ninguna petición puede proporcionar URL, bucket, directorio absoluto, nombre final alternativo, comando ni otro campo. El resultado contiene una ruta **relativa**, que todavía requiere comprobar el montaje y los descriptores de directorio antes de usarla.

## JSON canónico y bytes

El cuerpo máximo es 4096 bytes. El productor usa `encodeSyncJob`; no construye JSON a mano. La representación es JSON UTF8 sin BOM, espacios, saltos finales ni campos adicionales, con el orden de la tabla y números decimales producidos por JSON.stringify. Todos los valores de texto del esquema son ASCII.

`parseSyncBody` decodifica UTF8 de forma estricta, valida el esquema y vuelve a codificar. Exige igualdad de cada byte con el original. Esto rechaza claves duplicadas, orden alternativo, escapes Unicode equivalentes, espacios, exponentes o fracciones que representen el mismo entero, JSON truncado y UTF8 inválido. No normaliza una representación no canónica para aceptarla. En objetos productores también rechaza accesores, sin ejecutar getters de los campos.

## HMAC y reloj

La operación firmada está fijada a `POST /v1/sync`. Las cabeceras del contrato son `timestamp` y `signature`; el futuro adaptador HTTP debe rechazar sus duplicados, otros métodos/rutas y leer el cuerpo acotado antes de llamar al módulo.

`timestamp` es una cadena decimal canónica de segundos Unix: cero o entero positivo sin ceros iniciales, signo, fracción, exponente ni whitespace. Debe ser entero seguro. El receptor recibe una función `nowSeconds` explícita que devuelve segundos Unix enteros; admite una diferencia absoluta de **hasta 300 segundos**, incluidos ambos extremos. Un reloj inválido o que falla rechaza la petición.

El mensaje HMAC usa saltos LF y no tiene salto final:

```text
v1\n<timestamp>\nPOST\n/v1/sync\n<sha256-lowercase-del-cuerpo-canónico>
```

La clave contiene 32..128 bytes y se importa como HMAC/SHA-256 **no extraíble**. El formato de `signature` es `v1=` seguido de 64 caracteres hexadecimales lowercase. Los valores válidos se verifican con `crypto.subtle.verify`; no se calcula y compara un MAC mediante un bucle que termine en el primer byte distinto. El cuerpo público sí se compara byte a byte para la canonicalización. El módulo toma una copia antes del primer await para que una mutación posterior del cuerpo/cabeceras no cambie el mensaje autenticado.

Se utiliza el contrato WebCrypto documentado por [Cloudflare Workers](https://developers.cloudflare.com/workers/runtime-apis/web-crypto/) y [Node.js](https://nodejs.org/docs/latest-v24.x/api/webcrypto.html). La prueba real local usa Node 24.13.0; no se ha ejecutado un Worker remoto. No se genera una clave operativa ni se lee la bóveda en este módulo. El lanzador futuro recibirá exclusivamente la clave ya respaldada por raíz.

## Interfaz de uso

| Función | Resultado |
| --- | --- |
| validateSyncJob(value, policy) | Trabajo validado y congelado. |
| encodeSyncJob(value, policy) | Uint8Array con el cuerpo canónico. |
| parseSyncBody(body, policy) | Trabajo congelado, sólo si los bytes ya son canónicos. |
| importSyncKey(raw) | CryptoKey HMAC/SHA-256 no extraíble. |
| signSyncJob(value, policy, key, timestamp) | body, timestamp, signature y bodySha256. |
| verifySyncRequest(request, policy, key, nowSeconds) | job, bodySha256 y destinationRelativePath, en un resultado congelado. |

Los errores `SyncContractError` contienen un código y un mensaje fijo, sin devolver clave, cabeceras ni contenido de una excepción criptográfica. El router HTTP y sus estados aún no están implementados.

`bodySha256` es la huella para la **idempotencia persistente futura**; no es una afirmación de que el archivo se descargó o publicó. Un reintento conserva exactamente el cuerpo/hash y renueva timestamp/firma. La ventana temporal no impide repetir una petición válida dentro de ella. El siguiente incremento debe guardar de forma durable job_id, hash y estado: mismo job/hash se reconcilia; mismo job con hash distinto se rechaza; una caída no debe perder ese registro. No se sustituirá por un Set o diccionario de proceso.

## Verificación de esta unidad

Las pruebas incluyen un vector HMAC conocido calculado independientemente con node:crypto, MAC/clave/cuerpo alterados, extremos de reloj, cabeceras no canónicas, UTF8/BOM, duplicados, campos/rutas peligrosos, máximo de cuerpo, política requerida, claves no extraíbles, fallo criptográfico y mutación durante la operación. Las claves y UUID del fixture son datos públicos de prueba.

Receta local, sin npx:

```powershell
npm run lint
npm run typecheck
npm test
python scripts/ci/check_docs.py
```

## Próximo incremento: transferencia y publicación

Antes de acceder a datos reales se verificará la identidad de .85 y el montaje exacto `/mnt/producciones`, fuente `192.168.0.8:/volume2/producciones`, NFS4.1 rw. La ruta relativa no basta: hay que comprobar propiedad y ancestros sin symlinks, conservar descriptores de directorio y rechazar montajes/rutas ajenos. No habrá chmod/chown recursivo sobre el share.

La descarga usará el endpoint y bucket R2 configurados fuera de la petición y autenticación S3; no aceptará URL pública/firmada o destino suministrado por el cliente. El cliente debe acotar tamaño, plazo, concurrencia y cancelación, contar bytes recibidos y comprobar SHA256 real contra el trabajo. Content-Length o metadatos de HEAD no sustituyen esa comprobación. No se seguirán redirecciones hacia otro destino ni se registrarán credenciales.

Se escribirá sólo un temporal nuevo y propio, se verificará/fsync y se publicará atómicamente **sin sobrescribir**. Deben comprobarse las garantías efectivas sobre Linux/NFS; no se usará un rename que reemplace silenciosamente un destino existente. Una colisión requiere verificar identidad/hash y reconciliar el estado durable. La limpieza sólo podrá retirar temporales acreditados de esta operación.

Después vendrán pruebas HTTP controladas, almacenamiento en fixtures, reinicio/concurrencia/idempotencia y, finalmente, una transferencia real autorizada con recibo de checksum y publicación. Este documento no acredita esas etapas ni el servicio `3010` desplegado.
