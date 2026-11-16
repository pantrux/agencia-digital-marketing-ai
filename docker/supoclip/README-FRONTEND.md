# Frontend SupoClip con Prisma 6.19.3

El árbol de build `frontend-source.tar` está en
`.tools/backup-source-build/supoclip-frontend-v1`. Su SHA256 es
`9a59806c0b1211f33c9f1debb714aaae8ec974f7d3401e5adf3620afe30e3c9c`.
El recibo identifica los 597 miembros por SHA y conserva la licencia
upstream. El artefacto incluye Prisma, el lock resuelto y las tres fuentes
locales OFL. Los 28 archivos bajo `prisma/migrations` y `next.config.ts`
conservan sus bytes de la fuente fijada.

`frontend_prisma.py` verifica cuatro preimágenes del commit
`f6cb7e2ea9fb290519bb1aac2138d9d8722a6e38`. Fija Prisma, Client y
Adapter PG en 6.19.3, conserva el generador `prisma-client-js` y su ruta
de salida, activa `engineType = "client"` y elimina `binaryTargets`.
El singleton y el fixture e2e pasan a `new PrismaClient({ adapter })`.
Ambos requieren una `DATABASE_URL` explícita. Reservar el fixture e2e a
una DB descartable, ya que su inicialización modifica filas.
[Contrato Prisma 6](https://www.prisma.io/docs/orm/v6/prisma-client/setup-and-configuration/no-rust-engine).

## Lock y almacén offline

`pnpm@10.27.0` oficial resolvió el lock en una raíz privada, con
`--lockfile-only --ignore-scripts` y registro npm. No se creó
`node_modules`. Los 920 paquetes y sus SRI originales se conservan. El
adaptador añadió 17 paquetes, incluido `pg@8.23.1`. Better Auth adquirió
ese peer en su snapshot. El lock pasó
`pnpm install --offline --lockfile-only --ignore-scripts --frozen-lockfile`
sin cambiar su SHA.

El inventario `registry-delta.json` acredita los 17 TAR nuevos y tres
TAR Prisma del lock anterior, todos por SRI y metadata interna. Falta
completar el almacén pnpm para Linux amd64 glibc, incluidos los opcionales
nativos pertinentes para el build de Next. Usar el lock del árbol y
aplicar la instalación real con `--offline --ignore-scripts --frozen-lockfile`.
El control del lock no ejecutó la instalación ni compiló el frontend.
[Opciones pnpm 10](https://pnpm.io/10.x/cli/install).

## Generación y migración

1. Sobre el SDK Linux, fijar `CI=1` y `NEXT_TELEMETRY_DISABLED=1`.
   Probar `prisma generate --schema prisma/schema.prisma` sin red y
   verificar el cliente generado y el compilador de consultas WASM.
   Ejecutar después `tsc --noEmit` y `next build` explícitamente.
   La fábrica Prisma exige una URL de PostgreSQL. Para una compilación
   sin DB usar sólo un valor de prueba sin credenciales, con red
   deshabilitada, y verificar que las rutas prerenderizadas no dependan
   de una conexión. Conservar las variables de servicio reales para
   runtime y dejar vacías las credenciales de analítica.
2. Para `prisma migrate deploy`, usar
   `PRISMA_SCHEMA_ENGINE_BINARY=/opt/prisma/schema-engine` apuntando al
   binario Linux amd64 en la misma raíz privada. Se verificaron los
   SHA256 comprimido y descomprimido publicados por Prisma. El ELF
   `5d42b181631fd20bb0ecc5abcdba72575e7f467a0d52f4d5ef1ff28f0c74e6e9`
   requiere `/lib64/ld-linux-x86-64.so.2`, `libssl.so.3` y
   `libcrypto.so.3`. Las dos DSO Wolfi 3.6.5-r6 ya acreditadas para
   OpenCV contienen `OPENSSL_3.0.0`. Incorporarlas al entorno de
   migración y verificar loader y versión del engine en Linux antes de
   usar una base propia. OpenSSL 4 sigue en su SONAME original.
   [Override oficial del motor](https://www.prisma.io/docs/orm/v6/reference/environment-variables-reference#prisma_schema_engine_binary).
3. Ejecutar migraciones sobre la DB SupoClip vacía antes de iniciar el
   API Python. Probar Better Auth y transacción PostgreSQL. Empaquetar
   `.next/standalone`, `.next/static` y `public` para la aceptación
   privada del frontend. El motor de migración se entrega por separado
   del frontend que atiende peticiones.
