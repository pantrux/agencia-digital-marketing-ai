# Ensayo del lock de referencia de n8n en Linux

La cápsula `server-linux-x64-musl-delta-v2.tar.gz` tiene SHA-256 `b3405a0d625db295e62cc9cf3edd06eca25548c4c1c91e3c108b23397df406ed`, 439.342.873 bytes comprimidos y 4.542 entradas regulares. Su manifiesto tiene SHA-256 `203830d3062236403e21f1922839965cada6b8533d6f82b8dd9ebef0bdfa034c`. El lock candidato tiene SHA-256 `f9cc9a03cd857a379c8e36d4a457c797cd4f07cb71dab711c9f554273055d516`.

## Espacio y límites

La recepción necesita 1 GiB libre y 7.000 inodos en el volumen de `/opt/media-platform`. El máximo simultáneo de datos es 892.993.752 bytes antes del redondeo a bloques y metadatos. Tras una recepción correcta quedan 453.650.879 bytes de contenido extraído. Los 1.449 tarballs reutilizados permanecen en su árbol V5 existente.

El seed y la segunda instalación necesitan **5 GiB libres y 350.000 inodos adicionales** en el volumen de `/work` después de recibir. Los 3.142 tarballs autenticados suman 603.449.696 bytes comprimidos. Se midieron 224.450 archivos regulares y 2.563.071.012 bytes expandidos. A bloques de 4 KiB, el corpus completo ocupa 3.230.269.440 bytes sin deduplicar y 2.883.133.440 bytes al deduplicar por contenido. La guarda deja margen para el índice, variantes de modo, parches, directorios y enlaces de las dos instalaciones. El store y ambos árboles están en un mismo sistema de archivos con enlaces duros. Si `/work` y `/opt/media-platform` comparten volumen, reservar 6 GiB antes de recibir. El cálculo presupone el SDK y V5 ya presentes y no cubre compilación ni salida de producción.

El receptor impone el tamaño exacto de la cápsula, 457.154.560 bytes de TAR expandido y 900 segundos. El registro de bucle local admite ocho conexiones, 12.000 solicitudes GET, 30 segundos de inactividad por socket y tarballs de hasta 96 MiB. pnpm usa seis solicitudes simultáneas, sin reintentos y con límite de 900 segundos y 1 MiB de salida por instalación. Usar también un límite exterior para el contenedor y conservar el recibo o fallo sin reutilizar su raíz.

## Recepción

Fijar los SHA de los operadores y la identidad del padre Linux antes de usar. El receptor usa sólo el lector anterior `receive-n8n-js-linux-transfer-v1.py` con SHA-256 `f3ae4889a03f90ec989cff4b9c2b74dc4331916ccdb36d25738f427082aa9b2b`. Pasar la cápsula directamente por stdin al Python de la VM. La raíz fija debe estar ausente y el padre debe pertenecer a root, con modo 0700.

```sh
python3 -I -B /ops/receive_server_linux_baseline.py --reader /ops/receive-n8n-js-linux-transfer-v1.py --receive
```

El receptor verifica el SHA global antes de extraer, aplica límites de bytes y coteja los hashes de los miembros. Crea los archivos por descriptor y en exclusividad. Publica `inputs` con modo 0555, archivos 0444 y `receipt.json` privado bajo `/opt/media-platform/n8n-server-linux-transfer-v2`. Guardar su identidad y SHA del recibo antes del seed. La ejecución sin `--receive` carece de escrituras. `--inspect-capsule` permite verificar localmente el archivo íntegro sin extraerlo.

## Seed y segunda instalación

En el SDK Linux x64 musl usado para el runner, iniciar Python como UID/GID 1000 en red `none`, sin credenciales, sin puerto publicado y con raíz de sólo lectura. Montar las dos fuentes de este operador y el helper anterior como `/ops` de sólo lectura. Montar `inputs` del delta en `/inputs` y el árbol `inputs` de `/opt/media-platform/n8n-js-linux-transfer-v1` en `/v5`, ambos de sólo lectura. Dar un único volumen nuevo en `/work`, 0700 UID/GID 1000. El helper `n8n-js-loopback-store-v1.py` se coteja por SHA-256 `c8a3fde0022130f95961bef4931d16cce25155f18d7d2844290c2455c98bd36f`.

```sh
python3 -I -B /ops/seed_server_linux_baseline.py --loopback-helper /ops/n8n-js-loopback-store-v1.py --seed
```

El operador coteja por longitud, SHA-256 y SRI los 1.449 tarballs V5 y los 1.693 incluidos. Sirve únicamente las rutas del índice y los 2.841 metadatos del espejo autenticado. En la primera instalación, pnpm 12.4.2 usa el registro de bucle local y `--frozen-lockfile --ignore-scripts`. Así incorpora también el único tarball `file:` fijado, wa-sqlite. La segunda instalación usa un árbol de manifiestos separado, el mismo store, el espejo original en la caché propia y `--offline --frozen-lockfile --ignore-scripts --config.frozen-store=true`, con el registro local detenido. Ambas guardan logs y SHA del lock. [Instalación congelada de pnpm](https://pnpm.io/cli/install#--frozen-lockfile), [lector del tarball local en pnpm 12.4.2](https://github.com/pnpm/pnpm/blob/v12.4.2/pnpm11/fetching/tarball-fetcher/src/localTarballFetcher.ts).

El recibo exige la política de 3.478 entradas, cero descargas en la segunda instalación, integridad del índice SQLite y el mismo lock e índice entre etapas. `coverage.json` conserva las ausencias del índice y las rutas que pnpm omitió. Puede omitir opcionales en Linux y el operador preserva esa diferencia sin exigir que el máximo teórico se materialice. Los manifiestos de los 80 proyectos bastan para el ensayo del lock. La prueba no compila n8n, no ejecuta scripts de paquetes y conserva los WASM de referencia. La compilación y publicación requieren el lock derivado con SQLite y QuickJS corregidos y la aceptación de los componentes nativos.
