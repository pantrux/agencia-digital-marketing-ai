# Descriptor R2 S3 SigV4 para GET y HEAD

Estado: módulo puro implementado en `src/media/r2-signing.ts`, con pruebas en `tests-ts/r2-signing.test.ts`. No realiza fetch, descarga, acceso al disco ni lectura del entorno. No añade SDK o dependencias. La aceptación contra R2 real, el streaming y el servicio HTTP siguen pendientes; la fase declarada permanece en 3 y CD deshabilitado.

## Interfaz y destino fijo

```typescript
signR2Request(method, job, policy, credentials, amzDate)
```

- method: únicamente `GET` o `HEAD`, con esas mayúsculas exactas.
- job: se revalida mediante `validateSyncJob` del contrato congelado; conserva su esquema cerrado y object_key derivada.
- policy: `maxObjectBytes` obligatorio y válido. No hay fallback.
- credentials: registro cerrado `{ accountId, accessKeyId, secretAccessKey }`, recibido por argumento. No se ejecutan getters de sus campos.
- amzDate: cadena UTC exacta `YYYYMMDDTHHmmssZ`, con año 0001..9999 y fecha/hora reales. Rechaza días inexistentes, normalizaciones, offsets, fracciones y saltos finales.

El único host construido es `<accountId>.r2.cloudflarestorage.com`, con accountId de 32 hexadecimales lowercase. El bucket literal es **studio-media-bucket**. La URL tiene la forma:

```text
https://<accountId>.r2.cloudflarestorage.com/studio-media-bucket/ingest/<job_id>/<asset_id>.<extension>
```

No existe argumento para elegir URL, host, bucket, región, query string o cabeceras adicionales. El alfabeto permitido de object_key deja sólo caracteres no reservados y los separadores fijos; no acepta escapes, recorrido de rutas ni claves arbitrarias. Los parámetros y campos se capturan antes del primer await.

accessKeyId admite 1..128 caracteres ASCII alfanuméricos. secretAccessKey admite 1..256 caracteres ASCII visibles sin espacios ni controles; no se recorta ni normaliza. Son comprobaciones de sintaxis, no una afirmación de que las credenciales existan o tengan permisos. El lanzador futuro recibirá únicamente las credenciales operativas previamente respaldadas por raíz; este módulo no genera claves, consulta la bóveda ni las persiste.

## Firma y resultado

