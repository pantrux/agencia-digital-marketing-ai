# Piloto privado del chat de Sofía

El Worker habilita el chat en `BILLING_MODE=sandbox` para un único `SOFIA_OWNER_USER_ID`, el UUID que D1 asignó al titular tras su inicio de sesión. Antes de firmar, lee de D1 una orden Free que pertenezca a ese usuario, tenga una carga completada y cumpla los estados de producción y cuota esperados. Envía un identificador opaco derivado del UUID mediante HMAC; la orden y el modo `sandbox` forman parte del cuerpo firmado.

| Servicio | Dirección | Operación |
| --- | --- | --- |
| Ingreso NPM mTLS | `192.168.0.7:3147` | `POST /v1/sofia/chat`, sólo peer TCP `192.168.0.8` |
| Broker | `127.0.0.1:3107` | `POST /v1/sofia/chat` |
| Plugin de OpenClaw | `/run/user/1000/agency-sofia-owner/sofia-owner.sock` | `POST /v1/sofia/owner-turn` dentro del socket Unix |

El broker revalida firma, titular, plan y modo antes de reservar un turno en SQLite. Envía al socket una solicitud firmada con una clave distinta y comprueba la firma y los identificadores de la respuesta antes de guardarla. El plugin llama al harness Codex con el perfil OAuth ya administrado por OpenClaw. Cada turno usa un contexto efímero sin herramientas. El [plugin y su contrato de socket](openclaw-owner-plugin/README.md) describen su verificación de modelo, runtime y perfil.

## Ensayo local

`agency-sofia-broker.service.review` es una plantilla para el gestor de usuario de `jandrade` en `.7`. El broker necesita el mismo UID del Gateway para acceder al socket `0600`. El piloto V6 creó `/run/user/1000/agency-sofia-owner` en modo `0700` con una unidad transitoria. Antes del inicio del broker, verificar la sonda Unix de preparación y mantener la unidad del directorio activa. Las antiguas plantillas `agency-sofia-gateway.service.review` y `openclaw.review.json` documentan el ensayo separado previo.

Preparar dos archivos físicos `0400` o `0600` del UID del broker. `worker-hmac` coincide con el secreto del Worker `STUDIO_SOFIA_HMAC_SECRET` y contiene ASCII visible de 32 a 128 caracteres. `profile.json` es el perfil común del plugin, con el ID opaco derivado del titular y una segunda clave HMAC. Para derivar `ownerActor` calcular HMAC-SHA-256 hexadecimal minúsculo con la clave Worker sobre UTF-8 `studio-sofia-v1\nactor\n<UUID D1 real>`, donde `\n` representa LF. Usar los valores reales sólo en los archivos privados de la plataforma.

```sh
python3 -I -S -B "$HOME/.local/share/agency-sofia-broker-v1/sofia_broker.py" \
  --hmac-file "$HOME/.local/share/agency-sofia-broker-v1/worker-hmac" \
  --owner-profile-file "$HOME/.local/share/agency-sofia-owner-pilot-20261114-v6/profile.json" \
  --state-dir "$HOME/.local/state/agency-sofia-broker"
```

## Entrada mTLS desde NPM

`sofia_mtls_proxy.py` es un proceso independiente sin acceso a OAuth ni a HMAC. Exige el peer TCP `192.168.0.8` antes del handshake y valida una CA cliente dedicada, el SHA-256 DER de la hoja de NPM, su SAN DNS y el SNI del listener. Acepta únicamente HTTP/1.1, con `Host` igual al SNI configurado y el POST exacto. Preserva cuerpo, `timestamp` y `signature` hacia el broker fijo `127.0.0.1:3107`. Los logs del servicio omiten el cuerpo y las cabeceras.

