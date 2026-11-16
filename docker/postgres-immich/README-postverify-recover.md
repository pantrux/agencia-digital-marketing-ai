# Recuperación de lectura de dos bibliotecas 0700

Cinco archivos nuevos; no modifica fuentes congeladas, permisos, salidas o
recibos existentes. La unidad sólo prepara una aceptación independiente de
los datos conservados. No ejecuta SDK, compilador, SQL, instalación, Docker
exec/start/cp o imagen. Sólo raíz podrá aplicar tras revisar y respaldar pins.

## Causa observada y autorización limitada

La aceptación anterior falló con recibo
0cab178b0f3e4d255f4d6f5e0513fd2f7adc140977d8eea586ed543489950b57,
perfil97c0d701… y fuente72970b20…. El lector admitía modos0600/0644/0755/0444,
pero omitía0700. La auditoría autorizada del árbol propio encontró exactamente
dos archivos rechazados, ambos root:root y nlink1. Xtask instala vchord.so0755;
la copia posterior con umask077 produce0700. SQLite generado/copiado bajo ese
umask también conserva0700. No es una concesión de escritura a otro usuario.

La auditoría sólo leyó metadatos y después bytes por FD, con NoForeign/PID0
antes y después. Ningún chmod/chown, compilación o reinicio fue necesario.

| Archivo dentro de runtime-root | Device/inode | Bytes | SHA256 |
| --- | --- | --- | --- |
| usr/local/lib/postgresql/vchord.so | 2050/2530213 | 9178776 | 2b3f5c83265950603fa192dc84171b0024bde834ac2c64ec0842cba8ae07c0e7 |
| usr/local/lib/postgresql/libsqlite3.so.0 | 2050/2530231 | 1893504 | 78711a51904d2ce47d4c16cb53365e85d73bc15cd4aa6d10c4d2ef3754d81c53 |

Ambos requieren UID/GID0, nlink1 y modo0700 exactos. El manifiesto separado
postverify-recovery-files.json fija esos datos y las huellas de las dos
auditorías. El lector recibe logical_path del recorrido por FD; no infiere
la excepción por basename ni la concede a otro archivo. Comprueba metadata
antes de leer, SHA después y la identidad retenida. Incluso un inodo nuevo con
los mismos bytes, o un modo cambiado a0755, se rechaza en estos dos paths.
Para los demás archivos permanece intacto el conjunto de modos anterior.

La comprobación usa datos leídos realmente de .85. Las pruebas de la función
usan proveedores FD controlados en Windows; no se presentan como un ensayo
nativo de Linux. La identidad y los hashes reales figuran en:

- .tools/evidence/phase4-postgres-immich-accept-stat-audit.json,
  SHA d5c8c3c193b22d3c3ca8c86c95af79e5576b014830481e0a0e9a4e9ca4172c80.
- .tools/evidence/phase4-postgres-immich-accept-exact-files-sha.json,
  SHA eeb465dc537d8c41bb8a928a58b5b2d84e646aedb0c5b9200c82ff7b3c26ec22.

## Cadena de evidencia y namespace nuevo

Stage `postverify-v3-recover`, prefijo
`MEDIA_POSTGRES_IMMICH_POSTVERIFY_V3_RECOVER_`. Su perfil envuelve una sola vez
el perfil97c literal y el fallo0cab. No reconstruye ni altera la historia de
compile V3 b460, postverify407e, los perfiles correspondientes o STOP7c2.
Los archivos físicos de ambos intentos fallidos se comparan contra esos
respaldos antes de continuar. El SDK permanece detenido y con la misma
identidad NoForeign durante toda la lectura.

Conserva los23 miembros exactos Cargo y las relaciones de roles previamente
revisadas, el GET fijo del CID, límite4MiB y presupuesto180s/cliente190s. Guarda
el TAR EXCL antes del parser. Se mantienen las pruebas SQLite dinámico,
sourceid3.54.1, RUNPATH $ORIGIN, ELF/símbolos/flags, controles y conjunto SQL de
pgvector y VectorChord, y las cinco licencias contra los TAR/ZIP fijados.
No se elimina una feature bundled ni se oculta C vendorizado por este cambio.

Sólo añade en el control host propio `.postverify-v3-recover-started` y sus
archivos -profile.json, -archive.tar, -build-output.txt, -artifact.json y .json,
todos EXCL/root privados. No borra, adopta o sobrescribe intentos anteriores.
Un fallo conserva su evidencia y requiere revisión; no hay replay automático.
El éxito deja referencias a los fallos previos y al manifiesto exacto, con
sdk_executed/compiler_executed=false y sql_verified/final_sca_verified=false.

## CLI de raíz y respaldo previo

Plan sin bóveda/red y generación de perfil local ligado a la historia:

```powershell
python -X utf8 -B docker/postgres-immich/postverify_recover.py plan
python -X utf8 -B docker/postgres-immich/postverify_recover.py plan --output '.tools/postgres-immich-build/postverify-v3-recover-profile.json'
```

Raíz respalda PROFILE_B64, PROFILE_SHA256, LOCAL_SHA256, REMOTE_SHA256 y
MANIFEST_SHA256 bajo el prefijo nuevo antes de usar:

```powershell
python -X utf8 -B docker/postgres-immich/postverify_recover.py apply --profile '.tools/postgres-immich-build/postverify-v3-recover-profile.json' --expected-profile-sha256 '<SHA respaldado>' --root-update-vault
```

El CLI de raíz es el único que añade y relee RECEIPT_B64 nuevo. Reutiliza
Paramiko5.1.0, identidad SSH y pin existentes del CLI V4, sin credenciales
nuevas ni valores en logs. El agente sólo realizó las lecturas autorizadas y
preparó estas fuentes; no aplicó la recuperación.

Tras un éxito, una unidad distinta actualizará únicamente el gate y lector
FD del runtime congelado: al promover estos dos archivos también debe pasar
logical_path al lector exacto, sin chmod de origen. Construcción de imagen,
carga SQL, SCA final y aceptación de producción siguen pendientes.
