# SCA condicional del JavaScript embebido en Kitsu y Flasgger

Este controlador analiza hipótesis de versión ya inventariadas. No identifica
automáticamente cada biblioteca del bundle ni aprueba una imagen o el runtime.
El modo predeterminado sólo lee datos y emite un plan; únicamente Root puede
activar el scanner después de revisar y respaldar sus hashes.

## Alcance acreditado por los insumos

El inventario privado
`.tools/evidence/phase4-kitsu-swagger-embedded-js-inventory-v1.json`, SHA256
`6a09feefa77e8e252307a3fe3e75f65ca4ebe96c9b8a58ed754a15eb5371fda6`,
conserva los originales. El controlador vuelve a verificar los hashes de los
tres archivos comprimidos, el package.json y el lock del commit Kitsu fijado;
no descarga paquetes, ejecuta JavaScript o reproduce el build.

Se preparan dos CycloneDX independientes:

| Análisis | Identidades consultadas | Evidencia conservada |
| --- | --- | --- |
| `kitsu_candidates` | 136 pares npm nombre/versión | 137 referencias del lock para 128 nombres; cinco nombres ambiguos |
| `flasgger_self_declared` | jQuery 1.8.0, jQuery 2.2.4, Backbone 1.1.2 | Hash, miembro, texto y posición de cada autodeclaración |

Cada candidato Kitsu debe coincidir con la entrada real del lock: nombre/ruta,
versión, URL, integridad y licencia declarada. Los tres miembros de Flasgger se
leen de la rueda exacta y deben coincidir por hash y posición del selector. Esto
acredita la autodeclaración; no igualdad completa con una distribución npm.
Las licencias del lock permanecen como evidencia de candidatos, sin convertirlas
en licencias confirmadas de todos los bytes del bundle.

Existe una excepción de **datos**, cerrada por nombre, versión, ruta y URI:
`fabricjs-psbrush@0.0.22`, `node_modules/fabricjs-psbrush`, referencia
`git+ssh://git@github.com/cgwire/fabricjs-psbrush.git#17a0cf4e853ae21809176236f5db1428667887b7`,
con integridad ausente. Se etiqueta `lock-vcs-candidate`; su consulta npm sólo es
una hipótesis y no acredita la identidad de ese fork. El controlador nunca abre
esa URI, invoca Git o resuelve VCS. Cualquier otra referencia VCS se rechaza.

La consulta se limita a las 139 hipótesis. Quedan explícitos 93 nombres de
Flasgger sin versión, siete JS Kitsu y 33 JS Flasgger sin mapa enlazado, tres WASM
y bibliotecas anidadas dentro de bundles de proveedores. Los 128 nombres Kitsu
siguen sin equivalencia byte a byte entre las fuentes del bundle y el registro.
Por eso `component_version_coverage_complete` y `runtime_sca_approved` permanecen
siempre en false, incluso si ninguna consulta produce hallazgos.

Los SBOM no inventan sistema operativo, árbol de dependencias de runtime,
hashes de paquetes npm ni licencias confirmadas. Su generador tiene identidad
propia; no finge que Trivy produjo el inventario.

## Plan y ejecución de Root

```powershell
python -I -S -B docker/kitsu-zou/embedded_js_sca.py
```

El plan incluye source/plan SHA256, los dos SBOM previstos, límites, namespace
de salida, owner, herramienta y snapshot de la DB. No crea artefactos ni carga
NativeJob. No lee `.env`. Root debe respaldar esos pins y la ruta/owner antes
de habilitar la operación; el plan queda obsoleto si cambia un insumo o snapshot.

Sólo después de revisión y pins de Root:

```text
python -I -S -B docker/kitsu-zou/embedded_js_sca.py --scan --expected-source-sha256 SHA_FUENTE --expected-plan-sha256 SHA_PLAN
```

Requiere Windows amd64 y el NativeJob congelado, sin hooks ni cambios:
`sre/windows_build_job.py`, SHA256
`dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44`.
El lector/hash seguro se carga ejecutando exclusivamente sus bytes verificados:
`recover_opencv_sdk_scan_v2.py`, SHA256
`b15f8ed20e19b0b1b19dfdd5ffd81ea60667175a014ab63409b29012fa6f253c`.
Ninguno se modifica ni sustituye por monkeypatches.

Cada scan tiene 180 segundos, ocho procesos, 2 GiB y **8 MiB combinados de
stdout/stderr**. JSON sale a stdout bajo el límite del Job; no se permite que
Trivy escriba un reporte sin límite por `--output`. El scanner recibe entorno
mínimo, sin credenciales, PATH limitado a System32, configuración/ignorefile
desactivados explícitamente, `--offline-scan`, `--skip-db-update`, caché de
artefactos en memoria y telemetría desactivada. No se ejecutan npm, Git, SSH,
Docker o SDK de las aplicaciones.

