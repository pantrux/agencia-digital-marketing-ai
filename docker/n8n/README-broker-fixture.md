# Broker de ensayo para n8n 2.46.6

La prueba reproduce el protocolo del launcher 1.5.1 y el runner Python de n8n 2.46.6. Escucha sólo en `127.0.0.1` y admite un launcher y un runner Python. Usa dos credenciales de conexión de un solo uso y entrega una tarea fija que transforma el valor `21` en `42`. El resultado registra etapas y contadores sin credenciales, mensajes recibidos ni datos del proceso. [Handshake del launcher fijado](https://github.com/n8n-io/task-runner-launcher/blob/871cb76bc35c990f368eaa788f0b4ce7b145e143/internal/ws/handshake.go) · [Protocolo Python fijado](https://github.com/n8n-io/n8n/blob/bb72d756104542adb549b6966a57e21459bd52a2/packages/%40n8n/task-runner-python/src/message_serde.py)

## Prueba local con clientes simulados

Ejecutar `python -B -m unittest -v test_runner_broker_fixture` desde este directorio con `websockets==15.0.1`. El entorno local puede cargar directamente la rueda universal fijada por `uv.lock`, `websockets-15.0.1-py3-none-any.whl` de 169.743 bytes, SHA-256 `f7a866fbc1e97b5c617ee4116daaa09b722101d4a3c170c787450ba409f9736f`. Linux puede usar la misma versión ya instalada en el entorno del runner. La prueba local acredita el comportamiento HTTP y WebSocket del broker con clientes simulados.

## Aceptación con procesos reales en Linux

El operador prepara un espacio aislado propio que comparte loopback entre el broker, el launcher y su runner, sin puertos publicados. Usar el ELF y el entorno Python fijados, la configuración de runners original en lectura y el entorno Python en `/opt/runners/task-runner-python/.venv`. El broker toma un archivo privado de token sintético ASCII de 24 a 128 caracteres, sin vínculos y con una sola línea.

```sh
/opt/runners/task-runner-python/.venv/bin/python -I -B /ruta/propia/runner_broker_fixture.py \
  --auth-token-file /ruta/privada/token-sintetico --port 0 --deadline-seconds 40
```

La primera línea JSON entrega el puerto dinámico. El supervisor del ensayo lanza sólo `task-runner-launcher python` con entorno explícito, `N8N_RUNNERS_AUTH_TOKEN` leído del mismo archivo, `N8N_RUNNERS_TASK_BROKER_URI=http://127.0.0.1:<puerto>` y `N8N_RUNNERS_CONFIG_PATH` apuntando a la configuración fijada. Dar al launcher y al hijo sus puertos de salud propios dentro del espacio aislado. Se puede fijar `N8N_RUNNERS_LAUNCHER_GRACEFUL_SHUTDOWN_TIMEOUT=3` para acotar el cierre. Cualquier credencial de producción queda fuera de este ensayo.

Un resultado válido requiere `passed=true`, dos credenciales emitidas y consumidas y la secuencia `launcher_registered`, `launcher_deferred`, `python_registered`, `python_task_accepted`, `python_task_done`. El broker envía `broker:drain` tras verificar la salida. Después el supervisor envía SIGTERM al launcher y comprueba que el hijo y los listeners propios desaparecen dentro del presupuesto. La salud del launcher sólo confirma que su listener responde. La integración con JavaScript y la aceptación del sidecar completo son etapas separadas.