`agency-sofia-mtls.service.review` se prepara como unidad de sistema `agency-sofia-mtls-v1.service`, con usuario dinámico y binario Python sin dependencias externas. El perfil y los PEM se instalan mediante el mecanismo privado de la plataforma en `/etc/agency-sofia-mtls-v1`, directorio físico root `0700`, archivos `0400`: `profile.json`, `server.pem`, `server.key` y `client-ca.pem`. En Ubuntu systemd 255 los archivos que recibe el proceso son copias de sólo lectura en `/run/credentials/agency-sofia-mtls-v1.service`: directorio root:root `0550`, archivos root:root `0440` y una ACL que concede lectura al UID de la unidad. El proceso verifica esa ubicación y esos permisos, además de su acceso efectivo, antes de cargar TLS. El modo directo de desarrollo exige un directorio `0700` de su propio UID con archivos `0400` o `0600`. Las claves quedan en los directorios de TLS. El perfil tiene este contrato:

```json
{"server_name":"<DNS interno de Sofía>","npm_client_dns":"<SAN DNS de NPM>","npm_client_sha256":"<SHA256 DER de la hoja NPM en hexadecimal minúsculo>"}
```

El certificado de servidor debe incluir el DNS interno como SAN y el de cliente debe incluir el SAN dedicado de NPM. El Worker usa `SOFIA_BROKER_ORIGIN=https://api-studio.e-scale.cl`. Ajustar `sofia_tls` del inventario de ingress sólo tras comprobar el bind exacto y realizar las sondas. [Contrato de NPM y BunkerWeb](../../ingress/README.md)

1. Validar las unidades y el par TLS en el host. Arrancar sólo el broker de usuario y comprobar `401` en loopback sobre JSON sintético sin firma.
2. Arrancar el listener de prueba. Desde NPM, comprobar rechazo sin certificado y con SNI incorrecto. Con el certificado y Host correctos, un POST sin firma devuelve `401`. Un fallo TLS puede indicar que el peer real difiere de `.8`; verificar la ruta antes de cambiar el inventario.
3. Probar que un origen ajeno no puede usar el listener. Enviar un único turno sintético firmado a través de NPM y cotejar respuesta, firma, estado y registro de idempotencia. Conservar `sofia_tls:null` hasta pasar las pruebas privadas.
4. Para revertir, restaurar la proyección de ingress con `sofia_tls:null` y verificar el `404` público. Detener después el listener y el broker propios. Mantener el perfil y SQLite para inspección.

El piloto usa `agency-sofia-runtime.service` y `50-agency-sofia-runtime.conf` para crear el directorio Unix antes del Gateway. `agency-sofia-owner-broker.service` ejecuta `sofia_startup_probe.py` en `ExecStartPre`. Cada invocación firma una solicitud nueva y espera a que el socket escuche. Una respuesta rechazada termina el intento y activa el reintento de systemd con espera creciente entre cinco segundos y un minuto. El broker conserva SQLite y escucha en `127.0.0.1`. El listener mTLS está habilitado en `multi-user.target`. Se verificaron la preparación firmada, la conservación de SQLite y el reinicio del broker. La comprobación tras reiniciar el Gateway completo queda pendiente.

```sh
python -m pytest -q tests/test_sofia_startup_probe.py tests/test_sofia_broker.py tests/test_sofia_mtls_proxy.py
node --test tests/test_sofia_owner_plugin.mjs
npm run test -- tests-ts/studio-chat.test.ts
```

Una solicitud local sin firma devuelve `401` antes de contactar al plugin. Una solicitud correcta firmada devuelve sólo `actor_id`, `order_id`, `request_id` y `text`; repetir el mismo cuerpo devuelve la respuesta guardada. Dos cuerpos con el mismo identificador devuelven `409` y los turnos concurrentes pueden devolver `429`. El mensaje admite 2048 bytes y la respuesta 8192 bytes UTF-8. El límite del delegado Unix es 30 segundos, dentro de los 45 del Worker.

SQLite conserva identificadores, huella de solicitud y respuesta temporal durante una hora. Los identificadores de turnos terminados se retienen siete días. Si falla la conexión antes de enviar bytes, el broker permite reintentar el mismo turno. Si pudo llegar al plugin, se preserva la reserva y el plugin puede pausar nuevas admisiones hasta que se confirme la terminación del proceso nativo. Verificar la limpieza antes de reiniciar el plugin. El turno sintético V6 se acreditó por el socket Unix con Codex nativo. El 14 de noviembre el titular confirmó la respuesta en Studio y el broker `agency-sofia-owner-broker.service` registró el turno completado en 24 s.
