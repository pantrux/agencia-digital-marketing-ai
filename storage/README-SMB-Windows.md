# Cliente SMB de Windows: procedimiento y verificación de Fase 2

El cliente es `W11-HOMELAB` (`192.168.0.6`), con destino `\\192.168.0.8\producciones` en `P:`. Este documento conserva la auditoría inicial y el procedimiento, junto con el resultado real posterior del mapeo y la lectura compartida. El cierre formal de Fase 2 fue confirmado por el responsable el 2026-11-05 tras los controles integrados.

## Auditoría inicial del 2026-11-05

La raíz NAS autorizada es `/volume2/producciones`, según [ADR-001](../docs/decisions/ADR-001-volumen-producciones.md). El recurso SMB conserva su nombre; verificar que el servidor lo respalde desde esa ruta. Preservar el datastore existente `/volume2/NFS`. Las observaciones de esta sección precedieron al mapeo y no se sobrescriben con los resultados posteriores.

La consulta se ejecutó en el contexto `W11-HOMELAB\Jorge Andrade`, con token sin elevación. La evidencia está en `.tools/evidence/windows-smb-phase2-audit.json` de la raíz del proyecto; contiene nombres de recursos y estados, sin contraseñas.

| Comprobación | Resultado real |
|---|---|
| `Get-SmbMapping`, disco lógico `P:`, PSDrive `P` y reserva persistente `HKCU:\Network\P` | Sin entrada: `P:` libre en el contexto inspeccionado. |
| TCP hacia `192.168.0.8:445` | Accesible; conexión completada en 47 ms. |
| Mappings SMB y `Win32_NetworkConnection` hacia la IP del NAS | Sin entradas observadas. |
| `Get-SmbConnection` | Acceso denegado, `Windows System Error 5`. |
| Sesión autenticada reutilizable, usuario del NAS y dialecto negociado | No verificados por falta de acceso al inventario de sesiones. |
| Acceso a `producciones`, mapeo y prueba de archivo compartido | No ejecutados. |

La ausencia de mappings no demuestra ausencia de sesiones SMB sin letra de unidad. La conexión TCP no autentica al usuario ni demuestra SMB3 o permisos de la carpeta. No se mapearon/desmapearon unidades, no se extrajeron contraseñas y no se modificaron credenciales ni servicios. Otras sesiones de usuario de Windows quedan fuera de esta observación.

## Repetir la lectura antes de cualquier mapeo

Las comprobaciones de ocupación y permisos deben repetirse en el contexto que vaya a utilizar `P:`. Estos comandos solo consultan estado:

```powershell
Get-SmbMapping | Where-Object LocalPath -EQ 'P:' | Select-Object LocalPath,RemotePath,Status
Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='P:'" | Select-Object DeviceID,DriveType,ProviderName
Get-PSDrive -PSProvider FileSystem | Where-Object Name -EQ 'P' | Select-Object Name,Root,DisplayRoot
Get-ItemProperty -LiteralPath 'HKCU:\Network\P' -ErrorAction SilentlyContinue | Select-Object RemotePath,UserName,ProviderName
Get-SmbConnection -ServerName '192.168.0.8' | Select-Object ServerName,ShareName,UserName,Dialect,Encrypted
```

Si `P:` pertenece a otro destino o proyecto, conservarlo y detener la preparación de ese mapeo. Una reserva persistente también cuenta como ocupación aunque esté desconectada. Si ya corresponde al destino exacto, verificar propietario, acceso y dialecto; no reemplazarla por rutina.

`Get-SmbConnection` necesita un contexto con permisos de consulta suficientes. Ante acceso denegado, registrar el resultado como desconocido y no atribuir un dialecto o usuario. No exportar credenciales del Administrador de credenciales para completar esta lectura.

## Requisitos para el procedimiento posterior

1. Comprobar en Synology que existe el recurso `producciones`, que sirve `/volume2/producciones`, y que la cuenta destinada al cliente tiene los permisos acordados. Verificar la configuración SMB3 del servidor contra la auditoría y la captura vigentes.
2. Antes de utilizar cualquier credencial nueva, persistir su valor real en `.env` con `# Para qué:`, `# Dónde:` y `# Riesgo al rotar:`, actualizar `.env.example` con `CHANGE_ME` y recién entonces entregarla al cliente. Una sesión ya autenticada al mismo NAS y contexto puede ser candidata a reutilización; esta auditoría no pudo acreditarla.
3. Aplicar el mapeo mediante el procedimiento idempotente de Fase 2 solo después de cerrar los requisitos y la ocupación. No modificar una unidad ajena ni habilitar contenedores o bases de datos de plataforma en Windows.
4. Tras el mapeo, verificar destino exacto, acceso y dialecto negociado `3.x` con el inventario de sesiones autorizado. Conservar el resultado real, sin contraseñas ni argumentos que las expongan.
5. Para cerrar la prueba triple, crear el archivo de prueba desde `.85` sobre su montaje NFS verificado, leer esos mismos bytes desde `.7` y desde `P:\.probe`, y comparar su contenido o SHA256. La carpeta de nombre parecido en un disco local no sustituye un montaje NAS confirmado. Registrar las tres lecturas antes de declarar terminada la fase.