R2 utiliza el endpoint por cuenta y región `auto`, según su [contrato S3 oficial](https://developers.cloudflare.com/r2/api/s3/api/). La firma sigue la [construcción SigV4 de AWS](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html), con servicio `s3`, scope `<YYYYMMDD>/auto/s3/aws4_request` y fecha del scope extraída del mismo amzDate validado.

GET y HEAD tienen payload vacío. Su SHA256 es:

```text
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

No se usa el checksum del archivo como hash de la petición: el objeto se espera en la respuesta. Los headers producidos son Host, x-amz-date, x-amz-content-sha256 y Authorization. Los tres primeros se canonizan en lowercase y en orden `host;x-amz-content-sha256;x-amz-date`, con query vacía y los saltos LF requeridos. Authorization contiene algoritmo AWS4-HMAC-SHA256, accessKeyId/scope, SignedHeaders y firma hexadecimal lowercase.

La derivación aplica HMAC/SHA-256 sucesivamente a fecha, auto, s3 y aws4_request. Cada clave se importa mediante WebCrypto como no extraíble, con uso de firma. Sólo se limpian los buffers temporales controlados tras utilizarlos; **no se promete borrar completamente cadenas o copias internas de JavaScript/WebCrypto**. No se retorna el Secret Access Key, claves derivadas o material de derivación.

El resultado contiene method, url y headers, congelados tanto en tipos como en ejecución. `toJSON()` devuelve un diagnóstico con Authorization redactado. Esto **no autoriza registrar el descriptor ni sus headers**: console.inspect u otros loggers pueden eludir toJSON, y acceder a headers directamente revela la autorización necesaria para el transporte. No se deben registrar credenciales, Authorization ni firmas. Los errores propios usan únicamente códigos/mensajes fijos, sin incluir credenciales, firmas o excepciones privadas del proveedor criptográfico.

El módulo no consulta el reloj ni aplica frescura a amzDate. El transportador futuro debe suministrar la fecha UTC actual de su reloj confiable; la validación HMAC del mensaje `/v1/sync` y su ventana de ±300 segundos pertenecen al contrato anterior, no sustituyen el timestamp de SigV4.

## Vectores independientes

Las pruebas TS contienen resultados fijos calculados primero con Python stdlib, sin ejecutar este módulo. Fixture público: accountId de 32 unos, accessKeyId de 32 letras a, Secret Access Key de 64 letras b, fecha `20261106T120000Z` y clave:

```text
ingest/1e0b3af5-2f67-4bf0-8a20-4b7d0b1c2d3e/2f1c4b06-3a78-4ca1-9b31-5c8e1c2d3e4f.mp4
```

| Método | SHA256 de petición canónica | Firma |
| --- | --- | --- |
| GET | `51f07d2cb6daece3f6d0fdde67871f221e33ba1c69ec76e4dbb9ff58fcb56124` | `787cda65a9f2408e067b767830920a7e49625f2a3beb971b3c945807150b654c` |
| HEAD | `4acd9085a6eb3591a40be25a0b0e5eaad3481314c6794c62b4219ee0ec25963b` | `64ab793e8654ceecc257af080e852d636b8a6a0cb92d16a648ca124c771e1949` |

Reproducción independiente, sólo con datos de prueba:

```python
import hashlib
import hmac

host = "1" * 32 + ".r2.cloudflarestorage.com"
key_name = "ingest/1e0b3af5-2f67-4bf0-8a20-4b7d0b1c2d3e/2f1c4b06-3a78-4ca1-9b31-5c8e1c2d3e4f.mp4"
stamp, day = "20261106T120000Z", "20261106"
empty = hashlib.sha256(b"").hexdigest()
names = "host;x-amz-content-sha256;x-amz-date"
scope = day + "/auto/s3/aws4_request"
for method in ("GET", "HEAD"):
    headers = f"host:{host}\nx-amz-content-sha256:{empty}\nx-amz-date:{stamp}\n"
    canonical = "\n".join((method, "/studio-media-bucket/" + key_name, "", headers, names, empty))
    hashed = hashlib.sha256(canonical.encode()).hexdigest()
    message = "\n".join(("AWS4-HMAC-SHA256", stamp, scope, hashed))
    derived = ("AWS4" + "b" * 64).encode()
    for item in (day, "auto", "s3", "aws4_request"):
        derived = hmac.new(derived, item.encode(), hashlib.sha256).digest()
    signature = hmac.new(derived, message.encode(), hashlib.sha256).hexdigest()
    print(method, hashed, signature)
```

Además de ambos vectores, la suite comprueba fechas inválidas/bisiestas, vínculo del scope al día UTC, inyección de endpoint, object_key malformada, política requerida, credenciales inválidas, claves no extraíbles, mutación durante awaits, redacción y error criptográfico sin filtración. No contiene credenciales reales ni efectúa llamadas HTTP.

## Próximo incremento

El futuro transportador puede usar el descriptor para HEAD/GET sólo contra ese endpoint, sin reenviar la autorización HMAC entrante, seguir redirecciones ni aceptar URLs del cliente. Debe mantener método, URL y headers firmados; cualquier cabecera S3 adicional exige revisar explícitamente su canonicalización. Esta unidad no admite credenciales temporales/session token, firmas por query, Range ni otros métodos.

HEAD no acredita integridad del contenido. La descarga posterior tendrá tamaño, plazo, concurrencia y cancelación acotados; contará bytes y verificará SHA256 real del objeto contra el trabajo. El filesystem requiere el preflight NFS/rutas sin symlinks y publicación atómica sin sobrescritura del protocolo anterior, más idempotencia durable. Raíz realizará después la aceptación del descriptor con un objeto real y credenciales ya respaldadas, sin presentar estos tests como una transferencia desplegada.
