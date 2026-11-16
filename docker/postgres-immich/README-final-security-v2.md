# Operador de auditoría final V2

Esta revisión corrige el contrato de captura de la [unidad V1](README-final-security.md), que no fue operada: su CLI solicitaba 32 MiB a NativeJob, pero la fuente congelada dfe20e53 admite como máximo 1 MiB combinado para stdout y stderr. Ese valor habría fallado antes de crear un proceso. Los cinco archivos V1, su plan y su freeze se conservan byte a byte.

Son tres archivos nuevos: `audit_final_security_v2.py`, `tests/test_postgres_immich_final_security_v2.py` y este README. La implementación es una copia revisable del CLI V1; no importa ni modifica sus globals. Conserva el core `43ac1bbeefc602e78291c7963018003b7e5cdf83bde2d3d6829b9815ff86a923` y el manifiesto `2bfd9ad19fd988d0b1c9accfdd4a0b2812cc018b366ce81975c76eadde8813d0` como fuentes inmutables.

## Declaración y operador

El manifiesto sigue declarando owner `agencia-digital-marketing-ai:postgres-immich-final-sca:v1`, su namespace V1 y sus límites históricos. El plan V2 conserva esos campos en `declaration` y crea una copia explícita de configuración para el operador:

- Owner: `agencia-digital-marketing-ai:postgres-immich-final-sca:v2`.
- Namespace exclusivo: `.tools/evidence/phase4-postgres-immich-final-sca-v2`.
- Prefijo propuesto para los respaldos de raíz: `MEDIA_POSTGRES_IMMICH_FINAL_SECURITY_V2_`.
- Diagnóstico NativeJob: 1 MiB combinado para stdout y stderr.
- Reporte JSON independiente: lectura y validación acotadas a 16 MiB.

No se mutan el manifiesto, el core, sus módulos o los datos de entrada. La imagen sigue siendo el TAR `02c4948966cc261ef086f181930980dc2be9f6ad7a9a821ec9580b4e9d894bd2`, manifiesto `a00b7ac5…` y configuración `60331f93…`, ligados a los 13 archivos públicos ya verificados por V1. No se cambia ni inventa ningún prerrequisito.

## Captura separada

Cada reporte `os.trivy.json` o `cargo.trivy.json` se reserva con creación exclusiva, modo 0600 y un FD no heredable. Ese FD queda retenido mientras NativeJob ejecuta Trivy con `--output RUTA_PROPIA`; stdout deja de transportar el JSON.

Al terminar, se acredita el mismo dispositivo/inodo de la reserva, tipo regular, un único enlace y ausencia de reparse. La lectura se hace por el FD retenido, con máximo 16 MiB más un byte de detección. Se comparan las firmas antes y después, manteniendo ctime dentro de cada API; no se mezclan los relojes fstat/lstat de Windows. Un reporte sustituido, alterado durante la lectura, excesivo o no ordinario se rechaza y se conserva para diagnóstico. El límite de 16 MiB se aplica al validar el archivo; no se presenta como una cuota de disco del proceso.

`os.stdout.log`/`cargo.stdout.log`, stderr y el resultado del Job se guardan por separado, con exclusión. Un proceso fallido deja su reporte parcial y sus logs; no se parsea como resultado positivo. El Job mantiene 180 segundos, ocho procesos, 2 GiB y cierre del árbol propio. No se toca su implementación congelada.

La semántica restante es V1: inventario APK 45, Cargo 244 con 165 seleccionados/79 no seleccionados/13 locales, cobertura bijectiva positiva, DB vigente por SHA, OSV suministrado como artefactos de los 165 seleccionados, cinco anexos nativos y conservación de avisos originales. Cero hallazgos OS/Cargo no aprueba componentes manuales. `approved` y `release_approved` permanecen false; SQL y publicación siguen pendientes.

## Uso previsto por raíz

El plan no lee `.env`, no crea namespaces y no ejecuta consultas o herramientas:

```powershell
python -X utf8 docker/postgres-immich/audit_final_security_v2.py plan
```

Raíz debe revisar y respaldar el nuevo CLI, plan, owner y namespace antes de las etapas. Se conserva la interfaz, con pins independientes entre etapas:

```powershell
python -X utf8 docker/postgres-immich/audit_final_security_v2.py inventory --expected-plan-sha256 SHA_PLAN_V2
python -X utf8 docker/postgres-immich/audit_final_security_v2.py scan --expected-plan-sha256 SHA_PLAN_V2 --expected-inventory-sha256 SHA_INVENTARIO
python -X utf8 docker/postgres-immich/audit_final_security_v2.py assess --expected-plan-sha256 SHA_PLAN_V2 --expected-inventory-sha256 SHA_INVENTARIO --expected-scan-sha256 SHA_RECIBO_SCAN --supplement .tools/evidence/SUPLEMENTO.json --expected-supplement-sha256 SHA_SUPLEMENTO
```

No se adopta un namespace existente ni se sobrescribe un intento. Los reportes, logs y recibos se preservan cuando algo falla. El suplemento y los anexos siguen el esquema documentado en V1 y deben ligarse a los hashes reales de esta imagen.

## Pruebas sin ejecutar procesos

La regresión carga por bytes la función `run_job` dfe20e53 real. Con 32 MiB demuestra el rechazo por su validador antes de cualquier llamada al API nativo. Con el adaptador V2 de 1 MiB atraviesa el mismo validador y llega a un centinela en `create_job` que detiene la prueba antes de crear un Job, pipes o proceso suspendido. No se simula ni sustituye `run_job` y no se ejecuta Trivy.

Las otras pruebas ejercitan reserva EXCL, lectura por FD, tamaño excesivo, identidad distinta, separación de stdout y reporte, y declaración V1 frente al operador V2. Ninguna prueba escribe en `.env`, accede a infraestructura o crea el namespace operativo.
