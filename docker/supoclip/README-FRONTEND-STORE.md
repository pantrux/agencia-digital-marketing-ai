# Almacén offline del frontend SupoClip

El lock fijado es
`d3d98cbe5f080c30a77cc16e7f6e128b2d3d52cb08a9ffa609f80b7ffb8bc2fe`.
El archivo `frontend-source.tar` y sus instrucciones de compilación están
descritos en [README-FRONTEND.md](README-FRONTEND.md).

## Archivos de registro

Se verificaron 803 candidatos por identidad interna, tamaño y SRI SHA512.
El cierre alcanzable de dependencias normales, de build y opcionales
contiene 796 paquetes para Linux amd64. El lock omite la condición libc
en varios opcionales y ese cierre conserva ambos empaquetados Linux en
esos casos.

`.tools/backup-source-build/supoclip-frontend-npm-transport-v1` contiene
dos archivos PAX regulares con inventario de miembros en
`transport-receipt.json`. Su SHA256 es
`b30eed332c6d0cec35fc409a56b60f725c2710e92d3135ecc528b2a4f4e679d0`.

| Archivo | Bytes | Paquetes | SHA256 |
| --- | ---: | ---: | --- |
| `linux-npm-part-1.tar` | 175790080 | 211 | `41030016775b0708ae9990d3fa8ce2a29e226d9560bcd984429fe8ee69f17ba5` |
| `linux-npm-part-2.tar` | 133836800 | 585 | `6b549024c808e14a096f5a47943f3542899f945cf880005b27e960b8a8839a4d` |

Estos dos archivos permiten poblar pnpm en el SDK mediante un registro
loopback efímero que sirva únicamente las rutas de tarball acreditadas.
El lock mantiene resoluciones por SRI y el registro local no necesita
inventar respuestas de metadatos. El tarball oficial del pnpm fijado se
encuentra en `supoclip-frontend-v1/pnpm-10.27.0.tgz`, con SHA256
`d3c7c3d12d87d177e32f01748d5994fb20cd5becf11670ee60530c374ffeabb3`.

## Instalación desde el almacén

`supoclip-pnpm-store-v3/store/v10` contiene el almacén materializado con
pnpm 10.27.0. El recibo `store-receipt.json` tiene SHA256
`0969369fa180247aa3beec2c7f217256b511a2bb61b95fa0cea825f03a1069f1`.
La instalación física de prueba en una raíz Windows distinta fijó
Linux amd64 glibc y terminó en 37,9 segundos. Usó 796 paquetes del
almacén y cero descargas, con el lock intacto y los scripts de instalación
deshabilitados.

Para usar una copia física del almacén en Linux, apuntar `--store-dir`
al padre de `v10`. Ejecutar desde el directorio `frontend` del artefacto,
con la CLI pnpm 10.27.0 y red deshabilitada.

```sh
node /opt/pnpm/bin/pnpm.cjs install \
  --offline --frozen-lockfile --ignore-scripts --ignore-pnpmfile \
  --store-dir /inputs/supoclip-store --package-import-method=copy
```

Los blobs de los dos módulos SWC Linux superan 128 MiB una vez
descomprimidos. Si el receptor conserva ese límite por miembro, usar los
dos TAR de archivos de registro y dejar que pnpm forme el almacén en el
SDK. El ensayo de instalación acredita la caché. La carga de SWC,
la generación Prisma, la compilación de Next y las pruebas con
PostgreSQL quedan para la aceptación Linux.
