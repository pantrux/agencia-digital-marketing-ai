# Almacenamiento de producciones en Synology DSM

**Responsable:** plataforma / SRE. **Última revisión:** 2026-11-05. Este procedimiento corresponde al NAS `192.168.0.8:2222`, modelo DVA3221, hostname `NAS`, DSM 7.3.2 y `/volume2/producciones`, según [ADR-001](../docs/decisions/ADR-001-volumen-producciones.md). No administra otras carpetas compartidas, clientes Linux ni unidades Windows.

El recurso NAS ya fue aplicado y comprobado. También se observaron los dos montajes NFS, la lectura de la prueba desde Windows y el cifrado de su sesión en el NAS. El responsable confirmó el cierre de Fase 2 el 2026-11-05 tras 220 pruebas Python y los controles finales. No se acreditan backups 3-2-1 de fases posteriores.

## Entradas, acceso y alcance

El punto de entrada es [02-setup-synology-storage.ps1](02-setup-synology-storage.ps1). El modo predeterminado es `Audit`, con autenticación `Key`. Plan, Backup y Apply exigen la identidad RSA canónica respaldada; no recurren a claves del agente, credenciales alternativas ni aceptación automática de otra clave de host. La comparación del pin y la política `RejectPolicy` se aplican antes de operar.

La bóveda y su plantilla deben contener las rutas, identidad, contraseña sudo y política antes de usarlas. El programa lee los valores localmente; la contraseña llega a `sudo -S` exclusivamente por entrada estándar, sin argumentos, variables de entorno ni registros. Nunca copiar valores reales a los ejemplos de este documento. Paramiko 5.1.0 está fijado en [requirements-synology.txt](requirements-synology.txt); los comandos siguientes usan el entorno aislado existente.

El ejecutor fija el destino y el marcador `agencia-digital-marketing-ai:producciones:v4.1`. Rechaza un recurso ajeno, una ruta previa sin registro propio, enlaces, identidad/modelo/DSM distintos, soporte insuficiente de NFSv4.1 o SMB3, y menos de 16 GiB disponibles. Ese umbral técnico no reserva capacidad para la plataforma. Conserva el datastore ESXi existente `/volume2/NFS` y las exportaciones ajenas.

## Los cuatro modos

| Modo del wrapper | Acción | Mutación y condiciones |
|---|---|---|
| `Audit` | Consulta identidad y configuración del recurso; admite `-AuditOnly`. | Solo lectura remota. Guarda informe local. Predeterminado `-Authentication Key`. |
| `Plan` | Obtiene una captura completa del NAS y genera plan más resumen SHA256. | Solo lectura remota; escribe plan e informe locales. No necesita hash aprobado. |
| `Backup` | Crea la copia privada de configuración previa. | Escribe exclusivamente el respaldo propio del NAS. Exige plan revisado y estado crítico sin cambios. No sobrescribe un respaldo existente. |
| `Apply` | Revisa plan y copia, aplica el recurso propio y verifica condiciones finales. | Exige SHA256 del plan y manifiesto de respaldo previamente registrado en la bóveda. Puede modificar NFS y afectar clientes activos. |

`-Authentication Password` solo es una opción del modo Audit; no es el valor predeterminado del wrapper. `-AuditOnly` no puede combinarse con Plan, Backup o Apply. El wrapper admite `-VaultPath`, `-OutputPath`, `-PlanPath`, `-ExpectedPlanSha256`, `-Authentication`, `-TimeoutSeconds` de 2 a 30 y `-PythonPath`. No existe un modo de simulación de Apply.

## Auditoría y plan fresco

Desde PowerShell en la raíz del proyecto, los siguientes pasos son de lectura remota. Elegir nombres locales nuevos para conservar las evidencias anteriores:

