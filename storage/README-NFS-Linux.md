# Cliente NFS de producciones en los dos nodos Linux

`configure-nfs-client.sh` prepara exclusivamente el cliente local `192.168.0.85` o `192.168.0.7`. Utiliza el contrato puro de `nfs_client_config.py`; ambos archivos deben permanecer juntos. No conecta por SSH a otro equipo ni configura el servidor Synology.

El origen está fijado en `192.168.0.8:/volume2/producciones`, el destino en `/mnt/producciones` y la versión en NFSv4.1 sobre TCP. El usuario eligió `volume2` antes de crear el recurso; su ruta y exportación quedaron respaldadas en la bóveda antes de actualizar los clientes. No se admiten parámetros para cambiar servidor, exportación, destino o comandos. Se requiere root, Linux, Python 3, `nfs-common` y las utilidades estándar de Ubuntu. No se instalan ni actualizan paquetes automáticamente en el equipo de agentes.

## Auditoría previa y ejecución

Verificar el hostname e IP reales usando el acceso ya auditado de cada cliente. `.85` debe identificarse como `vm-media-01`; para `.7` se exige el hostname observado, sin inferirlo a partir del nombre de la VM. El script comprueba que el hostname coincida y que la IP LAN `/24` corresponda únicamente al cliente autorizado.

En `vm-media-01`:

```bash
sudo bash storage/configure-nfs-client.sh --target-ip 192.168.0.85 --expected-hostname vm-media-01 --check
# Aplicar cuando el export y sus permisos ya estén configurados y comprobados en el NAS.
sudo bash storage/configure-nfs-client.sh --target-ip 192.168.0.85 --expected-hostname vm-media-01 --apply
```

En `.7`, usar el mismo procedimiento con `--target-ip 192.168.0.7` y su hostname real previamente observado. No usar un nombre supuesto ni cambiar el hostname para satisfacer la guarda.

`--check` es el comportamiento predeterminado. Consulta identidad, fstab y montaje existente, sin crear archivos, directorios ni montajes. Si el recurso todavía no está montado, informa `share_mounted_and_verified: false`; esa salida no acredita que el NAS esté configurado o que permita acceder al export.

## Reconciliación y límites

- Se preservan las líneas ajenas de `/etc/fstab`. Solo se reemplaza el bloque delimitado por `INICIO/FIN AGENCIA AI PRODUCCIONES NFSv4.1` después de verificar que contenga una única entrada del recurso propio.
- Una entrada ajena para `/mnt/producciones`, incluso apuntando al mismo NAS, bloquea la operación. También se rechazan marcadores incompletos, duplicados y destinos codificados que oculten el mismo punto de montaje.
- Una entrada o montaje previo de `192.168.0.8:/volume1/producciones` se rechaza como recurso equivocado, incluso dentro de los marcadores propios. No se migra ni desmonta automáticamente.
- Un destino sin montaje debe estar vacío y no puede ser un enlace. Un destino ya montado debe acreditar exactamente el origen esperado, tipo `nfs4`, opciones `rw`, `vers=4.1` y `proto=tcp`.
- Solo en `--apply`, después de rechazar enlaces y destinos ocupados, se prepara el directorio vacío antes de validar el candidato con `findmnt`. Su verificador exige que el destino local exista, incluso con `nofail`, y omite la comprobación de fuentes NFS. Después se intenta únicamente el origen exacto, con plazo acotado, y se comprueba su versión real. No se usa `mount -a`, no se desmontan recursos ni se reinician servicios existentes.
- Se comprueba que fstab no haya cambiado durante la operación. Los respaldos propios se guardan bajo `/var/lib/media-platform-storage/backups`, con directorios `0700` de root y copia `0600`. Se rechazan enlaces o un directorio existente sin esa propiedad y privacidad.
- La publicación de fstab es atómica. Si falla `systemctl daemon-reload`, se restaura el archivo anterior; el montaje exacto que se haya realizado permanece disponible para revisión. No se detienen consumidores ni se fuerza un desmontaje como compensación.

La entrada persistente usa `_netdev`, `nofail` y un plazo de montaje de 30 segundos para conservar el arranque del cliente cuando el NAS no está disponible. Los servicios de medios deberán comprobar que el recurso esté realmente montado antes de utilizarlo; la existencia del directorio por sí sola no acredita almacenamiento compartido.

## Verificación

Las trece pruebas de `tests/test_nfs_client.py` comprueban preservación de entradas ajenas, idempotencia, reconciliación del bloque propio, rechazo de `volume1`, conflictos y escapes fstab, identidades y versiones de montaje, y el orden de preparación del destino con sus guardas. ShellCheck y el parser Bash validan el script localmente.

Después de verificar el NAS, ambos clientes ejecutaron `--apply` una vez: `.85` en 7,117 segundos y `.7` en 2,229 segundos. `findmnt` acreditó el origen de `volume2`, destino `/mnt/producciones`, tipo `nfs4`, lectura/escritura, versión 4.1 y TCP. La revisión posterior confirmó fstab reconciliado y el recurso montado. Se verificaron los respaldos originales con archivos `0600` y directorios `0700` de root, los bytes ajenos de fstab y los otros 24 montajes de `.85` y 51 de `.7`, sin nuevas unidades fallidas. Las evidencias locales son `.tools/evidence/media-nfs-client-apply.json` y `.tools/evidence/openclaw-nfs-client-apply.json`.

La prueba local real en `.85` con util-linux 2.39.3 reprodujo `unreachable on boot required target` con el destino ausente. El verificador indicó que omitía fuente y tipo NFS; la traza de llamadas de red no mostró acceso al NAS. Se conservó fstab y no se creó el punto de montaje. Ese comportamiento coincide con el [verificador oficial de util-linux 2.39.3](https://github.com/util-linux/util-linux/blob/v2.39.3/misc-utils/findmnt-verify.c).

La Fase 2 solo se acredita después de configurar el NAS y ejecutar la prueba real: escribir el mismo archivo de prueba desde `.85`, leerlo desde `.7` y desde `P:` en Windows. Este cliente no declara realizada esa verificación triple.
