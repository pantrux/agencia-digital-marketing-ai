# Aceptación parcial de MoneyPrinter y Speaches

Esta unidad prepara pruebas del overlay MIT del commit
`6992380c5e82f07f3e1fc46b90927b6f1c655407`. No importa MoneyPrinter, Streamlit,
Torch ni Whisper. No ejecuta FFmpeg, SDK, contenedores o instalación de paquetes.
No lee `.env`: Root conserva su transporte SSH, prepara las identidades y entrega
el único secreto por stdin cuando autoriza la prueba HTTP.

## Qué se ejecuta

El paquete contiene los dos módulos MIT compartidos y el proveedor congelados,
las fuentes originales de subtitle/task, sus variantes parcheadas, el escritor
upstream y la licencia MIT original de Harry (2024), íntegra. Cada entrada se
contrasta por SHA256; el preparador también coteja los blobs upstream y el
preparador de overlay fijado. El audio es exclusivamente la muestra sintética
Fase 3 de 493.624 bytes, SHA256 `00bf9721fcfbe2dfd69780d912048c969098d693c8ee25216a8b4f49da94fb99`.

Se registran origen, SHA de fuente completa, líneas, bytes y SHA de cada función
extraída: originales y parcheadas `create`, `transcribe_audio_bytes` y
`generate_subtitle`, además de las tres funciones del escritor. Los fragmentos
originales se conservan como datos. Sólo se compilan los fragmentos parcheados y
el escritor MIT con dependencias explícitas stdlib y un logger doble que no
imprime. No se presenta esto como una importación de App ni una prueba de UI.

La rueda SRT 3.5.3 fijada por SHA256
`bee6b3ba4b1f683f3d8199d60b0d22df316220b8851cae8cbab8f2ae18de8d12`
se conserva completa. Se carga su miembro `srt.py` por los bytes verificados de
la rueda, sin instalarlo. Las pruebas de parse/compose incluyen Unicode, texto
multilínea, tiempos 1,25/5,75 segundos, roundtrip y entrada inválida. Una ejecución
Windows no acredita Linux; Root cerrará ese pendiente con el modo `fixtures` en
el host.

## Plan y paquete público

Desde el workspace, sin red, secretos ni escrituras:

```powershell
python docker/moneyprinter/accept_speaches.py
```

El plan incluye el SHA del paquete reproducible, entradas y fragmentos. Los tres
archivos de código de aceptación se incluyen por sus bytes exactos. No se añade
un hash autorreferente dentro del propio código. `--bundle` emite el JSON canónico
completo por stdout; sólo Root debe reservar un archivo nuevo antes de capturarlo.
El límite del paquete es 2 MiB. El script remoto sin argumentos también muestra
un plan y no lee stdin.

Root debe respaldar el SHA del paquete, los dos scripts, ruta/owner/identidad y
marcador de una raíz nueva antes de cualquier transferencia. Raíz propuesta:

```text
/home/jandrade/.agency-moneyprinter-speaches-acceptance-v1
owner = agencia-digital-marketing-ai:moneyprinter-speaches-acceptance:v1
```

El arnés **no crea ni adopta esa raíz**. Root debe crearla privada 0700, con UID y
GID de su proceso (0 o 1000), y preparar `.agency-mpt-acceptance-owner.json` como
JSON canónico sin salto de línea, con este esquema:

```json
{"schema":1,"owner":"agencia-digital-marketing-ai:moneyprinter-speaches-acceptance:v1","path":"/home/jandrade/.agency-moneyprinter-speaches-acceptance-v1","directory":{"device":2050,"inode":12345}}
```

Los números son ilustrativos: Root debe usar los reales. El primer frame contiene
la prueba cerrada `path`, `owner`, `directory` y `marker_sha256`. El arnés verifica
ancestros NoFollow, propietario/modos, identidad y bytes del marcador. Exige Linux,
hostname `vm-media-01` e IP local .85 mediante ioctl, sin ejecutar comandos.
Los subdirectorios `fixtures-v1` y `http-v1` se crean EXCL: un intento anterior
se conserva y causa rechazo, sin sobreescritura ni borrado por patrón.

## Framing y ejecución posterior de Root

