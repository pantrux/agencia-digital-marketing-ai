# Aprovisionamiento aislado de la VM de medios

Estos scripts operan exclusivamente sobre `vm-media-01`, `192.168.0.85`, el host `192.168.0.3`, `datastore1` y el PortGroup estándar `LAN`. La plantilla debe ser `TPL-ubuntu2404-k8s`, con un disco de hasta 80 GB y una interfaz. Los valores de acceso se leen desde la bóveda; no se ejecuta su contenido ni se imprime la contraseña.

## Certificado de vCenter

La conexión de PowerCLI exige `InvalidCertificateAction=Fail` en el ámbito de la sesión. El script no hereda `Ignore` ni `Warn` de otros proyectos, no usa `-Force` para omitir validaciones y no cambia la configuración persistente de PowerCLI.

`Get-VCenterCertificate.ps1` obtiene únicamente el certificado público presentado por `192.168.0.2:443`. Rechaza el handshake TLS después de capturarlo, no envía credenciales y devuelve las huellas SHA256/SHA1, emisor y fechas. Esa observación aislada no autentica al servidor.

```powershell
./vcenter/Get-VCenterCertificate.ps1 | ConvertTo-Json
```

La primera observación real encontró un certificado vigente cuyo nombre corresponde a `192.168.0.2`, con cadena VMware del laboratorio aún no confiable en esta estación. Se utiliza un pin de primer uso: su observación no fue cotejada por un canal independiente y no debe describirse como tal. Registrar las huellas observadas como `VSPHERE_CERT_SHA256` y `VSPHERE_CERT_SHA1` en `.env`, con los comentarios de propósito, destino y rotación y las claves `CHANGE_ME` en `.env.example`, antes de configurar confianza.

`Register-VCenterTrust.ps1` vuelve a observar el certificado público y exige coincidencia de ambas huellas y vigencia. Después puede registrar únicamente el par exacto servidor/certificado en el PCTCS del usuario actual. Preserva todas las filas de otros servidores, reemplaza solamente las del endpoint `.2` y no instala una CA global. La segunda observación confirma continuidad con el pin respaldado; tampoco constituye un cotejo independiente.

```powershell
# Simula la escritura después de reobservar y comparar el certificado público.
./vcenter/Register-VCenterTrust.ps1 -WhatIf | ConvertTo-Json
# Registra únicamente el pin ya respaldado en el almacén del usuario actual.
./vcenter/Register-VCenterTrust.ps1 | ConvertTo-Json
```

La implementación instalada del SDK 13.5 confirmó que el archivo PCTCS consume registros `servidor,huella` sin encabezado, comillas ni BOM. El registrador reconoce y migra también el CSV con encabezado que se creó inicialmente; conserva los otros servidores. Las pruebas locales verifican ambos formatos y el rechazo de huellas inválidas.

Después de corregir ese formato, el SDK reconoció la excepción exacta, pero PowerCLI Core todavía rechazó la conexión con `PartialChain`. Esa comprobación no constituye una auditoría exitosa. `Get-VCenterCertificateBundle.ps1` descarga las CA públicas con un cliente HTTP que exige ambos pins del certificado de máquina, el nombre `.2` y su vigencia. No envía credenciales ni altera confianza global. Construye después una cadena `CustomRootTrust`, verifica firmas y vigencia y guarda las CA que verificaron el certificado en `.tools/evidence/`, junto con las huellas y el resultado. La comprobación local no consulta revocación externa.

Respaldar la CA DER como `VSPHERE_CA_CERT_DER_B64` y sus huellas `VSPHERE_CA_CERT_SHA256` y `VSPHERE_CA_CERT_SHA1` en la bóveda; actualizar después `.env.example`. `Register-VCenterCA.ps1` exige esos datos y el pin de máquina, reobserva el servidor y vuelve a verificar la cadena antes de importar únicamente esa CA en `Cert:\CurrentUser\Root`. Es idempotente y admite `-WhatIf`. La confianza abarca los certificados válidos emitidos por esa CA para el usuario actual; no escribe en el almacén de la máquina ni modifica vCenter.

```powershell
./vcenter/Get-VCenterCertificateBundle.ps1 | ConvertTo-Json -Depth 6
# Después del respaldo de la CA en .env y de actualizar la plantilla pública:
./vcenter/Register-VCenterCA.ps1 -WhatIf | ConvertTo-Json
./vcenter/Register-VCenterCA.ps1 | ConvertTo-Json
```