## Contrato del script y ejecución coordinada

`Map-AgencySmb.ps1` importa funciones sin operar infraestructura. Su contrato se contrastó con los metadatos de `New-SmbMapping` del Windows local: `Credential` admite `PSCredential`, y `RequirePrivacy` y `Persistent` son booleanos compatibles en el mismo conjunto de parámetros. La [documentación oficial de Microsoft](https://learn.microsoft.com/en-us/powershell/module/smbshare/new-smbmapping?view=windowsserver2025-ps) describe `RequirePrivacy` como exigencia de cifrado SMB.

La operación real lee `SYNOLOGY_USER` y `SYNOLOGY_PASSWORD` desde `.env` y exige sus entradas `CHANGE_ME` en `.env.example`. Una contraseña vacía o pendiente bloquea. Se entrega un `PSCredential` al cmdlet, sin convertir la contraseña a texto, incluirla en argumentos literales ni publicarla en registros. No se solicita `SaveCredentials`, no se eleva el proceso ni se crea un mapping global; la persistencia pertenece al contexto del usuario actual.

El análisis de esas dos variables coincide con `scripts/ci/check_env.py`: elimina un comentario precedido de espacio fuera de comillas, conserva `#` dentro de comillas y rechaza comillas sin cierre. Los valores se tratan literalmente, incluidas secuencias de sustitución, sin ejecutarlas. La plantilla se interpreta con el mismo contrato.

El script consulta todos los indicios locales de ocupación de `P:` y conserva cualquier recurso ajeno. Un mapping existente solo se reutiliza si apunta al destino exacto, exige privacidad y tiene reserva persistente del mismo usuario; una política desconocida bloquea sin remapear. Al crear utiliza `RequirePrivacy=true` y `Persistent=true`.

Estos son ejemplos para la ejecución coordinada posterior, desde PowerShell en la raíz del proyecto:

```powershell
# Solo lectura local: no autentica, no mapea y no lee el archivo de prueba.
./storage/Map-AgencySmb.ps1 -AuditOnly | ConvertTo-Json -Depth 5
# Simulación después de recibir el SHA256 real del archivo creado desde Linux.
./storage/Map-AgencySmb.ps1 -ExpectedProbeSha256 $Sha256ObservadoEnLinux -WhatIf
# Operación posterior, cuando el responsable haya coordinado la prueba triple.
./storage/Map-AgencySmb.ps1 -ExpectedProbeSha256 $Sha256ObservadoEnLinux
```

Sin SHA256 esperado válido, la operación bloquea antes de autenticar. Después de crear o verificar el mapping, lee únicamente el archivo existente `P:\.probe`, limita su tamaño a 1 MiB y compara su SHA256. Nunca lo crea ni sobrescribe; no publica sus bytes. La salida distingue la política de cifrado exigida del dialecto de sesión, que sigue sin acreditar mientras no pueda consultarse `Get-SmbConnection`.

Las pruebas de `tests/test_smb_mapping.py` sustituyen comandos e inventario por simulaciones sin red. Cubren modo de auditoría, simulación, contraseña pendiente, análisis literal, importación sin efectos, unidad ajena, idempotencia, persistencia y contrato del cmdlet. Sus resultados no sustituyen la ejecución real registrada a continuación.

## Resultado real posterior

El responsable ejecutó el mapper después de configurar el NAS y recibir el hash del archivo escrito y sincronizado desde `.85`. El informe del **2026-11-05 a las 22:39:22 UTC** acredita `P:` en el destino exacto, `RequirePrivacy=true`, `Persistent=true` y `CredentialManagerModified=false`. Leyó **170 bytes** con SHA256 `3a62a47c7709a3d340c12cffc974c4d0e945b1a90ebfcd50533d730545e882bc`, coincidente con `.85` y `.7`. Evidencia: `.tools/evidence/windows-smb-mapping.json`.

El campo `SessionDialectVerified=false` del mapper se conserva: no se eludió la restricción de `Get-SmbConnection`. Una captura posterior del NAS, filtrada por `jandrade` y sin publicar otras sesiones, acreditó a las **22:40:04 UTC** una conexión de `producciones` desde `.6`, correlacionada por sesión y proceso: **SMB3_11**, cifrado completo **AES-128-GCM**. El [verificador local](verify_smb_session.py) recibió JSON completo con salida cero y sin truncado; el resultado saneado está en `.tools/evidence/synology-smb-session-verification-final.json`.

La persistencia está configurada; no se ensayó mediante un reinicio de Windows. La evidencia de la lectura triple y el cierre integrado se registran por separado en [Fase 2](../docs/ci/evidence/2026-11-05-fase-2.md). El responsable confirmó su cierre tras 220 pruebas Python y los controles finales, conservando estos límites.
