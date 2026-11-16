# Inventario y auditoría fuente/binario de Valkey

El modo predeterminado prepara un plan local sin red. La implementación no
ejecuta archivos del TAR, recetas, Valkey, SDK, Git, Docker ni Trivy; no lee
`.env`. La auditoría HTTPS requiere una acción explícita de Root con hashes de
fuente y plan respaldados. Esta entrega no ha realizado esas consultas ni ha
creado la raíz de auditoría.

La unidad tiene cinco archivos: `binary_materials.py`,
`audit_binary_materials.py`, `binary-sca-inputs.json`, sus pruebas y este README.
El núcleo se carga por bytes y SHA verificados, sin elegir un `.pyc` alternativo.
Reutiliza el lector seguro `b15f8ed2…` y sólo la función de lectura de la
publicación del controlador OS23 `f0577bf6…`; no invoca su scanner, Job o caché
ni modifica sus variables globales.

## Entradas conservadas

- TAR oficial Valkey 9.1.3, SHA
  `184e34ed2d9b51879b682b5b15205e2b9d677c60280e3cb453be8f520d04e293`,
  4.440.532 bytes. Contiene 2.025 miembros, 1.925 archivos y 20.558.490 bytes
  de archivos. Su comentario PAX declara el commit
  `29173b67508b2de4c0f0ed551f3b47cf19fc3ca8`.
- Procedencia oficial `phase4-valkey-source-provenance-v1.json`, SHA
  `3338a16479d25c905c02f8a0fd84429f8b39b6950c064ff0738d01e421a255ac`.
  Se recomputan los cinco Gitblob con SHA1 como algoritmo de datos, sin invocar
  comandos Git. README, Makefile y COPYING de Valkey coinciden además con los
  miembros del TAR. Los textos de Dockerfile y versiones del proveedor quedan
  conservados por sus blobs exactos, aunque su URL original señale una rama.
- [Publicación del inventario V2](README-INVENTORY-v2.md), marcador, identidad,
  plan y recibo positivos; sus ocho archivos se verifican por el lector fijado.
- [OS23](README-OS-SCA.md) ya verificado por Root: recibo
  `b24e765de94369944f11aa48b02b38c00606e329678d91ce7db9a0e1bdc3304b`
  y reporte
  `37ee66d9fe4b73f5f389ea05f6d3a67cb1046ed9555ae98a079108576932d9b1`.
  Se cotejan otra vez los 23 APK y cero hallazgos, sin repetir el scanner.
  Esta cobertura OS no se atribuye a las bibliotecas embebidas.

El TAR se interpreta en memoria, con límite comprimido de 8 MiB, expansión de
32 MiB, archivo de 1 MiB y 4.096 miembros. Se rechazan enlaces, dispositivos,
sparse, duplicados, escapes, archivos como ancestros y trailer no nulo. No se
extraen archivos al filesystem ni se copian permisos 0775 del origen al host.
Los modos originales se conservan únicamente como datos de procedencia.

## Identidades y matriz por binario

Se distingue la declaración del proveedor de la evidencia exacta del TAR,
los hashes de los tres ELF y los marcadores observados del inventario. La receta
declara el SHA del TAR, TLS, conversión rápida de números y libbacktrace. Esto
constituye una base identificada para auditar; no se exige una reconstrucción
reproducible como requisito inventado. Tampoco se afirma una igualdad binaria
con las fuentes que no se haya demostrado.

| Componente | Identidad conservada | Evidencia y alcance |
| --- | --- | --- |
| Valkey | 9.1.3, commit PAX fijado | Proyecto de los tres ELF y alias; versión de header y declaración del proveedor. |
| jemalloc | 5.3.0-0-g0 modificado | VERSION, fuente de defragmentación y marcadores; candidato de enlace en los tres binarios. |
| Lua | 5.1.5 modificado | Header y módulo estático predeterminado del servidor; se conserva la modificación de seguridad. |
| libvalkey | 0.4.0 | Tres macros de versión; declaración de enlace de servidor, CLI y benchmark. |
| linenoise | 1.0 | Header y enlace del CLI. |
| HdrHistogram | commit e4448cf6d1cd08fff519812d3b1e58bd5a94ac42 modificado | Declarado en README; servidor y benchmark, no CLI. |
| ffc.h | 26/03/02 | Tres macros del puerto C99; no se inventa una versión del proyecto C++ fast_float. |
| fpconv | Sin semver acreditado | Subárbol y archivos por hash; sigue enlazado aunque la receta active USE_FAST_FLOAT. |
| lua-cjson | 2.1.0 | Macro y fuente modificada incluida en Lua; servidor. |
| lua-cmsgpack | 0.4.0 | Macro y fuente incluida en Lua; servidor. |
| LuaBitOp | 1.0.2 | Macro y fuente incluida en Lua; servidor. |
| lua_struct | Sin versión acreditada | Archivo exacto y aviso; no se deduce una versión de las macros de compatibilidad Lua. |
| libbacktrace | 96664e69b1ecdb76e824be1d9e8f475b76dd08cf | Declaración exacta del Dockerfile; fuente externa y avisos todavía pendientes. |
| gtest-parallel | Snapshot de fuente conservado | Sólo construcción/pruebas; no se transfiere artificialmente al runtime. |

