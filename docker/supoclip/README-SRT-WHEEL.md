# Rueda SRT 3.5.3: construcción única con SDK acreditado

Estado de esta unidad: controlador y entradas preparados; **no se ha ejecutado
el backend, construido la rueda ni instalado un paquete**. El modo predeterminado
es `plan`, sólo lectura. La construcción real requiere revisión del operador y
los hashes externos del manifiesto y del plan. No modifica la bóveda.

## Entradas y revisión

La fuente es el sdist oficial
[SRT 3.5.3 en PyPI](https://pypi.org/project/srt/3.5.3/), de 28.296 bytes,
SHA256 `4884315043a4f0740fd1f878ed6caa376ac06d70e135f306a6dc44632eed0cc0`.
El [manifiesto](srt-build-inputs.json) fija cada uno de sus 37 archivos
regulares, el URL HTTPS, el SDK, el backend propio y los helpers reutilizados.
Se conserva la licencia MIT de Christopher Down y el paquete fuente completo,
incluidos documentación, pruebas y los nueve CLI. No se usa un empaquetador
manual para fabricar la rueda.

Se leyeron completos `setup.py`, `setup.cfg`, `MANIFEST.in`, `srt.py`,
`srt_tools/utils.py` y las rutas CLI relevantes. No hay `pyproject.toml`.
`setup.py` sólo importa `codecs` y `setuptools`, lee `README.rst` y llama
`setup()`. No importa SRT ni sus herramientas, ni invoca Git, red o procesos.
La biblioteca SRT y sus utilidades tampoco invocan red o subprocesos. Se
conserva una distinción importante: el CLI `srt` despacha herramientas con
`os.execvp`, y `srt-process` admite imports/eval elegidos por su usuario.
**Ninguno de esos CLI se ejecuta en esta construcción o verificación.**

Se reutiliza el entorno que construyó Flasgger, sin instalar ni actualizar
nada: `C:\GPU-Bridge\immich\build-venv`, con Python 3.12.1 y
`setuptools.build_meta:__legacy__`. Setuptools 84.2.1, wheel 0.48.0 y packaging
26.3 coinciden con el backend acreditado. Se conservan los ocho paquetes del
SDK y su informe previo; una consulta OSV nueva cubrió las ocho versiones sin
avisos. Eso no certifica el intérprete completo ni convierte en limpio el pip
23.2.1 heredado: pip no se utiliza, su import se deniega y no se copia al runtime.

El inventario fija 1.979 archivos del venv y 1.235 del intérprete/base: EXE/DLL
de su raíz, `DLLs` y `Lib` excluyendo `Lib/site-packages`. Incluye los `.pyc`
existentes; `-B` evita crear otros. Los archivos de cada raíz, sus SHA, tamaños,
dev/inode y las identidades de las raíces se verifican de nuevo antes de cada
acción. Una actualización o archivo adicional provoca rechazo, no adopción.
Los DLL protegidos de Windows forman la frontera del sistema operativo.

El único `.pth` del venv activa `_distutils_hack`; no hay entry points
`setuptools.file_finders` adicionales. Se reutiliza el código de empaquetado
acreditado para Flasgger, fijando sus bytes actuales. No se afirma que
setuptools carezca de funciones de red/procesos: el backend propio deniega sus
eventos Python antes de importar setuptools.

## Propiedad y límites

Raíz nueva y privada autorizada:
`D:\Proyectos\Agencia Digital de Marketing AI\.tools\supoclip-srt-build-v1`.
Owner: `agencia-digital-marketing-ai:supoclip-srt-build:v1`. El operador ya
respaldó la raíz y el DTO completo de identidad. El marcador
`.agency-srt-build-owner.json` liga owner, ruta, dev/inode y SHA; el controlador
no crea otra raíz ni adopta una que carezca de esos datos.

La comprobación de permisos agrupa los archivos/directorios del SDK en una
consulta acotada al PowerShell integrado, sin secretos ni SDK ejecutados.
El ejecutable literal protegido de System32 puede tener hardlinks WinSxS;
esa excepción del sistema no se aplica al sdist, al SDK o a las salidas propias,
que siguen exigiendo archivos regulares con un solo enlace. No se siguen
symlinks, junctions ni otros reparse points.

La extracción usa el helper fijado `sre/prepare_backup_sdk.py` sobre un destino
nuevo dentro de la raíz. Antes de escribir contrasta el TAR completo, rutas,
duplicados/casefold y todos los hashes. Se rechazan enlaces, rutas ambiguas,
reservados Windows y entradas que no estén en el inventario.

El build utiliza el Job Windows original, SHA256
`dfe20e53ce750a00358dca0edfdf34851aa902c5e7b345452a33542b914a7c44`,
sin modificarlo ni interceptar sus llamadas nativas. Límites: 180 segundos,
8 procesos por Job, 2 GiB y 1 MiB combinado de salida. El backend puro deniega
crear procesos y sockets; el Job conserva además el cierre del árbol propio.
No es un ejecutor general de fuentes arbitrarias.

El entorno se construye desde cero: PATH sólo System32, perfiles/TEMP propios,
sin proxies, credenciales, PYTHONPATH ni variables heredadas. Usa el Python
absoluto fijado con `-I -B -X utf8`; el backend llama directamente a setuptools,
sin pip, build frontend, instalación, shell o comandos Git. El origen y
configuración de los argumentos no proceden de una petición de la aplicación.

## Acciones del operador

Ejecutar siempre el controlador con `-B`. La acción `plan` no extrae fuentes,
crea perfiles ni inicia el SDK; comprueba entradas y devuelve `plan_sha256`.
Los cuatro comandos siguientes describen pasos separados, no una ejecución
autorizada por este documento:

```powershell
python -B docker/supoclip/build_srt_wheel.py plan
python -B docker/supoclip/build_srt_wheel.py prepare --manifest-sha256 HASH_MANIFIESTO --plan-sha256 HASH_PLAN_INICIAL
python -B docker/supoclip/build_srt_wheel.py plan
python -B docker/supoclip/build_srt_wheel.py build --manifest-sha256 HASH_MANIFIESTO --plan-sha256 HASH_PLAN_PREPARADO
```

1. El operador revisa y respalda los SHA del controlador, backend, manifiesto,
   fuente y primer plan antes de `prepare`.
2. `prepare` sólo publica `source/`, backend fijado, carpetas privadas vacías y
   `prepare.json`. No ejecuta el SDK. Se vuelve a obtener un plan porque el
   estado cambió; su hash y el recibo preparado se respaldan antes del build.
3. `build` vuelve a verificar SDK, permisos y todos los archivos fuente. Reserva
   `attempt.json` con creación exclusiva antes de lanzar el Job. Hay un único
   intento: un fallo conserva stdout, stderr y resultado; no repite, sobrescribe
   ni borra el árbol de diagnóstico.
4. Sólo si el Job termina con exit 0 y cleanup completo se inspecciona la rueda.
   El DTO devuelve ruta, tamaño, SHA, tags y alcance; el operador respalda esos
   datos **antes de instalarla, transferirla o incorporarla a otro lock**.

`verify` permite repetir sólo la inspección de la salida existente, con un plan
actual y sus hashes; no vuelve a ejecutar el backend. El modo `build` no sirve
para reintentar un fallo o adoptar una rueda de otra ubicación.

Nombres propuestos para los pins adicionales del operador:
`MEDIA_SUPOCLIP_SRT_BUILD_INPUTS_SHA256`, `MEDIA_SUPOCLIP_SRT_BUILD_CONTROLLER_SHA256`,
`MEDIA_SUPOCLIP_SRT_BUILD_BACKEND_SHA256`, `MEDIA_SUPOCLIP_SRT_PREPARE_PLAN_SHA256`,
`MEDIA_SUPOCLIP_SRT_PREPARED_SHA256`, `MEDIA_SUPOCLIP_SRT_BUILD_PLAN_SHA256`,
`MEDIA_SUPOCLIP_SRT_WHEEL_PATH` y `MEDIA_SUPOCLIP_SRT_WHEEL_SHA256`.
Este controlador no escribe ninguna de ellas.

## Verificación de salida y aceptación posterior

La rueda debe llamarse `srt-3.5.3-py3-none-any.whl`. Se comprueban identidad,
purelib/tags, ausencia de dependencias nuevas, rutas/duplicados/tamaños y la
bijección completa de RECORD: cada miembro salvo RECORD tiene SHA256 y tamaño
exactos; RECORD sólo deja vacía su propia fila. No se acepta una extensión
nativa, `.pth` extra, código alterado o un CLI omitido aunque su RECORD se haya
recalculado. SRT, utils, licencia MIT y los nueve scripts se contrastan con el
sdist. Se admiten sólo las transformaciones comprobadas del backend fijado:
shebang a `#!python` y escritura uniforme LF o CRLF de los nueve scripts.
Se rechazan finales de línea mixtos y cualquier línea/cambio adicional; esta
normalización no se aplica a las bibliotecas ni a la licencia.
El `__init__.py` vacío de `srt_tools`, si el backend lo incluye, también debe
coincidir; no se fabrica ni modifica manualmente el paquete generado.

La construcción y el SHA de la rueda **siguen pendientes**. Después corresponden
SCA final con cobertura real, revisión del RECORD producido y una prueba de
parse/compose de SRT por el operador. La rueda no se instala globalmente ni en
el runtime de SupoClip durante esta unidad. No se cambia OpenCV, Prisma,
fuentes, el perfil CPU congelado o ninguna aplicación ajena.

Las pruebas de esta unidad usan exclusivamente archivos temporales y dobles de
Job; nunca llaman al SDK ni construyen una rueda real:

```powershell
python -B -m unittest tests.test_supoclip_srt_wheel
```
