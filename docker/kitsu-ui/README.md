# UI de Kitsu 1.0.77

Nginx sirve el sitio compilado en el puerto interno 8081 como UID/GID 1000.
La imagen parte del contexto OCI local `wolfi` de 19 paquetes y añade siete
APK firmados. Usa los alias `zou-api` y `zou-events` en la red privada.

## Ensamblar el contexto

1. Usar un directorio propio nuevo con `Dockerfile`, `nginx.conf` y los dos
   archivos del directorio `apk-inputs` de esta unidad. Acreditar el OCI base
   Linux amd64 con manifiesto `ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72`
   y configuración `b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7`
   antes de pasarlo como contexto nombrado `wolfi`.
   El Dockerfile comprueba que la base tiene 19 paquetes y conserva sus versiones
   al instalar exactamente los siete paquetes indicados en el plan de descarga.
2. Verificar el SHA-256 de `kitsu-1.0.77.tgz` antes de extraerlo.
   Debe ser `f030dc59ae28155d3686c09ce35a2855381582ab42fcc983158c727ec5f1204e`
   y medir 12.163.231 bytes. Extraer con `tar -xzf <archivo> -C <contexto>/dist`
   en un `dist` nuevo. El tar ya trae el sitio en la raíz (`./index.html`,
   `./assets`, `./fonts`). Contiene 776 archivos regulares y tres directorios,
   con 44.581.329 bytes de archivos. El marcador `dist/.version.txt` dice `1.0.77`.
3. Copiar los siete APK autenticados a `apk-inputs/apk-repo/x86_64/`. Usar
   los nombres exactos de `.tools/evidence/phase4-kitsu-nginx-download-plan-v1.json`
   y los SHA completos de `apk-inputs/SHA256SUMS`, cotejados con el recibo de
   adquisición `.tools/cache/kitsu-nginx-inputs-v1/receipt-attempt5.json`
   (SHA `e53a2157cd3053cb9591feb179f9259b153e442703e1232677c13d0f07cfc4d2`).
   El plan identifica cada URL en `https://packages.wolfi.dev/os/x86_64/` y
   el Q1 del índice firmado. El Q1 autentica el control del APK y el recibo fija
   el SHA del archivo completo.
4. Copiar el índice firmado como `apk-inputs/apk-repo/x86_64/APKINDEX.tar.gz`
   (SHA `9b27ab69add838665af44c9aa304a0dd910af90f2cd9e191e52e0b585a3ce2fd`)
   y la clave como `apk-inputs/keys/wolfi-signing.rsa.pub`
   (SHA `f0031424cf46f7db780ce63a45f0fd6aa6f85f601e6bb3b7a91fe3d4d5b7d2cc`).
   Conservar la ruta absoluta `/apk-inputs/apk-repo` y el fin de línea LF de
   `repositories`. El Dockerfile verifica los diez archivos de `SHA256SUMS`
   desde un bind de sólo lectura.

Root pasa el OCI base acreditado con `--build-context wolfi=oci-layout://…@sha256:…`,
plataforma `linux/amd64`, red `none` y `--pull=false`. El contexto contiene sólo
los insumos anteriores. La instalación APK usa el repositorio local y desactiva
scripts y hooks. La imagen resultante se verifica contra el inventario de 26 APK.

## Arranque y comprobaciones

En Compose, usar sistema de archivos de sólo lectura, `cap_drop: [ALL]`,
`no-new-privileges:true` y un tmpfs en `/tmp` de 64 MiB, `uid=1000,gid=1000`,
modo `0700`, `nosuid,nodev,noexec`. El PID y los temporales de Nginx viven allí.
El Compose monta el `nginx.conf` acreditado en sólo lectura. Conserva el archivo
en `/opt/media-platform/kitsu-ui-refresh-v1/dns-recovery-v1/nginx.conf`, con
propietario `root:root`, modo `0444` y SHA-256
`dfd236e2dc9ecc72cff55b30b40d7f7c416106833cf490a903f54db15a108986`.
Nginx consulta el DNS interno de Docker y vuelve a resolver los dos alias de
Zou. La prueba aislada comprobó API y WebSocket tras un cambio de IP, con el
mismo proceso Nginx. Recibos `17be900b…` y `02ad0d63…`.

- `/healthz` devuelve 200 y `ok` desde Nginx. Probar API y eventos por separado.
- `/api/config` llega a `/config` en `zou-api:5000`. La ruta API transmite el
  cuerpo sin acumularlo en disco y admite peticiones de hasta 128 MiB. Verificar
  que una respuesta API de error conserve su código y su cuerpo.
- `/socket.io/` conserva ruta y consulta en `zou-events:5001`. Verificar polling
  y, en una prueba separada, el upgrade `101` y conexión `/events` autenticada.
- `/assets/` y `/fonts/` entregan archivos o 404. Verificar que una ruta de la UI
  como `/login` entregue `index.html` y un recurso ausente responda 404.
- El acceso registra `$uri` sin consulta. El destino de errores de Nginx es
  `/dev/null` porque su formato nativo puede incluir la consulta original.
  Verificar por separado el código de salida de la prueba de configuración y
  los códigos HTTP al aceptar la imagen.

Referencias de Nginx sobre la sustitución del prefijo y las cabeceras del upgrade.
[proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)
y [WebSocket](https://nginx.org/en/docs/http/websocket.html).
