# Auditoría final de PostgreSQL Immich corregido

Esta unidad analiza datos de la imagen V3 construida desde el catálogo V7. No abre SSH, no lee `.env`, no ejecuta PostgreSQL, SDK, SQL, Docker ni consultas HTTP. `plan` sólo lee fuentes y datos locales. Las etapas que escriben o ejecutan Trivy requieren una llamada explícita de raíz después de respaldar sus pins.

La imagen candidata es `sha256:a00b7ac5ed30069abde2702a2b2b3d8321ea0f22d2ba64cc9f836c86b4c47601`, configuración `sha256:60331f9322af6e5449db67a68975e7ce57f58562af19843bc4de47ffd46f709f`. El archivo recibido tiene 119.867.904 bytes y SHA256 `02c4948966cc261ef086f181930980dc2be9f6ad7a9a821ec9580b4e9d894bd2`. Los pins físicos completos, sus tamaños, el recibo de recepción y los gates de producción están en [final-security-inputs-v1.json](final-security-inputs-v1.json).

## Cobertura y límites

- OCI: manifiesto, configuración, todos los blobs y DiffIDs, plataforma Linux amd64, usuario 70:70, etiqueta propia y 66 archivos proyectados. Se reutiliza por SHA el cierre AST de `final_image_remote.py` 9e0a2829, incluyendo `MAX_FILE`; no se carga su controlador histórico.
- Sistema operativo: los 45 paquetes APK reales del archivo `lib/apk/db/installed`, idénticos a los recibos de preparación y construcción. El informe de imagen debe devolver los mismos 45 nombres y versiones y la identidad Alpine observada.
- Rust: los 244 archivos `.crate` se cotejan con sus checksums y `Cargo.lock` autenticados. El grafo V7 acredita 165 paquetes de registro seleccionados, 79 no seleccionados y 13 paquetes locales. Se conservan los 244 en el SBOM y en Trivy; el artefacto OSV posterior debe cubrir exactamente los 165 seleccionados. Los 79 restantes no se presentan como código enlazado y los 13 locales no se confunden con paquetes de registro. `anyhow` es 1.0.103 y `crossbeam-epoch` es 0.9.20.
- Componentes manuales: PostgreSQL 16.15, pgvector 0.8.5, VectorChord 1.1.1, SQLite 3.54.1 y mimalloc 2.2.4 requieren anexos propios. Se identifican por los ELF físicos; mimalloc se liga al SHA del nuevo `vchord.so`, pero no se infiere su versión o retención sólo del nombre de su wrapper Rust.

El lector registra si hay un ejecutable SQLite en la imagen. Un aviso del CLI, como `.recover`, debe conservarse y evaluarse con esa evidencia y el hash de la biblioteca; no se elimina por una excepción general. Los avisos de mantenimiento, alias y features inactivas de Cargo también deben conservar sus datos y clasificación específica. La ausencia de coincidencias en un feed no demuestra por sí sola que una biblioteca manual esté aprobada.

Los cinco anexos son obligatorios incluso cuando Trivy devuelve cero HIGH/CRITICAL para APK y Cargo. Esta unidad siempre emite `approved: false` y `release_approved: false`; raíz debe revisar la cobertura, los avisos originales, las clasificaciones y los pendientes antes de emitir una aprobación externa. La aceptación SQL y la publicación siguen separadas.

## Etapas explícitas

Desde la raíz del repositorio, el plan se obtiene con:

```powershell
python -X utf8 docker/postgres-immich/audit_final_security.py plan
```

El plan fija el manifiesto, las fuentes y los 13 archivos de referencia. Si falta el archivo físico de imagen, declara `ready_for_inventory: false`; no lo descarga. Un pin distinto o un archivo existente con identidad incorrecta provoca rechazo.

Tras respaldar externamente el SHA del plan, raíz puede ejecutar:

```powershell
python -X utf8 docker/postgres-immich/audit_final_security.py inventory --expected-plan-sha256 SHA_PLAN
```

`inventory` crea de forma exclusiva `.tools/evidence/phase4-postgres-immich-final-sca-v1`, su marcador y el plan. Verifica el marcador e inodo del directorio de recepción, el TAR y los recibos positivos de preparación, catálogo, parada y construcción. Catálogo, reinicio y parada deben pertenecer al mismo nacimiento y época del SDK. No se adopta una salida anterior. Produce `inventory.json` y `cargo.cdx.json`; raíz debe respaldar el SHA del inventario antes de continuar.

```powershell
python -X utf8 docker/postgres-immich/audit_final_security.py scan --expected-plan-sha256 SHA_PLAN --expected-inventory-sha256 SHA_INVENTARIO
```

`scan` comprueba Trivy 0.76.2 y la DB por SHA, exige `UpdatedAt <= ahora < NextUpdate` y conserva sus hashes antes y después. Usa exclusivamente el NativeJob Windows dfe20e53 con límite por proceso de 180 segundos, 8 procesos, 2 GiB y 32 MiB de salida. Ejecuta secuencialmente `image --input` y `sbom`, ambos sin actualización de DB y con `--offline-scan --list-all-pkgs`. No hay contenedores ni acceso de la imagen a la red. El entorno hijo sólo incluye las rutas mínimas de Windows y temporales propios.

