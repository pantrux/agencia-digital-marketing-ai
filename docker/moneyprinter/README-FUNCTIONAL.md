# Prueba privada de MoneyPrinterTurbo en vm-media-01

El Compose usa el digest amd64 `2547b9b5…` ya cargado y el overlay del recibo
`44ed6587c977814ab20496f4e703e22fe71057a4c0838f85efe34861c8810c2e`.
Las cinco preimágenes físicas coinciden con el manifiesto en el recibo de
captura `fac2ccfe22cfff89171fa4f1b5ccb46fcd5a06a9634ce2ccb42f773a8054c641`.
La UI usa el ejecutable Streamlit capturado, cuyo intérprete es Python 3.11.
`main.py` levanta `app.asgi:app` con puerto 8080. Ambos procesos usan el
usuario real `www-data` (UID/GID 33), con `/var/www` como su directorio en
`passwd` y un tmpfs privado en esa ruta.

## Preparación de Root

- Fijar los SHA de estas fuentes y respaldar los tres nombres de ruta/red que
  requiere el Compose. Crear una raíz privada de insumos y una raíz de estado
  de esta prueba, ambas propias, sin enlaces ni adopción de datos previos.
- En `INPUT_ROOT/overlay`, copiar sólo las salidas del recibo, verificar su
  tamaño y SHA y dejar el código legible con montaje de sólo lectura. En
  `INPUT_ROOT/secrets/speaches.bearer`, preparar el Bearer respaldado como
  archivo regular de un enlace, UID/GID 33 y modo `0400`. Mantener su directorio
  padre privado. Usar ese archivo directamente como bind de sólo lectura.
- Crear `STATE_ROOT/ui` y `STATE_ROOT/api` con dueño `33:33` y modo `0700`.
  Cada uno recibe su propio `config.toml` de modo `0600`, generado a partir de
  la plantilla del overlay. Las rutas completas de ambos `config.toml` se
  respaldan antes del arranque. La clave de API propia se configura en
  `app.api_key` del TOML privado del API. Las credenciales se cargan por archivo.
- Usar la red externa privada ya aprobada para esta prueba. Su salida permite
  llegar a `192.168.0.6:8000` y no requiere proveedores externos. Comprobar que
  la imagen permite usar el hogar `/var/www` de `www-data`; el Compose conserva
  la resolución de hogar del usuario de la imagen.

En cada TOML, los campos relevantes para la prueba son estos. Fusionarlos con
la plantilla para que cada tabla quede declarada una sola vez.

```toml
log_level = "INFO"
listen_host = "0.0.0.0"
listen_port = 8080

[diagnostics]
log_requests = false

[app]
enable_redis = false
subtitle_provider = "speaches"
subtitle_speaches_token_file = "/run/secrets/moneyprinter-speaches"
```

El `app/paths.py` capturado (SHA `ba0785adcc6ea1f1cdc17c56e2905ca632db6b698e3f82615ffded2e3bd19a24`)
resuelve `MPT_HOME=/run/mpt`. El `app/utils/utils.py` capturado (SHA
`3e035942480a32e7ea2b6a5d4fea1a3cff34b830e497c5a30058cfd3dbf0cbcf`)
crea `storage/tasks` y `resource/public` bajo esa raíz. Las fuentes y recursos
de la imagen quedan en `/MoneyPrinterTurbo`. La aplicación puede copiar sus
recursos iniciales al directorio de estado. Para guardar el TOML usa un temporal
hermano y `os.replace`. Los dos directorios de estado separados evitan que
la UI y el API compitan por el mismo archivo con locks de procesos distintos.

## Primera aceptación

Root pasa sólo las tres referencias de ruta/red al proceso Compose y usa
`--env-file /dev/null` para evitar la carga implícita de un archivo local.
Primero ejecutar `config --quiet` con este YAML. Crear ambos servicios con
`create` y la política `pull_policy: never`. Cotejar y respaldar los CID, digest, usuario, mounts y
red. Después iniciar esos servicios propios. La UI publica únicamente en
`127.0.0.1:8501`; el API permanece en el puerto 8080 de la red privada.

Comprobar el documento `/openapi.json` desde el API, y una página completa de
la UI hasta ver el selector de subtítulos. Comprobar la escritura de su TOML
en el directorio privado tras un cambio sintético. Verificar el endpoint
protegido del API según su OpenAPI. La sonda lee su clave del TOML privado y
construye la cabecera en memoria. Para la prueba Speaches, usar el WAV
sintético ya fijado y el proveedor del overlay desde el contenedor. Comparar
28 palabras y tres segmentos con el recibo Linux existente. Conservar sólo
resultado saneado y metadatos. La SCA de la imagen y el procesamiento de
vídeo completo son aceptaciones separadas.

`functional_operator.py` ejecuta `prepare`, `start`, `status` y `stop`
como operaciones separadas. La preparación valida el Compose en la VM,
crea los contenedores sin arrancarlos y devuelve sus identidades. El
arranque usa las identidades respaldadas. En el bridge propio se permite
la salida IPv4 a Speaches en `192.168.0.6:8000` y el retorno de conexiones
establecidas. Los contenedores no usan un resolvedor DNS externo.

`functional_probe.py` se ejecuta por entrada estándar con el intérprete
del contenedor API como UID/GID 33. El WAV de prueba va en
`/run/mpt/fixtures/whisper-probe.wav`. El argumento opcional
`speaches-probe-v<N>` identifica un nuevo ensayo. El script recorre
`task.generate_subtitle` y compara el SRT generado con la descarga
autenticada. Para comprobar el selector con AppTest, usar un directorio
descartable dentro de `/run/mpt`. El tmpfs de 64 MiB resulta insuficiente
para la copia inicial de recursos de la interfaz.
