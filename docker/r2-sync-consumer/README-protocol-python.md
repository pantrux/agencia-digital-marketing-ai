# Verificación Python del protocolo R2 v1

[sync_protocol.py](sync_protocol.py) verifica en memoria el mismo cuerpo y mensaje HMAC que el [productor TypeScript](../../src/media/sync-contract.ts). Sólo usa Python estándar y la validación cerrada de [sync_storage.py](sync_storage.py). No abre sockets o archivos, consulta servicios, almacena claves ni mantiene un registro de solicitudes.

## API e identidad

```python
verified = verify_request(
    body,
    timestamp,
    signature,
    secret,
    max_object_bytes,
    now_seconds,
)
```

Todos los parámetros son obligatorios. `body` debe ser `bytes` inmutables y `secret` también, con una longitud de 32 a 128 bytes; no se aceptan strings, `bytearray`, conversiones de hex ni normalización automática. La política de tamaño de objeto se valida sin valor por defecto. `now_seconds` es una función de confianza que debe devolver segundos enteros entre cero y `2**53-1`; se rechazan booleanos, floats y errores del reloj.

El resultado `VerifiedRequest` es una dataclass congelada con `job` (`AssetJob` congelado) y `fingerprint` SHA-256 del cuerpo exacto. `body_sha256` es un alias del fingerprint y `destination_relative_path` deriva de los IDs/slug validados. No contiene secreto, firma ni cuerpo bruto. Para el futuro [ledger](README-ledger.md), `dataclasses.asdict(verified.job)` permite entregar una copia de los siete campos sin modificar el snapshot autenticado.

## Cuerpo canónico y firma

El cuerpo debe medir de 1 a 4096 bytes y decodificar como UTF-8 estricto. Se rechazan JSON truncado, BOM, claves duplicadas, anidación excesiva, constantes no JSON, floats/exponentes, campos adicionales, booleanos como tamaños y los demás valores inválidos del contrato. Los siete campos son exactamente `job_id`, `asset_id`, `project_slug`, `extension`, `bytes`, `sha256` y `object_key` en ese orden.

Tras validar con `storage.validate_job`, se vuelve a codificar el objeto sin espacios y se comparan los bytes exactos con el cuerpo recibido. Así se rechazan representaciones alternativas del mismo contenido: orden diferente, espacios, saltos finales, `12345.0`, `1.2345e4`, escapes Unicode innecesarios y barras escapadas. Los campos admitidos son ASCII, los tamaños son enteros seguros de JavaScript y esta recodificación coincide con `JSON.stringify` del productor; no se ofrece un canonicalizador JSON general.

El timestamp debe ser un string decimal canónico: `0` o un entero positivo sin signo ni ceros iniciales, con hasta 16 dígitos y valor máximo `2**53-1`. La distancia respecto del reloj debe ser como máximo **300 segundos, inclusive**. La firma debe ser exactamente `v1=` seguida de 64 caracteres hexadecimales en minúsculas.

El mensaje HMAC-SHA256 es UTF-8/ASCII y usa LF entre estas cinco líneas, sin salto final:

```text
v1
<timestamp>
POST
/v1/sync
<SHA256 hexadecimal del cuerpo exacto>
```

Método, ruta y versión son constantes del módulo. El MAC esperado y el recibido se comparan con `hmac.compare_digest` como dos valores de 32 bytes. Los errores son códigos fijos de `ProtocolError`, sin cuerpo, firma, secreto o diagnóstico del proveedor criptográfico.

## Relación con HTTP y persistencia

Las cabeceras congeladas del [contrato v1](README-protocol.md) se llaman literalmente **`timestamp` y `signature`**. Este módulo recibe sus valores ya separados; no interpreta cabeceras HTTP. El adaptador futuro debe rechazar duplicados, aplicar límites de framing/cuerpo y exponer únicamente el método/ruta fijados antes de llamar a esta función. Tampoco decide cómo se obtiene o rota el secreto.

Una solicitud válida puede verificarse más de una vez durante la ventana. Renovar su timestamp y firma conserva el fingerprint del mismo cuerpo. La idempotencia persistente y el conflicto de una misma `job_id` con otro fingerprint corresponden al ledger; no se usa una caché de replay en memoria que se pierda al reiniciar. El servidor, transporte R2, ledger/publicación/ACK completos y despliegue siguen pendientes.

## Verificación

```text
python -m unittest discover -s tests -p test_r2_sync_protocol.py -v
python -m ruff check docker/r2-sync-consumer/sync_protocol.py tests/test_r2_sync_protocol.py
python -m ruff format --check docker/r2-sync-consumer/sync_protocol.py tests/test_r2_sync_protocol.py
```

Las pruebas usan el vector público ya fijado en [los tests TypeScript](../../tests-ts/sync-contract.test.ts) y dos vectores adicionales calculados independientemente con WebCrypto de Node: timestamp máximo seguro y clave de 128 bytes con límites de slug/extensión/tamaño. Los tres coinciden por cuerpo, fingerprint y firma. La clave de prueba son bytes públicos consecutivos y no una credencial del sistema.

También se prueban ±300/301 segundos, época cero, reloj inválido, tamper de contenido/firma/clave, método/ruta/LF, tamaño del cuerpo, UTF-8/JSON/canonicalidad, tipos/esquema y saneamiento de fallos criptográficos. Los cuerpos no canónicos reciben MACs válidos independientes en las pruebas para demostrar que falla su representación y no sólo la autenticación. Estas pruebas son portables y no acreditan una ejecución HTTP ni un servicio desplegado.
