# Aceptación SQL funcional V2 — borrador para revisión

Esta unidad permite a raíz autorizar una prueba funcional aislada del runtime V3. No declara terminada la SCA ni aprueba producción, CI, Immich o sus migraciones. V1 y `acceptance.sql` permanecen intactos. El agente sólo preparó código y pruebas controladas: no leyó la bóveda, abrió SSH ni ejecutó SQL, Docker o SDK.

## Sujeto y evidencia física

Se acepta exclusivamente el manifiesto `sha256:a00b7ac5ed30069abde2702a2b2b3d8321ea0f22d2ba64cc9f836c86b4c47601`, configuración `sha256:60331f9322af6e5449db67a68975e7ce57f58562af19843bc4de47ffd46f709f` y SQL SHA256 `3615b4132cdbc7157fc766ad88c326b1832d132ea7a05e1de0c8bce59899885b`.

El CLI relee dos archivos públicos de evidencia, sin exigir que `.tools/evidence` sea privado. Sus ubicaciones son cerradas y sus bytes deben coincidir:

| Evidencia bajo `.tools/postgres-immich-security-inputs-v1/` | SHA256 del archivo |
| --- | --- |
| `security-runtime-v3.prepare.2991994f50484581ae7fe13e895786ad.json` | `a8ffd5244af037a40dfb5be2b924ab9c2ece75b7722cb7fe4cb9272e5e89f3fc` |
| `security-runtime-v3.build.f22671b0e0c14d108dc3a148b7480b2e.json` | `f0f8fab22ffedfa804091ff42936657174c21b7aefc62d497bd30e25373b3d58` |

Ambos deben tener envoltura canónica positiva, fuente/perfil V3, ABI verificada sin bibliotecas SDK copiadas y los cuatro hashes nativos exactos. La huella ABI es `c96f7f677e737daaf30b99bdc8e6fe9d5acd041143b1c9f2e676c5b72686627a`. El Build debe enlazar el recibo Prepare y a00/6033.

En `.85`, el padre es `/var/lib/media-platform/build/postgres-immich-security-runtime-v3/security-v7-20261106-01`, dispositivo `2050`, inodo `2531296`, marcador `.security-runtime-owner.json` SHA256 `1133849a55253203097f1d330ea26964c54a294d02f58985a9f22f6da99e0a94`. Se abre por descriptor y se cotejan el marcador y los `prepare.json`/`build.json` físicos antes de acceder al hijo. No se usa el marcador ni la ruta del runtime histórico V1.

Docker 29 identifica esta imagen por el manifiesto. Si `inspect` ofrece `config.digest`, debe ser 6033; si no, el vínculo de configuración procede expresamente del Build físico pineado y el manifiesto inmutable a00. No se acepta el config digest como sustituto de `.Id`. También se cotejan UID, entrypoint, comando, etiquetas y entorno público.

## Autorización funcional privada

Raíz precrea y respalda la raíz local absoluta equivalente a `.tools/backup-source-build/postgres-sql-functional-v2-inputs`, con ACL privada. El CLI no la crea ni adopta. Comprueba la privacidad de esa raíz y de su padre, cuyo marcador `.agency-backup-build-owner` contiene exactamente `agencia-digital-marketing-ai:backup-source-build:v1` **sin LF**, SHA256 `78925844fd312b9ce3be220ee9d2fe41442908101af8c14416895811110bcff6`.

Sólo `approval.json`, el perfil y las salidas propias se leen/escriben directamente en esa raíz privada. Se rechazan enlaces, reparse, hardlinks, rutas no canónicas, cambios de metadata y archivos excesivos. Las salidas se crean EXCL; no se sobrescribe una evidencia anterior. La comparación de ctime conserva separadas las APIs `fstat` y `lstat` en Windows.

La aprobación de raíz tiene exactamente estos campos, sin cuatro reportes SCA ficticios ni referencia `FINAL_SCA`:

```json
{
  "schema_version": 2,
  "decision": "functional_test_only",
  "production_approved": false,
  "coverage_complete": false,
  "global_high_critical": null,
  "issued_at_utc": "FECHA_UTC_DE_RAIZ",
  "expires_at_utc": "FECHA_UTC_MAXIMO_UNA_HORA_DESPUES",
  "script_sha256": "SHA_SQL_FIJO",
  "runtime": {
    "prepare": {"path": "RUTA_ABSOLUTA_PREPARE", "sha256": "SHA_PREPARE_FIJO"},
    "build": {"path": "RUTA_ABSOLUTA_BUILD", "sha256": "SHA_BUILD_FIJO"}
  },
  "abi_sha256": "SHA_ABI_FIJO",
  "image": {
    "id": "MANIFIESTO_A00_FIJO",
    "config_digest": "CONFIG_6033_FIJA",
    "user": "70:70",
    "entrypoint": ["docker-entrypoint.sh"],
    "cmd": ["postgres"],
    "env": ["ENTORNO_PUBLICO_REAL_DE_LA_IMAGEN"],
    "labels": {"ETIQUETAS": "REALES_DE_LA_IMAGEN"}
  }
}
```

Este ejemplo es una plantilla no operativa. Las fechas deben ser UTC con zona y satisfacer `issued <= ahora < expires`, con duración positiva de hasta 3.600 segundos. Las etiquetas reales deben incluir el owner del runtime V3. No se copia ninguna credencial a la aprobación o al perfil. Raíz produce el archivo real y respalda su SHA antes de usarlo. Las operaciones positivas vuelven a comprobar vigencia antes de emitir su recibo.