```powershell
$pythonNas = '.tools/venvs/synology-audit/Scripts/python.exe'
$planRevisado = '.tools/evidence/synology-storage-plan-revision.json'
$resumenPlan = '.tools/evidence/synology-storage-plan-revision-summary.json'
./storage/02-setup-synology-storage.ps1 -Mode Audit -Authentication Key -AuditOnly -PythonPath $pythonNas -OutputPath '.tools/evidence/synology-storage-audit-revision.json'
./storage/02-setup-synology-storage.ps1 -Mode Plan -PythonPath $pythonNas -PlanPath $planRevisado -OutputPath $resumenPlan
$shaPlan = (Get-Content -LiteralPath $resumenPlan -Raw | ConvertFrom-Json).plan_sha256
```

Revisar `crear_share`, las dos reglas NFS, `requiere_interrupcion_nfs`, los parámetros que deben preservarse y la identidad del destino. `plan_sha256` es el hash del JSON normalizado por `plan_hash`, **no** el hash de los bytes del archivo con su formato e indentación. No sustituirlo por `Get-FileHash` del plan ni reutilizar un resumen histórico para otra captura.

Para elaborar un plan exclusivamente desde una captura local completa, sin conectar al NAS, el Python admite esta entrada adicional:

```powershell
& $pythonNas storage/synology_storage_apply.py --snapshot '.tools/evidence/captura-revisada.json' --output '.tools/evidence/plan-local-revisado.json'
```

Esta salida es el plan; no constituye autorización ni evidencia de aplicación. En el Python del ejecutor, `--snapshot`, `--plan`, `--backup` y `--apply` son mutuamente excluyentes; sin seleccionar uno, se usa Plan con consulta remota. `--output` es obligatorio; también admite `--vault-path`, `--plan-path`, `--expected-plan-sha256` y `--timeout` de 2 a 60. Para auditoría Python directa, usar `synology_storage_audit.py --auth-method key`; su predeterminado propio es contraseña, a diferencia del wrapper.

## Respaldo previo y aplicación coordinada

Estos comandos describen la primera provisión autorizada; **no ejecutarlos en bloque ni repetir Backup en el estado actual**, donde la copia ya existe:

```powershell
./storage/02-setup-synology-storage.ps1 -Mode Backup -PythonPath $pythonNas -PlanPath $planRevisado -ExpectedPlanSha256 $shaPlan -OutputPath '.tools/evidence/synology-storage-backup-revision.json'
# Antes de Apply, el responsable registra el manifest_sha256 verificado en la
# clave SYNOLOGY_CONFIG_BACKUP_MANIFEST_SHA256 de la bóveda y sincroniza la plantilla.
./storage/02-setup-synology-storage.ps1 -Mode Apply -PythonPath $pythonNas -PlanPath $planRevisado -ExpectedPlanSha256 $shaPlan -OutputPath '.tools/evidence/synology-storage-apply-revision.json'
```

La ruta de respaldo está fijada en `/volume2/homes/jandrade/.agencia-media-config-backups/fase2-20261105`; no está dentro de la nueva carpeta de producciones. Contiene copias RAW de `/etc/exports`, `/etc/nfs/syno_nfs_conf` y `/etc/samba/smb.share.conf`, la captura `snapshot-before.json` y `manifest.json` con sus hashes. Se exige propietario Unix root, dos directorios propios `0700`, marcador `.owner` y cinco archivos del respaldo `0600`, sin enlaces ni SynoACL heredada. El contenido puede revelar configuración del laboratorio: conservarlo privado, sin publicarlo.

No borrar ni recapturar esa copia para superar una guarda. La ruta de esta ejecución es fija y el creador rechaza sobrescrituras. Una futura política de respaldos o época de despliegue necesita un cambio revisado del contrato; no se obtiene cambiando un argumento de ruta.

Apply recalcula el plan desde el estado real y exige que coincida con el revisado antes de mutar. Verifica después el manifiesto contra el hash de confianza de la bóveda y cada copia contra su hash. Un código shell cero no basta para una API DSM: también exige `success: true`. Los errores detienen los pasos posteriores; el procedimiento no ofrece reversión automática de cambios parciales.

