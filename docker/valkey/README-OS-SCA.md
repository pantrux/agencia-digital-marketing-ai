# Auditoría de los 23 paquetes del sistema base de Valkey

Esta unidad prepara una auditoría real de vulnerabilidades de los 23 APK del
sistema Alpine 3.24.3. El comando predeterminado sólo lee entradas y calcula un
plan. Esta entrega no ejecuta Trivy, Valkey, Docker, SSH, SDK ni aplicaciones;
no consulta `.env`. El escaneo queda para Root tras revisar y respaldar los
hashes de fuente y plan, la ruta y el propietario del intento.

El alcance es exclusivamente el sistema base. No acredita los tres binarios
propios de Valkey 9.1.3, Lua, jemalloc ni otros materiales embebidos. Tampoco
demuestra funcionamiento, identidad efectiva, TLS o instrucciones de CPU.
`binary_accepted`, `runtime_accepted`, `binary_sca_verified`,
`embedded_libraries_complete` y `whole_image_sca_approved` permanecen false,
incluso si el reporte OS no encuentra vulnerabilidades.

## Entradas y procedencia

Se exige la publicación positiva del [inventario V2](README-INVENTORY-v2.md),
en `.tools/evidence/phase4-valkey-inventory-v2`. El lector coteja el dispositivo
e inodo fijados por Root, propietario, marcador, plan y recibo positivo. Los
ocho archivos se releen por SHA y archivo regular, sin enlaces ni múltiples
vínculos físicos; no se adopta una carpeta por su nombre.

El plan publicado está fijado en
`71e67caf06bbf1c84b63ad724157cb9ab8cbcfbbf6de1c089531d4737bb48ae3`.
Se conservan sus fuentes CLI/núcleo/parser/lector y se verifican los hashes y
tamaños de sus cinco salidas. Las entradas del escaneo son los bytes originales:

- `inventory.json`:
  `7ebf54457de5fa7c6413d6d5f7c64fed94517f81894cd3e5da5596efc911ccfd`.
- `os-packages.cdx.json`:
  `abe00bc00ffb948e71874952db76058a4db13b458e2f21e905cbe45c4bc8dd3d`.

El inventario debe declarar Alpine 3.24.3 y exactamente 23 paquetes distintos.
El SBOM debe contener ese sistema operativo y las mismas 23 bibliotecas, con
biyección por nombre/versión. Cada PURL debe conservar arquitectura `x86_64`
y distribución `3.24.3`; `SrcName`, `SrcVersion` y `Arch` deben coincidir con
los datos de origen. No se regeneran identidades ni se completa cobertura por
suposición. El histórico intento fallido V1 se conserva íntegro.

## Herramienta, base y límites

Se cargan únicamente bytes verificados del lector/hash seguro
`recover_opencv_sdk_scan_v2.py`, SHA
`b15f8ed20e19b0b1b19dfdd5ffd81ea60667175a014ab63409b29012fa6f253c`,
y del supervisor `windows_build_job.py`, SHA
`dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44`.
El modo plan verifica el archivo del supervisor sin cargarlo. El supervisor
sólo se carga para `--scan`; sus bytes y semántica permanecen intactos.

Trivy 0.76.2 está fijado por SHA256 en
`dd07adf881a72b54d1a922191d62fa939938df2155be85a727da93b67b24e617`.
Se usa la caché existente `.tools/trivy-cache-current-20261106`, propietario
`agencia-digital-marketing-ai:trivy-current-db:v2`, con estas huellas:

- DB: `7520aa80bfb8db4a230514baa510ec6b1d7de9099b74f7879be65450407a6f1f`.
- Metadatos: `475ea2061a7f343953b880fd0eb2ce56253cca495d48f12661f3bd3e5e95c125`.

Los metadatos deben acreditar una DB ya descargada y vigente al preparar y
ejecutar el escaneo. Tool, DB, metadatos, propietario y árbol completo de caché
deben conservar identidad y hashes antes y después. No se actualiza la base,
no se cambian permisos y no debe ejecutarse otro scanner sobre esa caché durante
este intento. Una DB vencida exige una unidad nueva de preparación y pins;
no se ignora la caducidad.

