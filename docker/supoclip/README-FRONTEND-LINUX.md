# Operación offline del frontend SupoClip

El operador prepara un SDK Wolfi Linux amd64 con Node 24.22.2,
Python 3.12 y las bibliotecas OpenSSL 3 acreditadas para el motor
Prisma. El contenedor ejecuta como UID/GID 1000 y usa red `none`.
Las entradas se montan en `/inputs` de sólo lectura. El directorio
privado `/work` tiene modo 0700 y dueño 1000:1000.

Copiar las entradas ordinarias con nombres exactos a `/inputs`.
El script valida las huellas antes de extraer.

| Archivo | Procedencia bajo `.tools/backup-source-build` |
| --- | --- |
| `frontend-source.tar`, `frontend-source-receipt.json` | `supoclip-frontend-v1/` |
| `linux-npm-part-1.tar`, `linux-npm-part-2.tar`, `transport-receipt.json` | `supoclip-frontend-npm-transport-v1/` |
| `pnpm-10.27.0.tgz`, `schema-engine` | `supoclip-frontend-v1/` |
| `frontend_offline_linux.py` | Fuente del repositorio con SHA respaldado por el operador |

Para compilar, el operador monta el directorio privado
`/work/private/frontend-build-repair-v1` con `CHANGELOG.md` y
`eslint.config.mjs`, según el recibo de
`supoclip-frontend-build-repair-v1/`. El historial proviene del archivo
original fijado de SupoClip. La configuración conserva las reglas y
resuelve sus complementos junto a `eslint-config-next`.

## Etapas

1. `python3.12 -I -S -B /inputs/frontend_offline_linux.py seed`
   extrae los 796 tarballs y la CLI pnpm, comprueba la versión de la CLI y
   sirve únicamente las rutas acreditadas en loopback. Conserva el
   catálogo de peticiones y los SHA de los índices físicos en
   `/work/frontend-seed-v2`.
2. `python3.12 -I -S -B /inputs/frontend_offline_linux.py replay`
   extrae los 597 miembros del frontend en otra raíz. Instala con pnpm
   offline, lock congelado y scripts deshabilitados. Compara índices,
   lock y fuentes después de instalar. El recibo de esta etapa vive en
   `/work/frontend-replay-v3/replay.json`.
3. Ejecutar la compilación con orígenes públicos del ensayo, definidos
   por el operador. Son valores de URL que quedarán en el build.

   ```sh
   python3.12 -I -S -B /inputs/frontend_offline_linux.py build \
     --public-app-origin http://192.168.0.85:3080 \
     --public-api-origin http://192.168.0.85:3080
   ```

   El build verifica otra vez la fuente y el lock. Copia el motor Prisma
   acreditado a la raíz privada de compilación y comprueba su versión.
   Ejecuta Prisma generate, tsc y Next sin red. El proceso recibe una
   URL sintética de PostgreSQL y un valor sintético de autenticación.
   El inventario rechaza esos valores en la salida standalone y
   rechaza enlaces que escapen del árbol. Conserva el servidor, estáticos
   e historial bajo `/work/frontend-replay-v3/frontend/.next/standalone`.
   El recibo queda en `/work/frontend-build-v3/build.json`.

Cada etapa reserva una raíz nueva. Ante un fallo, guardar sus logs y
preparar otro workspace de ensayo. Prever varios GiB de espacio porque la
semilla y la reinstalación tienen árboles distintos. Las migraciones y la
aceptación con PostgreSQL y Better Auth se ejecutan en una operación
posterior con la base propia.