`supplier_link_candidates` registra entradas de enlace declaradas. Un archivo
estático presente en el argv del enlazador no prueba que éste retuviera todos
sus objetos. Los marcadores son evidencia adicional, no una equivalencia exacta
con la biblioteca upstream. No se da por presente una biblioteca opcional
simplemente porque exista en el TAR. RDMA y módulos requieren sus flags y
archivos; OpenSSL y musl dinámicos remiten al alcance OS23. `REDIS_VERSION`
7.2.4 es una cadena de compatibilidad, no un segundo paquete Redis instalado.

## Consulta OSV y autenticación del commit

El protocolo primario distingue commit de versión. OSV permite `commit` sin
versión y también documenta consultas por etiqueta con `ecosystem: GIT`, nombre
igual a la URL completa del repositorio y `version` igual a la etiqueta exacta.
No se puede inventar esa etiqueta a partir de un semver.
[Contrato oficial OSV](https://google.github.io/osv.dev/post-v1-query/).

Esta revisión utiliza **sólo consultas por commit** para Valkey, HdrHistogram y
libbacktrace. Primero consulta exactamente
`https://api.github.com/repos/PROPIETARIO/REPO/commits/COMMIT`, con los tres
repositorios y commits ya incluidos en el plan. Exige SHA y URL canónica de
commit coincidentes. Después envía `POST https://api.osv.dev/v1/query` con
`{"commit":"SHA"}`. Una respuesta GitHub distinta impide la consulta OSV.
No ejecuta VCS ni descarga código adicional; no usa un ecosistema inventado.

Los componentes que sólo tienen una versión declarada no reciben una etiqueta
Git supuesta. Queda registrado ese límite de cobertura. La ausencia de un feed
o una identidad resoluble no crea una vulnerabilidad ni se presenta como cero
avisos de todos los ecosistemas.

OSV se pagina por `next_page_token`. Tokens repetidos, paginación pendiente,
esquema no admitido, `affected` ausente o lista malformada hacen fallar el
intento. Los datos originales permanecen en las respuestas conservadas.

## Diccionario NVD antes de CVE

Primero se consulta el diccionario NVD 2.0 por el nombre de proyecto fijado en
el manifiesto. Sólo se selecciona un CPE de aplicación activo cuando coinciden
la versión declarada y una referencia primaria de ese proyecto, o el CPE
explícito del proveedor Valkey. Se conservan todos los candidatos no elegidos,
incluidos CPE de sistema/hardware o versiones distintas. Los CPE deprecados no
se adoptan como identidad activa por su cuenta.
[API y paginación del diccionario NVD](https://nvd.nist.gov/developers/products).

Cada identidad seleccionada se consulta con el parámetro `cpeName` de
`https://services.nvd.nist.gov/rest/json/cves/2.0`. Las páginas se verifican por
formato, versión, índice, cantidad y total estable. No se aplanan ni se borran
negaciones, operadores AND/OR, rangos o requisitos de plataforma del CVE.
Se preserva el registro bruto para resolver la aplicabilidad caso por caso;
una coincidencia del feed no prueba por sí sola alcanzabilidad en producción.
[Semántica de aplicabilidad CPE](https://nvd.nist.gov/vuln/Vulnerability-Detail-Pages).

Un diccionario vacío es una ausencia observada de ese resultado. Productos sin
versión o referencia coincidente quedan como identidad pendiente. Ninguno de
esos casos equivale a un reporte universal limpio. El SPDX genérico entregado
en la imagen tampoco se trata como si fuera un paquete npm/APK cubierto por
Trivy. La cobertura del scanner depende del tipo de software identificado.
[Cobertura oficial Trivy](https://trivy.dev/docs/dev/scanner/vulnerability/).

## Transporte, límites y conservación

Las únicas solicitudes permitidas son los GET de metadatos GitHub fijados,
POST OSV y GET del diccionario/CVE NVD. No hay URL elegible por una petición de
usuario, redirects, proxies, cookies o autenticación heredados. Se verifica TLS
con los certificados de confianza del sistema. No se siguen enlaces de avisos
como nuevas instrucciones o rutas de descarga.

La colección HTTP comparte un plazo absoluto de 300 s, 15 s por solicitud,
40 respuestas como máximo, cuatro páginas por consulta, 2 MiB por respuesta y
32 MiB acumulados. NVD se espacia al menos 6,2 s. El mismo límite por solicitud
cubre la espera DNS y la conexión/lectura; un temporizador cierra exclusivamente
el socket propio. El resolver aislado sólo puede devolver direcciones: si el
SO tarda, queda como máximo un hilo daemon pendiente y nunca inicia HTTP tarde.
La CLI aborta ese intento; ese hilo termina al resolver o al salir el proceso.

Los 300 s corresponden a la colección HTTP, no a la lectura/validación local
previa y posterior ni a la escritura de los documentos fuente. Root debe usar
su límite externo de proceso si requiere un máximo duro para toda la CLI.
Estos tiempos locales no permiten ampliar consultas, páginas o bytes. Un límite
alcanzado, respuesta parcial o estado fallido conserva evidencia y falla.

Se guardan solicitud, estado y cuerpo por número, con hashes y bandera de
captura completa; los bytes parciales no se adoptan como JSON válido. No se
recortan hallazgos de un reporte positivo para cumplir el límite. Se preservan
respuestas, índices, alias y todos los niveles de severidad.

Los avisos de OSV/NVD se conservan individualmente y se agrupan por alias sin
perder registros ni rebajar una severidad superior. HIGH, CRITICAL y UNKNOWN
activos, aplicables o pendientes de resolver bloquean la política de hallazgos
conocidos. Una retirada/rechazo explícitos de la fuente quedan registrados por
ID y con el documento original; no desaparecen del inventario. Sin severidad
nominal utilizable, el registro queda UNKNOWN: no se calcula un CVSS parcial
para presentar un cero. Los vectores originales se conservan.

El manifiesto inicial no incluye excepciones y esta revisión rechaza listas
no vacías. Cualquier decisión futura de no afectación o corrección necesitará
ID, componente, hashes concretos, evidencia del parche/función/feature y revisión
de Root en una revisión nueva. Que Lua o jemalloc estén modificados no los
declara corregidos automáticamente. Ningún resultado aprueba el release o el
runtime; `binary_accepted`, `runtime_accepted` y `release_accepted` siguen false.

## Avisos y licencia

El catálogo conserva los bytes completos de 407 archivos detectados por nombres
de licencia o avisos, codificados en base64 con SHA, tamaño y relación con los
componentes. Incluye COPYING BSD-3-Clause de Valkey y avisos de sus dependencias.
No se transfieren los avisos Apache-2.0 de gtest-parallel al runtime como si
estuviera enlazado; sus textos sí se conservan como materiales de construcción.
Los avisos no asignados siguen identificados como fuente pendiente de clasificar.

La imagen oficial carece de los textos completos observados en la fuente y el
material externo libbacktrace sigue pendiente. Se deja `license_overlay_pending`
explícito. Una incorporación posterior de avisos será otra unidad: esta CLI no
modifica la imagen, permisos, binarios ni código original.

## Uso por Root

Plan local sin red ni publicación:

```powershell
python -I -S -B docker/valkey/audit_binary_materials.py
```

Root revisa las 19 consultas iniciales y los hashes de fuente, núcleo,
manifiesto, inputs y salidas previstas. Sólo después de respaldarlos puede usar:

```text
python -I -S -B docker/valkey/audit_binary_materials.py --audit --expected-source-sha256 SHA_CLI --expected-plan-sha256 SHA_PLAN
```

Raíz nueva: `.tools/evidence/phase4-valkey-binary-sca-v1`.
Propietario: `agencia-digital-marketing-ai:valkey-binary-sca:v1`.
Debe estar ausente; mkdir y cada archivo son exclusivos. No hay adopción,
sobrescritura ni eliminación de intentos previos. Se conservan cuatro
documentos previstos, solicitudes/respuestas, hallazgos y recibo o fallo. Los
inputs se revalidan antes de emitir un recibo completo. Exit 2 indica hallazgos
bloqueantes con reporte íntegro; exit 1 indica una auditoría incompleta.

Pruebas con transporte falso, sin consultas reales ni binarios:

```powershell
python -I -S -B -W error::ResourceWarning tests/test_valkey_binary_sca.py -v
python -m ruff check docker/valkey/binary_materials.py docker/valkey/audit_binary_materials.py tests/test_valkey_binary_sca.py
python -m ruff format --check docker/valkey/binary_materials.py docker/valkey/audit_binary_materials.py tests/test_valkey_binary_sca.py
```

Cubren TAR, límites, fuente/commit, candidatos por binario, pruebas/licencias,
feed ausente, CPE ambiguo, metadatos incorrectos, paginación, avisos brutos,
alias, severidades, límites y publicación exclusiva. El watchdog se prueba
contra socket/HTTP simulados que se cierran; no se abre un puerto ni conexión.