Se conservan stdout, stderr y resultado del Job por separado, incluso si el proceso falla o termina por plazo. Los informes sólo acreditan cobertura si existe una biyección positiva y exacta; cero paquetes se rechaza. Los hallazgos HIGH/CRITICAL/UNKNOWN se conservan. Un retorno 0 de esta etapa indica que la captura y validación terminaron, no que el candidato esté aprobado.

Sólo se clasifican los tres WARN literales ya observados en Trivy: SBOM de tercero, recomendación de generar el SBOM con Trivy y selección de severidades de otros proveedores. Sus líneas completas quedan en stderr y sus mensajes en el recibo; cualquier WARN desconocido, ERROR o FATAL provoca rechazo. Esta clasificación exige previamente la cobertura exacta y no cambia las severidades ni filtra hallazgos.

El directorio y `.scan-started` son exclusivos. Ante un fallo se conservan los archivos y no se repite la etapa ni se sobrescriben datos; una recuperación necesitará otra unidad revisada.

## Artefactos externos de OSV y componentes manuales

Esta unidad no consulta feeds. Raíz prepara un suplemento JSON público, con SHA respaldado, dentro de `.tools/evidence/`. Debe contener `image_id`, `config_digest`, un `osv` opcional y un diccionario `annexes` opcional. Cada referencia tiene `path` relativo y `sha256`. No se aceptan rutas externas al directorio de evidencia ni `.env`.

El artefacto OSV tiene este contrato de transporte, conservando cada respuesta original en `pages`:

```json
{
  "image_id": "sha256:MANIFIESTO_REAL",
  "config_digest": "sha256:CONFIG_REAL",
  "ecosystem": "crates.io",
  "queries": 165,
  "omitted": 0,
  "pagination_exhausted": true,
  "results": [
    {
      "name": "nombre-real",
      "version": "version-real",
      "pagination_exhausted": true,
      "pages": [{"vulns": []}]
    }
  ]
}
```

La lista real debe incluir exactamente los 165 pares seleccionados. Se admiten como máximo 8 páginas por consulta, tokens distintos y agotamiento explícito. Los IDs, alias, rangos, severidades y campos originales de cada aviso se conservan. Si la severidad no está puntuada en el formato admitido, permanece UNKNOWN, nunca se transforma en cero.

Cada anexo usa su clave exacta `postgresql`, `pgvector`, `vectorchord`, `sqlite` o `mimalloc`, y contiene al menos:

```json
{
  "image_id": "sha256:MANIFIESTO_REAL",
  "config_digest": "sha256:CONFIG_REAL",
  "version": "VERSION_FIJADA",
  "binary_sha256": "SHA_ELF_O_VCHORD_QUE_CONTIENE_MIMALLOC",
  "source_sha256": "SHA_FUENTE_AUTENTICADA",
  "assessment": "evaluated_by_root",
  "raw_findings": [],
  "unresolved_advisories": [],
  "unresolved_high_critical": 0,
  "evidence": [{"path": ".tools/evidence/PRUEBA_REAL.json", "sha256": "SHA_PRUEBA_REAL"}]
}
```

Las referencias de evidencia deben demostrar procedencia/licencia, versión, fingerprint o símbolos retenidos, alcance de los feeds, respuestas originales y clasificación de aplicabilidad específica a estos bytes. En mimalloc es indispensable demostrar la versión C y su retención dentro del nuevo `vchord.so`; una lista vacía del feed no sustituye esa prueba. El campo `source_sha256` debe estar explicado y acreditado por esos archivos, no por una inferencia del nombre del paquete. La evaluación declarada por raíz se conserva como tal; este controlador no la convierte en una aprobación propia.

Tras respaldar también el SHA físico de `scan-receipt.json`, raíz puede incorporar el suplemento:

```powershell
python -X utf8 docker/postgres-immich/audit_final_security.py assess --expected-plan-sha256 SHA_PLAN --expected-inventory-sha256 SHA_INVENTARIO --expected-scan-sha256 SHA_RECIBO_SCAN --supplement .tools/evidence/SUPLEMENTO.json --expected-supplement-sha256 SHA_SUPLEMENTO
```

`assess` vuelve a verificar los informes y logs físicos, los pins entre etapas, la DB vigente y las referencias de los anexos. Conserva los hallazgos nativos originales en el total bruto incluso cuando se hayan clasificado como no aplicables. Publica exclusivamente `assessment-SHA_SUPLEMENTO.json`; un suplemento posterior distinto no cambia el anterior. Cualquier anexo u OSV ausente queda señalado como pendiente.

## Verificación de esta unidad

Las pruebas públicas usan fixtures locales y los parsers OCI/TAR/ELF reales sin depender de `.tools`. La compatibilidad con el TAR y los datos de laboratorio se registra por separado como lectura de datos, sin presentarla como un escaneo actual. El plan y el freeze privado registran los hashes finales de los cinco archivos. No se ha ejecutado Trivy, HTTP, SDK, PostgreSQL o SQL al implementar esta unidad.
