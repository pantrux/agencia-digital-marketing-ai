# Aceptación aislada de Flasgger

Estas cinco fuentes preparan una aceptación funcional separada de Zou, PostgreSQL, Kitsu y del SDK. El modo predeterminado lee artefactos existentes y devuelve un plan sin crear directorios, sockets ni procesos. El agente sólo implementa y ejecuta pruebas de contrato; la captura Flask y Edge quedan para Root después de revisar y respaldar pins, identidades y argumentos.

La baseline exacta es `.tools/kitsu-zou-build-flasgger-v2/wheels/flasgger-0.9.7.1-py2.py3-none-any.whl`, SHA256 `ee71b3e30ed15c59274d72a27277ae258378a7f28dc50ac17a4526d343c4618f`. La derivada es `.tools/kitsu-zou-flasgger-ui-v1/flasgger-0.9.7.1+agency1-py2.py3-none-any.whl`, SHA256 `db3bb5145d3d86d82a15c3fca4cd72c665873785c3ca143466962b9747a5355e`; su recibo positivo de datos tiene SHA256 `0fc3304316ed4dee2dc176136a7bcf019cadbf885e0b5e7f003663bc04ee002c`. Ese recibo no aprobó instalación, seguridad ni funcionamiento.

## Clausura y plan

[El controlador](accept_flasgger_ui.py) coteja el lock [zou-runtime-linux-cp312.lock](zou-runtime-linux-cp312.lock), SHA256 `dd6377b09d7ec1456bf5f34b7ae6e01bfa2d7e4d1c1f9680e76cd8e4d3b3d545`. Lee las ruedas auténticas existentes y comprueba ZIP, rutas, `RECORD`, identidad, `Requires-Python`, hashes y todos los `Requires-Dist` activos para Linux/Python 3.12, sin extras. Cierra dieciocho identidades: Flasgger y diecisiete dependencias. No instala con pip ni consulta un índice.

El analizador es deliberadamente finito: admite los releases numéricos, operadores y markers observados en estas ruedas. Rechaza sintaxis, URLs, extras activos o markers desconocidos antes de aceptar una clausura. No se presenta como resolvedor PEP 508 general ni omite una dependencia que no comprenda. La derivada debe conservar los mismos requisitos de la baseline.

```powershell
python -I -S -B -W error::ResourceWarning docker/kitsu-zou/accept_flasgger_ui.py
```

La salida contiene el hash del plan, modelos de las ruedas, clausura, hashes de las cinco fuentes y límites propuestos. Root debe respaldarlos antes de:

```text
python -I -S -B docker/kitsu-zou/accept_flasgger_ui.py --prepare --expected-plan-sha256 SHA256_REAL_RESPALDADO
```

La preparación crea exclusivamente `.tools/backup-source-build/flasgger-ui-acceptance-v1`, propietario `agencia-digital-marketing-ai:flasgger-ui-acceptance:v1`, bajo el padre privado y marcador de compilación ya acreditados. Publica marcador, plan, bundle, copias de ruedas y fuentes por exclusión. Incluye el Job congelado `dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44`. Comprueba ACL y bytes; no abre SSH ni ejecuta las copias. Root respalda inmediatamente identidad dev/inode, SHA del marcador, recibo de preparación y bundle. La copia privada del controlador admite la misma raíz canónica del workspace; no se sobrescriben originales ni salidas previas.

Familia propuesta única: `MEDIA_ZOU_FLASGGER_ACCEPT_V1_`, con `ROOT`, `OWNER`, `PLAN_SHA256`, `CONTROLLER_SHA256`, `BROWSER_SOURCE_SHA256`, `BUNDLE_SHA256`, `ROOT_PROOF_SHA256` y los punteros/SHA de cada etapa. Root escribe la bóveda; estas fuentes nunca la leen ni actualizan.

## Gate de runtime pendiente

La base candidata `sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72` contiene diecinueve APK y **no contiene Python**. No sirve por sí sola para esta aceptación. El SDK completo tampoco se reutiliza como imagen de aplicación.

Root debe preparar por separado un runtime finito con clausura autenticada, revisión de seguridad y Python **3.12.16** exacto, ejecutable `/usr/bin/python3.12` SHA256 `54bb87946dd0d8231507c0cef7a5745443a97411c406bf6f010f3ec75780a75b`. El controlador compara versión y bytes del intérprete real. No importa las extensiones Linux en Windows ni supone que el Python del host Ubuntu tenga esa identidad.

