# Compilador Prisma de PostgreSQL en el frontend

La prueba integrada llegó a HTTP 200 y completó las 27 migraciones Prisma y
26 del backend. El registro por correo devolvió HTTP 500 porque Prisma Client
6.19.3 intentó abrir `src/generated/prisma/query_compiler_bg.wasm`. El
empaquetado standalone conservó `schema.prisma` y `package.json` del cliente,
pero omitió el binario. El SDK ya había generado Prisma correctamente.

El binario recuperado mide 1.950.269 bytes y tiene SHA256
`8ad65dab5c22d8ce70db4bcb6c8c0a5069feb9841d9154cfb59f2003c2bd0236`.
Los bytes coinciden en tres copias verificadas. La primera está en
`prisma@6.19.3`, ruta `build/query_compiler_bg.postgresql.wasm`. La segunda
está codificada en base64 en
`@prisma/client@6.19.3/runtime/query_compiler_bg.postgresql.wasm-base64.js`.
La tercera está en el almacén offline de pnpm del build original. El recibo
del directorio `.tools/backup-source-build/supoclip-frontend-prisma-wasm-v1`
registra las huellas de ambos tarballs npm, su integridad SHA-512 y el
destino runtime. [Prisma 6.19.3](https://www.npmjs.com/package/prisma/v/6.19.3) ·
[Prisma Client 6.19.3](https://www.npmjs.com/package/@prisma/client/v/6.19.3).

## Imagen candidata de una capa

Usar un directorio de contexto nuevo que contenga únicamente
`Dockerfile.frontend-prisma-wasm` y el WASM recuperado. La referencia
`base_tag` debe resolver al manifiesto local
`sha256:aec042d9f6589e5f5fad61cb5e481a57320569cda23f50bba1f2befe493d8c05`,
configuración `sha256:8d4eefb264d202029e60460a25ad0b58f07292748efc126ff4d40ae33b520198`.
Comprobar el ID con `docker image inspect` antes y después del build.
Fijar `candidate_tag` a un nombre propio nuevo. El Dockerfile agrega
exclusivamente el WASM con dueño `0:0` y permisos `0444`.

```bash
sha256sum "$context/query_compiler_bg.wasm"
docker --context default buildx build --builder default \
  --platform linux/amd64 --network none --pull=false --provenance=false --sbom=false \
  --build-arg "BASE_IMAGE=$base_tag" --file "$context/Dockerfile.frontend-prisma-wasm" \
  --tag "$candidate_tag" "$context"
candidate_id=$(docker --context default image inspect --format '{{.Id}}' "$candidate_tag")
```

Guardar el ID y el recibo de build. Exportar la imagen por stdout para el
reescaneo local si la VM tiene poco espacio. El smoke del operador verifica
en un proceso sin red la huella, el modo y la compilación WebAssembly.

## Sustituir sólo el frontend del ensayo

Colocar `replace_trial_frontend.py` junto al `trial_operator.py` del ensayo.
El operador lee el plan y los CID que ya quedaron registrados. Exige una
capa adicional, configuración de imagen idéntica y WASM válido. Guarda una
copia histórica del plan y de los CID antes de usar `docker compose up
--no-start --no-deps --force-recreate --no-build --pull never frontend`.
Antes de arrancar, valida el CID nuevo, los otros seis CID y la configuración
actualizada. Pausa el proxy durante la sustitución y arranca el mismo CID
para que Nginx resuelva la dirección del frontend nuevo. Conserva las bases,
los datos y las migraciones del ensayo. El plan fija la imagen del CID
`migrate` por separado. En un plan anterior, el operador toma esa identidad
del recibo original y vuelve a cotejarla con el CID existente antes de
guardar el plan actualizado. El arranque reutiliza el resultado de una
migración que salió con código 0 y vuelve a consultar sus filas en PostgreSQL.

```bash
python3 -I -B "$config_dir/replace_trial_frontend.py" \
  --root "$trial_root" --image "$candidate_id"
```

Los recibos intermedios quedan en `frontend-replacement-*` dentro de la raíz
del ensayo. Si se interrumpe después de `up`, cotejar los CID del historial
con `docker inspect` y los dos archivos principales antes de reanudar. El
operador comprueba `/healthz` desde la VM. Repetir después la sonda completa
de registro, inicio de sesión, carga y cierre con una cuenta sintética
nueva. El resultado del HTTP 500 se conserva como evidencia previa.
