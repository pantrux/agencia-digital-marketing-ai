# Repetición n8n sobre el almacén nativo de pnpm 12

El CID de replay V1 terminó con código 1 y PID 0 antes de invocar pnpm. El preflight esperaba un archivo `tarball-integrity` que usa el requester TypeScript de pnpm 11. La lectura física del almacén Linux confirma `v11/files` y `v11/index.db` como únicas entradas. La fila de wa-sqlite está en el índice SQLite. El fallo V1 queda preservado.

La implementación Rust de pnpm 12.4.2 consulta el índice y los archivos CAS por SRI e ID antes de abrir un tarball. El modo `frozenStore` usa SQLite con `immutable=1`. El replay V2 exige ese árbol físico, el mismo SHA del índice, las 3.125 claves acreditadas y la clave del tarball local. El archivo local que se entrega al workspace conserva el SHA de su versión de referencia. [Lectura del CAS](https://github.com/pnpm/pnpm/blob/v12.4.2/pnpm/crates/tarball/src/ingestion.rs#L39-L81), [clave de tarballs](https://github.com/pnpm/pnpm/blob/v12.4.2/pnpm/crates/deps-restorer/src/create_virtual_store/cache_keys.rs#L254-L268), [SQLite inmutable](https://github.com/pnpm/pnpm/blob/v12.4.2/pnpm/crates/store-dir/src/store_index.rs#L177-L205).

```powershell
python -I -B docker/n8n/n8n_server_replay_vm_v2.py plan
```

Con el SHA del plan respaldado, usar `create`, `start` y `status` por separado con las entradas y recibos descritos en [la guía del replay](README-SERVER-BASELINE-REPLAY.md). El operador confirma que V1 sigue detenido antes de crear `agency-n8n-server-baseline-replay-v2` bajo `baseline-replay-v2`. Respaldar el nuevo CID antes del arranque. El store completo permanece de sólo lectura. No se crea ningún marcador ni se copia la base. La verificación exige 3.476 entradas de política, 3.164 snapshots, cero descargas, el lock intacto y el índice original sin cambios.
