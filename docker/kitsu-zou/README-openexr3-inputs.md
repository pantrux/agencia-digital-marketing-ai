# Inputs alternativos de OpenEXR 3.5.3

Esta unidad prepara **datos**, no instala paquetes ni ejecuta CMake, SDK,
Docker, SSH o el probe. Se conservan el kit `755ea032…`, los recibos y todos
los outputs parciales del intento que compiló OpenEXR 2.3.0. El código anterior
permanece congelado. No se deshabilita OpenEXR ni se modifica su licencia.

## Causa y corrección propuesta

OpenCV 4.13.0 intenta encontrar OpenEXR con `find_package(OpenEXR 3 QUIET)`.
Si falla, su detector añade `3rdparty/openexr` y activa la variable normal
`BUILD_OPENEXR`, aunque el cache aún diga OFF. El Config 3.5.3 seleccionado
necesita los paquetes CMake de libdeflate y OpenJPH, que faltaban. El import
oficial de libdeflate también comprueba un target estático que OpenEXR no usa.

Se añaden como fuentes de datos autenticadas:

| APK | SHA256 del archivo | Uso |
| --- | --- | --- |
| libdeflate-dev 1.19-r10 | `99f8627ca991a0439e0813043bfd4b59e5371f376593859ad715e034c9060c33` | CMake/pkgconfig, header y enlace local. |
| openjph-dev 0.32.0-r0 | `53c855b870f8417e48537a7a9cf96dac888004defb3c464a6bb6d139ba456deb` | CMake/pkgconfig, headers y enlace local. |
| libdeflate-static 1.19-r10 | `ec17d371abbcbbd25e3e4592732ecbbe409a9f6d4c277a215baa59140dd7ff67` | Sólo existencia del import estático; no enlace efectivo ni rueda. |

Los tres archivos se descargaron una sola vez por TLS, después del respaldo de
Root. Se verificaron con el índice Wolfi `9b27ab69…`, firma RSA-SHA256, Q1 de
control, PKGINFO y SHA256 del payload mediante los helpers `831a2804…` y
`b00b1a1a…`. No tienen firma individual de APK. Los archivos originales y sus
SBOM SPDX se conservan. No se ejecutan `.melange.yaml` ni hooks de paquetes.

## Whitelist y salida como datos

`openexr3-inputs.json` fija 15 APK de origen y 242 archivos/enlaces: mantiene
los headers y bibliotecas runtime anteriores, agrega los metadatos oficiales
sin cambiar sus bytes y conserva sus licencias/orígenes. Cada ELF mantiene
el SHA observado en el inventario anterior; no se presupone una biblioteca
nueva por compartir nombre. Los dos dev aportan diez archivos CMake/pkgconfig,
once headers y dos enlaces locales. `libdeflate.a`, SHA `65326b6d…`, sólo
completa el import oficial de CMake.

La salida futura de `prepare` será un directorio nuevo EXCL con marcador,
`overlay.tar` y recibo. Los enlaces se representan dentro del TAR; no se
crean symlinks del sistema en Windows ni se usa `extractall`. El TAR conserva
archivos upstream y añade seis datos generados bajo `agency`: manifiesto de
selección, proyecto y fuente del probe, guard, lector ELF y receta candidata.
Los archivos se representan root:root 0444. No se crean entradas de una DB APK
ni se presentan estos parciales como paquetes instalados.

Root debe respaldar antes de preparar la ruta nueva propuesta
`.tools/kitsu-zou-openexr3-inputs-v1`, propietario lógico
`agencia-digital-marketing-ai:opencv-openexr3-inputs:v1`, hashes de las cuatro
fuentes y del plan. No se adopta una salida existente ni se borra un parcial.
La unidad no lee ni escribe `.env`.

```powershell
python docker/kitsu-zou/prepare_openexr3_inputs.py plan
```

Root revisa el SHA mostrado y, en una acción posterior autorizada, puede usar:

```powershell
.tools/venvs/synology-audit/Scripts/python.exe docker/kitsu-zou/prepare_openexr3_inputs.py prepare --output .tools/kitsu-zou-openexr3-inputs-v1 --expected-plan-sha256 $shaRevisado --root-authorized
```

