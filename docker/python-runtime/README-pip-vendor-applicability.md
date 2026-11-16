# Aplicabilidad propuesta para el subconjunto setuptools de pip

Esta nota acompaña exclusivamente a
`.tools/evidence/phase4-python-pip-vendor-applicability-v1.json`. Es una propuesta
de revisión de código presente como datos. No aprueba runtime, imagen, release,
instalación o excepción automática; Root debe revisar y respaldar la decisión.

El sujeto es la rueda pip 26.2.2 de 1.828.619 bytes, SHA256
`38f4aacff7c3b0d7b216cac0a106a1036eef98b0a7ff5b932ea2fcb787e97bc5`,
incluida como `rootfs/usr/share/python-wheels/pip-26.2.2-py3-none-any.whl` en el
contexto `f41fbd541699f2476db70ab3ae2d65c6cb369c3ce39b9070d86f9ed99b7c9d23`.
No se ejecutó su Python, pip, pkg_resources, imports o funciones. Tampoco se
ejecutaron scanners, SDK, instalación o infraestructura durante esta revisión.

## Cadena de evidencia

Se releen por SHA el inventario `40d75320…`, su recibo `4263e075…`, el SBOM
`ef383b18…`, el reporte Trivy `db6b8268…` y el recibo de consulta `97f86b72…`.
Se comprueba también el contexto TAR contra el recibo preparado `9aa542b0…`.
El JSON registra los hashes completos y las ubicaciones de cada entrada.

Los 481 archivos reales de la rueda se cotejan uno a uno contra el inventario:
ruta, tamaño y SHA256. Se comprueban CRC y las 481 entradas de RECORD, con la
excepción reglada del propio RECORD sin hash/tamaño. TAR y ZIP sólo se leen en
memoria; no se materializan enlaces o payloads en el filesystem.

El `pip/_vendor/README.rst` real, SHA `b7b2229e…`, declara la reducción de
setuptools a pkg_resources. `vendor.txt`, SHA `7e43aba7…`, mantiene la declaración
`setuptools==70.3.0`. La revisión conserva ambas observaciones: esa versión es una
hipótesis de la distribución original, no prueba de que se copie toda ella.

El subconjunto observado contiene sólo:

- `pip/_vendor/pkg_resources.pyi`, SHA `574e4ddb…`.
- `pip/_vendor/pkg_resources/LICENSE`, SHA `86da0f01…`.
- `pip/_vendor/pkg_resources/__init__.py`, SHA `bdb4c9d3…`.

Los hashes completos y el hash del catálogo de esos tres archivos quedan en el
JSON. Las licencias y los bytes originales se conservan sin cambios.

## Tres avisos conservados

| Hallazgo bruto | Severidad Trivy | Implementación descrita por la fuente primaria | Propuesta limitada al sujeto |
| --- | --- | --- | --- |
| CVE-2025-47273 / GHSA-5rjg-fvgr-3xxf | HIGH | `setuptools/package_index.py`, `PackageIndex._download_url` | `proposed_not_affected` / `code_not_present` |
| CVE-2026-59890 / GHSA-h35f-9h28-mq5c | MEDIUM | `setuptools/command/egg_info.py` y `setuptools/_distutils/filelist.py`, `FileList` | `proposed_not_affected` / `code_not_present` |
| GHSA-xmj3-9gf7-h52w | LOW | `setuptools/wheel.py`, instalación heredada como egg y generación de namespaces | `proposed_not_affected` / `code_not_present` |

Los cuatro módulos están ausentes, tanto en la raíz como bajo prefijos
vendorizados. El análisis AST de los 406 archivos Python comprueba la ausencia de
las implementaciones y métodos concretos. La propuesta no descansa en que la
rueda sea un dato sin ejecutar ni en que su uso previsto sea Linux; descansa en
los bytes de código ausentes para cada aviso.

Sí hay una clase `PackageIndex` en `pip/_internal/models/index.py`: administra
URLs y no implementa la descarga vulnerable. También hay clases `Wheel` propias
de pip. El JSON conserva sus rutas, métodos y hashes; no se confunden con la
clase de `setuptools/wheel.py`. `declare_namespace` está presente en
pkg_resources, pero la cadena que genera archivos desde metadatos de una rueda
pertenece al módulo ausente de setuptools. Un nombre aislado no demuestra
presencia de la implementación vulnerable.

Fuentes primarias consultadas: [PackageIndex](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf),
[FileList](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c)
y [Wheel](https://github.com/pypa/setuptools/security/advisories/GHSA-xmj3-9gf7-h52w).

Para CVE-2026-59890, Trivy informa `FixedVersion=83.0.0`, mientras la página
primaria consultada mantiene `Patched versions=None`. Ambas observaciones se
conservan sin inventar una resolución. La etiqueta primaria actual de
CVE-2025-47273 es Moderate; se conserva el HIGH del reporte original. Ninguna
de esas diferencias se usa para borrar o degradar avisos.

## Decisión pendiente y frontera

El reporte bruto conserva HIGH=1, MEDIUM=1 y LOW=1. Los tres registros quedan
referenciados por posición JSON y SHA canónico, junto con sus campos originales
de severidad, versión, CVSS y corrección declarada. No se añade `.trivyignore`,
no se cambia el SBOM ni se altera la consulta para ocultarlos.

Hay **un HIGH pendiente de revisión de aplicabilidad por Root para este sujeto**.
Se propone descartarlo por código ausente, junto con los otros dos avisos, pero
esta propuesta no es una aceptación ya aplicada. No se calcula ni afirma un
contador global de HIGH/CRITICAL pendientes para toda la plataforma.

Una aprobación eventual debe enlazar este JSON y los hashes exactos de rueda y
contexto. Cambiar bytes, instalar setuptools completo, añadir un overlay o cargar
una distribución externa exige otra revisión. El resultado no sustituye la SCA
de los 19 paquetes base y 14 payloads nativos, ni certifica que pip o el resto del
runtime carezcan de otros fallos. `identity_complete=false`, `root_approved=false`,
`runtime_approved=false` y `release_approved=false` permanecen explícitos.
