# Cliente S3 R2 del consumidor Linux

`r2_client.py` prepara GET SigV4 y un iterador de bytes con Python estándar. El endpoint se deriva exclusivamente del ID de cuenta configurado, HTTPS y puerto 443; bucket fijo `studio-media-bucket`, región `auto`, servicio `s3` y clave validada `ingest/<job_id>/<asset_id>.<ext>`. No admite URL, bucket, puerto, método o ruta del cliente. La validación reutiliza el trabajo inmutable del publicador.

`R2Credentials` requiere valores literales de cuenta y pareja S3 ya respaldados por el operador. No lee la bóveda ni crea claves. `sign_get` toma la fecha UTC SigV4 explícita y devuelve un descriptor congelado con cabeceras de sólo lectura. La representación diagnóstica omite las cabeceras y credenciales; no se debe registrar su contenido mediante otros mecanismos. Las cadenas y copias de Python no tienen garantía de borrado seguro de memoria.

`download` exige una política de bytes, un plazo total positivo de hasta 300 segundos y uno de inactividad de hasta 15 segundos, menor o igual al total. Usa TLS del sistema, sin SDK, cookies, proxies de entorno, redirecciones ni descompresión. Requiere respuesta 200, un único Content-Length decimal canónico igual al trabajo, sin Transfer-Encoding y Content-Encoding ausente o identity. Un redirect o error HTTP se rechaza sin leer su destino ni cuerpo. Entrega chunks inmutables de hasta 1 MiB, cuenta bytes y rechaza exceso/truncado. El SHA256 del contenido sigue siendo responsabilidad del publicador, que ya lo calcula antes de publicar.

Los timeouts del socket y las comprobaciones monotónicas alrededor del I/O limitan la descarga observada; `getaddrinfo` y resolución DNS no quedan acotados por el timeout del socket. El servicio debe añadir un presupuesto externo antes del uso operativo. Este módulo no garantiza por sí solo un plazo de pared estricto ni implementa el servidor HTTP. El iterador se debe consumir por completo o cerrar explícitamente en un `finally`/`contextlib.closing`; así se cierra la respuesta y conexión también si el publicador rechaza un chunk o el trabajo se cancela.

Las pruebas usan credenciales públicas de fixture, una referencia HMAC independiente y respuestas controladas: fecha, credenciales, cabeceras ambiguas, redirecciones, tamaño, presupuesto, cancelación, snapshot del trabajo y errores saneados. No instalan un servicio ni prueban conectividad R2 por sí mismas.

```text
python -W error::ResourceWarning -m unittest discover -s tests -p test_r2_sync_client.py -q
python -m ruff check docker/r2-sync-consumer/r2_client.py tests/test_r2_sync_client.py
```

El 2026-11-06 se ejecutó este módulo concreto en `.85` con SHA fijado, en una aceptación integrada sin servidor HTTP: protocolo HMAC, ledger local y publicación NFS. Descargó la muestra pública propia de 43 bytes; un proceso salió intencionalmente con 77 después de publicar/reconciliar el archivo existente, conservando pending. Un segundo proceso descargó de nuevo, reconcilió el mismo inodo y completó el registro; la reapertura devolvió completed y un cuerpo distinto con el mismo job_id produjo conflicto antes de descargar. La recuperación observada duró 0,308 s y cada proceso tuvo un presupuesto externo de 35 s con GNU timeout. El recibo local es `phase4-r2-sync-recovery-real.json`. No es una prueba HTTP, p95, caída del NAS ni despliegue del servicio.
