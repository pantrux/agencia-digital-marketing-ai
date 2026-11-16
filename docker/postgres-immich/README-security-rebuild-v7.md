# Reconstrucción PostgreSQL/VectorChord V7

## Cambio y evidencia

Esta unidad contiene cinco fuentes nuevas. No ejecuta SSH, SDK, Docker, SQL,
compiladores ni `.env` al importar o producir el plan. Las fuentes V6, su perfil,
freeze y log fallido están preservados byte exactos en
`.tools/evidence/postgres-immich-security-before-v7`.

V6 pasó su regresión funcional, pero su build falló antes de completar vchord:
`phase4-pg-security-build-capture-v6-cargo-build.log`, SHA256
`0ec6556d84bce145ea13da91b1bc5d5134ee05c79a09c89a271a96a947e52f71`,
recibo `72f9c39c…`. Pgrx no pudo abrir libclang.so.21.1.8 porque el ejecutable
host musl no admitía carga dinámica. El SDK quedó detenido y no se reintenta V6.
No se adopta ninguno de sus targets ni resultados parciales.

La comparación de fuentes acredita la pérdida de configuración: el CONFIG de
`augment_sdk_remote.py` fijado `997877a7…` tiene SHA256
`08671c66e5d2551e67f408fcb5a0346a1cfb366788beb2d962d1b7d32c0674ea`.
Conserva source/net, unstable, host y build. El NEW_CONFIG de V6 sólo conservaba
source/net (`38edea9f…`). V7 recupera el CONFIG completo cambiando únicamente
`directory = "/work/vendor"` al directorio vendor propio.

```toml
[unstable]
host-config = true
target-applies-to-host = true
[host]
rustflags = ["-Dwarnings", "-Ctarget-feature=-crt-static", "-Cpanic=unwind"]
[build]
rustflags = ["-Dwarnings", "-Ctarget-feature=-crt-static", "-Cpanic=unwind"]
```

