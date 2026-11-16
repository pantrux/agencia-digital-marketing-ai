# Derivada local de Flasgger: UI3 sin jQuery

## Alcance y estado

Se preparan cuatro fuentes nuevas. El plan sólo lee ZIP/JSON y transforma datos
en memoria: no instala Flasgger, ejecuta JavaScript, importa Zou/Flasgger, inicia
SDK ni consulta `.env`. La publicación es una operación explícita posterior de
raíz; no crea el namespace ni concede ACL por su cuenta.

La entrada es la rueda real `flasgger-0.9.7.1-py2.py3-none-any.whl`, SHA256
`ee71b3e30ed15c59274d72a27277ae258378a7f28dc50ac17a4526d343c4618f`.
El manifiesto fija los 99 miembros completos, sus tamaños y SHA256. El recibo
histórico de SCA `64b2e900…` conserva 3 HIGH y 9 MEDIUM de jQuery. No se modifica
ese recibo, el original, su sdist, su rueda ni los locks vigentes.

Zou 1.1.1 configura `/apidocs/`, recursos `/docs`, `/openapi-raw.json` y
`/openapi.json` con OpenAPI 3.0.2. No selecciona UI2; Flasgger elige UI3 por
defecto. Son rutas de aplicación, sujetas al prefijo del proxy futuro.

## Transformación cerrada

Se retiran físicamente sólo 67 miembros exactos del manifiesto: los 65 de UI2 y
`ui3/static/lib/jquery.min.js` con su mapa. Se rechazan listas incompletas,
adicionales o duplicadas. Se conservan los avisos/licencias presentes en los
miembros retenidos y el LICENSE de Flasgger byte exacto.

Los seis miembros con contenido cambiado son:

| Miembro | Cambio |
| --- | --- |
| `flasgger/base.py` | Rechaza explícitamente una UI distinta del entero 3 antes de registrar su blueprint. |
| `flasgger/__init__.py` | Versión local `0.9.7.1+agency1`. |
| `ui3/templates/flasgger/body_scripts.html` | Retira sólo la etiqueta de carga jQuery. |
| `ui3/templates/flasgger/swagger.html` | Título con DOM `textContent` y Jinja `tojson`; encadena la finalización existente. |
| `ui3/templates/flasgger/head.html` | Retira únicamente el enlace externo Google Fonts, conservando CSS local y estilos existentes. |
| `METADATA` | Cambia sólo la cabecera Version; dependencias y descripción conservadas. |

El wrapper de `onComplete` se instala después de combinar las opciones de
Flasgger, `ui_params` y `ui_params_text`. Invoca el callback anterior con su
`this` y argumentos, devuelve su resultado y actualiza el título en `finally`,
incluso si el callback lanza una excepción. También intenta la actualización
inmediata, y respeta `hide_top_bar`. No se sobrescriben interceptores JWT,
configuración OAuth, plugins, especificaciones ni callbacks distintos.

Los restantes 25 contenidos conservados permanecen byte exactos, incluidos
Swagger JS/CSS/mapas, OAuth redirect, utilidades Python, WHEEL, entry points y
LICENSE. Se renombra dist-info y se regenera RECORD para los 32 miembros de
salida. ZIP/RECORD se validan antes y después: rutas, tipos, límites, alias de
mayúsculas, nombres locales, intervalos físicos, CRC y cobertura completa. No
se extrae ningún ZIP al sistema de archivos.

La rueda se genera de forma determinista con fechas fijas, orden estable y el
zlib indicado en el plan. El plan incluye SHA/tamaño del resultado calculado
en memoria. Esto es evidencia de datos, no un build instalado ni aceptación UI.

## Uso por raíz, después de respaldar pins

```powershell
python -B docker/kitsu-zou/derive_flasgger_wheel.py
```

Raíz debe persistir primero SHA de las cuatro fuentes, manifiesto, plan y rueda
prevista. Propuesta de campos bajo `MEDIA_ZOU_FLASGGER_UI_`: `CONTROLLER_SHA256`,
`INPUTS_PATH/SHA256`, `PLAN_PATH/SHA256`, `ROOT_PATH`, `OWNER`,
`ROOT_PROOF_PATH/SHA256` y `OUTPUT_PATH/SHA256`. Este controlador no escribe la
bóveda. El namespace propuesto es `.tools/kitsu-zou-flasgger-ui-v1`, owner
`agencia-digital-marketing-ai:flasgger-ui-derivative:v1`.

