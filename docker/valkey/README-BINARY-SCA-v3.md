# Auditoría fresca del binario Valkey: revisión V3

Esta revisión conserva las cinco fuentes V1, las tres V2 y todos sus intentos.
V2 capturó seis respuestas completas y falló antes de recibir cabeceras o bytes
de la tercera página del diccionario Lua. Su registro no permite atribuir aquel
fallo a TLS, DNS, conexión o plazo. El supervisor acabó con código 1 y limpieza
completa. No se presenta ese intento incompleto como una auditoría aprobada.

V3 **consulta todos los feeds de nuevo**. No lee, reproduce, copia ni adopta las
respuestas V2. Los materiales estáticos se reconstruyen desde las fuentes e
inventarios fijados y las declaraciones V1 conservan su owner original. La nueva
ejecución usa exclusivamente:

- Owner: `agencia-digital-marketing-ai:valkey-binary-sca:v3`.
- Namespace: `.tools/evidence/phase4-valkey-binary-sca-v3`.
- Entrada: [audit_binary_materials_v3.py](audit_binary_materials_v3.py).
- Pruebas: [test_valkey_binary_sca_v3.py](../../tests/test_valkey_binary_sca_v3.py).

No se crea ese namespace al importar o consultar el plan. Su creación sólo ocurre
con la acción explícita y pins externos; si existe, se rechaza sin adoptar o
sobrescribir nada. No usa `.env`, credenciales, SSH, Git, SDK de compilación,
contenedores, binarios Valkey ni Trivy.

## Reutilización explícita y límites

El wrapper autentica la fuente V2 completa por SHA256
`dff05453d15919a401cec24acd86f56d161bf564999337ecea600972149534b1`.
Compila en un módulo nuevo únicamente la lista AST cerrada que figura en el plan:
colector, paginador, transporte HTTPS y helpers de validación/IO. No ejecuta
`main`, `prepare`, la publicación histórica ni los statements superiores de V2.
No importa ese auditor ni modifica sus globals. El diccionario de límites nuevo
se enlaza sólo en el módulo nuevo; el core se carga por bytes/SHA `2ef9c173…` y
el lector `b15f8ed2…` se conserva intacto.

La única ampliación de política aprobada es **cuatro a cinco páginas por
consulta**. El diccionario Lua real anunció 473 productos: requiere cinco páginas
de 100. No se recorta ese conjunto ni se cambia la selección de identidad CPE.
Una sexta página, un total cambiante, índices incorrectos o una página vacía sin
agotamiento provocan fallo. Las negaciones, rangos y avisos brutos permanecen en
la evidencia. La declaración V1 de cuatro páginas sigue intacta y se distingue
de la política operativa V3 en el plan.

| Presupuesto | Límite |
| --- | --- |
| Colección HTTP | 300 segundos monotónicos |
| Cada intento de transporte | Hasta 15 segundos, sujeto al tiempo global restante |
| Intentos físicos, incluidos fallos y reintentos | 40 |
| Cuerpo por respuesta o parcial capturado | 2 MiB |
| Suma de cuerpos, incluidos parciales de intentos fallidos | 32 MiB |
| Páginas por consulta | 5 |
| Separación entre inicios de intentos NVD | Al menos 6,2 segundos |
| Supervisor externo de Root | 390 segundos, 8 procesos, 2 GiB y 1 MiB de salida |

El límite de 300 segundos cubre la colección y las esperas de los reintentos.
El cálculo previo de materiales y la revalidación/publicación posterior quedan
dentro del supervisor externo de 390 segundos, no se describen como parte de esos
300. Si un cuerpo hace superar la suma, se conserva la captura acotada y falla;
al alcanzar exactamente el tope no se inicia otra operación HTTP.

## Transporte y reintento finito

Se conserva exactamente el transporte V2 con `HTTPResponse.read1`, validación
de longitud, framing cerrado y watchdog sobre el socket propio. Una longitud
100 con sólo `{}` falla y conserva los dos bytes. El EOF sin Content-Length
válido se acepta; un chunk truncado falla. No se siguen redirecciones ni se
aceptan hosts arbitrarios. Las rutas HTTPS y los cuerpos OSV se validan antes
de cada intento.