## Permisos, exportación y riesgo de interrupción

- NFS permite únicamente `.85` y `.7`, con `rw`, escritura síncrona, `crossmnt=false`, `insecure=false` y autenticación `AUTH_SYS`. La política `all_admin` mapea **todas** las identidades NFS de esos clientes a `admin`, UID 1024 y GID 100 del NAS; no conserva la identidad UID 1000 de Linux. Estos nodos deben tratarse como clientes de confianza.
- La ACL del recurso propio contiene únicamente entradas explícitas de control completo para `admin` y `jandrade`, heredables a archivos y directorios (`fd--`). Se comprueban el propietario y los permisos efectivos; no se modifican las ACL de otras carpetas compartidas. El UID 1024 de producciones corresponde a `admin`; no debe confundirse con el UID 0 de los respaldos privados.
- La jerarquía comprende `_biblioteca_global`, `_backups` y, bajo `proyectos/sansevieras`, los seis nombres fijos `01_raw`, `02_brief`, `03_assets`, `04_edicion`, `05_revision` y `06_renders`, incluyendo los padres. Se rechazan enlaces en esas rutas. El alta y las ACL usan el contrato DSM auditado; no se editan manualmente sus archivos generados para persistir los permisos.
- Habilitar NFSv4.1 afecta al servicio NFS global y puede interrumpir clientes existentes, incluido ESXi con `/volume2/NFS`. Coordinar la ventana antes de cualquier plan con `requiere_interrupcion_nfs=true`; no asumir que ausencia de VMs registradas significa ausencia de consumidores.
- Se preservan NFS 2/3 y los demás parámetros avanzados. Las exportaciones ajenas se comparan tras las operaciones y al finalizar, admitiendo únicamente la normalización de un LF final por DSM. No se aplica una normalización general de espacios, orden o contenido. SMB global debe permanecer idéntico; la disponibilidad del protocolo 3 no acredita por sí sola cifrado en una sesión concreta.

## Fallos reales y recuperación acotada del 2026-11-05

1. **Backup detenido por permisos heredados.** Se habían creado las copias y el manifiesto completos, pero SynoACL bajo `homes` presentó los ocho objetos propios como `0711`. Se diagnosticó en lectura; después se repararon únicamente los dos directorios, `.owner`, manifiesto y cuatro copias, retirando la ACL heredada de esas rutas y verificando `0700/0600`. No se cambió el directorio `homes/jandrade`, no se repitió Backup y los bytes y hashes permanecieron idénticos. La verificación posterior de la copia terminó con código cero. Evidencias: `.tools/evidence/synology-backup-failure-diagnosis.json`, `synology-backup-permission-repair.json` y `synology-storage-backup-verified.json`.
2. **Primer Apply detenido en ACL.** El alta del share propio ya había ocurrido. Tras `chmod`, `synoacltool -get` devolvió código 255 con el estado Linux mode, que el código anterior trataba como error genérico. La corrección admite únicamente ese resultado específico como ausencia de entradas SynoACL; otros errores siguen bloqueando. Se generó un plan nuevo para el share con nombre, ruta y marcador propios, con `crear_share=false`. La recuperación aplicó ACL, directorios y NFS sin recrear el share ni repetir Backup. Evidencias: `.tools/evidence/synology-storage-apply.json`, `synology-first-apply-diagnosis.json` y `synology-storage-apply-own-share.json`.

Ante otro fallo, conservar informe, respaldo y estado parcial; diagnosticar primero en lectura. El diagnóstico actual incluye paso, código de salida y detalle saneado. No utilizar un borrado de recurso, una repetición ciega ni una restauración global de exportaciones como compensación.

## Evidencia observada y comprobaciones posteriores