Ese comando sólo autentica y escribe el TAR de datos. No monta el prefijo, no
reinicia el CID anterior ni ejecuta ninguna receta.

## Probe y rechazo del fallback

El proyecto generado exige OpenEXR **3.5.3 EXACT CONFIG REQUIRED** y verifica
los ocho imported targets y `Threads::Threads`. Compila una biblioteca pequeña
con referencia real a `Imf::RgbaInputFile`, sin ejecutar su código. Su ELF debe
acreditar `libOpenEXR-3_5.so.34` y un conjunto cerrado de dependencias runtime.
Los targets y sus ubicaciones deben coincidir con la whitelist del prefijo.

El lector ELF se obtiene de las funciones puras `part`/`elf_info` del helper
congelado `9e0a2829…`, por bytes y hash. No se modifica ese helper ni se carga
una biblioteca nativa. El guard también comprueba hashes de todo el prefijo,
montaje readonly y ausencia de `libdeflate.a` en las líneas de enlace reales.

La receta candidata deriva de `5f957784…`. Agrega el probe antes del build
principal y las opciones `CMAKE_REQUIRE_FIND_PACKAGE_OpenEXR=ON` y
`CMAKE_EXPORT_COMPILE_COMMANDS=ON`. Después de configurar exige que no haya
fuentes `3rdparty/openexr` en `compile_commands.json`, antes de compilar.
Permanece el guard final del packager que exige OpenEXR compartido externo.
La biblioteca estática no se selecciona para la rueda.

El comportamiento de fallo temprano está documentado por CMake:
[CMAKE_REQUIRE_FIND_PACKAGE](https://cmake.org/cmake/help/v3.31/variable/CMAKE_REQUIRE_FIND_PACKAGE_PackageName.html)
y [propagación de QUIET/REQUIRED en find_dependency](https://cmake.org/cmake/help/v3.31/module/CMakeFindDependencyMacro.html).

Esta receta requiere un **contrato nuevo de CID/mounts y un work/output nuevo**,
propuesto como `/work/openexr3-attempt-v2`. No se puede inyectar el prefijo en
el contrato NoForeign congelado ni adoptar el árbol parcial previo. El nuevo
lanzador y la materialización Linux corresponden a una unidad posterior.

## SCA requerida antes de usar el probe

El alcance nuevo es el snapshot de **89 APK instalados**, dos parciales
originales (Imath/OpenEXR), tres parciales nuevos (libdeflate-dev,
openjph-dev y libdeflate-static): **94 identidades APK más NumPy 2.4.6**,
95 componentes. El plan identifica explícitamente cada parcial y su uso.
Esta unidad no cambia los paquetes del SDK ni afirma haberlo reinspeccionado
en vivo.

El recibo `2ce0c511…` sólo acredita su alcance histórico; no cubre los tres
inputs nuevos. La futura auditoría de Root debe conservar esa distinción,
probar cobertura positiva del superset y cerrar SCA vigente antes de ejecutar
CMake. Este preparador no ejecuta Trivy ni emite aprobación SCA. La receta es
un artefacto pendiente, sin permiso automático para ejecutarse por existir.

## Verificación y límites

Los tests públicos usan fixtures portables: identidad/versiones y targets,
proyección exacta y EXCL, enlaces dentro del prefijo, rechazo de OpenEXR interno,
prohibición de enlace estático, carga del guard y alcance SCA honesto.
La comprobación privada de datos autentica los 15 APK y coteja los 242 nodos;
no crea el prefijo de instalación ni ejecuta el probe.

```powershell
.tools/venvs/synology-audit/Scripts/python.exe -W error::ResourceWarning -m unittest discover -s tests -p test_openexr3_inputs.py -v
```

Quedan pendientes la preparación por Root, SCA nueva, materialización readonly,
probe y build reales, aceptación de la rueda y su imagen. Los controles de
fixtures y lectura de ELF como datos no sustituyen esas pruebas.