Raíz crea ese directorio privado vacío y su marcador
`.agency-flasgger-ui-owner.json`. El marcador JSON contiene exactamente
`schema_version=1`, `owner`, `root` absoluto, `device` e `inode`. El archivo de
prueba externo contiene esos mismos campos más `marker_sha256`; su ruta/SHA
deben quedar respaldados antes de publicar.

```powershell
python -B docker/kitsu-zou/derive_flasgger_wheel.py --operation apply `
  --manifest-sha256 SHA_MANIFIESTO_RESPALDADO `
  --plan-sha256 SHA_PLAN_RESPALDADO `
  --root-proof RUTA_PRUEBA_PRIVADA_RESPALDADA `
  --root-proof-sha256 SHA_PRUEBA_RESPALDADO
```

Apply exige correspondencia de todos los pins y la identidad física. Reutiliza
`sre/backup_tools_access.py` por sus bytes/SHA `ae1c6d5e…` para verificar ACL
privada; no consulta loaders `.pyc`. La raíz debe contener sólo el marcador.
Intent, rueda y recibo se crean con EXCL/0600 y se sincronizan. Un fallo conserva
sus archivos propios para diagnóstico; no borra, adopta ni sobrescribe parciales.
La rueda queda como dato y el recibo mantiene `installed=false`,
`runtime_approved=false` y `sca_approved=false`.

## Comprobaciones locales y aceptación pendiente

```powershell
python -B -W error::ResourceWarning -m unittest discover -s tests -p test_flasgger_ui_derivative.py -v
python -m ruff check docker/kitsu-zou/derive_flasgger_wheel.py tests/test_flasgger_ui_derivative.py
python -m ruff format --check docker/kitsu-zou/derive_flasgger_wheel.py tests/test_flasgger_ui_derivative.py
```

Las pruebas usan fixtures de bytes, ZIP, RECORD y AST, además de directorios
temporales propios. La guarda UI3 se ejecuta sólo sobre una función de fixture
Python aislada; no se ejecuta código de Flasgger. Los contratos de callback son
comprobaciones de la estructura generada, sin afirmar ejecución JavaScript.
Se verifican reproducibilidad, preservación de dependencias/licencia, entrada
alterada, rutas/enlaces/alias, parches no únicos, EXCL, lectura acotada,
identidad del namespace, ausencia de efectos al importar y pins antes de apply.

Antes de sustituir la rueda del lock, raíz realizará por separado:

1. Comparación con Flask real de ambos JSON OpenAPI antes/después, incluidos
   componentes, muestras de código y rutas; `/apidocs/`, assets y OAuth redirect.
2. Navegador real: cero errores JavaScript, modelos/operaciones visibles,
   callback previo una sola vez con sus argumentos/retorno, título seguro con
   comillas y `</script>`, ambos estados de hide_top_bar, JWT fixture y Try it out
   sin secretos reales. Verificar carga diferida de React mediante onComplete.
3. Inventario y SCA de todos los bytes de la rueda nueva y su nuevo lock Python,
   conservando los reportes anteriores. Los hashes de paquetes no se rellenan
   desde versiones npm supuestas ni se trasladan las excepciones de la entrada.

La retirada cierra la presencia física de los dos jQuery autodeclarados; aquí
no se ejecutó un nuevo escaneo para afirmar cero hallazgos. Los bundles Swagger
antiguos todavía contienen dependencias sin identidad/versionado completos.
El ajuste adicional aprobado retira el único enlace Google Fonts de head.html
mediante un parche literal único, con hashes antes/después. No modifica otros
estilos, fuentes ni recursos. Una prueba conserva exactamente el CSS/fallback
local de la fixture. El funcionamiento offline completo requiere todavía la
aceptación de navegador y el inventario de peticiones reales.
No se aprueba Kitsu/Zou para producción con estos contratos puros.

Fuentes primarias: [Flasgger oficial](https://github.com/flasgger/flasgger),
[jQuery, corrección Ajax](https://github.com/jquery/jquery/commit/b078a62013782c7424a4a61a240c23c4c0b42614),
[jQuery, interpretación de selectores](https://bugs.jquery.com/ticket/11290/),
[versiones locales PEP 440](https://peps.python.org/pep-0440/#local-version-identifiers)
y [formato wheel/RECORD](https://packaging.python.org/en/latest/specifications/binary-distribution-format/).
