# Repetición offline del baseline n8n

La primera instalación Linux terminó con código 0 y lock intacto. El operador V1 se detuvo al comparar las 3.476 entradas verificadas por pnpm con su constante de 3.478. El documento principal del lock tiene 3.475 paquetes de registro y un tarball local. El documento de bootstrap es separado. El error del operador y el CID original detenido se conservan como evidencia.

El catálogo SQLite original tiene 3.125 claves, 49.029.120 bytes y SHA-256 `9f134672f2ec03d43bfd7809e837b83154ad4f1c2ccc4e2410d5aa35a3c42d14`. Las 3.124 claves de registro coinciden exactamente con las rutas servidas. La clave restante corresponde al tarball local de wa-sqlite. Los 18 tarballs no servidos corresponden a snapshots opcionales. El log registra 520 advertencias GET de 486 rutas permitidas que después se sirvieron e ingresaron al índice. El agregado histórico de 3.623 rechazos no conserva la causa de cada evento y queda fuera de la aceptación del replay.

## Operación

1. Generar el plan local y respaldar su SHA. El plan usa la imagen Linux musl original y las identidades de recepción, creación, estado terminal e índice ya verificadas. La guarda requiere 5 GiB y 350.000 inodos libres en el volumen de trabajo.

   ```powershell
   python -I -B docker/n8n/n8n_server_replay_vm.py plan
   ```

2. Invocar `create --plan-sha256 <sha-respaldado> --receipt <ruta-nueva>` con el mismo DTO privado `ssh`, `parent` y `daemon` que el [operador V1](README-SERVER-BASELINE-VM.md). El CID nuevo y su `create.json` se guardan en `baseline-replay-v1`, al lado del control original. Respaldar el CID y el recibo completo antes de arrancar.
3. Invocar `start` con otro recibo nuevo y añadir `created` con la respuesta completa de `create` al DTO de stdin. `start` coteja el respaldo contra el recibo físico, verifica el CID creado y reserva el intento antes del arranque.
4. Consultar `status` con los mismos campos de `start` y un recibo nuevo. El estado `exited` con código 0 requiere un recibo del replay verificable para declarar `baseline_verified=true`.

El contenedor monta el store original completo de sólo lectura y conserva el marcador `tarball-integrity` de wa-sqlite. pnpm 12.4.2 abre el índice SQLite con `immutable=1` en modo `frozenStore`, por lo que el operador no crea una copia del índice. Usa `copy` para importar desde el bind RO al trabajo nuevo. La raíz de sólo lectura, la red `none`, el plazo de 1.050 segundos y los límites del CID se verifican al crear y antes de arrancar. [Índice de sólo lectura de pnpm 12.4.2](https://github.com/pnpm/pnpm/blob/v12.4.2/pnpm11/store/index/src/index.ts), [verificación de tarballs locales](https://github.com/pnpm/pnpm/blob/v12.4.2/pnpm11/installing/package-requester/src/packageRequester.ts), [límites de enlaces duros en Linux](https://man7.org/linux/man-pages/man2/link.2.html).

La repetición exige el mismo lock y el mismo índice antes y después, 3.476 entradas de política, 3.164 snapshots y cero descargas. Los scripts de dependencias permanecen deshabilitados. Esta prueba conserva el SQLite de referencia y el alcance previo a compilación.