Una futura aprobación específica de Root debe estar disponible como `/fixture/runtime-approval.json`, protegida por el SHA respaldado en la prueba externa de salida. Debe declarar `kind=flasgger-isolated-runtime-root-release-v1`, autorización del fixture aislado, clausura verificada, manifiesto completo de la imagen y SHA del recibo de seguridad. Su objeto `python` debe ser exactamente `{"version":"3.12.16","path":"/usr/bin/python3.12","sha256":"54bb87946dd0d8231507c0cef7a5745443a97411c406bf6f010f3ec75780a75b"}`. Los campos `authorized_isolated_fixture` y `runtime_closure_verified` sólo los acreditará Root tras esas comprobaciones. Actualmente no existe esa aprobación: no se genera aquí.

## Captura Linux de Root

Root prepara un namespace host nuevo propio para resultados, UID/GID 1000 y modo 0700, y respalda su identidad antes de iniciar el contenedor finito. Monta los inputs en `/fixture` sólo lectura y la salida propia en `/output`; sin otros bind mounts, sockets Docker ni secretos. La prueba externa `/fixture/linux-proof.json` tiene exactamente:

```json
{
  "owner": "agencia-digital-marketing-ai:flasgger-ui-acceptance:v1",
  "path": "/output",
  "directory": {"device": 0, "inode": 0},
  "marker_sha256": "SHA256_REAL_DEL_MARCADOR",
  "runtime_approval_sha256": "SHA256_REAL_APROBACION_ROOT"
}
```

Los ceros y marcadores son sólo formato, nunca pruebas válidas. El `.owner.json` de salida contiene únicamente `owner`, `path` y la misma `directory`. Root respalda los datos reales y el SHA de la prueba. El directorio debe contener sólo ese marcador antes de la captura; no hay adopción ni replay.

El contenedor debe conservar red `none`, raíz sólo lectura, usuario 1000:1000, una CPU, 512 MiB, límite de 32 procesos y un límite externo de 60 segundos. Root acredita su imagen/configuración/CID propios antes del arranque y su parada por identidad. Esas operaciones de infraestructura no se ejecutan desde este controlador. Dentro del runtime se invoca:

```text
/usr/bin/python3.12 -I -S -B -W error::ResourceWarning /fixture/code/accept_flasgger_ui.py --linux-capture --bundle-sha256 SHA_BUNDLE_RESPALDADO --proof-sha256 SHA_PROOF_RESPALDADO
```

Se crean dos procesos distintos para baseline y derivada, con entornos nuevos. Cada uno extrae sólo los miembros autenticados a su site privado nuevo y añade ese site explícitamente a `sys.path`; `-I -S -B` impide site externo, `.pth` y bytecode. Se rechazan colisiones entre ruedas. Un audit hook prohíbe sockets y procesos desde el fixture; no sustituye el aislamiento externo. El padre captura como máximo 1 MiB combinado, mantiene al líder mediante `waitid(WNOWAIT)` y termina únicamente su grupo propio antes de recogerlo. Reserva tiempo de limpieza dentro de la ventana; Root mantiene el límite externo del contenedor ante una espera de kernel no interrumpible.

La aplicación Flask es sintética. Documenta GET, POST, path/query/body, definiciones y referencias, sin bases de datos ni llamadas a servicios. `Flask.test_client` captura los JSON Swagger/OpenAPI, `/apidocs/`, OAuth redirect y hashes/content-type de los assets reales servidos. Exige JSON canónico idéntico entre baseline y derivada, sin ignorar campos. Prueba ambos valores de `hide_top_bar`, callback previo y configuración JWT exclusivamente sintética. La derivada debe carecer de miembros UI2 y devolver 404 para jQuery; sus scripts servidos deben corresponder al wheel exacto.

Se conservan `baseline.json`, `derived.json` y `capture.json`, o el fallo, por exclusión. Las capturas llevan HTML/JSON y hashes de assets; no vuelven a transferir los bundles JS. Root recoge esos datos mediante su transporte autenticado y respalda su SHA antes de la etapa Windows.

## Navegador finito de Root

El perfil browser es JSON público de operación, sin credenciales. No lo genera el agente. El esquema exacto consumido por `--browser-profile` es:

