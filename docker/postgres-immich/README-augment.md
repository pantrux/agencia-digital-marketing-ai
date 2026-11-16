# Aumento acotado del SDK con rustfmt

Estas cinco fuentes nuevas preparan la reanudación y el aumento del SDK tras
el fallo de compile V2. No se ejecutó SSH, Docker, instalador o SDK durante
su implementación. Se descargó e inspeccionó como datos únicamente el
archivo oficial autorizado, con SHA y raíz privada respaldados previamente.
V4, U1, compile V1/V2 y los 78 inputs quedan intactos.

## Artefacto y payload mínimo

`rustfmt-augmentation.json` fija el manifiesto Rust de 2026-11-05, SHA
325df53718c60181d663c7196f251f4ba9bac44ef2616896d1e33e8396a562db,
commit 01ddda61fada79bbc835476209518a6f68322984 y rustfmt-preview
1.11.0-beta.10 para x86_64-unknown-linux-musl. El archivo tiene SHA
6780fae76a43c0f7df06406813909697d2dc08445179260d73c8fb6b6ef4180d
y 2.938.832 bytes. Su catálogo contiene 16 archivos sin enlaces o tipos
especiales. Las fuentes primarias son el
[manifiesto Rust](https://static.rust-lang.org/dist/2026-11-05/channel-rust-beta.toml)
y la [política pgrx 0.17.0](https://raw.githubusercontent.com/pgcentralfoundation/pgrx/v0.17.0/pgrx-bindgen/src/build.rs).

Sólo se copian bin/rustfmt, LICENSE-APACHE, LICENSE-MIT y README declarados
por manifest.in. No se ejecuta install.sh ni se instala cargo-fmt. El prefijo
/opt/agency-rustfmt-v1 debe ser nuevo, root privado, y los archivos se crean
con EXCL/NoFollow. Se rechazan otro propietario, prefijo o payload adicional.
El archivo original conserva todos los avisos, aunque no instale el helper.

Rustfmt necesita librustc_driver-03ff6c29172e9b06.so. Se contrasta el driver
existente en /opt/agency-rust/lib contra su TAR rustc original: 310.364.368
bytes y SHA 626e0c6efaac2ce87441a7fffa7ee53f66c3a90df282803420771698d33b2e2f.
No se duplica, reemplaza o enlaza. La ejecución usa búsqueda privada
/opt/agency-rust/lib; compile V3 añadirá su SQLite custom antes de esa ruta.
Nunca se modifica PATH o la búsqueda de bibliotecas del host.

La versión de distribución beta.10 es distinta de la etiqueta de canal que
compila el binario: se espera `rustfmt 1.11.0-beta (01ddda61fa 2026-11-04)`.
La admisión exige además los SHA reales del archivo, binario y driver.
Se registra la salida real; esta inspección estática no la presenta como
una ejecución ya realizada.

## Plan local y transiciones de raíz

El único CID es f042555d8eabccbc31cb2b73d2ad349aa89b5ce92050862254bf832178f73a7d.
El perfil está ligado a compile V2 777b9fa2, fallo 39443be3 y log 898807c4;
usa como instancia anterior el RESTART incluido en aquel perfil. No adopta
el PID del start V4 histórico ni sobrescribe sus B64.

El modo predeterminado sólo lee metadata y bóveda local:

```powershell
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/augment_sdk.py plan --output 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\augment-v1-profile.json'
```

Antes de conectar, raíz respalda bajo `MEDIA_POSTGRES_IMMICH_AUGMENT_V1_`
PROFILE_B64, PROFILE_SHA256, LOCAL_SHA256, REMOTE_SHA256 y MANIFEST_SHA256.
Las variables RUSTFMT_INPUT_ROOT/OWNER/ARCHIVE_URL/ARCHIVE_SHA256/
INSTALL_PREFIX del prefijo MEDIA_POSTGRES_IMMICH_RUSTFMT_ deben coincidir
literalmente con los valores ya respaldados. El staging local exige su
marcador propio. No se imprime ninguna credencial ni se pasa al contenedor.

Sólo raíz ejecutará cada etapa separada, tras revisar el resultado anterior:

```powershell
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/augment_sdk.py observe-stopped --profile 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\augment-v1-profile.json' --expected-profile-sha256 SHA_RESPALDADO --root-update-vault
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/augment_sdk.py restart --profile 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\augment-v1-profile.json' --expected-profile-sha256 SHA_RESPALDADO --root-update-vault
& '.tools/venvs/synology-audit/Scripts/python.exe' -X utf8 -B docker/postgres-immich/augment_sdk.py augment --profile 'D:\Proyectos\Agencia Digital de Marketing AI\.tools\postgres-immich-build\augment-v1-profile.json' --expected-profile-sha256 SHA_RESPALDADO --root-update-vault
```

observe-stopped comprueba NoForeign, PID 0/parada limpia, Store78, software
histórico y el fallo V2. Guarda y respalda STOPPED_B64 antes de permitir
restart. restart inicia sólo el hold original, verifica nuevos PID/StartedAt,
4 CPU/12 GiB/swap adicional 0/pids 256/red none, SDK3, 110 APK, vendor,
Cargo.lock, configuración y xtask. Cataloga parciales V1/V2 sin modificarlos.
Sólo ese catálogo de parciales admite leer hardlinks regulares de Cargo;
las fuentes y SDK admitidos siguen exigiendo archivos sin hardlinks.
Raíz respalda RESTART_B64 antes de augment.

augment exige esa instancia viva y el recibo literal respaldado. Conserva
el único TAR adicional en control host root:0600 y lo copia al namespace
propio /work/.augment-agency-v1, comprobado vacío salvo el verificador.
No escribe en inputs. El frame de entrada tiene límite 8 MiB para transportar
el Base64 del archivo de 2,94 MB; esto no amplía los logs o capturas de 4 MiB.

## Prueba real exigida antes de compilar

Después de contrastar payload/driver, la etapa futura ejecuta el binario real
para obtener versión, rechazar con --check un fragmento público sin formato,
formatearlo con --edition 2021, comprobar bytes esperados y aceptar --check.
La prueba no invoca Cargo, compiladores, red o Git. Se repiten hashes después
de ejecutar; los payloads SDK3 y sus metadatos no se cambian.

Guarda rustfmt-augmentation-receipt.json en outputs y su envelope host en
augment-v1-augmentation.json. Sólo esta etapa declara sdk_executed=true
porque ejecuta el formatter; compiler_executed e installer_executed son false.
Las etapas de observación y verificación no ejecutan SDK. Raíz respalda y
relee AUGMENT_B64 antes de que una nueva receta compile V3 pueda usarlo.

Las transiciones son EXCL, sin reintento automático. Los fallos conservan
parciales y diagnóstico privado y detienen sólo el CID con NoForeign; esa
parada no confía en outputs escribibles. La verificación tiene plazo 180 s,
el formatter 90 s externo con subpruebas acotadas, el helper 240 s y el cliente
270 s con margen de limpieza. No hay reinstalación de APK/SDK3 ni SQL/build
de imagen final. La siguiente receta deberá usar WORK/output/SQLite/target
V3 y dejar los objetos V2 sin uso.

Las pruebas locales acreditan guardas, transporte y validación de datos con
fixtures. No acreditan la reanudación, el formatter o la compilación real.
