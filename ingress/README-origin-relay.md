# Relay privado de origen en vm-media-01

`cloudflared` conecta al socket Unix local. Nginx elige uno de tres hosts exactos, envía ese nombre como Host y SNI a `192.168.0.8:9443`, verifica su certificado y presenta una credencial cliente dedicada. Los vhosts nuevos de BunkerWeb deben verificar esa credencial por separado del mTLS BunkerWeb → NPM. Docker presenta al WAF la misma IP de puente para `.85` y para otros orígenes LAN, así que el control de acceso de esos vhosts se basa en el certificado. `studio.e-scale.cl` y los hosts legados quedan fuera del relay.

```sh
python -I -S -B ingress/origin_relay.py nginx
python -I -S -B ingress/origin_relay.py tunnel-origins
```

Cada comando escribe una propuesta determinista a stdout. El segundo emite sólo las tres filas nuevas para insertar antes del catch-all vigente del túnel autorizado. Cloudflared 2026.11.0 conserva el Host original cuando usa `unix:`. En el transporte HTTP por socket no aplica `httpHostHeader`. El relay comprueba `$http_host` antes de reenviar y mantiene la URI original mediante `proxy_pass` sin sufijo URI. [Código de cloudflared 2026.11.0](https://github.com/cloudflare/cloudflared/blob/2026.11.0/ingress/origin_proxy.go#L27-L48) · [Nginx `proxy_pass`](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)

## Preparación en `.85`

1. Comprobar que `docker image inspect` devuelve el ID exacto `sha256:bee0896a7f9f2934cc5000afd21ba860a032092a48d36cce675e0fcc1529654c`, `linux/amd64`, ya presente en `.85`. Su Nginx 1.31.8 tiene OpenSSL 4.0.3 y soporte SNI. Confirmar que el Docker local conserva el mapeo de UID/GID previsto y que reconoce los límites del servicio.
2. Reservar una identidad de sistema sin login para el relay y el grupo exclusivo `agency-studio-origin-relay`. Se observó que UID `64320` y GID `64321` estaban libres. Los IDs definitivos del servicio y los archivos deben coincidir. El grupo suplementario del conector sólo necesita atravesar `/run/agency-studio-origin-relay`. Provisionar ese directorio como `64320:64321` modo `02710`, sin socket previo. Nginx crea el socket con modo `0666`. La protección efectiva es el permiso de recorrido de este directorio. [Código de Nginx 1.31.8](https://github.com/nginx/nginx/blob/release-1.31.8/src/core/ngx_connection.c#L596-L615)
3. Preparar `/etc/agency-studio-origin-relay` de root `0700`, con `nginx.conf` de root `0444` y un subdirectorio `tls` de root `0700`. Montar el config y los tres archivos TLS individualmente sólo lectura. Certificados `bunkerweb-server-ca.pem` y `relay-client.pem` de root `0444`. Llave `relay-client.key` propietario `64320`, modo `0400`, provisionada desde la bóveda mediante el operador de raíz. La CA cliente de este enlace debe confiarse sólo en los vhosts de agencia. La clave no forma parte del perfil del conector.
4. Antes de activar, ejecutar `nginx -t` con los mismos mounts y usuario en un **directorio de ensayo separado** montado en el destino del socket. `-t` prueba `bind` y elimina el socket que abre. Cotejar el estado BunkerWeb y NPM. En el conector añadir un drop-in propio con `SupplementaryGroups=agency-studio-origin-relay` y `After=agency-studio-origin-relay.service`. Comprobar después que el proceso tiene AF_UNIX permitido y ese grupo efectivo.

## Unidad de ejemplo

`/etc/systemd/system/agency-studio-origin-relay.service`, con los IDs y la imagen comprobados. En un arranque limpio, la unidad crea el directorio volátil bajo `/run`. Cuando el directorio ya existe, exige identidades y permisos exactos y que el socket esté ausente.

```ini
[Unit]
Description=Relay de origen privado del estudio
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=simple
RuntimeDirectory=agency-studio-origin-relay-control
RuntimeDirectoryMode=0700
ExecStartPre=/bin/sh -ec 'p=/run/agency-studio-origin-relay; if [ ! -e "$$p" ] && [ ! -L "$$p" ]; then mkdir -m 0700 -- "$$p"; chown 64320:64321 "$$p"; chmod 2710 "$$p"; fi; [ ! -L "$$p" ] && [ -d "$$p" ] && [ "$$(stat -c "%%u:%%g:%%a" -- "$$p")" = 64320:64321:2710 ] && [ ! -e "$$p/origin.sock" ] && [ ! -L "$$p/origin.sock" ]'
ExecStart=/usr/bin/docker run --rm --name agency-studio-origin-relay-v1 \
    --cidfile /run/agency-studio-origin-relay-control/container.cid \
    --label agency.owner=agencia-digital-marketing-ai:studio-origin-relay:v1 \
    --pull never --network bridge --user 64320:64321 \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --cpus 0.5 --memory 128m --pids-limit 32 --log-driver none \
    --stop-signal SIGQUIT --stop-timeout 15 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777 \
    --mount type=bind,src=/run/agency-studio-origin-relay,dst=/run/agency-studio-origin-relay \
    --mount type=bind,src=/etc/agency-studio-origin-relay/nginx.conf,dst=/etc/agency-relay/nginx.conf,readonly \
    --mount type=bind,src=/etc/agency-studio-origin-relay/tls/bunkerweb-server-ca.pem,dst=/etc/agency-relay/tls/bunkerweb-server-ca.pem,readonly \
    --mount type=bind,src=/etc/agency-studio-origin-relay/tls/relay-client.pem,dst=/etc/agency-relay/tls/relay-client.pem,readonly \
    --mount type=bind,src=/etc/agency-studio-origin-relay/tls/relay-client.key,dst=/etc/agency-relay/tls/relay-client.key,readonly \
    --entrypoint /usr/bin/nginx sha256:bee0896a7f9f2934cc5000afd21ba860a032092a48d36cce675e0fcc1529654c \
    -e stderr -c /etc/agency-relay/nginx.conf
ExecStop=/bin/sh -ec 'p=/run/agency-studio-origin-relay-control/container.cid; [ -f "$$p" ] || exit 0; cid=$$(cat "$$p"); printf "%%s\\n" "$$cid" | grep -Exq "[0-9a-f]{64}"; /usr/bin/docker stop --time 15 "$$cid"'
TimeoutStopSec=20s

[Install]
WantedBy=multi-user.target
```

El supervisor se conecta al único contenedor nombrado. Comprobar la ausencia inicial del nombre y confirmar CID y etiqueta antes de operar la unidad. Si el proceso se interrumpe y queda un socket o contenedor, reconciliar sus identidades antes de limpiar. La red bridge permite la conexión saliente al NAS. El relay sólo escucha en el socket Unix y el contenedor carece de puertos publicados.

## Aceptación y reversión

- Confirmar identidad de imagen, mounts RO, propietario/modo del socket y directorio, salida a `.8:9443`, ausencia de listeners TCP y ausencia de puertos publicados. En el host, el grupo suplementario del conector debe poder conectar al socket y un usuario sin ese grupo debe recibir `EACCES`.
- Usar `curl --unix-socket /run/agency-studio-origin-relay/origin.sock` con los Hosts exactos. Host ajeno `421`. Para API, `GET /v1/sync` debe dar `405` y cualquier URI alternativo `404`. Verificar frase de prueba local con cuerpo y cabeceras firmadas intactos y rechazo de longitud ausente o framing por chunks. Desde un origen LAN sin certificado, BunkerWeb debe rechazar los tres vhosts. La verificación del WAF y la del certificado del servidor se prueban juntas a través del relay.
- Probar salud Kitsu e Immich, `101` de Socket.IO y un envío sintético acotado con streaming. `128m` es el límite local del relay y del WAF. Cloudflare puede aplicar un límite de carga menor según el plan. Para `/v1/sync` sólo se admite `POST` exacto con Content-Length y hasta 4096 bytes. El relay conserva las cabeceras de firma, desactiva reintentos y no registra solicitudes. Sofía se agrega en otro cambio.
- Restaurar primero las filas del túnel a su estado previo seguro, comprobar el catch-all y conservar `studio` y el resto de las filas. Volver los tres hosts nuevos a borrador si requieren retirada. Deshabilitar/parar sólo el relay propio, retirar el drop-in del conector y reiniciarlo con su configuración anterior. Confirmar socket retirado y contenedor propio detenido. Conservar el inventario y la configuración previos para repetir la comparación.
