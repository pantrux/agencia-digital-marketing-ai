# Reconstrucción de los bundles Edge de SupoClip

El frontend probado usa Next 15.5.28. Su standalone contiene Undici 6.21.0 en dos bundles. La auditoría atribuida por bytes encontró siete avisos altos para esa identidad. Next 15.5.29 sigue en mantenimiento y contiene exactamente los mismos bytes en ambos. Next 16.4.5 cambia `edge-runtime/index.js` y conserva Undici 6.21.0 en los dos. El cambio de rama no resuelve el hallazgo. [Política de soporte](https://nextjs.org/support-policy) · [Next 15.5.29](https://registry.npmjs.org/next/15.5.29) · [Next 16.4.5](https://registry.npmjs.org/next/16.4.5)

| Entrada o evidencia | SHA-256 |
| --- | --- |
| TAR npm Next 15.5.29, 31.330.085 bytes | `c3ed93e652d9c76afb11b722356ac786cc1fffb3c417713070fde6d553b070a3` |
| TAR npm Next 16.4.5, 43.659.658 bytes | `a9b934c4deda237c1a269da4e68be28f39c7c5e07296a891e0a8346ce847ef97` |
| `dist/compiled/edge-runtime/index.js` en 15.5.28 y 15.5.29 | `06703ed6b001b0f02f8043bb499f3716f2c3b76b7df628a1cea30d1ee671edb2` |
| `dist/compiled/@edge-runtime/primitives/load.js` en los tres Next | `d36b91e07ff16e770688afc5578702030e7eccaa3de59303cd6b0cef0707d230` |
| Undici 6.29.1, TAR npm de 297.796 bytes | `29c4b8e0b2d86cb8795571c1959012ce057a14ecfd34858335f47c31e770a04f` |
| Fuente Edge Runtime `edge-runtime@4.0.1`, TAR de 536.350 bytes | `4d5f17ebc1294aea74e889196f2eccaf39f9011ed2f379c548bce8c5879ba58e` |
| Fuente Next `v15.5.29`, TAR de 48.428.096 bytes | `bf8f5ff4e994ee37f3d183eb44b844fc8bbb2b6a7aa87e2cfb1f524758646d8a` |
| TAR npm pnpm 8.4.0, 3.016.144 bytes | `d1a8ef1df5ac0b2898c157fdb056ae16ff167abb96d8f153eaf5f8b8823a29bc` |
| TAR npm Edge Runtime 4.0.1 | `798c01bf3fe06016af4651b2920ce16db5b82eb54c6015af3e0ab1c9b82793b3` |
| TAR npm `@edge-runtime/vm` 5.0.0 | `b93d227af99e8fd58e582bab5278e86183705fad1e530a2bf08c8304db6338d9` |

Undici 6.29.1 es el piso de la rama 6 que cubre los siete altos observados. Los parches se publicaron en 6.24.0 para CVE-2026-1526, CVE-2026-1528 y CVE-2026-2229, en 6.27.0 para CVE-2026-12151, en 6.28.1 para CVE-2026-19534 y en 6.29.1 para CVE-2026-109383 y CVE-2026-87018. La clasificación de severidad anterior corresponde al informe de la imagen. [Versiones y correcciones de Undici](https://github.com/nodejs/undici/releases) · [Avisos oficiales](https://github.com/nodejs/undici/security/advisories)

## Regenerar Edge Runtime Primitives

1. Crear una raíz de build nueva. Verificar los SHA anteriores, el SRI del registro de cada tarball npm y extraer sólo miembros regulares y directorios con rutas relativas canónicas. Usar Node 24 del SDK frontend y pnpm 8.4.0 para el lock de Edge. El archivo fuente del repositorio se obtiene como un TAR HTTPS oficial. [Fuente Edge Runtime](https://github.com/vercel/edge-runtime/tree/edge-runtime%404.0.1)
2. En `packages/primitives/package.json`, cambiar sólo la versión exacta `undici` de `6.21.0` a `6.29.1`. En el lock upstream, actualizar `specifier`, `version` y la única entrada `/undici@6.21.0`. La versión nueva conserva el requisito de Node `>=18.17` y no tiene dependencias npm. Sustituir el SRI de esa entrada por `sha512-EvRlUt/QqAEvXdQeRMYUfwOQsfzpDztykrG2owGEE/rBOBoYv9uF39ZzsNDh4lTFHhTr2XOZjKkYgueD12ZmGA==`. [Metadata Undici](https://registry.npmjs.org/undici/6.29.1)
3. En el `build.ts` original `251ad12d134d6b7d22ad03daf6958d4dbe6540bba806b861dde6f086e1c088fa`, retirar exactamente los plugins `alias-undici-core-request` y `request-host-header`. El primero compilaba una copia de `core/request.js` basada en 6.21.0 que relajaba restricciones de cabeceras. El segundo quitaba un borrado de `Host` del Fetch de Undici. Mantener `import-path`, target, generación de polyfills, formatos y tipos. Conservar el archivo fuente del primer parche como evidencia fuera del paquete resultante. Esta decisión conserva las validaciones de cabeceras de Undici original. Si SupoClip depende de una excepción anterior, la prueba queda pendiente de un análisis específico del flujo antes de reintroducirla. [Script oficial](https://github.com/vercel/edge-runtime/blob/edge-runtime%404.0.1/packages/primitives/scripts/build.ts#L58)
4. Verificar las huellas derivadas de la receta y resolver únicamente la clausura de build desde el lock congelado. Preparar previamente el almacén acreditado de paquetes. Ejecutar en el workspace nuevo, con usuario sin privilegios, entorno limpio, sin credenciales y sin acceso externo durante el build. El importador del lock conserva esbuild 0.24.0, tsup 8.0.0, ts-node 10.9.2 y TypeScript 5.7.2. Evitar los scripts de ciclo de vida e invocar directamente las entradas de build fijadas. [Lock upstream](https://github.com/vercel/edge-runtime/blob/edge-runtime%404.0.1/pnpm-lock.yaml)

| Archivo Edge corregido | SHA-256 |
| --- | --- |
| `packages/primitives/package.json` | `5c24ca1ffe020da804ab19acfdce05017295551857e75ab7b3439aa01c8aa5ff` |
| `pnpm-lock.yaml` | `f90077da3e27a4b038c58856cda90e5035737d7835c2c64fa013b53f0d3cf41e` |
| `packages/primitives/scripts/build.ts` | `be305d47bd5d8de09803788e6eb579d021a2b3d93acc374e373f6ff022338ac6` |

## Regenerar ambos bundles de Next

Usar el `taskfile.js` SHA `373543d1c25262e9ff0e080ae64e26dbda8167c33e06f447d7194cd73a536bf9` y su plugin `taskfile-ncc.js` SHA `717a9a720881e3c0b24556b8bedeaa5606d38c84a4cd783d80d68eff6370f3ae` de Next 15.5.29. Su lock fija pnpm 9.6.0, Taskr 1.1.0 y ncc 0.34.0. Reutilizar las fuentes y opciones oficiales en una clausura aislada de ambos tasks. El `node_modules` que se entrega al task debe ser una copia privada, ya que el task modifica el archivo `edge-vm.js` de esa copia antes de empaquetar. Ejecutar los tasks específicos y conservar la clausura y sus recibos. La tarea global `ncc` regenera componentes ajenos a este cambio. [Tasks Edge de Next](https://github.com/vercel/next.js/blob/v15.5.29/packages/next/taskfile.js#L345) · [Plugin ncc](https://github.com/vercel/next.js/blob/v15.5.29/packages/next/taskfile-ncc.js)

1. `ncc_edge_runtime_primitives` copia tipos y distribución de `@edge-runtime/primitives` 6.0.0 recién generada. Su salida incluye `load.js`, `fetch.js`, polyfills, archivos de licencia y metadatos. El `fetch.js` separado está en el paquete npm y no se halló en el standalone previo.
2. `ncc_edge_runtime` compila `edge-runtime` 4.0.1 y `@edge-runtime/vm` 5.0.0. El VM importa `@edge-runtime/primitives/load`. El bundle ncc incorpora una segunda copia de Undici a través de ese subpath. Confirmar que las dos resoluciones de Primitives apuntan a los mismos bytes nuevos y que el archivo y la resolución externa que prepara el task son los esperados. [VM upstream](https://github.com/vercel/edge-runtime/blob/edge-runtime%404.0.1/packages/vm/src/edge-vm.ts)
3. Reempaquetar Next 15.5.29 como artefacto interno derivado. Comparar catálogo de archivos, tipos, licencias, manifiestos y hashes contra el TAR npm. Limitar las diferencias a los dos directorios generados y al registro de procedencia externo. Si se elige un sufijo SemVer para el paquete interno, verificar sus rangos de pares en el lock final. Conservar la licencia y el nombre npm auténticos, y registrar la versión base y el SHA derivado en el recibo. Exigir identidad 6.29.1 y trazabilidad a código en ambos bundles por separado, incluidos `fetch.js` y los archivos `LEGAL` del paquete. Un marcador textual de versión aislado no acredita el reemplazo.
4. Actualizar `next` y los compañeros que resuelve su nuevo manifiesto en un lock nuevo del frontend. El registro fija `@next/env` 15.5.29 y, para este SDK Linux glibc amd64, `@next/swc-linux-x64-gnu` 15.5.29. La prueba existente usa también `eslint-config-next`, que resuelve `@next/eslint-plugin-next` de su misma versión. Verificar esos cambios y cualquier otro delta de resolución con pnpm 10.27.0 y el almacén offline del frontend. No aplicar un reemplazo global de la cadena `15.5.28` al lock. Regenerar el standalone con la receta ya probada y comparar sus dos archivos físicos y sus importaciones con el paquete derivado. [Receta offline del frontend](README-FRONTEND-LINUX.md)

## Sondas y aceptación

Ejecutar la sonda primero contra el paquete oficial, después contra el paquete interno y al final contra la copia que resulte en el standalone. El modo `bundles` abre un listener loopback efímero, prueba POST con stream y Response.error en ambas copias y verifica `dispatchFetch` de Edge. Corrió con éxito sobre el TAR npm 15.5.29 en Node 24.13.0 Windows. La aceptación del candidato debe repetirse en Node 24.22.2 Linux.

```sh
node smoke_next_undici.cjs bundles /ruta/privada/node_modules/next
node smoke_next_undici.cjs application 3000 /opt/supoclip/frontend http://192.168.0.85:3080
```

El modo `application` reutiliza los tres resultados del ensayo frontend V3. Valida `/sitemap.xml` con el origen configurado, un archivo estático por igualdad de SHA y `POST /api/upload/authorization` sin sesión con 401. Usar el contenedor de prueba propio, rootfs de sólo lectura, red aislada, UID 1000, valores sintéticos y el mecanismo de parada por CID ya acreditado. Las pruebas de sesión o datos que requieran PostgreSQL quedan separadas. La aceptación SCA vuelve a evaluar únicamente las identidades y bytes afectados en este paso y conserva el alcance incompleto del inventario general.

## Ensayo funcional mínimo con la imagen existente

La imagen `sha256:aeee7514bdf53e44f8241ce884533025d266c5c8f9d0c4956e1809080e26df28` trae Next 15.5.28 físico. Su `package.json` y los dos bundles coinciden con el TAR npm 15.5.28. Los tres consumidores `dist/server/web/sandbox/{context,sandbox,index}.js` tienen los mismos bytes en 15.5.28 y 15.5.29. Los nueve archivos auxiliares que el standalone conservó de ambos directorios Edge también coinciden byte a byte con los de la prueba local. La actualización de los dos bundles puede probarse sobre esta imagen mediante dos montajes de archivo de sólo lectura. Next mantiene la identidad 15.5.28 durante este ensayo. El `fetch.js` separado no figura en el standalone y su código ya se incluye en el `load.js` reconstruido.

La prueba local preparó el lock de Edge con 930 paquetes en 56,1 s. El build de `format` tomó 5,578 s. `tsup` rechazó las rutas absolutas Windows del build de Primitives. Una copia de prueba con las dos listas de entradas relativas a `packages/primitives` compiló en 5,187 s y conservó el script Linux original de la receta. Se extrajeron del lock de Next las 14 identidades para ncc, con SRI exacto también comprobado contra los TAR npm. El control ncc sobre Primitives original dio 525.800 bytes frente a 525.805 del bundle npm. El layout de ensayo y el bundle npm tienen distinta identidad. Ambos bundles nuevos juntos pasaron la sonda `bundles` con scope CommonJS del paquete oficial. Se probó `application` contra una fixture HTTP local y se rechazó un chunk de bytes distintos.

El recibo `phase4-supoclip-next-undici-overlay-v1.json` fija la imagen, las preimágenes, los productos, los archivos auxiliares y la sonda. El operador debe verificar esos hashes, preparar una raíz propia en Linux con los dos productos regulares y fijar su identidad antes de crear el contenedor. Usar la configuración del ensayo V3 y añadir estos dos montajes, con `BASE` igual a la raíz privada del operador y `NEXT` a la ruta absoluta física del manifiesto. Montar el destino real de pnpm, sin montar sobre `node_modules/next`, que es un enlace. Mantener `--network none`, `--read-only`, UID/GID 1000 y los tres tmpfs del ensayo V3.

```sh
--mount "type=bind,src=$BASE/edge-runtime.index.js,dst=$NEXT/dist/compiled/edge-runtime/index.js,readonly"
--mount "type=bind,src=$BASE/primitives.load.js,dst=$NEXT/dist/compiled/@edge-runtime/primitives/load.js,readonly"
```

Guardar el CID antes de arrancar. Verificar la imagen, que los dos montajes coinciden con el plan y que el resto del contrato del ensayo se preserva. Después del arranque y la salud, ejecutar los dos modos por stdin dentro del CID, con plazo y limpieza del operador. El `PUBLIC_ORIGIN` debe ser el que ya aparece en el sitemap del standalone ensayado.

```sh
docker exec -i "$CID" node --input-type=commonjs - bundles "$NEXT" < smoke_next_undici.cjs
docker exec -i "$CID" node --input-type=commonjs - application 3000 /opt/supoclip/frontend "$PUBLIC_ORIGIN" < smoke_next_undici.cjs
```

Registrar ambos resultados y parar por el CID retenido. Retirar los dos montajes al crear otro ensayo revierte a los bytes oficiales. El ensayo Linux sigue pendiente. La publicación de un artefacto de aplicación con el cambio requiere el lock derivado, la reconstrucción Linux del standalone y la auditoría de los bytes resultantes.