Cada frame es longitud big-endian de cuatro bytes seguida de sus bytes exactos:

1. Prueba de raíz, máximo 4 KiB, JSON canónico sin secretos.
2. Paquete público, máximo 2 MiB, cuyo SHA se pasa explícitamente por argumento.
3. Sólo en modo `http`: Bearer ASCII, 32–256 bytes, sin salto de línea.

Root usa su conector SSH acreditado, Python host y entorno mínimo; no hay un
conector nuevo dentro de estos scripts. Debe usar su supervisor congelado para
acotar proceso/captura y conservar stdout/stderr sin ampliar el alcance.

```text
python3 -I -S -B -W error::ResourceWarning accept_speaches_remote.py --execute fixtures --bundle-sha256 HASH_RESPALDADO
python3 -I -S -B -W error::ResourceWarning accept_speaches_remote.py --execute http --bundle-sha256 HASH_RESPALDADO
```

El modo fixtures reserva 45 segundos y ejecuta exactamente 22 pruebas stdlib,
sin skips ni fallos aceptados. Los sockets son socketpair propios; no publica
puertos ni conecta a la LAN. Comprueba protocolo multipart, transcripción,
silencio **simulado**, 401/429/503/redirect, JSON inválido/truncado, timeout,
cancelación, límite de audio, limpieza de hilos y archivos, selección de funciones,
publicación SRT y rueda SRT. En Windows las ramas NoFollow/permisos Linux se
declaran pendientes; no se fabrican skips ni pruebas nativas.

El modo HTTP reserva 310 segundos para una aceptación y su limpieza. Hace sólo
dos peticiones, sin reintentos, al endpoint fijo
`http://192.168.0.6:8000/v1/audio/transcriptions`: primero token erróneo (debe
rechazarse con 401), después la muestra Fase 3 y el token recibido. Modelo fijo
`Systran/faster-whisper-small`; no hay host, URL o modelo elegibles por petición.
La petición válida conserva `verbose_json`, `stream=false` y tiempos de palabras.
Se exigen las 28 palabras y los tres segmentos observados en la aceptación previa.
Un puente apagado o cualquier diferencia falla explícitamente.

El cliente real obtiene una sola respuesta válida. Ese JSON se convierte usando
el proveedor congelado y se publica mediante las funciones extraídas con un doble
explícito que devuelve el resultado ya obtenido: no se repite inferencia para
probar el escritor. El recibo distingue cliente HTTP real, conversión real y doble
de E/S del publicador. El enlace proveedor→cliente se comprueba por transporte
controlado en fixtures. No se atribuye una llamada App completa a esa composición
de pruebas. Silencio real de GPU no se acredita con una respuesta simulada.

El Bearer se escribe EXCL en un temporal 0600 dentro del run propio, se usa por
el lector privado congelado y se retira sólo si identidad/bytes siguen siendo
los propios. Nunca se imprime ni se guarda su hash. El resto de materiales,
logs, SRT y recibos permanecen para auditoría, incluidos los fallos. Los originales,
el overlay y sus licencias no se modifican. Las temporales de las pruebas usan
`TMPDIR` privado y deben quedar vacías.

SIGTERM y la alarma desenrollan los bloques de limpieza. SIGKILL o una espera de
kernel no interrumpible pueden dejar el run privado pendiente: Root debe revisarlo
por su identidad antes de retirar un archivo, nunca borrar por patrón.

## Verificación local y límites

```powershell
python -B -W error::ResourceWarning -m unittest discover -s tests -p test_moneyprinter_speaches_acceptance.py -v
python -m ruff check docker/moneyprinter/accept_speaches.py docker/moneyprinter/accept_speaches_remote.py tests/test_moneyprinter_speaches_acceptance.py
```

Los resultados locales se limitan a funciones extraídas y sockets controlados.
Linux nativo, HTTP a la GPU y sus recibos siguen pendientes hasta la ejecución
explícita de Root. No se certifican aplicación completa, UI, dependencias CPU,
imagen/SCA, extracción FFmpeg, servicios, 90 minutos ni fragmentación. El límite
de audio sigue siendo 24 MiB; sobrepasarlo se rechaza antes de conectar.
