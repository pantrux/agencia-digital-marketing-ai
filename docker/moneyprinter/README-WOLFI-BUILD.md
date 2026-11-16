# MoneyPrinterTurbo sobre Wolfi

Receta del ensayo Linux amd64 de MoneyPrinterTurbo 1.3.9, commit
`6992380c5e82f07f3e1fc46b90927b6f1c655407`. Reutiliza el runtime Wolfi
con CPython `3.12.16`, el lock V2 de 99 ruedas y Speaches V2. La primera
imagen construida es `e1f090ca…`. El recibo `290f0fd0…` acredita la
instalación en Linux. El ensayo privado de vídeo tiene recibo `50feabe7…`.

## Contexto cerrado

Root prepara y respalda el TAR propio con los siguientes miembros
regulares, sin enlaces ni entradas de directorio explícitas.

| Ruta | Material fijado |
| --- | --- |
| `Dockerfile` | Copia de [Dockerfile.wolfi](Dockerfile.wolfi). |
| `launch_wolfi.py` | Lanzador y verificador de esta unidad. |
| `requirements.lock` | Copia de [requirements-wolfi-cp312-v2.lock](requirements-wolfi-cp312-v2.lock), SHA256 `cfdb90ecf17f52c1f23b441c703bf81d219b6f0f43a9c972b3cf57e0882b902c`. |
| `wheels/` | 99 miembros del [manifiesto V2](wolfi-cp312-lock-inputs-v2.json) y de `.tools/moneyprinter-remediation/wolfi-cp312-litellm-fix-v2/wheelhouse-receipt.json`, SHA256 `53149f55795886fe5edc197919c0d6f12848be000773c1d30be8d102c86104d3`. |
| `repositories` | Única línea `/inputs/apk-repo` con LF final. |
| `keys/wolfi-signing.rsa.pub` | Clave del repo nativo, SHA256 `f0031424cf46f7db780ce63a45f0fd6aa6f85f601e6bb3b7a91fe3d4d5b7d2cc`. |
| `apk-repo/x86_64/APKINDEX.tar.gz` | Índice original firmado, SHA256 `9b27ab69add838665af44c9aa304a0dd910af90f2cd9e191e52e0b585a3ce2fd`. |
| `apk-repo/x86_64/` | Los 58 APK del repo y los siete proveedores reconciliados descritos abajo. |
| `app-tree/` | 83 archivos de fuente proyectada, sin el TAR original. |
| `build-inputs.json` | Manifiesto exacto de entradas instalado sólo en el contexto de build. |

