# Activación de la sincronización de varios materiales

El protocolo `/v2/sync` usa un ledger independiente para cada pareja de orden y
material. El ledger anterior se mantiene para `/v1/sync`; ambos comparten el
bloqueo del directorio de estado. No habilitar la ruta externa hasta acreditar
el ledger nuevo, el servicio actualizado y la versión correspondiente del
provisionador del NAS. El estado `ready` de `/healthz` solo acredita el ledger
original y no demuestra que `/v2/sync` esté listo.

Tras revisar y aprobar el cambio, preparar una imagen nueva con las fuentes
fijadas del proyecto y conservar su identidad. Usando el mismo usuario,
montajes, configuración privada y límites del consumidor revisado, ejecutar
`sync_runtime.py init-source-db --config <configuración-privada>` una sola vez
en una ejecución controlada, sin publicar credenciales. La inicialización
requiere que la base de datos nueva no exista y no borra ni reinicializa la
base original. Conservar el resultado antes de habilitar `/v2/sync`.

En arranques y comprobaciones posteriores usar el modo de solo lectura
`sync_runtime.py check-source-db --config <configuración-privada>`. Su salida
`ready` acredita el esquema y la custodia del ledger nuevo; un resultado de
error o un bloqueo ocupado requiere comprobar el proceso existente y no se
debe resolver borrando el ledger. Si la inicialización se interrumpe y deja
una base incompleta, detener la activación y diagnosticar el estado respaldado.

La configuración de entrada candidata se genera explícitamente con
`--sync-v2` tanto en `ingress/origin_relay.py` como en
`ingress/phase6_ingress.py`. Revisar los cambios de los tres saltos y del perfil
del provisionador antes de aplicarlos. La aceptación requiere enviar dos
materiales sintéticos firmados de una misma orden, comprobar sus recibos e
identidades por separado, repetir una solicitud y verificar que el despacho se
produzca únicamente tras confirmar ambos archivos. Ninguna de estas operaciones
se ejecuta por generar la configuración candidata.
