# Auditoría Valkey V2: integridad del cuerpo HTTP

Esta revisión conserva íntegros los cinco archivos, plan, freeze y logs de V1.
No adopta su namespace ni cambia sus fuentes, matrices, consultas o reglas de
aplicabilidad. Corrige el defecto de framing confirmado durante la revisión
independiente: una respuesta con `Content-Length: 100` y sólo `{}` antes del EOF
podía pasar como JSON completo y producir un cero de avisos OSV incorrecto.

## Cambio y datos conservados

La nueva CLI es `audit_binary_materials_v2.py`. Reutiliza por SHA los bytes del
núcleo `binary_materials.py`:
`2ef9c1735fa908a0099815f6c0e36446e7188271a3f4b5dbde00c18ebd25c442`.
El manifiesto sigue siendo **el original** `binary-sca-inputs.json`, SHA
`f06af3fd6e4e12299bebeb9caceb9f17516e7fafa341647aaefc1d3123ec5632`.
Su propietario de declaración permanece
`agencia-digital-marketing-ai:valkey-binary-sca:v1`; no se renombra su procedencia.
La CLI exige ese SHA antes de interpretar el manifiesto.

La ejecución V2 tiene propietario independiente
`agencia-digital-marketing-ai:valkey-binary-sca:v2` y namespace nuevo
`.tools/evidence/phase4-valkey-binary-sca-v2`. El plan distingue ese propietario
de `declaration_owner`. No hay adopción, migración, sobrescritura ni eliminación
de V1. Los inputs y los cuatro documentos de materiales previstos mantienen
los mismos bytes; cambian el controlador, el plan de ejecución y su identidad.

## Framing completo antes de interpretar JSON

Se mantienen las guardas previas de cabeceras únicas, sintaxis decimal de
Content-Length, rechazo de CL junto a Transfer-Encoding y transferencia
chunked como único encoding admitido. Cuando llega EOF:

- Si existe Content-Length, el total recibido debe coincidir exactamente.
  Un total inferior produce `TransportFailure` con los bytes parciales.
- Sin CL ni TE, el EOF puede delimitar legítimamente la respuesta.
- Una respuesta chunked debe completar sus terminadores. El error real
  `http.client.IncompleteRead` conserva su `partial`, además de los bloques
  recibidos previamente, respetando el límite de captura.

La lectura usa `HTTPResponse.read1()` para conservar los bytes disponibles sin
esperar a completar el bloque solicitado. Esto mantiene también el parcial
cuando el EOF cae dentro de los datos de un chunk, antes de su terminador.

El colector guarda esos bytes con `captured_complete=false` y falla. No llama al
analizador OSV para convertir `{}` truncado en un resultado vacío ni emite un
recibo positivo. Los límites no se amplían, no se aceptan respuestas parciales
y no se ocultan hallazgos para completar un intento.

## Contratos heredados sin cambios

El [contrato completo V1](README-BINARY-SCA.md) sigue aplicándose a fuentes,
licencias, matrices, TLS, hosts, autenticación del commit, CPE, paginación y
severidades. Esta revisión no ejecuta consultas ni programas de terceros al
preparar el plan. No consulta `.env`, no abre SSH ni ejecuta Git, SDK o imágenes.

La colección HTTP conserva 300 s totales, 15 s por solicitud, 40 respuestas,
cuatro páginas por consulta, 2 MiB por respuesta y 32 MiB acumulados. El plazo
se refiere a la colección; Root aplicará su supervisor finito si exige un
límite duro de todo el proceso, incluidas verificaciones y escrituras locales.
Ningún límite de consultas se aumenta automáticamente.

Se siguen conservando todos los avisos y alias, los rangos/negaciones NVD y las
limitaciones de feed. HIGH, CRITICAL y UNKNOWN activos o pendientes bloquean
la política. Los estados de retirada/rechazo quedan ligados al registro bruto
de la fuente. No existen excepciones automáticas. `binary_accepted`,
`runtime_accepted` y `release_accepted` siguen false.

## Plan y ejecución explícita por Root

```powershell
python -I -S -B docker/valkey/audit_binary_materials_v2.py
```

El modo predeterminado sólo lee fuentes fijadas y calcula el plan en memoria.
No crea la raíz ni realiza solicitudes. Root debe revisar y respaldar fuente,
plan, ruta y propietario antes de:

```text
python -I -S -B docker/valkey/audit_binary_materials_v2.py --audit --expected-source-sha256 SHA_CLI_V2 --expected-plan-sha256 SHA_PLAN_V2
```

La raíz V2 debe estar ausente; todos sus archivos se publican exclusivamente.
Un fallo se conserva y no se reintenta adoptando ese directorio.

## Regresión sin conexiones

```powershell
python -I -S -B -W error::ResourceWarning tests/test_valkey_binary_sca_v2.py -v
python -m ruff check docker/valkey/audit_binary_materials_v2.py tests/test_valkey_binary_sca_v2.py
python -m ruff format --check docker/valkey/audit_binary_materials_v2.py tests/test_valkey_binary_sca_v2.py
```

Se usa `http.client.HTTPResponse` real sobre un socket simulado cuyo archivo es
un `BytesIO`; no hay red, servidor ni SDK. La prueba roja reprodujo tres
resultados: CL corto aceptado, falso cero de OSV y pérdida del parcial chunked.
La revisión prueba CL100 con `{}` como fallo y parcial exacto de dos bytes,
CL exacto válido, EOF legítimo sin CL, chunked completo, chunked truncado antes
del terminador y datos truncados a mitad de chunk.
Conserva además las 24 pruebas portables anteriores y coteja los cinco SHA V1.

Estas pruebas no acreditan una consulta externa, un scanner o un release real.