Se fija Trivy 0.76.2 por hash y la caché
`.tools/trivy-cache-current-20261106`, owner
`agencia-digital-marketing-ai:trivy-current-db:v2`. La metadata debe seguir vigente
según UpdatedAt/DownloadedAt/NextUpdate. Herramienta, DB, metadata, marcador y
árbol de caché se cotejan antes/después de cada scan. La observación del árbol
es acotada (512 nodos, profundidad 12, archivos auxiliares hasta 32 MiB), rechaza
enlaces y conserva logs/directorios preexistentes. No se actualiza caché ni se
cambian permisos. Los scans de Root deben coordinarse con otros usuarios de la
misma caché para evitar que un cambio concurrente invalide la prueba.

## Evidencia, advertencias y resultados

La salida nueva es exclusivamente
`.tools/evidence/phase4-kitsu-swagger-embedded-js-sca-v1`, owner
`agencia-digital-marketing-ai:kitsu-swagger-embedded-js-sca:v1`. Debe estar ausente:
se crea EXCL con marker/devino, sin adoptar o reemplazar un directorio anterior.
Los archivos se publican nuevos, sin modificar originales. Conserva:

- `.owner.json` y `analysis.json`: identidad, todas las referencias candidatas,
  ambigüedades y huecos.
- Por análisis: `.cdx.json`, `.trivy.json` (stdout exacto) y `.stderr.log`.
- `receipt.json` sólo tras completar ambos; `failure.json` conserva intentos,
  parciales y scans anteriores si algo falla. No se borra evidencia ni reintenta.

La cobertura exige biyección exacta de nombre/versión/PURL en resultados
`lang-pkgs`/`node-pkg`, sin faltantes, duplicados, paquetes adicionales, cambios
de origen o findings suprimidos. Cada hallazgo debe corresponder a una identidad
consultada. Se conservan UNKNOWN, LOW, MEDIUM, HIGH y CRITICAL; no se fabrica un
CVSS cuando falta. JSON duplicado, no finito, truncado o con UTF-8 inválido falla.

Sólo después de esa cobertura se aceptan los tres textos WARN exactos ya
acreditados: las dos advertencias de SBOM externo y la advertencia de selección
de severidad de otros proveedores, con su URL v0.76 exacta. Los textos completos
se conservan. ERROR/FATAL o un warning distinto requieren revisión y producen
fallo. Las descripciones dentro del JSON son datos de advisories, no logs.

Exit 0 significa análisis completo sin HIGH/CRITICAL/UNKNOWN entre las hipótesis;
exit 2 conserva un análisis completo con esos hallazgos; exit 1 indica fallo
técnico/cobertura. MEDIUM/LOW siguen en el informe y en sus contadores. Ninguno
de esos códigos aprueba el runtime con sus huecos pendientes. No se prescribe
remediar jQuery por su nombre o antigüedad: primero se obtiene el reporte y se
evalúan identidad, aviso y aplicabilidad.

## Validación de la implementación

```powershell
python -I -S -B -W error::ResourceWarning -m unittest discover -s tests -p test_kitsu_swagger_embedded_sca.py -v
python -m ruff check docker/kitsu-zou/embedded_js_sca.py tests/test_kitsu_swagger_embedded_sca.py
python -m ruff format --check docker/kitsu-zou/embedded_js_sca.py tests/test_kitsu_swagger_embedded_sca.py
```

Las pruebas usan datos sintéticos y TemporaryDirectory propias. Cubren
ambigüedad/deduplicación, procedencia del lock, excepción VCS, autodeclaraciones,
PURL, cobertura, cinco severidades, warning exacto, frescura/alteración de caché,
timeout, cleanup fallido, colisiones y plan sin ejecutar el scanner. Los dobles de
NativeJob nunca ejecutan procesos ni prueban el scanner real. El plan real sí
comprueba los insumos como datos, sin atribuirle hallazgos o SCA aprobada.

Fuentes primarias de Trivy 0.76.2: [entrada CycloneDX](https://github.com/aquasecurity/trivy/blob/v0.76.2/docs/guide/target/sbom.md),
[opciones de CLI](https://github.com/aquasecurity/trivy/blob/v0.76.2/docs/guide/references/configuration/cli/trivy_sbom.md),
[decodificación/origen y paquetes sin árbol](https://github.com/aquasecurity/trivy/blob/v0.76.2/pkg/sbom/io/decode.go)
y [PURL npm a NodePkg](https://github.com/aquasecurity/trivy/blob/v0.76.2/pkg/purl/purl.go).