La fuente completa local tiene SHA256
`0d6da61ce12040c8f54d5eea6d83500c91da3a6685f916d5fc4d7c5148e340ec`.
Validar el TAR como datos y proyectar sus 73 archivos de `app/` y
`webui/`, más `LICENSE`, `cli.py`, `main.py`, `pyproject.toml`,
`config.example.toml` y `resource/public/index.html`. Confirmar las
preimágenes contra [el manifiesto de fuente](wolfi-cp312-lock-inputs.json)
antes de incorporar las ocho salidas de
`.tools/backup-source-build/moneyprinter-response-budget-v2/overlay/`.
El recibo V2 tiene SHA256
`ce55182784780d1ebe86cada3448997e8a376febfe3e92f59d16ff52bf667003`
y contiene los bytes y SHA de cada salida. La proyección excluye
los recursos tipográficos y de música del TAR upstream. La receta agrega
Liberation Sans del APK acreditado. Los dos `__init__.py` vacíos se
conservan. [Fuente upstream](https://github.com/harry0703/MoneyPrinterTurbo/tree/6992380c5e82f07f3e1fc46b90927b6f1c655407)

El manifiesto usa las claves `schema` igual a 1, `kind` igual a
`moneyprinter-wolfi-build-inputs-v2`, `runtime_manifest` igual a
`sha256:ba69f696b21955483e5e47e46eab2d7a9e556a3cf5fd4bd40817554901781af8`,
`base_packages` y `apk_packages` como objetos nombre→versión y `files`
como objeto ruta relativa→`{bytes,sha256}`. `files` declara 251 filas.
Dockerfile, lanzador y el propio manifiesto se fijan en el plan externo.
El lanzador verifica la copia de la aplicación y los conjuntos de archivos
antes de ejecutar los instaladores.

El lock V2 fija LiteLLM `1.100.4`. El escaneo de los metadatos de las
99 ruedas reportó cero hallazgos. Su recibo local `sca/receipt.json`
tiene SHA256 `a53928e78b0ef343262100c2ec10cfee83c9d9dcadb27965650cdad0a0143f39`.
La auditoría de la imagen y la prueba de las bibliotecas nativas se ejecutan
por separado. El lock V1 se conserva para reproducir el informe anterior.

## Instalar los APK que aportan las bibliotecas físicas

El [recibo del repo](../../.tools/moneyprinter-remediation/wolfi-ffmpeg-native-v1/receipt.json)
tiene SHA256 `5d8278025697ade7642e9dc655f7c56e43c9a452300f956823ae429e1c3a18f7`.
Los 58 APK se cotejan con el índice RSA, Q1 y hashes de carga acreditados.
El plan previo asumió que seis bibliotecas de la proyección física de
Python se podían reutilizar como proveedores del solver. APK necesita
registrar sus paquetes y el requisito exacto de terminfo de ncurses.

- Del plan nativo SHA256 `7ae0fe0fbd144997787783e0c39ad9275b46df0cf5ca40383f658f7e309a8c14`,
  copiar las seis entradas `physical_only_reused[].archive` al contexto.
  Son libbz2-1, libexpat1, libffi, libstdc++, ncurses y xz.
- De `.tools/backup-source-build/python-runtime-lib64-recovery-v1/inventory.json`,
  SHA256 `eb771205202ecc94b632288c46b14806aa7139938f7dcc3a51ceb2e63b803b00`,
  copiar el archivo autenticado de `ncurses-terminfo-base=6.6.20261031-r0`,
  SHA256 `ca983c16e5a6ecf07d5fb0268fe48369f2153074108fb28378159dc86af1ab39`.
- Las 19 entradas base coinciden con las bibliotecas de
  `.tools/backup-source-build/python-runtime-v1/base-os19.cdx.json`, SHA256
  `3cc41e7434b4f056e3174fe5d7c77c5c19f11b5e0b69d4bf16e63474b73df23a`,
  y con la DB física del OCI. El conjunto final esperado es 19 + 65 = 84.
  Los APK del repo incluyen `openssl-4.0-dev=4.0.3-r8` para los dos
  requisitos `pc:` de libsrt. Conservar ese proveedor, el índice intacto
  y los APK originales. Quedan siete materiales manuales de Python para
  la auditoría de la imagen derivada.

La única etapa RUN ejecuta APK offline con nombres/versiones exactos,
sin scripts, hooks o excepciones de confianza, comprueba la DB final
y ejecuta `ldconfig`. Luego coteja los binarios y fuente física y prueba
el arranque de FFmpeg/FFprobe, incluidos los encoders `libx264` y `aac`.
pip se carga desde la rueda oficial instalada físicamente en la base y
se usa con `-I -S -B`, `--isolated`, sólo ruedas, sin índice ni resolver
dependencias, y con los hashes del lock. Los instaladores leen el
contexto por montaje de sólo lectura y dejan sus archivos de entrada
fuera de la imagen. [Receta anterior de Zou](../kitsu-zou/README-application-context.md)

## Operación de build y primera aceptación

Reutilizar los helpers revisados de la operación privada Zou para
anclar host/daemon, materializar el OCI local y ejecutar Buildx con
latido, presupuesto y recibos. Su operación completa está ligada a
Zou. El parser de contextos histórico tiene cotas de 256 MiB por TAR y
32 MiB por miembro. Este contexto requiere límites separados de hasta
320 MiB y 96 MiB, respectivamente. PyArrow y Azure Speech superan
32 MiB. Cotejar el total, las entradas exactas y los hashes del TAR
por streaming antes del build. La exportación de la imagen final
también requiere su propia cota. Las 99 ruedas comprimidas suman
227.233.387 bytes, antes de incluir el código y los APK.

Usar el `final.tar` acreditado de la base Python, SHA256
`20c0dd18c0632089a5d0209de1efad7489563f7879d5c1b41b6e3631cd421187`,
config `sha256:0539324cc567221fcd9db19acec7798c790e906f7398515d2dddf36d6e46534c`.
El operador prepara su layout OCI y enlaza el alias `pythonruntime`.
Exportar los tres valores NoGit antes del proceso Buildx.

```bash
export BUILDX_GIT_INFO=false BUILDX_GIT_LABELS=false BUILDX_GIT_CHECK_DIRTY=false
docker buildx build --builder default --platform linux/amd64 \
  --network none --pull=false --provenance=false --sbom=false --progress plain \
  --build-context "pythonruntime=oci-layout://${base_oci}@${base_manifest}" \
  --iidfile "$iid" --metadata-file "$metadata" --file Dockerfile --load - < "$context_tar"
```

Con el ID de la imagen respaldado, crear procesos privados `api` y
`ui` mediante el entrypoint de esta imagen. El usuario real de Wolfi
es `nonroot` (`65532:65532`), con hogar `/home/nonroot`. Preparar
un tmpfs privado para su hogar y otro para `/tmp`. Montar por rol un
directorio propio `/run/mpt`, modo `0700` y UID/GID `65532`, con su
`config.toml` modo `0600`. Mantener el Bearer fuera del contexto y
montarlo de sólo lectura en `/run/secrets/moneyprinter-speaches`,
legible por el mismo UID. Pasar `STREAMLIT_GLOBAL_DEVELOPMENT_MODE=false`
al crear el servicio `ui`. Streamlit interpreta el destino de pip
`/opt/moneyprinter/site` como una instalación de desarrollo y rechaza
el puerto explícito si ese ajuste falta. La API y la UI corregida
respondieron 200 en la prueba privada `8070e822…`. Conservar las guardas de red del [ensayo funcional
anterior](README-FUNCTIONAL.md) y adaptar sólo la imagen, UID, comando
y rutas acreditadas. En la prueba de vídeo, elegir
`LiberationSans-Regular.ttf` explícitamente en UI o en la carga del API
y usar recursos locales sintéticos. El valor por defecto upstream puede
referenciar una fuente que esta proyección no incluye. La [sonda de vídeo](functional_wolfi_video_probe.py)
se ejecuta por stdin como UID `65532` y verifica el MP4, los 28 subtítulos
y la descarga de ambos archivos con la clave privada del API.

## Servicio permanente en la LAN

El [Compose LAN](compose.lan.yml) publica la interfaz en `192.168.0.85:8501`.
El API se mantiene en el bridge `agency-moneyprinter-lan-speaches`. Los
TOML y el estado de cada rol están en el disco local y los directorios
`tasks` y `local_videos` se montan desde el área de medios propia del NAS.
La clave del API reside en su TOML privado y el Bearer de Speaches se
monta de sólo lectura en ambos roles.

La [unidad systemd](agency-moneyprinter-lan.service) supervisa Compose
después de Docker, la red, el firewall del host, el montaje NFS y la unidad
de observabilidad cuando también está programada. La [guarda](lan_guard.py)
coteja la imagen, los contenedores y el origen del NAS, y aplica las
reglas del bridge antes de arrancar. La interfaz admite entradas desde
`192.168.0.0/24` por `media0`. La salida TCP de los contenedores se
limita a `192.168.0.6:8000` y se admite el retorno de conexiones
autorizadas. La unidad es la responsable de los reinicios del servicio.
[Orden de reglas de Docker](https://docs.docker.com/engine/network/firewall-iptables/)
· [Políticas de reinicio de Docker](https://docs.docker.com/engine/containers/start-containers-automatically/)

### Arranque junto a observabilidad y SupoClip

En IPv4 e IPv6, observabilidad instala sus dos guardas en `FORWARD`
antes del salto `DOCKER-USER`. Coinciden únicamente con paquetes que
entran o salen por `br-aobsoutv1`. Su regla `INPUT` también coincide
sólo con ese bridge. MoneyPrinter conserva sus prefijos propios al
inicio de `DOCKER-USER` y justo después de la guarda de observabilidad
en `INPUT`, o al inicio de `INPUT` cuando observabilidad está detenida.
Sus reglas tienen predicados para sus bridges o el puerto `8501`.
SupoClip arranca después de ambas unidades y conserva estos prefijos.
Su bridge `sco24754668cc` y el puerto `3080` quedan fuera de los
predicados de los dos servicios anteriores. Una coincidencia parcial
de los prefijos de observabilidad o un salto genérico anterior a
`DOCKER-USER` bloquea el arranque de MoneyPrinter.

Para actualizar la VM sin detener contenedores, transferir sólo
`lan_guard.py` y `agency-moneyprinter-lan.service`. Desde el directorio
de transferencia, ejecutar como root con Bash y sin cambios
concurrentes en Docker ni en el firewall.

```bash
set -euo pipefail
work=/opt/media-platform/moneyprinter-lan-v1
unit=/etc/systemd/system/agency-moneyprinter-lan.service
systemd-analyze verify "$PWD/agency-moneyprinter-lan.service"
install -o root -g root -m 0400 lan_guard.py "$work/lan_guard.py.new"
/usr/bin/python3 -I -S -B "$work/lan_guard.py.new" --verify-only
cp --preserve=mode,ownership "$work/lan_guard.py" "$work/lan_guard.py.before-order"
cp --preserve=mode,ownership "$unit" "$work/agency-moneyprinter-lan.service.before-order"
mv -T "$work/lan_guard.py.new" "$work/lan_guard.py"
install -o root -g root -m 0644 agency-moneyprinter-lan.service "$unit"
systemctl daemon-reload
systemctl show agency-moneyprinter-lan.service -p After -p ActiveState -p UnitFileState
```

`--verify-only` debe devolver `ok=true` y `firewall_changed=false`.
Si falla, conservar la versión instalada y comparar `iptables -S` e
`ip6tables -S` para `INPUT`, `FORWARD` y `DOCKER-USER`. Al volver de un
reinicio completo, verificar el estado activo de las tres unidades y
sus logs con `journalctl -b -u`, luego comprobar las tres guardas.

```bash
systemctl is-active agency-observability-nas.service agency-moneyprinter-lan.service agency-supoclip-private.service
/usr/bin/python3 -I -S -B /opt/media-platform/observability-nas-v1/collector_boot.py inspect
/usr/bin/python3 -I -S -B /opt/media-platform/moneyprinter-lan-v1/lan_guard.py --verify-only
/usr/bin/python3 -I -S -B /opt/media-platform/supoclip-boot-v1/supoclip_boot.py inspect
```

Confirmar los CID y la salud de las tres aplicaciones. La interfaz de
MoneyPrinter debe responder desde la LAN autorizada, SupoClip debe
responder desde la VM y rechazar conexiones LAN nuevas a `3080`, y
Prometheus debe seguir recibiendo muestras por mTLS. Para revertir
los dos archivos, reinstalar las copias `.before-order` con sus modos
originales y ejecutar `systemctl daemon-reload`. La versión anterior
puede rechazar un arranque si las guardas de observabilidad preceden
a `DOCKER-USER`.

Verificar el arranque del API y Streamlit, los imports nativos, la
transcripción y descarga autenticada del SRT de 28 palabras con el WAV
acreditado, y el render/decode de un vídeo sintético con la fuente fija.
La SCA final debe cubrir las 84 entradas de APK, los siete materiales
manuales que permanecen, las 94 ruedas y los componentes embebidos.

### Prueba de vídeo en el despliegue persistente

La sonda de aceptación verifica ambos CID activos y la guarda en modo lectura. Ejecuta el pipeline local desde el contenedor API, con el WAV público y un clip sintético generado con FFmpeg. Usa `LiberationSans-Regular.ttf` de la imagen fijada. El aplicativo prepara la fuente en el estado privado al usarla por primera vez. La sonda exige el archivo preparado y su hash antes de crear los directorios de la tarea en NAS.

```sh
/usr/bin/python3 -I -S -B functional_wolfi_video_probe.py --persistent-exec \
  --cid "$API_CID" --ui-cid "$UI_CID" --task-id "$TASK_ID_NUEVO" --wav whisper-probe.wav
```

El primer intento distinguió dos presentaciones del mismo subárbol en `mountinfo`. El verificador acepta la raíz del bind sobre el origen general o el origen NFS con el subárbol exacto. En ambos casos exige NFSv4.1 por TCP, dispositivo, inodo y propietario registrados. La tarea `wolfi-video-probe-v2` produjo un MP4 H.264/AAC de 1080 × 1080 y 11,2 segundos, y un SRT de 28 palabras. Ambas descargas autenticadas coincidieron con los hashes del NAS y la descarga anónima devolvió HTTP 401. Se conservaron las dos épocas de contenedor y los archivos sintéticos de la tarea. Recibos `99200f43…` y `79590c3f…`.