- `schema_version: 1`, `owner`, `root_proof_sha256`, `plan_sha256`.
- `capture_path` absoluto y `capture_sha256` del resultado Linux verificado.
- `controller_sha256` y `browser_source_sha256` de las copias preparadas.
- `node_path` absoluto, `node_sha256`: Node existente **24.13.0**.
- `edge_path`: `C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe`, `edge_sha256` y `edge_product_version` real respaldada.

Root revisa y respalda perfil/argv y versiones antes de:

```text
python -I -S -B RUTA_PRIVADA/code/accept_flasgger_ui.py --browser-profile PERFIL_ABSOLUTO --profile-sha256 SHA_PERFIL_RESPALDADO
```

El controlador exige la captura positiva ligada al mismo bundle. Reconstruye el replay con los HTML/JSON capturados y los assets de las ruedas locales cuyo hash ya verificó Flask. Crea `browser-v1` exclusivamente y ejecuta Node/Edge bajo el Job congelado: 90 segundos, ocho procesos, 2 GiB y 1 MiB combinado de salida. El entorno se construye desde cero, salvo variables de sistema Windows permitidas; HOME/TEMP/TMP apuntan al intento propio. No transmite tokens reales ni lee `.env`.

[El cliente CDP](accept_flasgger_browser.mjs) no necesita Playwright. Al importarlo o ejecutarlo sin `--run` no crea recursos. Durante la operación levanta un replay HTTP finito en `127.0.0.1` y usa pipes CDP de un Edge con perfil nuevo; no reutiliza sesiones de otros proyectos ni desactiva sandbox o seguridad web. Coteja SHA del ejecutable y versión informada por CDP. El Job cierra descendientes aunque Node falle; el cliente también solicita cierre del browser y cierra servidor/conexiones en `finally`.

Los cuatro casos deben renderizar SwaggerUI y conservar la spec. El callback previo debe invocarse realmente. Las pruebas puras ejecutan el fragmento auténtico SHA256 `33fab8e4252025d7f3f9a9a1e6dea1ff46a501ae2d8d3f8aeddc3cd45b155a68` con retorno, receptor `this`, argumentos y excepción, acreditando `finally` sin cambiar los bytes de la rueda.

El título incluye comillas, `</script>` y una sonda SVG que sólo cambia una bandera local. La derivada debe mostrarlo como texto y no crear el nodo ni ejecutar la sonda. La baseline es control histórico vulnerable y su resultado se registra separado; no se convierte en candidata aprobada. El navegador ejecuta `Try it out` contra `/fixture/echo` del replay y comprueba la cabecera JWT sintética mediante una bandera, sin registrar el valor. El OAuth redirect original se sirve por sus bytes y se ejecuta con un `opener`/cierre sintéticos: estados correcto e incorrecto, callback y token de fixture se comprueban sólo como banderas. No se realiza autenticación con proveedor, cuenta o token operacional.

CDP bloquea y registra solicitudes del renderer fuera del origen/mapa de rutas cerrado; errores JavaScript y `console.error` invalidan la derivada. Esto observa las peticiones del renderer, sin afirmar aislamiento de toda actividad de red de procesos auxiliares del navegador. El perfil nuevo, flags de fondo y ausencia de secretos reducen el alcance; no sustituyen una política de red del sistema.

Un recibo positivo describe exclusivamente esta aceptación aislada. Conserva `application_accepted=false` y `sca_approved=false`; no cierra Zou, la aplicación completa, seguridad de todos sus bundles ni producción. No se borra ni sobrescribe un intento fallido.

## Pruebas de contrato

```powershell
python -I -S -B -W error::ResourceWarning tests/test_flasgger_ui_acceptance.py -v
node --test tests/test_flasgger_ui_browser.mjs
python -m ruff check docker/kitsu-zou/accept_flasgger_ui.py tests/test_flasgger_ui_acceptance.py
python -m ruff format --check docker/kitsu-zou/accept_flasgger_ui.py tests/test_flasgger_ui_acceptance.py
node --check docker/kitsu-zou/accept_flasgger_browser.mjs
```

Estas pruebas sólo usan datos sintéticos/directorios temporales y VM JavaScript en memoria. No importan Flask, no ejecutan fixture Linux, Edge, servidores, Docker, SSH ni SDK. El plan real puede comprobar los bytes y la clausura sin conceder ninguna de esas aceptaciones.