## Campos que raíz debe respaldar

Familia única: `MEDIA_POSTGRES_IMMICH_SQL_FUNCTIONAL_V2_`.

| Sufijo | Valor o contrato |
| --- | --- |
| `APPROVAL_PATH`, `APPROVAL_SHA256` | `approval.json` privado, ruta absoluta exacta y SHA de bytes |
| `ROOT` | Padre remoto V3 seguido de `/sql-functional-v2` |
| `DATA_PATH`, `SECRETS_PATH` | `ROOT/data`, `ROOT/secrets` |
| `OWNER` | `agencia-digital-marketing-ai:postgres-immich-sql-functional:v2` |
| `CONTAINER_NAME` | `agencia-pg-immich-sql-v2-` y sufijo propio revisado |
| `PASSWORD` | Contraseña de fixture persistida por raíz antes de uso, 32–256 caracteres URL seguros; no se genera en RAM |
| `PROFILE_PATH`, `PROFILE_B64`, `PROFILE_SHA256` | Archivo privado, JSON canónico y hash externo revisado |
| `LOCAL_SHA256`, `REMOTE_SHA256`, `SQL_SHA256` | Tres fuentes exactas; SQL no cambia |
| `RECEIPTS_B64` | Mapa duradero de recibos; inicialmente ausente o vacío |

Se reutilizan por hash el conector SDK V4, su helper a13, layout, parser canónico, ACL nativa y updater de raíz. El transporte se construye con los AST exactos de `connect`/`collect` y constantes `PREFIX`/`KEYS`; sus globals se cierran explícitamente y el parser de respuesta procede de a13 compilado desde sus bytes pineados. No se importa el controlador SDK completo ni se modifican sus fuentes/API. Los demás módulos locales también se compilan desde los mismos bytes que se verificaron, sin ejecutar cachés `.pyc`. No se arranca el SDK. Sólo raíz invoca el CLI operacional. La bóveda se lee con límite de 8 MiB, parser fijado y ACL privada; el secreto viaja exclusivamente en stdin durante `prepare`. Los comentarios del updater conservan los tres prefijos canónicos.

## Etapas e identidad del CID

Plan puro, sin bóveda ni red:

```powershell
python docker/postgres-immich/sql_acceptance_v2.py plan
```

Después de precrear la raíz privada, preparar aprobación y respaldar valores, raíz puede generar el perfil nuevo (no se ejecuta aquí):

```powershell
python docker/postgres-immich/sql_acceptance_v2.py plan --output RUTA_PRIVADA_PROFILE_JSON --root-update-vault
```

Raíz revisa y respalda perfil/fuentes antes de una etapa:

```powershell
python docker/postgres-immich/sql_acceptance_v2.py prepare --profile RUTA_PRIVADA_PROFILE_JSON --expected-profile-sha256 SHA_REVISADO --root-update-vault
```

La misma interfaz admite, separadamente, `prepare → create → start1 → verify1 → stop1 → start2 → verify2 → stop2`. Cada transición exige los recibos anteriores físicos y respaldados; se conserva un intent local EXCL y el resultado físico antes del updater. No hay replay. Si el transporte o el respaldo falla, se investiga la evidencia existente; no se repite automáticamente.

| Etapa | Presupuesto heredado |
| --- | --- |
| `prepare` | 30 s |
| `create` | 15 s |
| `start1`, `start2` | 90 s cada una |
| `verify1` | 180 s |
| `verify2` | 60 s |
| `stop1`, `stop2` | 20 s cada una |

Se reservan 10 segundos internos para la limpieza propia. Create conserva el CID completo antes del postinspect, y nunca lo inicia. Cada start/query/stop contrasta CID, imagen, nombre, etiquetas, mounts, configuración y estado. La parada no necesita aprobación vigente, contraseña ni lectura de PGDATA/secretos: usa perfil, Prepare/Create duraderos, padre físico y NoForeign. No borra contenedor, PGDATA, logs ni recursos ajenos.

## Aislamiento y alcance

La fixture usa UID/GID 70:70, rootfs RO, red `none`, sin puertos publicados, `cap-drop=ALL`, `no-new-privileges`, 2 CPU, 2 GiB RAM/swap total igual, 128 pids y restart `no`. Sólo se permiten dos binds propios: PGDATA RW y secretos RO. Los tmpfs y shm conservan los límites V1. PGDATA/secretos se crean con identidad y modo privados; no se reajustan recursivamente después de initdb. No se toca NAS ni una DB de aplicación.

El SQL original prueba PostgreSQL 16.15/checksums, vector 0.8.5, vchord 1.1.1, cube/earthdistance, 4.096 vectores de 512 dimensiones, oráculo exacto e índice para cinco consultas. El segundo ciclo exige el mismo cluster, sentinel, datos e índice tras reiniciar. El arranque verifica rechazo sin password y acceso por pgpass; initdb tiene autenticación SCRAM explícita.

Los recibos positivos o negativos declaran `authorization_scope=functional_test_only`, `production_approved=false`, `coverage_complete=false`, `global_high_critical=null`. Un éxito sólo es evidencia funcional de esta fixture. La revisión completa OS/Cargo/cinco componentes manuales, configuración de producción y migraciones siguen siendo trabajos separados. Las pruebas controladas del código no son aceptación Linux ni SQL real.