Sólo se permite **un intento adicional** para GET que haya fallado antes de
cualquier cabecera/cuerpo por plazo DNS, timeout, conexión reiniciada/abortada,
pipe roto o EOF TLS transitorio. Se conserva cada intento como evidencia nueva.
No se reintentan POST OSV, errores de confianza de certificados, errores SSL
restantes, framing, cuerpo parcial, esquema ni estados HTTP, incluidos 429/5xx.
Una respuesta HTTP inválida vuelve al colector y falla con su registro íntegro.
No hay bucles de reintento genéricos, renovaciones de token ni cambios de TLS.

El diagnóstico conserva sólo tipo, fase acotada y, cuando existe, código
numérico de verificación. No serializa `str(error)` ni sus argumentos. La fase
`pre_headers` identifica una frontera, no una causa precisa: un timeout ahí no
se declara automáticamente fallo de TLS. Los errores inesperados también dejan
su intento y no se reintentan.

El supervisor debe fijar y verificar `SSL_CERT_FILE` contra el PEM público de
dos CA aprobado, SHA256 `ace948cba698b9fa4b428ce170aa0f2ba35782c408acfd6be1b7d24f8439572d`.
Debe conservar `CERT_REQUIRED`, comprobación de hostname y ausencia de
`PARTIAL_CHAIN`. V3 no instala CA, no toca confianza global ni crea un contexto
inseguro. Root validará los tres hosts en el mismo entorno antes de la acción.

## Plan y ejecución posterior de Root

```powershell
python -I -S -B docker/valkey/audit_binary_materials_v3.py
```

El plan sólo lee entradas fijadas y reconstruye materiales en memoria. No consulta
feeds ni publica artefactos de ejecución. Contiene hash del wrapper, fuente V2,
core/lector/manifest, inputs, política efectiva y cuatro outputs estáticos. Root
respalda y revisa fuente/plan/owner/ruta antes de invocar bajo su supervisor:

```text
python -I -S -B docker/valkey/audit_binary_materials_v3.py --audit --expected-source-sha256 SHA_FUENTE_RESPALDADA --expected-plan-sha256 SHA_PLAN_RESPALDADO
```

Cada intento deja `attempt-NN.body` y `attempt-NN.json`, con hora real de captura,
request, estado, hash/tamaño y diagnóstico. Cada respuesta lógica apunta a los
intentos frescos que la produjeron. Los intentos anteriores al reintento también
cuentan para los topes físicos. No se afirma que un feed sea una instantánea
transaccional; se preservan sus timestamps y páginas y se exige un total estable.

`findings.json` conserva todos los avisos, aliases, rangos, candidatos y huecos.
El recibo imprime un resumen acotado, conserva los hashes de la evidencia y deja
`binary_accepted=false`, `runtime_accepted=false` y `release_accepted=false`.
HIGH/CRITICAL/UNKNOWN activos siguen bloqueando la política conocida; un feed
ausente sigue siendo una limitación de cobertura. Cero avisos conocidos no se
convierte en aprobación universal. Si falla, `failure.json` conserva todos los
intentos ya escritos y nunca se emite un recibo positivo.

## Verificación local

```powershell
python -I -S -B -W error::ResourceWarning tests/test_valkey_binary_sca_v3.py -v
python -m ruff check docker/valkey/audit_binary_materials_v3.py tests/test_valkey_binary_sca_v3.py
python -m ruff format --check docker/valkey/audit_binary_materials_v3.py tests/test_valkey_binary_sca_v3.py
```

Las pruebas usan reloj controlado, transportes falsos y `HTTPResponse` real con
socket en memoria. Cubren 473 productos, sexta página/total cambiante, longitud
exacta/truncada, EOF/chunks, limpieza, presupuesto/reintentos, ausencia de retry
POST/TLS/framing/status, publicación EXCL y preservación de parciales. No
acreditan consultas reales ni una evaluación final del binario.