Los true de la tabla unstable habilitan features; no son la opción homónima de
nivel superior. La documentación primaria de [Cargo host-config](https://doc.rust-lang.org/cargo/reference/unstable.html#host-config)
explica que activa configuración del host y cambia el valor por defecto de
la opción de comportamiento a false. [Cargo build.rustflags](https://doc.rust-lang.org/cargo/reference/config.html#buildrustflags)
distingue los flags del target de los de herramientas host cuando se proporciona
--target. Por eso RUSTFLAGS del entorno no demostraba por sí solo cómo se enlazó
el build script. No se cambia el SDK beta fijado ni RUSTC_BOOTSTRAP=1 existente.

## Límites y preservación

Se conservan 244 crates vendorizados, las dos correcciones de seguridad, todos
los checksums, lock, xtask offline/frozen, PG16/SIMD/cshim, SQLite 3.54.1 dinámica,
pgvector 0.8.5, VectorChord 1.1.1 y los flags target:

```text
-Dwarnings -Ctarget-feature=-crt-static -Cpanic=unwind -Clink-arg=-Wl,-rpath,$ORIGIN
```

No hay -Awarnings, cap global, modificación de vendor, actualización de paquetes,
Git, red, reinstalación del SDK ni adopción de targets previos. El harness conserva
versiones de registro exactas y su lock de cuatro paquetes: el harness propio,
anyhow 1.0.103, crossbeam-epoch 0.9.20 y crossbeam-utils 0.8.21. Los caps externos
siguen admitiendo exactamente warn o allow; el harness mantiene -Dwarnings sin cap.
Los casos Rust y los datos comprimidos del log real V5 conservan sus bytes/SHA.

| Función | Nueva identidad |
| --- | --- |
| Owner | `agencia-digital-marketing-ai:postgres-immich-security-rebuild:v7` |
| Work/target | `/work/.security-rebuild-v7` y su target privado |
| Outputs | `/outputs/security-rebuild-v7` |
| Staging | `/home/jandrade/.agency-pg-security-rebuild-v7` |
| Control | `<CONTROL_SDK>/security-rebuild-v7` |
| Prefijo | `MEDIA_POSTGRES_IMMICH_SECURITY_REBUILD_V7_` |
| Perfil | `.tools/postgres-immich-security-inputs-v1/security-rebuild-v7.profile.json` |

El CID propio f042 se reutiliza sólo después de que raíz respalde los pins,
confirme su parada y autorice la transición. Se mantienen NoForeign, nacimiento,
imagen c908, época de restart respaldada, 4 CPU, 12 GiB, jobs2, pids256 y red none.
Build conserva su presupuesto total de 1800 s y las reservas de limpieza.
Stop sigue independiente de inputs, SDK, SCA vencida y resultados de build.

## Prueba nueva de flags y ELF host

Sólo la etapa build añade `-vv` al Cargo exterior y `CARGO_TERM_VERBOSE=true`
al entorno de ese paso. La variable documentada en [term.verbose](https://doc.rust-lang.org/cargo/reference/config.html#termverbose)
permite conservar invocaciones de los procesos Cargo hijos sin cambiar xtask.
Graph y regression no reciben esa variable adicional. El límite finito de log
continúa siendo 4 MiB; si se excede se rechaza la etapa y se detiene el SDK propio,
sin presentar una salida recortada como completa.

Después de un build exitoso, el log debe identificar una sola invocación rustc
host de `pgrx-pg-sys-0.17.0/bindgen.rs`, con -crt-static y panic=unwind, sin
--target en esa compilación host. El out-dir debe corresponder exactamente a
`target/release/build/pgrx-pg-sys/<16hex>/out` bajo el work V7. El binario obligado
es `build_script_bindgen`, el nombre real que falló en V6; si hay además una
invocación distinta de build_script_build del mismo paquete, se conserva también.
Se rechazan comandos duplicados, flags ausentes y rutas fuera de target.

El lector nuevo se limita a estos artefactos generados bajo target V7. Abre por
FD sin seguir enlaces simbólicos y exige UID/GID 0, ancestros seguros y firmas
físicas iguales antes y después. Nlink 1 es el caso ordinario. Nlink 2 sólo se
admite cuando ambos nombres exactos derivados de un único extra-filename del
argv existen, comparten dev/inodo/tamaño/mtime/ctime y contenido, y cubren ambos
enlaces. No busca aliases con glob; no amplía el lector heredado inside_read ni
las reglas de inputs/SDK. Conserva las firmas y nombres físicos en el resultado.
Si build_script_build no tiene una invocación propia, no se exige ese alias.
Sólo analiza datos ELF64 amd64:
ET_EXEC o ET_DYN, una tabla de programas acotada, intérprete exacto
`/lib/ld-musl-x86_64.so.1` y un segmento PT_DYNAMIC. El proof conserva SHA/tamaño,
ruta, SHA del comando y del log. No ejecuta el binario de prueba ni fabrica una
biblioteca libclang alternativa. Estos campos permanecen sin acreditar hasta
que raíz ejecute V7; los tests locales sólo usan fixtures de datos.

## CLI y transición de raíz

```powershell
# Plan local puro, sin bóveda ni red:
python -B docker/postgres-immich/security_rebuild_v7.py plan
python -B docker/postgres-immich/security_rebuild_v7.py plan --output '.tools/postgres-immich-security-inputs-v1/security-rebuild-v7.profile.json'

# Sólo raíz, tras revisar perfil físico y fuentes:
python -B docker/postgres-immich/security_rebuild_v7.py plan --root-update-vault
python -B docker/postgres-immich/security_rebuild_v7.py receive --expected-profile-sha256 SHA_RESPALDADO --root-update-vault
```

Los modos siguientes se invocan por separado con el mismo perfil/SHA y flag
explícito de raíz: prepare, restart, materialize, graph, regression, build,
catalog y stop. Cada recibo se persiste antes de la transición posterior.
Graph requiere revisión PG16/SIMD literal; los modos SDK exigen también la
aprobación SCA real y vigente del SDK. El nuevo catálogo exige SQL/control y
licencias iguales al baseline, preserva SQLite y pgvector y sólo permite cambiar
vchord.so. No ejecuta SQL ni aprueba imagen final o SCA por inferencia.

Las funciones del CLI y el protocolo/collector conservan las APIs congeladas.
El guard del bootstrap V7 admite hasta 120 KiB (122880 bytes) de código UTF-8;
comprueba además el comando SSH completo con la envoltura sudo/shell y NUL,
manteniendo menos de 131072 bytes. No cambia framing ni transporte a stdin.
El perfil es pequeño por referencias; el lector canónico/ACL de `.env` admite
8 MiB. No se escriben secretos ni credenciales en perfiles, argv o logs. El agente
no ejecutó ninguna operación de bóveda o infraestructura durante esta entrega.

## Verificación

```powershell
python -B -W error::ResourceWarning -m unittest discover -s tests -p test_postgres_immich_security_rebuild_v7.py -v
python -m ruff check docker/postgres-immich/security_rebuild_v7.py docker/postgres-immich/security_rebuild_remote_v7.py tests/test_postgres_immich_security_rebuild_v7.py
python -m ruff format --check docker/postgres-immich/security_rebuild_v7.py docker/postgres-immich/security_rebuild_remote_v7.py tests/test_postgres_immich_security_rebuild_v7.py
```

Se conservan las regresiones V6 y se agregan CONFIG autenticado, flags host,
ELF dinámico, selección de verbose sólo para build, aliases físicos y transporte.
Son 51 IDs AST: 48 portables
y 3 POSIX. Windows omite honestamente los 3 POSIX; deberán ejecutarse en Linux
real en una unidad de arnés separada, sin SDK. Los tests de cargo_step usan mocks
del contrato de IO, no simulan que Cargo o Linux se hayan ejecutado. La memoria
ELF de fixture no acredita el binario real; la aceptación exige el proof futuro.
