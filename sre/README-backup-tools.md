# Herramientas de respaldo: preparación local

Estado al 6 de noviembre de 2026: el instalador está preparado, pero **los cuatro binarios oficiales están bloqueados por su auditoría de vulnerabilidades**. No se instalaron ni ejecutaron restic/rclone, no se inicializó ningún repositorio y no se hicieron operaciones de respaldo en NAS o R2.

Esta unidad prepara solamente CLI independientes. Los contratos de respaldo, restauración aislada, programación y retención se desarrollan en la [especificación de Fase 4](../docs/specs/FASE-4-APLICACIONES-Y-BACKUPS.md). La compilación propia que se estudia para resolver los hallazgos requiere otro inventario y otra auditoría; no habilita el manifiesto actual.

## Inventario y bloqueo real

[backup-tools-artifacts.json](backup-tools-artifacts.json) fija las publicaciones oficiales, revisión fuente, licencia, URL, tamaño y SHA-256 de cada archivo comprimido y de su ejecutable extraído. La revisión estática identifica también el compilador Go incorporado.

| Herramienta | Versión | Plataformas | Go incorporado | HIGH / CRITICAL por binario | Resultado |
|---|---|---|---|---|---|
| restic | 0.19.1 | Windows amd64 y Linux amd64 | 1.26.4 | 22 / 0 | Bloqueado |
| rclone | 1.75.2 | Windows amd64 y Linux amd64 | 1.26.9 | 1 / 0 | Bloqueado |

La política de esta unidad exige Go 1.27.3 o posterior y cero HIGH/CRITICAL, con cobertura de auditoría acreditada. Ambos compiladores oficiales están afectados por `CVE-2026-111943`; restic incorpora además dependencias con hallazgos HIGH. Actualizar únicamente Go no acredita que el grafo de restic quede limpio. Se conservan también los hallazgos de otras severidades en el informe íntegro.