El escaneo tiene límites de 180 segundos, ocho procesos, 2 GiB y **1 MiB
combinado de stdout/stderr**, usando el Job fijado. La salida JSON procede de
stdout; no se permite un archivo de reporte externo sin ese límite. Un exceso,
plazo agotado o limpieza incompleta conserva los bytes parciales y falla,
sin interpretar un JSON truncado como reporte válido.

El entorno del proceso se construye desde una lista mínima de variables de
sistema, con PATH al directorio del sistema. No se heredan configuraciones,
tokens, proxies ni exclusiones del operador. Los argumentos fijan configuración
y archivo de exclusiones vacíos, caché en memoria, escaneo offline, sin
actualización de DB/Java ni telemetría, y sólo vulnerabilidades OS. Se solicitan
todos los paquetes y todas las severidades; no se ignoran hallazgos sin arreglo.

## Validación y resultado

Un exit 0 del proceso no basta. El reporte debe declarar exactamente Alpine
3.24.3, un resultado `os-pkgs`/`alpine` y los mismos 23 paquetes, sin faltantes,
extras o duplicados. Nombre/versión/PURL y los tres campos de fuente se cotejan
uno a uno. Cada hallazgo debe remitir al paquete y PURL observados y al origen
de avisos Alpine. Las listas de hallazgos malformadas o suprimidas fallan.

Sólo tras acreditar cobertura, origen y PURL se admiten las tres advertencias
literales conocidas fijadas en `KNOWN_WARNINGS`. Se conservan sus mensajes
originales y su clasificación española. Un aviso nuevo o una línea de error
invalida el intento; no se silencian diagnósticos.

El reporte bruto conserva UNKNOWN, LOW, MEDIUM, HIGH y CRITICAL. HIGH, CRITICAL
o UNKNOWN bloquean la política OS y producen exit 2 con recibo completo. LOW y
MEDIUM permanecen registrados aunque no bloqueen ese umbral. Un fallo de
herramienta, esquema, cobertura o integridad produce exit 1 y `failure.json`,
sin recibo positivo. El recibo separa `os_coverage_complete` de
`os_policy_passed`; nunca declara aprobación del binario, de la imagen completa
o del servicio.

## Plan y ejecución exclusiva de Root

```powershell
python -I -S -B docker/valkey/scan_os_packages.py
```

Root debe respaldar el plan exacto y la fuente antes de esta acción explícita:

```text
python -I -S -B docker/valkey/scan_os_packages.py --scan --expected-source-sha256 SHA_FUENTE --expected-plan-sha256 SHA_PLAN
```

La raíz nueva del intento es `.tools/evidence/phase4-valkey-os-sca-v1`;
el propietario es `agencia-digital-marketing-ai:valkey-os-sca:v1`. Debe estar
ausente y se crea de forma exclusiva. Contiene marcador, plan, copia exacta del
SBOM, reporte, diagnósticos y recibo o fallo. No se sobrescriben archivos ni se
adoptan parciales o recursos anteriores. El inventario publicado y sus recibos
se vuelven a verificar antes y después, sin modificarlos.

## Pruebas sin escáner

```powershell
python -I -S -B -W error::ResourceWarning tests/test_valkey_os_sca.py -v
python -m ruff check docker/valkey/scan_os_packages.py tests/test_valkey_os_sca.py
python -m ruff format --check docker/valkey/scan_os_packages.py tests/test_valkey_os_sca.py
```

Los datos sintéticos prueban la cadena positiva y su manipulación con el lector
real; biyección, fuentes, PURL, OS, duplicados, severidades, JSON, caducidad,
advertencias, saturación de salida, plazo, limpieza, cambios de DB y no adopción.
El transporte simulado conserva reportes completos o parciales y nunca ejecuta
la herramienta. Una prueba llama a la API real del Job congelado con un doble
que se detiene antes de `CreateProcess`; acredita los límites exactos y el
cierre de los seis handles sintéticos. Otra rechaza 8 MiB antes de cualquier
llamada de API. No se modifica el Job ni se crean procesos Win32 en esas pruebas.
