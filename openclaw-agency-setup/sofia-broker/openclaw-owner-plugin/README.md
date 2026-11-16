# Piloto privado de Sofía con OAuth de Codex

El plugin usa el perfil OAuth que OpenClaw ya administra para `agency-account`. Recibe un mensaje por un socket Unix privado, pide al SDK un turno nuevo con `isolated-agent-runtime` y devuelve texto. El SDK selecciona el harness Codex, crea el contexto efímero y entrega las credenciales al proceso nativo. El plugin comprueba de antemano el runtime, el modelo `openai/gpt-6.1-terra` y el tipo OAuth del perfil fijado. Al recibir la respuesta comprueba también la atribución de la ejecución. [Contrato del SDK](https://github.com/openclaw/openclaw/blob/v2026.10.10/docs/plugins/sdk-runtime/models.md) · [Implementación nativa](https://github.com/openclaw/openclaw/blob/v2026.10.10/extensions/codex/src/app-server/isolated-completion.ts)

Cada solicitud firmada se ejecuta una vez en el programador de servicios de OpenClaw, con el contexto y la cancelación del Gateway. Si el cliente se desconecta antes del inicio, se cancela el trabajo en cola. Si el trabajo ya empezó, se interrumpe mediante la señal y el Gateway conserva su responsabilidad de limpieza. [Programador de servicios](https://docs.openclaw.ai/plugins/sdk-runtime/gateway-and-nodes#service-scheduling)

## Preparación de un ensayo aislado

- Ejecutar `node --test tests/test_sofia_owner_plugin.mjs` desde la raíz del proyecto en Linux. El fixture no usa credenciales, modelos ni servicios. En Windows ejecuta las pruebas de contrato y omite la prueba de permisos POSIX.
- El archivo de perfil debe tener una ruta absoluta canónica y modo `0400` o `0600`, ser un archivo regular sin enlaces y pertenecer al UID del Gateway. Sus únicas propiedades son `billingMode`, `oauthProfileId`, `ownerActor`, `socketPath` y `hmacSecret`. Los valores esperados son `sandbox`, el ID privado del perfil OAuth `openai:…`, el identificador opaco de 64 hexadecimales minúsculos del único titular, la ruta absoluta de `sofia-owner.sock` y una clave HMAC aleatoria de 32 a 128 caracteres ASCII visibles. El directorio inmediato del socket debe ser real, pertenecer al mismo UID y tener modo `0700`. El socket queda en `0600`.
- Para una configuración aislada del Gateway, añadir la ruta canónica del paquete en `plugins.load.paths` y el ID `agency-sofia-owner-pilot` a la lista de plugins permitidos. Conservar las entradas y rutas existentes. La entrada se configura así:

```json
{
  "enabled": true,
  "config": { "profileFile": "/ruta/privada/perfil-sofia.json" },
  "llm": {
    "allowAgentIdOverride": true,
    "allowAuthProfileOverride": true,
    "allowedCompletionModels": ["openai/gpt-6.1-terra"]
  }
}
```

El modelo efectivo de `agency-account` debe ser Terra con política explícita de runtime `codex`. El app-server debe usar stdio, home de agente y el binario empaquetado normal. Si el binario y el OAuth existente no admiten Terra bajo esas condiciones, el ensayo devuelve un error. La prueba de un turno sintético con el SDK real es el siguiente paso para acreditar el harness nativo y el OAuth antes de cualquier integración de Studio. Este repositorio incluye el fixture del contrato, sin registro del plugin en el Gateway existente.

## Contrato local del socket

Durante el arranque, la guarda puede retirar un socket Unix obsoleto de Sofía o Mateo. Exige el directorio canónico privado `0700` y un socket `0600` del mismo usuario. Prueba una conexión local y sólo retira el socket si recibe `ECONNREFUSED` y el dispositivo, inodo, propietario y modo siguen coincidiendo. Cualquier otro resultado impide que el plugin sustituya la ruta.

La ruta es `POST /v1/sofia/owner-turn`. El servicio se registra sólo como servicio interno de OpenClaw y carece de listener TCP o ruta en el Gateway. El cliente envía `Content-Type: application/json`, `Content-Length`, `timestamp` (Unix UTC de diez dígitos, ±60 segundos) y `signature` (`v1=` seguido de 64 hexadecimales minúsculos). El cuerpo es JSON UTF-8 compacto, con este orden de propiedades:

```json
{"actor_id":"<id opaco del titular>","order_id":"<UUID v4>","request_id":"<UUID v4>","billing_mode":"sandbox","plan_id":"free","message":"<un mensaje>"}
```

La firma usa HMAC-SHA-256 de las siguientes seis líneas separadas por LF, sin LF final. El último valor es el SHA-256 hexadecimal minúsculo de los bytes del cuerpo.

```text
studio-sofia-owner-unix-v1
request
<timestamp>
POST
/v1/sofia/owner-turn
<sha256 del cuerpo crudo>
```

El `200` devuelve `actor_id`, `order_id`, `request_id` y `text`; incluye `timestamp` y `signature`. La firma de respuesta sustituye `request` por `response` y `POST` por `200`. El cliente debe validar firma, frescura y coincidencia de los tres identificadores antes de usar la respuesta. Los errores tienen un cuerpo genérico. `401` indica firma inválida, `403` fuera del titular o plan admitido, `409` repetición reciente, `422` cuerpo inválido, `429` concurrencia o admisión pausada y `503` indisponibilidad. Los mensajes se limitan a 2048 bytes y las respuestas a 8192. Se admite un turno simultáneo, ocho IDs en dos minutos y un plazo solicitado de 25 segundos al SDK. Tras un error del SDK la admisión queda pausada, porque el SDK puede comunicar un timeout antes de terminar la limpieza del proceso efímero. Verificar la terminación de ese proceso antes de reiniciar el plugin.

Tras cada carga del plugin, ejecutar primero `POST /v1/sofia/owner-readiness` por el mismo socket privado. Firmar con esa ruta en las seis líneas y enviar `message` igual a `startup-readiness`. Comprueba modelo, runtime y tipo del perfil OAuth sin invocar inferencia. El `200` firmado devuelve `text` igual a `ready` y habilita los turnos. Un fallo de admisión devuelve `503` firmado sobre el código de etapa `SOFIA_OWNER_PREFLIGHT_*`. Una comprobación fallida vuelve a bloquear los turnos hasta que pase otra comprobación firmada.

El broker HTTP integra el cliente Unix para este piloto. Parte de un `Turn` ya autenticado, exige Free en sandbox y el actor del perfil, conserva su reserva de idempotencia y firma con la clave local distinta de la del Worker. El cliente Unix y el plugin ejecutan con el mismo UID. El broker verifica la respuesta firmada antes de guardar el texto. El OAuth sigue administrado por el Gateway.