Las fuentes de publicación son [restic v0.19.1](https://github.com/restic/restic/releases/tag/v0.19.1) y [rclone v1.75.2](https://github.com/rclone/rclone/releases/tag/v1.75.2). Las licencias declaradas en sus revisiones fijadas son [BSD-2-Clause para restic](https://raw.githubusercontent.com/restic/restic/6aa3a516ce654808a1f28f9fa21e9b7c8e6e90bf/LICENSE) y [MIT para rclone](https://raw.githubusercontent.com/rclone/rclone/be8d6044eacb843b74d335db0c5fbe09222031fe/LICENSE). Se compararon los SHA-256 publicados en `SHA256SUMS`, el digest del activo en la API oficial y los archivos descargados. **No se verificaron firmas PGP**.

El informe local `.tools/evidence/backup-tools-binary-full-sca.json` contiene cuatro resultados Go: restic Linux/Windows con 81/82 paquetes y rclone Linux/Windows con 244/242. Se empleó Trivy 0.76.2 con patrón explícito `gobinary:.*`, porque los primeros intentos no cubrieron los cuatro ejecutables. Esos intentos parciales no acreditan aprobación. El recibo `.tools/evidence/backup-tools-audit-receipt.json` conserva hashes, cobertura, severidades y metadata de la base utilizada. El código de salida de la captura del informe no representa una aprobación de seguridad.

## Plan sin instalación

Desde la raíz del proyecto, Python 3.12 permite consultar el inventario sin red, cambios de destino ni ejecución de los CLI:

```powershell
python sre/install_backup_tools.py
python sre/install_backup_tools.py --platform windows-amd64
python sre/install_backup_tools.py --platform linux-amd64 --tools restic
```

Si los archivos oficiales ya están descargados en una carpeta local, el Plan puede comprobar sus bytes y descomprimir únicamente en memoria el ejecutable seleccionado:

```powershell
python sre/install_backup_tools.py --platform windows-amd64 --archives .tools/backup-tools-artifacts
python sre/install_backup_tools.py --platform linux-amd64 --archives .tools/backup-tools-artifacts
```

Estos dos últimos comandos se verificaron con los cuatro archivos reales. Emitieron `archive_and_binary_integrity_verified: true` e `installation_allowed: false`. Un Plan satisfactorio acredita el inventario o su integridad según las opciones, y no acredita permisos del destino, instalación, seguridad aprobada ni funcionamiento de un respaldo.

## Contrato de aplicación preparado

La interfaz explícita es `python sre/install_backup_tools.py --apply --archives CARPETA_LOCAL --manifest-sha256 SHA256_REVISADO`, con `--manifest` opcional y selección `--tools restic`, `--tools rclone` o ambas. **El manifiesto entregado rechaza esa operación con salida 2 antes de crear el destino.** No se debe cambiar una bandera para eludir el bloqueo. Una futura habilitación exige un manifiesto revisado, sus nuevos binarios, SCA completa y respaldo previo de rutas/pins por el responsable de la bóveda.

El hash externo debe proceder del manifiesto aprobado y respaldado; calcularlo de una copia arbitraria justo antes de aplicar no acredita su aprobación. El instalador no descarga artefactos ni consulta servicios de vulnerabilidades durante la aplicación. Confía en el inventario previamente revisado y exige sus bytes exactos.

| Plataforma nativa | Destino único | Propietario requerido |
|---|---|---|
| Windows amd64 | `C:\GPU-Bridge\backup-tools` | SID actual, SYSTEM o Administradores; ACL privada |
| Linux amd64 | `/opt/media-platform-tools` | root; directorio privado y padre sin escritura de grupo/otros |

No hay una opción para sustituir el destino. Linux exige ejecución como root para aplicar; el script no eleva ni modifica ACL. Windows consulta la ACL mediante PowerShell 7. El directorio padre debe existir y acreditar permisos adecuados. No se modifica `PATH`, `/usr/bin`, servicios, tareas programadas ni almacenes de credenciales.

Un destino existente, incluso vacío, requiere el marcador propio `.agency-backup-tools-owner.json`. Un directorio sin marcador, archivos ajenos, enlaces/reparse points, hardlinks, binarios alterados, recibos incompatibles o un `.install.lock` existente detienen la operación. No se adopta ni elimina un lock antiguo automáticamente.

La aplicación publica exclusivamente los ejecutables seleccionados y `install-receipt.json`, vinculado al SHA del manifiesto. Una repetición con los mismos bytes es idempotente; este incremento no migra instalaciones de otra versión. Ante un fallo puede quedar una instalación parcial propia sin recibo nuevo: no se ejecuta rollback destructivo ni se borran recursos ajenos. Debe inspeccionarse la causa antes de repetir. Un recibo válido acredita los archivos instalados, y no una copia de seguridad funcional.

## Verificaciones y límites

```powershell
python -m unittest discover -s tests -p test_backup_tools.py -v
python -m ruff check sre/install_backup_tools.py sre/backup_tools_access.py tests/test_backup_tools.py
python -m ruff format --check sre/install_backup_tools.py sre/backup_tools_access.py tests/test_backup_tools.py
```

Las pruebas usan archivos aislados en `.tools`, ejecutables sintéticos y límites de permisos simulados. Cubren archivos comprimidos con rutas peligrosas, enlaces, colisiones de nombre, expansión/truncamiento, formato y Go discordantes, hash externo, modificación de un ejecutable, FIFO, propiedad, idempotencia y conservación del recibo si aparece un archivo ajeno durante una ampliación. Las ACL y permisos POSIX tienen predicados probados; no se acredita una aplicación real en los destinos Windows/Linux.

Las futuras operaciones R2 deben usar la familia separada `R2_PLATFORM_*` que administra la bóveda. El instalador no lee esas claves ni `RESTIC_PASSWORD`. La existencia de buckets y una consulta S3 de lectura satisfactoria no acreditan escritura, cifrado de un repositorio restic ni restauración. Esas aceptaciones continúan pendientes de Fase 4.

## Consulta de permisos Windows

El lector `backup_tools_access.py` usa Windows PowerShell integrado mediante su ruta absoluta bajo `SystemRoot`, sin buscar ejecutables en PATH ni exigir una instalación opcional de PowerShell 7. Para consultar ACL/SID, fija el catálogo de módulos de ese mismo intérprete y elimina las variantes heredadas de `PSModulePath`; así no intenta cargar ensamblados de PowerShell 7 en 5.1. No eleva privilegios ni cambia permisos.

Una regresión reproducida y corregida verifica la ruta y el catálogo de módulos; las 19 pruebas de herramientas/acceso pasaron con ResourceWarning estricto. El lector también aprobó realmente la carpeta y archivos privados de la exportación D1 en Windows. La validación POSIX y la política que bloquea los CLI vulnerables se conservan.
