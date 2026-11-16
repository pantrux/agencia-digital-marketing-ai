# JavaScript incorporado en Kitsu y Flasgger

Esta unidad produce un inventario de datos para una SCA posterior. No ejecuta JavaScript, Node, npm, pnpm, SDK, Trivy, instalación, HTTP o SSH; no lee `.env`, no extrae archivos al disco ni modifica los artefactos existentes. Todas las salidas mantienen `sca_approved=false`. El análisis previo de distribuciones Python no acredita las bibliotecas JavaScript incluidas en ellas.

## Entradas y procedencia

[embedded-js-inputs.json](embedded-js-inputs.json) fija rutas, límites y hashes completos:

| Artefacto | Identidad |
|---|---|
| Kitsu 1.0.77, TAR oficial de release | 12.163.231 bytes, SHA256 `f030dc59…`; `.commit.txt`, `.tag.txt` y `.version.txt` se cotejan con `c1edc91a3e2bfcefabf6f9abf682e2c420f0831f`/`v1.0.77` |
| Flasgger, rueda propia ya construida | SHA256 `ee71b3e3…`; se reutiliza la rueda real y su recibo existente, sin recompilar |
| Sdist oficial Flasgger 0.9.7.1 | SHA256 `ca098e10…`; todos los miembros `flasgger/ui2/` y `flasgger/ui3/` de la rueda deben conservar bytes idénticos a esa fuente |
| package-lock.json Kitsu del mismo commit | 334.034 bytes, SHA256 `5e8e2589…`, blob API `463cb3fa…`, formato v3 |
| package.json Kitsu del mismo commit | 3.229 bytes, SHA256 `88d73c7b…`, blob API `65f97878…` |

Root obtuvo los dos últimos mediante lectura pública de GitHub y respaldó sus pins antes de guardarlos. El helper coteja SHA256 y el identificador de blob mediante el algoritmo de hash sobre bytes; no ejecuta comandos Git. Los documentos primarios están en [package.json](https://github.com/cgwire/kitsu/blob/c1edc91a3e2bfcefabf6f9abf682e2c420f0831f/package.json) y [package-lock.json](https://github.com/cgwire/kitsu/blob/c1edc91a3e2bfcefabf6f9abf682e2c420f0831f/package-lock.json).

El TAR se lee en memoria. Se rechazan escapes, enlaces/tipos especiales, duplicados y colisiones de mayúsculas. El ZIP rechaza miembros cifrados o enlaces. Se limitan a 5.000 miembros, 16 MiB por miembro y 128 MiB expandidos por artefacto; los archivos originales también tienen límites y pins. No se ejecutan los `.py` presentes en Flasgger ni los scripts `prepare` del manifiesto Kitsu.

## Qué prueba cada registro

El catálogo conserva nombre, tamaño y SHA256 de cada archivo. El preparador vincula cada anotación local `sourceMappingURL` con los bytes de su mapa y del JS generado. También decodifica el mapa inline base64 realmente presente en `flasgger/ui2/static/swagger-ui.js`, como JSON acotado, sin evaluar JavaScript.

Se decodifican los segmentos VLQ de mapas planos v3 según la estructura documentada por [ECMA-426](https://tc39.es/ecma426/#sec-mappings). Sólo las fuentes efectivamente referidas por un segmento generan una atribución: estar en la lista `sources` no basta. Se conservan el índice, ruta textual y SHA256 de `sourcesContent` cuando existe. `ignoreList` no elimina componentes. Fuentes locales y módulos `external` no se convierten en paquetes incorporados.

La atribución a `node_modules` procede del mapa publicado. No es una reproducción del build ni una prueba de que el mapa sea fiel a todas las instrucciones del bundle. El helper sólo implementa el subconjunto necesario para estos artefactos: mapas planos, enlaces relativos e inline JSON/base64. No es un consumidor general de todos los formatos/extensiones de source maps.

El lock aporta candidatos por nombre, versión, ruta anidada, origen, integridad y licencia declarada. **La versión del componente sigue pendiente:** coincidir en nombre y commit no demuestra por sí solo que el bundle contenga exactamente los bytes de una distribución npm. Se conservan versiones múltiples y resoluciones VCS como datos; nunca se descargan ni resuelven automáticamente. La dependencia `fabricjs-psbrush` requiere ese tratamiento explícito si una unidad futura reconstruye Kitsu.

Tres evidencias de versión sí están revisadas sobre miembros concretos y SHA256 completos:

| Miembro Flasgger | Versión autodeclarada | Evidencia |
|---|---|---|
| `ui2/static/lib/jquery-1.8.0.min.js` | jQuery 1.8.0 | Propiedad literal `jquery:"1.8.0"` |
| `ui3/static/lib/jquery.min.js` | jQuery 2.2.4 | Banner del archivo |
| `ui2/static/lib/backbone-min.js` | Backbone 1.1.2 | Propiedad `.VERSION="1.1.2"` |

La salida guarda el selector, offset y hash del miembro. Esto acredita autodeclaración, no igualdad completa con un tarball de registro. El nombre `handlebars-4.0.5.js` no se transforma en versión confirmada. Tampoco se asignan a Swagger cadenas como `2.6.11` de una dependencia interna; cada identidad requiere su propia atribución.

## Huecos que deben permanecer visibles

- JS sin mapa vinculado, fuentes sin `sourcesContent`, CSS y los tres archivos WASM de Kitsu permanecen catalogados. No se consideran cubiertos por identificar otros paquetes.
- Swagger UI2/UI3 carecen aquí de un lock de build acreditado. Los mapas ayudan a localizar código, pero sus nombres no resuelven versiones de todas las dependencias, incluidas copias anidadas dentro de bundles de proveedores.
- El catálogo no decide qué UI de Swagger se habilitará. Conserva ambas presentes en la rueda, sin excluir archivos antiguos ni declarar ausente código porque una configuración futura pudiera no servirlo.
- No se consultó OSV ni otro scanner en esta unidad, no hay conteo de vulnerabilidades aprobado y no se eliminan ni reempaquetan bibliotecas.

La siguiente SCA deberá separar las tres versiones autodeclaradas de los candidatos del lock y de componentes aún sin versión. Para elevar un candidato a identificación comprobada se requiere evidencia adicional reproducible, por ejemplo cotejo de fuentes incorporadas con archivos autenticados del paquete y atribución del build. Un informe sin hallazgos sobre un subconjunto no cierra la cobertura del resto.

## Uso local

```powershell
# Verifica entradas e imprime un resumen; no publica archivos.
python docker/kitsu-zou/inventory_embedded_js.py

# Publica datos nuevos de forma exclusiva. Otro nombre si ya existe.
python docker/kitsu-zou/inventory_embedded_js.py --output .tools/evidence/phase4-kitsu-swagger-embedded-js-inventory-v1.json

python -W error::ResourceWarning -m unittest discover -s tests -p test_embedded_js_inventory.py -v
```

Las pruebas usan fixtures y no necesitan cachés privadas: distinguen fuentes listadas/referidas, deltas VLQ, índices inválidos, mapas inline, fuentes alteradas, versiones sin evidencia, lock como candidato, archivos sin mapa, WASM, escapes, enlaces y recursos Flasgger distintos del sdist. El inventario no habilita imagen, runtime ni despliegue.
