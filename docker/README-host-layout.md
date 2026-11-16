# Raíces propias de aplicaciones en vm-media-01

Esta unidad prepara únicamente `/opt/media-platform` para código/configuración y `/var/lib/media-platform` para estado local. No instala aplicaciones, crea subdirectorios de servicios, despliega contenedores, cambia montajes ni toca medios NAS. El preflight real `.tools/evidence/phase4-media-preflight-readonly.json` encontró ambas rutas ausentes; ese informe histórico no sustituye la inspección inmediata del script.

`host_layout.py` usa sólo biblioteca estándar. Sin argumentos, o con `plan`, inspecciona sin escribir. `apply` es explícito, requiere Linux, UID efectivo 0, hostname exacto `vm-media-01` e IPv4 real `192.168.0.85`, y exige el hash de un plan previamente revisado. No lee la bóveda maestra, utiliza contraseñas ni genera identificadores aleatorios. El controlador del responsable debe verificar el hash de las fuentes transferidas y respaldar rutas, propietario lógico y hash aprobado en la bóveda antes de ejecutar `apply`.

## Interfaz para el controlador

Desde un staging propio ya verificado en `.85`, el responsable puede recoger la salida estándar de:

```bash
sudo -n python3 host_layout.py plan
```

El JSON contiene `schema_version`, `owner`, `host`, `inventory`, `targets` y `plan_sha256`. El inventario registra tipo, propietario, permisos, dispositivo/inodo de los ancestros y destinos, marcador/recibo públicos verificados y número de entradas adicionales; no publica nombres ni contenidos de archivos de aplicaciones. Cada destino lleva una acción `create`, `finish_receipt` o `keep`. No contiene hora de inspección variable: sin cambios en la identidad ni en el inventario, el hash es estable.

`plan_sha256` es SHA256 del JSON canónico sin el propio campo `plan_sha256`: claves ordenadas, UTF-8 y separadores compactos. **No es el SHA256 del archivo JSON formateado**. Respaldar el valor devuelto en ese campo. La identidad del plan omite el UID del inspector, permitiendo inspección sin root si tiene permiso de lectura; la aplicación comprueba de nuevo UID 0 y todos los datos del host. La lista completa de IPv4 observadas forma parte del plan, incluyendo loopback.

Después de revisar y respaldar el plan, guardarlo como archivo regular privado en un staging propio. Su ruta debe ser absoluta, sin enlaces ni hardlinks, y su tamaño máximo es 64 KiB. Invocar:

```bash
sudo -n python3 host_layout.py apply --plan /ruta/propia/plan.json --expected-plan-sha256 HASH_APROBADO
```

La ruta y el hash del ejemplo deben sustituirse por los respaldados. No se admite cambiar los dos destinos por parámetros. Antes de mutar se verifica el hash recibido y se recompone el plan completo desde el host y filesystem actuales. Un cambio en cualquier destino, ancestro, propietario, permiso, marcador, recibo o contador de entradas vuelve obsoleto el plan. Se exige nueva inspección y respaldo del hash; no hay reintento de adopción automático.

La salida exitosa de `apply` contiene `success`, `owner`, `plan_sha256` y dos resultados con `path`, `role`, `changed`, identidad de directorio y SHA256 de marcador/recibo. El controlador conserva esa salida y respalda inmediatamente las identidades y hashes antes de utilizar los directorios. No imprime secretos. Los errores CLI devuelven salida 1 y un mensaje JSON genérico de rechazo; los errores de argumentos son explícitos.

## Propiedad y operaciones permitidas

Los cuatro ancestros `/`, `/opt`, `/var` y `/var/lib` deben ser directorios UID/GID 0, sin escritura de grupo u otros. Se abren con descriptores POSIX y `O_NOFOLLOW`; las operaciones posteriores usan nombres fijos relativos a esos descriptores. Se comprueba que la ruta siga apuntando al descriptor observado. Cada raíz nueva se crea UID/GID `0:0`, modo `0700`, sin chmod/chown recursivos. Sólo se ajustan los recursos que esta ejecución acaba de crear.

Cada raíz recibe estos archivos UID/GID 0, modo `0600`, regulares, sin enlaces ni hardlinks, abiertos con creación exclusiva:

- `.agencia-media-owner.json`: JSON canónico con propietario lógico `agencia-digital-marketing-ai:media-platform:v4.1`, rol `code` o `state`, ruta absoluta, host/IP, dispositivo/inodo, fecha UTC de creación y hash del plan de creación.
- `.agencia-media-layout-receipt.json`: recibo que vincula ruta/directorio con el SHA256 del marcador, la fecha de terminación y el hash del plan que completó el recibo.

Los SHA256 de esos documentos corresponden a sus bytes canónicos exactos, sin salto de línea final. Se exige el formato al releerlos. No editarlos ni moverlos entre directorios: un marcador de otra ruta, rol, host o inodo no acredita propiedad. Se sincronizan archivo y directorio después de escribir. Los planes y recibos no sustituyen un respaldo de datos.

Un directorio existente sin marcador válido se rechaza aunque esté vacío y pertenezca a root. Un directorio acreditado con ambos documentos se conserva íntegro, incluidas sus entradas de aplicaciones, sin recorrerlas ni cambiar sus permisos. Replanificarlo y aplicar el plan nuevo produce `changed=false`. Reutilizar el plan anterior a la creación se rechaza como obsoleto.

Se reconoce un parcial recuperable sólo cuando hay un marcador íntegro ligado al directorio exacto, falta el recibo y no existen entradas adicionales. Con un plan nuevo se crea exclusivamente el recibo faltante; se conserva el marcador y el hash original de creación. Si la creación se interrumpe antes de escribir un marcador íntegro, no hay adopción ni limpieza automática: requiere investigación manual. Una escritura de marcador/recibo incompleta se rechaza. Los dos directorios no forman una transacción atómica: si falla el segundo, el primero acreditado permanece y se debe replanificar. El script no elimina recursos ni revierte borrando rutas.

## Verificación y límites

Desde el workspace Windows:

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -p test_media_host_layout.py -v
python -m ruff check docker/host_layout.py tests/test_media_host_layout.py
python -m ruff format --check docker/host_layout.py tests/test_media_host_layout.py
```

Los contratos usan un filesystem modelado y archivos temporales propios para validar identidad, ambas colisiones antes de escribir, enlaces/propietario/permisos, marcador/recibo alterados, parcial recuperable, preservación de entradas, hash obsoleto y límites del archivo de plan. No llaman sudo ni contactan `.85`; no crean `/opt` ni `/var/lib`. La ejecución POSIX real, fsync y permisos Linux deberán verificarse por el responsable primero mediante `plan` y después con `apply` autorizado. Estas pruebas no acreditan un cambio en el laboratorio ni el cierre de Fase 4.

Referencias: [especificación de Fase 4](../docs/specs/FASE-4-APLICACIONES-Y-BACKUPS.md) y [operaciones POSIX por descriptor de Python](https://docs.python.org/3.12/library/os.html#files-and-directories).