PowerCLI describe el archivo y sus dos campos en su [ayuda oficial del almacén de confianza](https://www.powershellgallery.com/packages/VMware.VimAutomation.Sdk/13.3.0.24145081/Content/about_invalid_certificates.help.txt). La guía de Broadcom explica cómo [instalar la CA de vCenter en la estación](https://knowledge.broadcom.com/external/article/330833/download-and-install-vcenter-server-root.html). Una renovación exige comprobar y respaldar primero la nueva huella; no desactivar la comprobación TLS para resolver el fallo.

## Auditoría y simulación

Ejecutar después de cerrar las comprobaciones de la Fase 0:

```powershell
./vcenter/01-deploy-media-vm.ps1 -AuditOnly | ConvertTo-Json -Depth 6
./vcenter/01-deploy-media-vm.ps1 -WhatIf | ConvertTo-Json -Depth 6
```

La auditoría informa memoria y almacenamiento libres, discos e interfaces de la plantilla, identidad de cualquier VM existente y respuestas de la IP por inventario, ICMP y TCP 22. La ausencia de respuesta no demuestra disponibilidad: contrastar también la reserva DHCP y la tabla ARP del router. Cualquier VM existente sin el marcador de propiedad, o con ID/UUID distintos de la bóveda, bloquea las mutaciones.

## Clonado y registro inmediato

La primera ejecución real clona la VM apagada y devuelve `VmId` y `VmUuid`. Se detiene antes de configurar o encender. Respaldar inmediatamente esos valores en `.env` como `VSPHERE_MEDIA_VM_ID` y `VSPHERE_MEDIA_VM_UUID`, documentarlos y agregar las claves a `.env.example`. Respaldar también los identificadores de infraestructura descubiertos que se utilizarán en la operación.

La siguiente ejecución valida los dos identificadores y el marcador de propiedad. Antes de personalizar necesita `MEDIA_VM_SSH_PUBLIC_KEY` Ed25519 y su clave privada correspondiente ya respaldadas por el responsable en `.env`, además de `MEDIA_TEMPLATE_CLOUDINIT_VMWARE_VERIFIED=true` documentado después de comprobar el origen VMware de cloud-init en la plantilla. La auditoría no puede probar ese requisito consultando solamente hardware. La [documentación oficial del origen VMware de cloud-init](https://github.com/canonical/cloud-init/blob/main/doc/rtd/reference/datasources/vmware.rst) describe los campos `guestinfo.metadata` y `guestinfo.userdata` utilizados.

```powershell
# Tras registrar los IDs y verificar los prerrequisitos, configura sin encender.
./vcenter/01-deploy-media-vm.ps1
# El arranque explícito utiliza la configuración ya respaldada y verificada.
./vcenter/01-deploy-media-vm.ps1 -Start
```

La configuración ajusta 6 vCPU, 16 GB RAM y un disco Thin de 80 GB sin reducir volúmenes; establece `.85/24` mediante cloud-init y mantiene bloqueado el acceso SSH por contraseña. Reutiliza `Get-MediaSshHostIdentity` y `New-MediaCloudInitPayload` de `Test-MediaCloudInit.ps1`, cuya importación no ejecuta infraestructura: conserva tanto la clave de acceso como la identidad SSH del servidor ya respaldadas, sin generar otras. Una VM ya encendida se conserva para verificarla por SSH. La instalación de Docker, el runner y el snapshot `01-vm-limpia` debe completarse y comprobarse antes de declarar terminada la Fase 1.

## Verificaciones locales realizadas

Antes de instalar paquetes, `New-MediaBaselineSnapshot.ps1 -Name 00-pre-bootstrap` permite crear la salvaguarda inicial únicamente con la VM propia apagada. Al finalizar la instalación del runner y sus verificaciones, `-Name 01-vm-limpia` crea la segunda referencia con la misma condición. Ambos nombres están restringidos; no se admiten más de dos instantáneas ni se elimina ninguna automáticamente.

El registrador comprueba nombre, ID, UUID, host y marcador de propiedad de la VM. Solo reutiliza una instantánea cuyo nombre y descripción acreditan la misma propiedad; cualquier instantánea ajena bloquea la operación. Devuelve `Name`, `Id`, `Created` y `CreatedAtUtc`: `Created` es un booleano que indica creación en esa ejecución; `CreatedAtUtc` es la fecha real de la instantánea, tanto al crear como al reutilizar. No usa el ID recién creado para consultar ni restaurar antes de que el responsable lo respalde en `.env` y actualice `.env.example`. La creación utiliza `Memory=false` y `Quiesce=false` porque el invitado debe estar apagado. `-WhatIf` consulta y valida, sin crear. Las siete pruebas de `tests/test_vcenter_snapshots.py` comprueban estas guardas con objetos locales y sin cargar PowerCLI.

### Instantánea previa a una actualización mayor de Ubuntu o Docker

Usar un nombre `pre-cambio-` con un identificador nuevo para cada intervención. La primera ejecución revisa la capacidad y la identidad de la VM en vCenter. Si las dos referencias iniciales ocupan el inventario, hay que resolver su retención antes de la actualización. El script devuelve `CAPACIDAD_OCUPADA_POR_REFERENCIAS_INICIALES`. Las instantáneas residen en el mismo datastore que el disco base. Broadcom recomienda conservarlas como puntos temporales de reversión, hasta 72 horas. [Buenas prácticas de Broadcom](https://knowledge.broadcom.com/external/article/318825/best-practices-for-using-vmware-snapshot.html)

```powershell
./vcenter/New-MediaBaselineSnapshot.ps1 -Name pre-cambio-docker-cambio-01 -WhatIf
```

En la ventana de mantenimiento, apagar ordenadamente `vm-media-01` y repetir el comando sin `-WhatIf`. Si el inventario está lleno y contiene una instantánea propia de un cambio anterior, el script consolida la más antigua de esas instantáneas, conserva sus descendientes y vuelve a cotejar el inventario antes de crear la nueva. Si se interrumpe entre consolidación y creación, repetir con el mismo identificador. Respaldar el ID devuelto en `.env` y agregar su clave `CHANGE_ME` a `.env.example` antes de iniciar la actualización. Si hay que revertir, medir desde la orden de reversión hasta que la VM y las sondas de sus servicios vuelvan a responder. El objetivo de recuperación es inferior a tres minutos. [Arquitectura de continuidad](../docs/ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md#25-continuidad-operativa-estrategia-de-backups-3-2-1-y-retención-automática)

Los scripts tienen sintaxis validada con el parser y análisis PSScriptAnalyzer sin errores ni advertencias. Las pruebas `tests/test_vcenter_config.py` comprueban valores congelados, credenciales pendientes, claves duplicadas y rechazo de una IP de otro equipo. `tests/test_vcenter_trust_format.py` comprueba el formato nativo, la migración del CSV antiguo preservando otros servidores y el rechazo de huellas inválidas.

Antes de operar infraestructura, ejecutar `./vcenter/Test-PowerCLIContracts.ps1`: analiza el AST de todos los scripts de vCenter y contrasta sus parámetros con el SDK instalado, normalizando alias de comandos y parámetros y comprobando que exista un conjunto compatible. No conecta a vCenter ni ejecuta las llamadas analizadas. La comprobación real con PowerCLI `13.5.0.25380678`, después de incorporar el registrador de instantáneas, validó 48 llamadas; detecta tanto el parámetro `-Server` inexistente en `Set-AdvancedSetting` como la combinación incompatible de `-Portgroup` y `-StartConnected` en `Set-NetworkAdapter`.

La auditoría real con `InvalidCertificateAction=Fail` se completó después de instalar la CA verificada en el almacén del usuario; su evidencia está en `.tools/evidence/vcenter-audit.json`. El clonado real dejó la VM apagada, con el hardware heredado de 4 vCPU, 8 GB RAM y disco Thin de 40 GB, acreditado en `vcenter-clone.json` y `vcenter-clone-hardware.json` del mismo directorio de evidencias. Sus identificadores ya fueron respaldados por el responsable en la bóveda. Esto todavía no acredita la prueba aislada de cloud-init, el ajuste a 6/16/80, SSH operativo, la instalación del runner ni el snapshot inicial; la Fase 1 sigue pendiente de esas verificaciones.

## Preparación de Ubuntu y registro del runner

`bootstrap-media-vm.sh` tiene cuatro modos separados. Todos comprueban Linux, hostname `vm-media-01`, dirección `192.168.0.85/24`, Ubuntu 24.04 amd64 y que el UID 1000 pertenezca a `jandrade`, con directorio personal `/home/jandrade`. Se detienen si el destino no coincide. La tabla describe el comportamiento del código; no acredita que se haya ejecutado sobre el clon.

Antes de usar estos modos, completar y registrar la prueba aislada de cloud-init, reconciliar el hardware a 6 vCPU/16 GB/80 GB, habilitar LAN y comprobar SSH con las claves respaldadas. Transferir por ese canal el script auditado desde la copia local. `SCRIPT_PREPARACION` en los ejemplos es su ruta absoluta ya verificada en `.85`; no descarga fuentes del repositorio público.

| Modo | Usuario que lo ejecuta | Operación y resultado esperado |
|---|---|---|
| `packages` | root mediante `sudo` | Instala Docker Engine, Compose 2 y herramientas de NFS, copias y diagnóstico. Habilita Docker y añade `jandrade` a su grupo; no despliega contenedores de aplicación. |
| `runner-install` | root mediante `sudo` | Descarga la versión de Actions Runner fijada en el script, verifica SHA256 y la instala en `/opt/actions-runner`. No registra el ejecutor ni inicia un servicio. |
| `runner-configure` | `jandrade`, UID 1000, sin `sudo` | Registra el ejecutor con un token ya respaldado, conserva los tres archivos de identidad y termina antes de instalar/iniciar el servicio. |
| `runner-start` | root mediante `sudo` | Exige un recibo válido del respaldo de identidad, instala la unidad si falta y comprueba servicio activo y proceso con UID 1000. |

### Instalar paquetes y distribución del ejecutor

En una sesión de `jandrade` sobre `.85`:

```bash
: "${SCRIPT_PREPARACION:?Falta ruta absoluta del script auditado en .85}"
sudo bash "$SCRIPT_PREPARACION" packages
sudo bash "$SCRIPT_PREPARACION" runner-install
```

Las versiones y hashes están fijados en el script; su actualización requiere un cambio revisado. `packages` se detiene si encuentra paquetes incompatibles como `containerd`, `runc`, `docker.io` o Compose de Ubuntu, en lugar de desinstalar componentes de la plantilla. Inspeccionar entonces el clon propio antes de preparar su sustitución; no aplicar cambios a la plantilla ni a otros equipos.

`runner-install` conserva una instalación que tenga la marca SHA256 esperada y sus ejecutables. Si existe otra instalación, una marca distinta o un directorio ocupado sin marca, rechaza sobrescribirlo. La distribución se instala sin configuración ni trabajo en ejecución.

### Respaldar el token y registrar sin iniciar

1. Obtener el token de registro del repositorio autorizado y persistir su valor real como `GITHUB_RUNNER_REGISTRATION_TOKEN` en la bóveda maestra de Windows, con `# Para qué:`, `# Dónde:` y `# Riesgo al rotar:`. Sincronizar después su clave `CHANGE_ME` en `.env.example`. Si caduca antes del registro, repetir ese orden con el nuevo token.
2. Solo después transferir los mismos bytes del token a un archivo regular en `.85`, propiedad de UID 1000 y permisos `0600`. No añadir saltos de línea a los bytes utilizados para su SHA256. La huella esperada debe obtenerse de la copia ya respaldada antes de transferirla; no dar por respaldado un token calculando la huella únicamente de su archivo remoto.
3. Ejecutar como `jandrade`, sin `sudo`:

   ```bash
   : "${ARCHIVO_TOKEN:?Falta ruta absoluta del token ya respaldado}"
   : "${SHA256_TOKEN_RESPALDADO:?Falta SHA256 hexadecimal de la copia respaldada}"
   bash "$SCRIPT_PREPARACION" runner-configure \
     --token-file "$ARCHIVO_TOKEN" \
     --token-vault-sha256 "$SHA256_TOKEN_RESPALDADO"
   ```

El script comprueba propietario, permisos, formato y coincidencia de SHA256 antes de registrar. Transmite el token a `config.sh` mediante su variable de entorno de entrada; no lo incluye como argumento del proceso. El registro fija repositorio, nombre `vm-media-01`, etiquetas `media-platform,homelab,vm-media-01`, directorio de trabajo `_work` y actualización automática desactivada. Si ya existe un registro completo del mismo repositorio y nombre, lo conserva; un registro de otro ejecutor o incompleto bloquea.

### Respaldar la identidad generada antes del servicio

El registro crea estos archivos en `/opt/actions-runner` y los deja con permisos `0600`:

| Archivo | Variable base64 en la bóveda | Variable del SHA256 | Contenido |
|---|---|---|---|
| `.runner` | `GITHUB_RUNNER_CONFIG_B64` | `GITHUB_RUNNER_CONFIG_SHA256` | Configuración e identificadores del ejecutor registrado. |
| `.credentials` | `GITHUB_RUNNER_CREDENTIALS_B64` | `GITHUB_RUNNER_CREDENTIALS_SHA256` | Credenciales persistentes del ejecutor. |
| `.credentials_rsaparams` | `GITHUB_RUNNER_RSA_PARAMS_B64` | `GITHUB_RUNNER_RSA_PARAMS_SHA256` | Material de identidad usado por el ejecutor. |

Transferir los tres archivos por el canal SSH verificado a la estación responsable. Guardar sus bytes completos codificados en base64 en las tres variables de `.env`, cada una documentada, y registrar sus SHA256. Base64 conserva bytes; no cifra el contenido. Respaldar además `GITHUB_RUNNER_ID` con el `agentId` real de `.runner` y `GITHUB_RUNNER_SERVICE_UNIT` con `actions.runner.pantrux-agencia-digital-marketing-ai.vm-media-01.service` antes de arrancar. Sincronizar las claves correspondientes con `CHANGE_ME` en `.env.example`. No pegar estos archivos en registros de consola, incidencias ni repositorios.

Verificar que los bytes decodificados de la bóveda tengan exactamente los hashes de los tres archivos remotos. Hasta completar esa comparación, el registro permanece sin servicio. El modo `runner-start` no lee la bóveda de Windows directamente: exige que el responsable deje constancia del respaldo mediante un recibo escrito por root después de esta verificación.

### Crear el recibo y arrancar como UID 1000

Guardar el recibo como archivo regular, propiedad de root y permisos `0600`, dentro de un directorio controlado por root, por ejemplo `/root/media-platform-receipts/runner.json`. Debe contener exactamente estos campos; los textos de fecha y hash del ejemplo deben sustituirse por valores reales verificados:

```json
{
  "schema_version": 1,
  "repository_url": "https://github.com/pantrux/agencia-digital-marketing-ai",
  "runner_name": "vm-media-01",
  "vault_path": "D:\\Proyectos\\Agencia Digital de Marketing AI\\.env",
  "env_example_synchronized": true,
  "vault_persisted": true,
  "created_at_utc": "FECHA_UTC_ISO8601_REAL",
  "files": {
    ".runner": "SHA256_REAL_DE_64_HEXADECIMALES",
    ".credentials": "SHA256_REAL_DE_64_HEXADECIMALES",
    ".credentials_rsaparams": "SHA256_REAL_DE_64_HEXADECIMALES"
  }
}
```

No crear el recibo ni marcar sus dos booleanos como verdaderos antes de persistir y verificar las tres copias y la plantilla. La fecha debe tener zona horaria explícita; el controlador rechaza fechas futuras que excedan cinco minutos. Después:

```bash
: "${RECIBO_RESPALDO:?Falta ruta absoluta al recibo escrito por root}"
sudo bash "$SCRIPT_PREPARACION" runner-start --vault-backup-receipt "$RECIBO_RESPALDO"
```

El controlador rechaza recibos de otro repositorio/nombre, campos ausentes o adicionales, archivos enlazados, hashes distintos y credenciales con propietario o permisos incorrectos. Solo inicia la unidad `actions.runner.pantrux-agencia-digital-marketing-ai.vm-media-01.service` después de estas verificaciones. Comprueba que la unidad use `jandrade`, que esté activa y que su proceso tenga UID 1000. El grupo Docker permite administrar el motor del clon; que el proceso no sea root no elimina ese alcance.

Confirmar además por la API de Actions que el ejecutor del nombre esperado figure en línea y respaldar su ID real. Esa comprobación demuestra registro y conexión saliente del servicio; no demuestra un pipeline ejecutado ni una entrega de aplicaciones.

### Repositorio público limitado a la licencia

Por instrucción del usuario, el código de la plataforma permanece en la copia local. El repositorio público [pantrux/agencia-digital-marketing-ai](https://github.com/pantrux/agencia-digital-marketing-ai) contiene únicamente `LICENSE`, identificada por GitHub como MIT. Los modos de preparación no publican fuentes ni ejecutan comandos Git. El runner puede quedar registrado y en línea aunque ese repositorio no contenga workflows; su registro no acredita los siete controles remotos de Fase 0.

Se verificaron por API del repositorio `approval_policy=all_external_contributors`, `default_workflow_permissions=read` y `can_approve_pull_request_reviews=false`; las variables `GITHUB_ACTIONS_*` correspondientes se respaldaron antes de aplicar y volver a consultar esas políticas. La consulta real, incluida la presencia exclusiva de `LICENSE`, quedó en `.tools/evidence/github-actions-permissions.json`. La [referencia oficial de permisos de Actions](https://docs.github.com/en/rest/actions/permissions) documenta las consultas de aprobación de contribuciones externas y permisos del token. Mantener esa configuración al operar el runner; cualquier publicación futura de workflows o fuentes requiere una nueva instrucción del usuario.
