# Candidato glibc para Kitsu 1.0.77 y Zou 1.1.1

Investigación al 6 de noviembre de 2026. No se construyeron imágenes, ruedas ni contenedores y no se ejecutó código descargado. El manifiesto `runtime-candidates.json` identifica URLs, hashes y comprobaciones realizadas; **no es un lock resuelto ni autoriza despliegue**.

La vía propuesta es utilizar Wolfi glibc para el intérprete, FFmpeg y sus bibliotecas, instalar Zou desde ruedas verificadas en un entorno Python privado y servir el artefacto estático de Kitsu mediante el proxy HTTP de la aplicación. API, eventos y workers utilizan la misma imagen Zou con comandos distintos. PostgreSQL y Valkey siguen siendo servicios separados; no se incluyen bases de datos en esta imagen.

## Base y paquetes

[Wolfi usa glibc y APK](https://www.chainguard.dev/supply-chain-security-101/wolfi-overview). Su [repositorio oficial](https://github.com/wolfi-dev/os) publica `https://packages.wolfi.dev/os` y la clave `wolfi-signing.rsa.pub`. No se mezclarán APK Alpine con Wolfi, ni se intentará cargar ruedas manylinux sobre musl.

Se verificaron por HTTPS y SHA los metadatos OCI de `cgr.dev/chainguard/wolfi-base`:

| Identidad | Valor fijado |
| --- | --- |
| Índice | `sha256:8acb3a7694eda954b3ad4256aec3291bdd92715d7a8c030abd1a21546337b4e4` |
| Linux amd64 | `sha256:ce3d463053d289b9cb4fd8a1b5202acaa5b20093acefb232df7304cf2be0bb72` |
| Configuración OCI | `sha256:b70d618358405f051541c210351e890477c8bf782624d2275cbb3198aff2cfb7` |
| APKINDEX observado | `9b27ab69add838665af44c9aa304a0dd910af90f2cd9e191e52e0b585a3ce2fd` |

Raíz escaneó realmente ese pin amd64: `.tools/evidence/phase4-wolfi-base-trivy.json` registra 19 paquetes y cero HIGH/CRITICAL. Esa prueba cubre **la base**, no Python, FFmpeg, la venv ni Kitsu/Zou añadidos. El manifiesto conserva el hash del informe y mantiene `final_sca_verified=false`.

Candidatos principales del índice x86_64:

| Paquete | Versión | Uso |
| --- | --- | --- |
| glibc-2.44 y su loader/locales | 2.44-r13 | Ya presentes en la base; conservar la familia coherente. |
| python-3.12 y python-3.12-base | 3.12.16_git20261030-r0 | Intérprete compatible con Zou `>=3.11,<3.15`, construido por Wolfi con parches de mantenimiento. |
| ffmpeg-8.1 | 8.1.2-r2 | Comandos ffmpeg/ffprobe y bibliotecas de esa misma familia. |
| libstdc++, libgcc, libgomp | 16.2.0-r2 | Bibliotecas C++/OpenMP que se contrastarán con ELF de las ruedas. |
| glib | 2.90.1-r2 | Candidato si las dependencias ELF de OpenCV lo exigen. |
| ca-certificates-bundle | 20261020-r0 | TLS del backend. |
| xmlsec y xmlsec-openssl | 1.3.13-r0 | Opcionales para SAML; no se habilita SAML por incluir pysaml2. |

El JSON también conserva alternativas examinadas; no se instalarán todas. Se descartan para la selección principal el paquete `glibc` 2.43, los FFmpeg 7/8.0 y el alias genérico `ffmpeg` antiguo. No se necesita el cliente PostgreSQL de Wolfi, que en el índice observado es 16.14: Zou usa psycopg-binary y el servidor externo permanece fijado por su propia unidad.

Se descargaron y calcularon SHA256 de 22 APK candidatos sin instalarlos. Aún falta resolver y fijar toda la clausura, incluidos los proveedores concretos de los SONAME de Python y de FFmpeg, y verificar las firmas APK/índice contra la clave respaldada. El SHA de la clave descargada consta en el manifiesto; no se presenta su descarga como cotejo por un canal independiente. Las recetas Wolfi pueden usar Git en su construcción upstream: este plan consume APK oficiales y no ejecuta esas recetas ni Melange.

## Ruedas Python y único sdist directo observado

El wheel Zou previamente respaldado tiene SHA256 `4f753a130dde563da7ef48d5ec4b403630740738a9451226e3d0687041a72455`. Se comprobaron las 55 dependencias directas activas para Linux/Python 3.12 y los extras solicitados por esas dependencias, como psycopg[binary] y flask-fs2[s3,swift]. No se seleccionaron los extras de desarrollo, test, lint, monitoring ni prodpg de Zou.

Hay ruedas compatibles para 54 dependencias directas. Entre las nativas se observaron:

- NumPy 2.4.6 y Pillow 13.0.0: cp312, manylinux 2.27/2.28.
- OpenCV headless 4.13.0.92: cp37-abi3, con variantes manylinux 2.17 y 2.28.
- OpenTimelineIO 0.18.1: cp312, manylinux 2.17; su paquete de plugins es Python puro.
- gevent 26.4.0: cp312, manylinux 2.28; orjson y psutil también ofrecen ruedas compatibles.

Wolfi glibc 2.44 satisface esos mínimos declarados. Esto acredita disponibilidad por tags, no carga ELF ni compatibilidad funcional. Deben comprobarse `DT_NEEDED`, versiones GLIBC/GLIBCXX, bibliotecas vendorizadas y las importaciones en el runtime Linux real. El JSON conserva todas las variantes compatibles y sus hashes; el lock futuro debe seleccionar una sola por distribución/target.

También se consultaron 48 candidatos transitivos de primer salto, incluyendo adaptadores OTIO, dependencias S3/Swift, future y psycopg-binary. Todos tienen rueda compatible en la versión examinada. Se preservó `xmlschema>=2,<3` de pysaml2, seleccionando como candidato 2.5.1; su versión más reciente 4.3.2 incumple esa restricción. **No se identificó otro sdist en ese alcance**, pero no se ha completado una resolución transitiva con retroceso ni un lock. El fallo previo de pip al llegar a Flasgger tampoco demostraba que el resto del grafo estuviera resuelto. Después de construir su rueda se repetirá la resolución completa y se auditarán todos los paquetes nuevos.

### Construcción propuesta de Flasgger

La distribución oficial [Flasgger 0.9.7.1](https://pypi.org/project/flasgger/0.9.7.1/) no publica wheel. Se descargó su sdist a `.tools/cache/phase4-sources/flasgger-0.9.7.1.tar.gz`: 3.979.409 bytes, SHA256 `ca098e10bfbb12f047acc6299cc70a33851943a746e550d86e65e60d4df245fb`.

Se leyeron setup.py, setup.cfg, MANIFEST.in y egg-info sin ejecutarlos. No hay pyproject.toml. Setup usa setuptools, obtiene la versión del texto de `flasgger/__init__.py`, lee README e incluye datos del paquete. No declara extensiones C, invoca compiladores ni contiene una llamada Git observada. setup.cfg solicita rueda universal; no se propone reempaquetar un ZIP manualmente.

La rueda se construirá en una venv de build separada, con backend PEP 517 `setuptools.build_meta:__legacy__` y estas herramientas candidatas, cuyas ruedas y SHA ya se verificaron:

| Herramienta | Pin candidato |
| --- | --- |
| build | 1.6.1 |
| setuptools | 84.2.1 |
| wheel | 0.48.0 |
| packaging | 26.3 |
| pyproject-hooks | 1.3.3 |

Primero se resuelve y audita ese grafo de build; después se instala offline con hashes en su entorno propio. Sobre una extracción nueva, validada y sin enlaces, se propone `python -m build --wheel --no-isolation --outdir <salida-propia> <fuente-verificada>`, con red deshabilitada y fechas de build fijadas. El backend, no un empaquetador propio, genera METADATA, WHEEL y RECORD. El resultado debe ser Python puro, plataforma `any`, con versión y dependencias exactas y sin ELF; el tag universal esperado del setup se contrasta con la salida real antes de aprobarlo.

Se harán dos builds con las mismas entradas y se compararán bytes. El SHA de la rueda producida se respaldará antes de instalarla en la venv final. Se comprobarán RECORD, licencia y todos los datos Swagger/templates/static incluidos por MANIFEST.in. Después se probarán importación con Flask 3.1.3, generación OpenAPI, serialización JSON, sanitización Markdown y rutas documentadas de Zou. Flasgger ya contempla `DefaultJSONProvider` y Markup de markupsafe en la fuente inspeccionada, pero esta observación no sustituye la prueba. Los JavaScript embebidos de Swagger requieren inventario/SCA propio.

### pip vendorizado: aplicabilidad precisa

Python-base depende del APK py3-pip-wheel 26.2.2-r2. Su payload real contiene pip wheel SHA256 `38f4aacff7c3b0d7b216cac0a106a1036eef98b0a7ff5b932ea2fcb787e97bc5`. Wolfi ha actualizado sus componentes vendorizados msgpack a 1.2.3 y urllib3 a 2.8.0; vendor.txt conserva la identidad setuptools 70.3.0.

El [advisory primario CVE-2025-47273](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf) afecta `setuptools.package_index.PackageIndex._download_url`. Se inspeccionó el ZIP completo y el AST de sus 406 módulos Python: sólo incluye el subconjunto pkg_resources, sin paquete setuptools completo ni package_index.py. La clase homónima de `pip/_internal/models/index.py` sólo contiene constructor y formación de URL; no tiene download ni _download_url. Por ello, el manifiesto registra **no afectado por ausencia del código vulnerable exclusivamente para ese wheel y CVE**, con hashes de los miembros examinados. No exime otros setuptools ni otras vulnerabilidades.

Se conservan metadatos y reportes brutos; no se añadió una regla ignore ni se declaró limpio el runtime ampliado. Omitir herramientas de instalación en la imagen final puede reducir superficie, pero no es el único arreglo demostrado ni un requisito para este CVE. La política definitiva de tooling se elegirá tras el escaneo completo y la prueba de creación de la venv.

## FFmpeg, configuración y login

La [guía vigente de self-hosting](https://dev.kitsu.cloud/self-hosting/setup) separa API Gunicorn, eventos WebSocket, frontend estático y servicios externos. En contenedores se preservará ese contrato sin instalar Systemd. La API se ejecutará como usuario propio sin root, con Gunicorn/gevent sobre `zou.app:app` o la fábrica original; eventos usa `zou.event_stream:app` con GeventWebSocketWorker. Cada comando tendrá su cierre/control de señales y health real; workers de colas se ensayarán aparte.

Kitsu 1.0.77 ya es un bundle estático: SHA256 `f030dc59ae28155d3686c09ce35a2855381582ab42fcc983158c727ec5f1204e`. Sus `.commit.txt`, `.tag.txt` y `.version.txt` coinciden con el commit respaldado `c1edc91a3e2bfcefabf6f9abf682e2c420f0831f`. index.html solicita `/api/config`; el proxy debe retirar el prefijo `/api` hacia Gunicorn y preservar `/socket.io` hacia eventos, con fallback SPA sólo para rutas de la UI. El proxy y sus límites se fijarán en otra unidad, no mediante copiar sin revisar los valores amplios de la documentación.

Zou invoca `ffmpeg` y `ffprobe` por PATH. El wheel usa libx264, AAC, PNG, filtros de escala y padding y el demuxer concat. Las CLI y sus siete bibliotecas se cargan desde `/opt/zou-media-v1`. OpenCV carga sus proveedores desde `/usr/lib`. La imagen aislada `9.0.2-zou.1` pasó las pruebas de siete formatos y una carga autenticada que produjo vídeo H.264/AAC, versión ligera y PNG.

La [receta de mantenimiento](build-ffmpeg-media.sh) usa el TAR firmado y los protocolos `file` y `pipe`. La revisión `9.0.2-zou.2` añade correcciones del parser HEVC, el decodificador FLIC, el decodificador EXR y audio MPEG a las del lector MP4 y del filtro AAC. También deshabilita componentes de FFmpeg ajenos a los formatos probados. El soporte EXR de OpenCV usa su biblioteca independiente. La [sonda de formatos](probe_ffmpeg_media.py) verifica la versión elegida, las conversiones, los casos sin audio, las miniaturas y la concatenación.

La revisión `9.0.2-zou.2` pasó esas pruebas y una carga autenticada aislada. Zou API y eventos usan la imagen `c906cfe1…` en la VM. La comprobación en producción verificó las dos escrituras NFS, la identidad del bot en la API LAN y el WebSocket de eventos. Los [recibos de activación](../../ESTADO.md) registran el alcance y la comprobación de las dependencias.

La [configuración oficial](https://dev.kitsu.cloud/self-hosting/environment-variables) y el wheel requieren DB_HOST/PORT/USERNAME/PASSWORD/DATABASE, KV_HOST/PORT/PASSWORD, SECRET_KEY, rutas absolutas PREVIEW_FOLDER/TMP_DIR y DOMAIN_NAME/PROTOCOL coherentes. Los secretos y el administrador inicial los respalda raíz antes de inyectarlos. Un lanzador futuro leerá los Docker Secrets y sólo preparará el entorno mínimo necesario antes de importar Zou; esta unidad no genera ni transmite credenciales. No se pasará una contraseña como argumento del CLI.

El login inicial conserva `auth_local_classic`, bcrypt y los JWT originales de Zou. Se probarán clave ausente/default, contraseña incorrecta, usuario desactivado, logout/revocación, refresh, cookies y 2FA. El código fija cookie SameSite=Lax y desactiva por defecto CSRF de JWT; no se presumirá una configuración endurecida por poner una variable que Zou no lee. La fábrica `create_app(config_object=...)` permite un objeto de configuración explícito: el wrapper y las pruebas posteriores deben fijar los atributos efectivos necesarios, comprobar Secure/CSRF y respetar el transporte que usa Kitsu. Origen único permite mantener CORS vacío; no se habilita comodín. SAML/OIDC/LDAP y la integración con Google se ensayan y configuran por separado; una cookie del portal no equivale a una sesión Zou.

PREVIEW_FOLDER se situará bajo una ruta propia y acreditada de `/mnt/producciones`; TMP_DIR y logs quedan en directorios propios del contenedor/host. No se aplica chmod/chown recursivo sobre el share. La prueba final incluirá un preview real en NFS, permisos, reinicio, persistencia y las colas RQ sobre Valkey. No se asume Redis/Valkey compatible sólo por el nombre del cliente. Las migraciones y la creación de usuarios son operaciones posteriores explícitas, con snapshot/backup y sin telemetría incidental.

## Seguridad, fuente y licencia

Los artefactos Kitsu/Zou y sus documentos de licencia están fijados en `docker/stack-artifacts.json`. Ambos declaran **AGPL-3.0-only**; se preservarán avisos y se ofrecerá el código fuente correspondiente, incluidas modificaciones y scripts necesarios, según las obligaciones aplicables de distribución e interacción por red. La AGPL permite uso comercial cumpliendo sus condiciones; no se sustituye por la SUL de n8n. FFmpeg y cada biblioteca tienen además sus propias licencias.

El bundle Kitsu no contiene un lock de instalación. La auditoría debe examinar también el grafo frontend del commit fijado y el código incorporado al bundle; servir sólo estáticos no elimina sus dependencias. Su [package.json primario](https://github.com/cgwire/kitsu/blob/c1edc91a3e2bfcefabf6f9abf682e2c420f0831f/package.json) contiene una dependencia GitHub abreviada de fabricjs-psbrush y un prepare de Husky. Si hace falta reconstruir la UI, no se ejecutará directamente una instalación que invoque Git: habrá que fijar su fuente por HTTPS/commit/SHA y adaptar el lock, desactivar lifecycle y revisar cada herramienta antes del build. Consumir el bundle ya verificado no ejecuta npm ni Git en esta unidad.

Quedan bloqueantes: clausura y firmas APK; rueda Flasgger reproducible; resolución Python completa con hashes; SCA de build/runtime y componentes embebidos; ELF/imports Linux; autenticación y WebSocket; migraciones/DB/Valkey; FFmpeg y almacenamiento NFS; imagen final, pruebas y recibos. No hay Dockerfile final ni despliegue aprobado. No se editaron .env, Compose, capacidades, ESTADO ni fuentes anteriores; fase declarada 3 y CD deshabilitado permanecen sin cambios.