La recuperación Apply terminó con código cero en **7,58 s**, según la ejecución del responsable. La inspección posterior acreditó ACL exacta, propietario UID 1024/GID 100, directorios objetivo `0700` y sin enlaces. Se crearon diez directorios, contando padres. NFS presentó `+2 +3 +4 +4.1 -4.2`, con 128 hilos. Parámetros avanzados, SMB global y exportaciones ajenas se conservaron bajo la regla del LF final. Evidencias: `.tools/evidence/synology-storage-apply-own-share.json` y `synology-storage-postapply-readonly.json`.

| Artefacto de esta ejecución | SHA256 de referencia |
|---|---|
| Código del ejecutor congelado | `07ba3bbac329cd7cf96e9cf369e98c980818f1814fd429e5df53ccafe7339ef9` |
| Plan inicial histórico, sustituido tras el primer Apply parcial | `e8162939f3529b9524b0b5c15ffae93ab636f946cae3171390fc97dadc9dbf52` |
| Plan de recuperación aplicado al share propio existente | `9522170ddd31207a0469a9df6de7fe649ecfa30011f6254ec3106a0da2dc1d76` |
| Manifiesto privado, registrado en bóveda antes de Apply | `bf69fe6f67898d6f6e0b82b5671cd956f5fe49538169b8015b163bddba21bb06` |
| Plan posterior de solo lectura | `518e2ef3ee1c9e1d481345bf10633e350c3b08649cd86bc9b48ec44eaae81448` |

El plan posterior informa `crear_share=false`, `actualizar_reglas_nfs=false`, `requiere_interrupcion_nfs=false` y `smb_global_cambia=false`. No se ejecutó otro Apply para demostrarlo. Esto acredita esas decisiones del plan; no promete que un Apply completo carezca de pasos de ACL y directorios. Para cualquier operación posterior generar y revisar un plan fresco; ninguno de los hashes históricos sirve como autorización reutilizable.

Los [clientes NFS](README-NFS-Linux.md) montaron el origen exacto de `volume2` en `/mnt/producciones`, con `nfs4`, `rw`, `vers=4.1` y TCP. Se conservaron fstab ajeno, otros montajes y respaldos root `0600/0700`; los `--check` posteriores no requieren cambios. Evidencias: `.tools/evidence/media-nfs-client-apply.json` y `openclaw-nfs-client-apply.json`.

El responsable escribió y sincronizó una prueba de **170 bytes** desde `.85` y observó el mismo SHA256 desde `.7` y `P:`: `3a62a47c7709a3d340c12cffc974c4d0e945b1a90ebfcd50533d730545e882bc`. El [mapper Windows](README-SMB-Windows.md) registró `RequirePrivacy=true`, `Persistent=true` y ningún cambio del Administrador de credenciales; evidencia `.tools/evidence/windows-smb-mapping.json`.

La captura NAS del **2026-11-05 a las 22:40:04 UTC**, filtrada por `jandrade`, terminó con código cero, sin truncado ni stderr. [verify_smb_session.py](verify_smb_session.py) correlacionó una sesión y un recurso `producciones` desde `.6`: **SMB3_11**, cifrado completo **AES-128-GCM**. Se guardó únicamente el resultado saneado en `.tools/evidence/synology-smb-session-verification-final.json`; no se publicaron archivos abiertos ni sesiones ajenas. El helper solo analiza JSON por stdin y no conecta al NAS; por separado el capturador debe exigir éxito y salida completa antes de invocarlo.

Las pruebas locales del ejecutor/auditoría y del verificador SMB complementan la evidencia real; no la sustituyen. No se verificó persistencia mediante reinicio de `.7` o Windows ni una ejecución remota de workflows. Tampoco se acreditan aún restic, copias R2, restauraciones de aplicación o el objetivo futuro 3-2-1. El cierre de Fase 2 y el avance autorizado a Fase 3 constan en la evidencia general; este procedimiento no acredita los componentes GPU siguientes.
