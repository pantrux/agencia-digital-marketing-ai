# BORRADOR: operador SCA final V3

Este borrador queda pendiente de revisión de raíz antes de congelar fuentes o ejecutar etapas. No se ha creado su namespace ni se han ejecutado escaneos. V1, V2 y sus freezes permanecen intactos.

Raíz detectó un segundo error de preflight: `.tools/evidence` no tiene ACL privada en este equipo, por lo que el `open_run` de V1/V2 habría rechazado el padre antes de operar. No se cambia esa ACL ni ningún directorio histórico. Raíz ya comprobó por separado que `.tools/backup-source-build` sí es privado; las pruebas de este borrador no se presentan como reproducción de esa comprobación real.

## Precondiciones para revisar

- La declaración física sigue siendo `final-security-inputs-v1.json`, SHA `2bfd9ad19fd988d0b1c9accfdd4a0b2812cc018b366ce81975c76eadde8813d0`, owner V1. El core sigue siendo `43ac1bbeefc602e78291c7963018003b7e5cdf83bde2d3d6829b9815ff86a923`.
- El operador propuesto tiene owner `agencia-digital-marketing-ai:postgres-immich-final-sca:v3`, prefijo `MEDIA_POSTGRES_IMMICH_FINAL_SECURITY_V3_` y destino exclusivo `.tools/backup-source-build/postgres-final-security-scan-v3`.
- El padre se deriva del destino y debe ser exactamente `.tools/backup-source-build`. Otro padre o leaf provoca rechazo antes de `mkdir`.
- Se exige el archivo ordinario sin enlaces `.agency-backup-build-owner`, con los 51 bytes exactos `agencia-digital-marketing-ai:backup-source-build:v1`, sin salto final; SHA256 `78925844fd312b9ce3be220ee9d2fe41442908101af8c14416895811110bcff6`.
- El helper de acceso ae1c6d5e, cargado por su SHA original, debe acreditar `check_access(padre, private=True)` en la máquina real. No hay fallback, reparación de ACL o adopción de otro padre.
- El plan readonly guardará dispositivo/inodo del padre, pin del marcador, pin del helper y la comprobación privada real. Cada `open_run` vuelve a comprobarlos y exige igualdad con el plan respaldado antes de crear o leer el namespace hijo.

La carga/importación sólo define funciones. El comando `plan` realiza lecturas locales, incluida la comprobación real de ACL; no lee `.env`, no abre red, no crea directorios y no lanza Trivy, Docker, SDK o PostgreSQL:

```powershell
python -X utf8 docker/postgres-immich/audit_final_security_v3.py plan
```

Raíz debe revisar primero estas condiciones y el resultado del plan; todavía no hay freeze V3 ni autorización de etapas. Tras aprobar el borrador se registrarán los hashes definitivos y la interfaz operativa, conservando los intentos anteriores.

## Alcance conservado

Fuera de esas precondiciones e identidad nueva, el operador conserva [V2](README-final-security-v2.md): NativeJob dfe con diagnóstico combinado de 1 MiB, reporte propio EXCL por FD de hasta 16 MiB, 180 segundos/ocho procesos/2 GiB, firmas separadas fstat/lstat, inventario y cobertura exactos, pendientes OSV165 y cinco anexos nativos. No se cambia el TAR `02c49489…`, su imagen `a00b7ac5…` o configuración `60331f93…`. La declaración V1 se conserva como historia y no se confunde con los límites efectivos del operador.

`approved` y `release_approved` siguen false. Un resultado de ACL o de captura correcto no constituye aprobación SCA, aceptación SQL ni permiso de producción.

Las pruebas nuevas separan dos alcances: predicados de padre/marcador y una transición con fixtures que demuestra que una identidad del padre distinta al respaldo impide crear el hijo. Esa transición usa un doble de la comprobación previa y no certifica una ACL Windows real. Se conservan las pruebas del validador NativeJob real mediante centinela anterior a cualquier creación nativa, y de los reportes por FD. Ninguna prueba crea un Job/proceso, lee la bóveda o accede a infraestructura.
