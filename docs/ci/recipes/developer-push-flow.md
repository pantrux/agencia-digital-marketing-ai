# Cómo verificar un cambio y preparar su entrega

El manifiesto declara Fase 7 con CD deshabilitado. El workflow valida el código con siete controles. Los comandos de Git requieren instrucción explícita del usuario.

Cuando un push alcanza las 300 rutas que devuelve la comparación de GitHub, el control documental coteja los árboles completos de los commits anterior y actual. Así verifica también las rutas añadidas, eliminadas o modificadas más allá del límite. Los archivos con una huella fijada conservan sus bytes originales mediante `.gitattributes`, incluso al pasar de Windows a Linux.

La Fase 7 añade el ciclo de producción, la entrega y la reversión a los mínimos previos del [portal y el Worker](../../specs/FASE-6-CLOUDFLARE-Y-PORTAL.md). Ejecuta `python -I -B scripts/ci/validate_phase.py --phase 7` para cotejar los mínimos acumulativos. Los resultados reales se registran en el [estado del piloto](../../../ESTADO.md#primera-producción-del-portal).

Para habilitar HyperFrames, instala las dependencias fijadas del renderer y verifica Chrome y FFmpeg en el worker de Windows. Aplica la migración 0021 de D1 antes de actualizar el Worker y habilita el selector del portal después de validar el renderer y los adaptadores de producción. En el ensayo, confirma que la vista previa muestra el vídeo con sus capas y que Elena puede revisar el MP4 antes de entregarlo.

Para habilitar la banda de títulos del montaje continuo, actualiza primero el puente GPU. Comprueba un MP4 NVENC con la fuente completa debajo de la banda y que el alfa de la gráfica ProRes conserva transparente el área del vídeo. Después actualiza Valeria y Elena. Verifica en la escaleta las muestras de inicio y cierre y en el dictamen de Elena el encuadre del vídeo publicado.

Para probar la comparación de encuadre de Elena, audita un máster con banda cuyo original ya tenga un elemento parcialmente fuera de cuadro. Comprueba que ambos archivos se cotejan al inicio, al centro y al cierre con la huella del original de la orden. El dictamen debe distinguir un encuadre heredado de uno introducido por la edición y seguir rechazando los casos que incumplen el objetivo del brief. El cambio se verifica en los adaptadores de OpenClaw.

Para verificar el p95 de entregas automáticas, compara una orden entregada sin intervención con otra reanudada tras un ajuste humano. Coteja la guarda y el historial de ajustes en D1 antes de comparar el recuento y el percentil por entorno con Prometheus. Ambas órdenes deben aparecer en el total de entregas. Sólo la automática debe influir en el p95 y en el recuento bajo 360 s.

Al cambiar el aprovisionador de DSM, el monitor de D1 o la cola de la bóveda, ejecuta además `python -m pytest tests/test_r2_sync_provision.py tests/test_d1_backup_metrics.py tests/test_vault_changes.py`. Las pruebas por descriptor del aprovisionador se ejecutan en Linux. Para cambiar el manifiesto o el pipeline, valida también la lista del cambio con `python scripts/ci/check_docs.py --changed-files <archivo-json>` e incluye esta receta y el diagrama.

Si Prometheus y Alertmanager quedan detenidos tras reiniciar DSM, usa `plan-start` para comprobar los contenedores, las bibliotecas fijadas y la sintaxis del plan. Aplica `start` con el mismo perfil y verifica la salud y TLS mutuo desde la red autorizada. [Procedimiento en DSM](../../../docker/observability/README-nas.md#recuperación-programada-en-dsm)

Para sustituir la imagen de PostgreSQL de aplicaciones, coteja el Compose resuelto y los pines de [n8n](../../../docker/n8n/README-private-deploy.md) y del [respaldo diario](../../../sre/README-backup-321-orchestrator.md). La aceptación en la VM exige conservar PGDATA, confirmar las conexiones de Kitsu y n8n y comprobar el perfil actualizado antes de la siguiente copia programada.

Antes de promover PostgreSQL 16.16, verifica el manifiesto oficial y el candidato por digest. Ensaya un clúster desechable con dos arranques, autenticación SCRAM y persistencia. Revisa por base de datos las extensiones C y los índices afectados por los cambios publicados. Immich requiere recompilar sus extensiones contra los headers de 16.16 y repetir la aceptación vectorial. [Notas de PostgreSQL 16.16](https://www.postgresql.org/docs/16/release-16-16.html)

En Immich, repite la actualización en una copia fría antes de publicar. Comprueba que el Compose resuelto cambia sólo la imagen de PostgreSQL y que el capturador desde el nuevo CID genera un volcado con SHA y trama verificables. Tras el arranque, coteja el inventario de los proyectos y la salud de los contenedores. [Contrato LAN de Immich](../../../docker/immich-server/README-LAN.md) · [Verificación de proyectos](../../../docker/README-phase4-runtime.md)

Si el formato cambia un script que otro operador autentica por SHA256, coteja la equivalencia del código y actualiza las referencias de las fuentes locales. En destino, prepara y verifica el conjunto de scripts y sus pines en la misma ventana de mantenimiento. Los comprobantes existentes siguen identificando los bytes que se ejecutaron.

Para cambios en las reglas del receptor de métricas, valida los casos de fallo, ausencia y recuperación con `promtool test rules`. Después de la recarga, comprueba el estado de las reglas y la edad de las muestras por mTLS. [Procedimiento del NAS](../../../docker/observability/README-nas.md#alertas-de-recolección-en-prometheus)

Para ensayos del codificador facial, coteja las fuentes fijadas de Immich y prueba el formato real por API con un corpus desechable. Registra las particiones y los umbrales antes de inferir. [Aceptación facial](../../../docker/immich-server/README-ACEPTACION-FACIAL.md#ensayo-de-vistas-previas-webp-sin-pérdida)

Para probar la llegada de fotos a grupos existentes, fija tres tandas en el manifiesto sintético y coteja las asignaciones acumuladas después de drenar las colas de cada una. [Sonda por manifiesto](../../../docker/immich-server/README-ACEPTACION-FACIAL.md#espacio-y-orden-de-ejecución)

Cuando la agrupación difiere para las mismas vistas previas, registra las consultas y asignaciones del trabajador en una instancia de ensayo. Verifica la cobertura de la traza y conserva los resultados de la consulta vectorial de sólo lectura en el recibo privado.

## Prerrequisitos

Ejecuta los comandos desde la raíz del proyecto con Node.js 24, npm, Python 3.12 y Bash para los scripts de shell. Comprueba que `pwsh --version` funciona desde la misma terminal. Pytest invoca PowerShell 7 para los contratos del puente GPU, SMB y vCenter, también en Linux. `requirements-dev.txt` fija PyYAML, Ruff, packaging, pytest y sus dependencias, Paramiko, NumPy y los contratos HTTP e imagen de `gpu-bridge-win11/requirements-tests-cpu.txt`. En Windows también instala `tzdata` para que las pruebas de respaldo resuelvan `America/Santiago`. El grafo de los runtimes CUDA se audita por separado. La bóveda local queda fuera de los archivos publicables.

## Procedimiento local

1. Persistir cualquier credencial o ID nuevo en `.env` con los tres comentarios obligatorios, actualizar `.env.example` con `CHANGE_ME` y después inyectar en destino. La revisión local de Greptile usa `GREPTILE_API_KEY` desde la bóveda. El aprovisionador de Kitsu consulta el registro de todas las variables al regenerar la plantilla. `DAVINCI_MCP_TOKEN` se reconoce por su nombre exacto y documenta la conexión de Lucas entre Windows y OpenClaw.

Para activar la recepción de másteres y kits largos en Kitsu, aplica `docker/kitsu-zou/kitsu-studio-upload-limit.conf` dentro de la ubicación de previsualización y adjuntos del proxy de Kitsu. Valida el archivo activo con `nginx -t` antes de recargar. El adaptador limita el medio a 3 GiB, conserva las comprobaciones de huella y prepara el cuerpo en disco.
2. Instalar dependencias reproducibles y ejecutar los controles del código presente:

   ```bash
   npm ci
   npm --prefix gpu-bridge-win11/render-worker ci --ignore-scripts
   python -m pip install -r requirements-dev.txt
   python scripts/ci/check_env.py
   npm run lint
   ./node_modules/.bin/eslint gpu-bridge-win11/render-worker/src gpu-bridge-win11/render-worker/composition gpu-bridge-win11/render-worker/tests
   python -m ruff check scripts tests sre vcenter storage gpu-bridge-win11 docker openclaw-agency-setup
   python -m ruff format --check scripts tests sre vcenter storage gpu-bridge-win11 docker openclaw-agency-setup
   npm run typecheck
   npm --prefix gpu-bridge-win11/render-worker run typecheck
   npm test
   npm --prefix gpu-bridge-win11/render-worker test
   python scripts/ci/prepare_test_fixtures.py
   python -m pytest tests -m 'not evidencia_operativa' -p no:cacheprovider --disable-plugin-autoload
   python scripts/ci/validate_phase.py
   if [ -f docker/docker-compose.yml ]; then
     python scripts/ci/check_compose_base.py
   fi
   python scripts/ci/check_docs.py
   npm audit --audit-level=high
   npm --prefix gpu-bridge-win11/render-worker audit --audit-level=high
   ```

   En PowerShell, el comando ESLint del renderer puede ejecutarse como `npx --no-install eslint gpu-bridge-win11/render-worker/src gpu-bridge-win11/render-worker/composition gpu-bridge-win11/render-worker/tests` desde la misma raíz.

   La comprobación de Compose requiere Docker CLI con el complemento Compose disponible desde una configuración temporal limpia. El validador usa `docker/compose.synthetic.env` y ejecuta `docker compose config --quiet`. Su resultado acredita la configuración declarada. El control 5 instala PyYAML 6.0.3 y exige esta comprobación cuando existe `docker/docker-compose.yml`.

3. Instalar las herramientas fijadas y ejecutar también los controles de secretos y sintaxis. En Windows sus ejecutables tienen extensión `.exe`:

   ```bash
   python scripts/ci/install_tools.py --tools gitleaks actionlint shellcheck trivy
   ./.tools/bin/gitleaks dir . --config .gitleaks.toml --redact --no-banner --timeout 120
   ./.tools/bin/actionlint .github/workflows/ci-cd-media-platform.yml
   ./.tools/bin/shellcheck sre/*.sh vcenter/*.sh storage/*.sh
   ./.tools/bin/trivy fs --scanners vuln,misconfig --include-dev-deps --severity HIGH,CRITICAL --exit-code 1 --skip-dirs .git --skip-dirs .tools --skip-dirs node_modules --skip-dirs coverage --skip-files .env .
   ```

   El workflow contiene la configuración completa de cada control; una falla de descarga o del analizador debe corregirse, no omitirse.

   Para analizar una copia temporal dentro de `.tools`, cambia a la raíz de esa copia y pasa `dir .` a Gitleaks. Así la exclusión de `.tools` no alcanza la ruta absoluta de la copia. Comprueba que el resumen indique bytes analizados. Las excepciones de `generic-api-key` requieren tanto la ruta como el valor exacto de la huella o del dato de prueba. El control positivo añade credenciales ficticias a una copia de un archivo con excepción y comprueba que se detecten antes de descartar esa copia.

   En Windows, Trivy 0.76.2 puede perder la letra de unidad al extraer la base descargada en el directorio temporal del perfil. Estas rutas absolutas dentro del proyecto evitan el fallo sin omitir la actualización ni la validación:

   ```powershell
   New-Item -ItemType Directory -Force -Path '.tools\tmp','.tools\trivy-cache' | Out-Null
   $env:TEMP=(Resolve-Path -LiteralPath '.tools\tmp').Path
   $env:TMP=$env:TEMP
   .\.tools\bin\trivy.exe --cache-dir (Resolve-Path -LiteralPath '.tools\trivy-cache').Path fs . --db-repository mirror.gcr.io/aquasec/trivy-db:2 --scanners vuln,misconfig --include-dev-deps --severity HIGH,CRITICAL --exit-code 1 --timeout 3m --skip-dirs .git --skip-dirs .tools --skip-dirs node_modules --skip-dirs coverage --skip-files .env --no-progress
   ```

   `--include-dev-deps` incorpora las dependencias de desarrollo de esta fase. Verificar que el informe incluya los locks de raíz y renderer; una tabla vacía no acredita la auditoría de esas dependencias. La corrida observada de Trivy cubrió ambos locks Node y no demuestra cobertura Python. Si todavía no existen manifiestos Docker u otros archivos admitidos, la columna de configuración aparece como no analizada.

   En `vm-media-01`, la base de Trivy puede ocupar más espacio que el paquete descargado. Si el volumen raíz tiene poco espacio, ejecuta Trivy en una sesión SSH con un directorio temporal privado en `/dev/shm`. Usa subdirectorios de esa misma ruta para `--cache-dir` y `TMPDIR`, comprueba la memoria libre y retira el directorio de ensayo al terminar. Conserva el informe JSON fuera de la caché.

   Auditar también el grafo Python fijado, en un entorno separado para la herramienta. En Linux/CI:

   ```bash
   python -m venv .tools/venvs/python-audit
   .tools/venvs/python-audit/bin/python -m pip install pip-audit==2.10.1
   mkdir -p .tools/evidence
   .tools/venvs/python-audit/bin/python -m pip_audit --strict --progress-spinner off --vulnerability-service pypi --requirement requirements-dev.txt --format json --output .tools/evidence/python-sca.json
   .tools/venvs/python-audit/bin/python scripts/ci/check_python_audit.py --report .tools/evidence/python-sca.json
   ```

   En Windows, usar el ejecutable `Scripts/python.exe` del mismo entorno:

   ```powershell
   python -m venv .tools/venvs/python-audit
   $pythonAuditoria = '.tools/venvs/python-audit/Scripts/python.exe'
   & $pythonAuditoria -m pip install pip-audit==2.10.1
   New-Item -ItemType Directory -Force -Path '.tools/evidence' | Out-Null
   & $pythonAuditoria -m pip_audit --strict --progress-spinner off --vulnerability-service pypi --requirement requirements-dev.txt --format json --output .tools/evidence/python-sca.json
   & $pythonAuditoria scripts/ci/check_python_audit.py --report .tools/evidence/python-sca.json
   ```

   Exige salida cero de ambos comandos. El validador coteja 33 pines en Linux y 34 en Windows, incluido `tzdata`, y rechaza diferencias, omisiones y vulnerabilidades conocidas. El auditor y el validador usan el mismo entorno separado del entorno de pruebas.

   Audita los grafos GPU por separado. Comprueba la cobertura de cada uno antes de aprobar el control 6.

   | Grafo | Fuente y condición de aprobación |
   |---|---|
   | Métricas | Cuatro paquetes fijados, consultados en OSV sin instalarlos en el entorno de la herramienta. |
   | Immich: ejecución y construcción | `gpu-bridge-win11/immich-artifacts.json` fija 70 artefactos runtime: 69 consultados en OSV y una excepción de fuente. Los ocho build se auditan aparte. No fusionar `packaging==23.2` de runtime con `packaging==26.3` de build. Construir los dos paquetes de fuente en un entorno distinto y verificar las ruedas derivadas antes de instalarlas en runtime. |
   | Fuente `immich-model==0.2.0` | Excepción explícita de paquete no indexado: fuente oficial, commit, URL y SHA256 exactos; dependencias cubiertas por los grafos. No informar este paquete como consultado satisfactoriamente en PyPI u OSV. |
   | ORT CUDA 12 | `onnxruntime-gpu==1.27.1` procede del feed Microsoft fijado por SHA256 y queda incluido en las 69 consultas OSV de runtime por nombre y versión. Fallo de consulta, versión omitida o vulnerabilidad bloquean. |
   | Whisper | `gpu-bridge-win11/requirements-whisper.lock` y su manifiesto deben coincidir con las 94 ruedas fijadas para Windows/Python 3.12; auditar el grafo completo. |
   | Renderer | Usar su `package-lock.json`, instalación sin scripts y `npm audit --audit-level=high`, además de la auditoría npm de la raíz. |

   Generar entradas auditables desde los manifiestos, consultar los cuatro grafos y validar su cobertura. En Linux/CI, usando el entorno de auditoría ya preparado:

   ```bash
   python scripts/ci/check_gpu_dependencies.py prepare --output .tools/evidence/gpu-dependencies
   for graph in metrics immich-runtime immich-build whisper; do
     .tools/venvs/python-audit/bin/python -m pip_audit --strict --no-deps --disable-pip --vulnerability-service osv --progress-spinner off --requirement ".tools/evidence/gpu-dependencies/$graph.requirements.txt" --format json --output ".tools/evidence/gpu-dependencies/$graph.json" 2> ".tools/evidence/gpu-dependencies/$graph.stderr.txt" || exit 1
   done
   python scripts/ci/check_gpu_dependencies.py validate --inventory .tools/evidence/gpu-dependencies/inventory.json --reports-dir .tools/evidence/gpu-dependencies
   ```

   En Windows, sustituir el bucle por este bloque después del mismo comando `prepare`; `$pythonAuditoria` es el ejecutable del entorno aislado definido arriba:

   ```powershell
   foreach ($grafo in @('metrics', 'immich-runtime', 'immich-build', 'whisper')) {
       & $pythonAuditoria -m pip_audit --strict --no-deps --disable-pip --vulnerability-service osv --progress-spinner off --requirement ".tools/evidence/gpu-dependencies/$grafo.requirements.txt" --format json --output ".tools/evidence/gpu-dependencies/$grafo.json" 2> ".tools/evidence/gpu-dependencies/$grafo.stderr.txt"
       if ($LASTEXITCODE -ne 0) { throw "Falló la auditoría del grafo $grafo." }
   }
   python scripts/ci/check_gpu_dependencies.py validate --inventory .tools/evidence/gpu-dependencies/inventory.json --reports-dir .tools/evidence/gpu-dependencies
   ```

   El validador recompone las fuentes y sus hashes y exige correspondencia exacta de cada nombre/versión entre inventario e informes. También exige los cuatro archivos `stderr.txt`: rechaza avisos OSV con esquema incompatible o sin lista `affected`, aunque pip-audit termine con salida cero. Sólo entonces emite `receipt.json`, con hashes del inventario, cuatro JSON y cuatro diagnósticos. Regenerar los informes en cada corrida: un reporte que omita versiones o conserve un grafo anterior no acredita la cobertura actual. Estos comandos consultan versiones declaradas sin instalar los runtimes Windows en el runner Linux.

   Los informes sólo acreditan las dependencias enumeradas y verificadas. No descargan pesos ni prueban CUDA o NVENC. El manifiesto Immich mantiene `buffalo_l` bloqueado para SaaS facial comercial hasta resolver una licencia independiente; el permiso del código no se extiende automáticamente a los pesos.
4. Si se modifica un workflow, manifiesto o contrato SRE, actualizar en el mismo cambio esta receta, el [diagrama](../diagrams/pipeline-overview.mmd) y el procedimiento afectado. Registrar evidencia real y dependencias pendientes en `ESTADO.md`.

## Controles del portal de Fase 6

Ejecutar los controles según los componentes presentes. Los tipos del Worker se incluyen en `npm run typecheck` de raíz y sus pruebas en `npm test`. Las pruebas de migraciones usan SQLite local y las de ingreso y Sofía usan datos sintéticos. Mantener las credenciales de producción fuera de la comprobación.

Las pruebas de carga múltiple importan el módulo de carga del portal. Por ello, antes de los tipos o pruebas de raíz hay que instalar también su lock: `npm ci --ignore-scripts --no-audit --prefix cloudflare-saas/studio-portal-web`. Los controles 3 y 4 de GitHub lo hacen de forma independiente. La experiencia conversacional requiere además las migraciones `0022`–`0026`, el Worker de referencias aislado y consumidores compatibles; los pasos y la verificación previa a activación se describen en [operación de Studio](../../operations/STUDIO-EXPERIENCIA-CONVERSACIONAL.md).

Para aceptar un cambio del servicio de revisión de Studio, comprueba que Valeria reciba los defectos, las reglas incumplidas y la solicitud del titular dentro de las rondas de calidad del plan. La solicitud conserva su reserva y el kit anterior en todas las rondas. Si el equipo consigue una aprobación, verifica que se publique únicamente esa versión y que se pueda usar como base de otra revisión. Si agota las rondas, comprueba que el portal muestre el estado y conserve la entrega previa. La unidad tiene un límite de cuatro horas para las rondas y el empaquetado. El control 4 cubre los contratos de publicación y el rechazo de un dictamen de vista previa alterado.

El control 4 llama también al punto de entrada de la unidad y verifica que una cola vacía devuelva `idle`. Confirma el arranque de esa unidad contra el entorno de ensayo antes de habilitar su temporizador.

El control de infraestructura ejecuta la misma comprobación de D1 desde una carpeta temporal. Wrangler aplica las migraciones ordenadas sobre la base ficticia y un segundo pase coteja que el registro de migraciones siga idéntico. Ambos pases verifican integridad y claves foráneas. [Migraciones locales de D1](https://developers.cloudflare.com/d1/wrangler-commands/#d1-migrations-apply)

Para habilitar el montaje por escenas, comprobar el despacho de `output_edit` y el rechazo de vista previa antes del máster. Aplicar la migración `0018_production_edit.sql` y verificar el receptor, los agentes y el puente antes de publicar el selector del portal.

Para el anuncio por guion, aplicar primero `0019_brief_ad_cta.sql`, verificar que los briefs anteriores siguen respondiendo y probar el CTA, el reordenamiento de tomas y la aprobación de vista previa con una orden MP4 de ensayo. Publicar la interfaz después del Worker, los agentes y el renderer. Antes de habilitar anuncios, provisionar `_biblioteca_global/musica-stock/carefree-usuan1400037.mp3` desde la ficha oficial de [Carefree](https://www.incompetech.com/music/royalty-free/index.html?isrc=USUAN1400037) y cotejar la huella definida por el renderer. Verificar con una orden de ensayo el nivel de música al cierre y el crédito en el ZIP.

Para aceptar el piloto de acabado en Resolve, preparar el OTIO y los cinco archivos vinculados al recibo en un destino dedicado. Cotejar la importación de 900 fotogramas y las pistas de voz y música en una estación con Resolve Studio activado. Tras exportar, ejecutar el revisor de Elena contra la huella del MP4 producido y conservar su dictamen. Las pruebas de GitHub cubren el contrato y la comparación de la música. El ensayo del editor se registra por separado. Consulta el procedimiento en `docs/specs/ENTREGAS-FASE-6.md`.

```bash
./node_modules/.bin/eslint cloudflare-saas/studio-api-worker/src
python -m ruff check ingress
python -m ruff format --check ingress
npm exec --yes --package=wrangler@4.162.0 -- python scripts/ci/check_d1_migrations.py
python -m pytest tests/test_phase.py tests/test_d1_resource_owner.py tests/test_d1_saas_schema.py tests/test_phase6_ingress.py tests/test_sofia_broker.py -p no:cacheprovider --disable-plugin-autoload
node --test tests/test_sofia_owner_plugin.mjs tests/test_mateo_isolated.mjs

(
  cd cloudflare-saas/studio-portal-web
  export NEXT_TELEMETRY_DISABLED=1
  npm ci --ignore-scripts --no-audit
  ./node_modules/.bin/next typegen
  ./node_modules/.bin/tsc --noEmit
  npm --ignore-scripts run build
  test -s out/index.html
)

mkdir -p .tools/evidence
npm --prefix cloudflare-saas/studio-portal-web audit --package-lock-only --audit-level=high --json > .tools/evidence/studio-portal-npm-audit.json
```

`next typegen` y el build generan archivos locales. La comprobación de tipos usa el `tsconfig.json` del portal y el resultado esperado del build es `out/index.html`. La auditoría consulta el registro npm y usa el lock propio del portal, incluidas las dependencias de desarrollo. El workflow compara el total informado con las entradas del lock, excluida la raíz, y registra las huellas del manifiesto, lock e informe. Verificar los ingresos reales, enlaces Pages/Worker y servicios externos mediante sus procedimientos operativos. [BunkerWeb y NPM](../../../ingress/README.md) · [CLI de Next.js](https://nextjs.org/docs/app/api-reference/cli/next#next-typegen-options) · [Auditoría npm](https://docs.npmjs.com/cli/v11/commands/npm-audit/).

## Resultado esperado

Para aceptar el provisionador NAS de carga múltiple, envía dos materiales firmados bajo la misma orden y coteja la identidad de la carpeta, las dos huellas y el reintento. [Procedimiento del consumidor](../../../docker/r2-sync-consumer/README-multiple-sources.md)

Los contratos de almacenamiento utilizan `192.168.0.8:/volume2/producciones` hacia `/mnt/producciones` y el recurso SMB `producciones` hacia `P:`, según [ADR-001](../../decisions/ADR-001-volumen-producciones.md). Actualizar juntos contrato, documentación y pruebas cuando cambie el origen; un nombre local de montaje idéntico no acredita el servidor correcto. Las pruebas locales no crean el recurso ni montan NFS/SMB; la evidencia real de Fase 2 se registra por separado y debe preservar `/volume2/NFS`.

El control de Fase 4 exige las fuentes del supervisor R2 y sus unidades. La aceptación en destino coteja el CID, la exportación NFSv4.1 y su marcador antes de iniciar el contenedor. [Operación del consumidor R2](../../../docker/README-r2-service.md)

El control exige la receta y los perfiles de Alertmanager para el NAS. La aceptación en destino comprueba el filtro de red, TLS mutuo desde Prometheus y el acceso SMTP con verificación del servidor. [Observabilidad en Synology](../../../docker/observability/README-nas.md)

Todos los controles aplicables terminan con salida cero. El control 5 valida la fase declarada, admite 0 a 7 y exige CD deshabilitado. `--phase N` exige coincidencia con el manifiesto vigente. `python -m unittest discover -s tests -p 'test_phase.py'` usa manifiestos sintéticos y coteja los mínimos acumulativos hasta producción, publicación y recuperación. Una fase sin implementar, un archivo mínimo ausente o una omisión en `required_files` bloquean. Investigar una falla intermitente antes de repetir la prueba.

Para revisar localmente los contratos PowerCLI, con la cadena del SDK fijada en el workflow disponible, ejecutar `./vcenter/Test-PowerCLIContracts.ps1` desde PowerShell. Solo analiza metadatos y no conecta al laboratorio. CI conserva ese mismo control en Fases 1 a 7 e instala los módulos fijados si faltan. Registrar por separado las pruebas reales de VM, red, Docker, runner y snapshot antes de elevar `phase`.

## Qué ocurre cuando GitHub esté configurado

Una solicitud de cambio hacia `main`, una actualización de `main` o la ejecución manual disparan los siete controles en paralelo. El control 4 instala `requirements-dev.txt` antes de ejecutar pytest e incluye las pruebas Node del renderer. Los trabajos 2 y 3 revisan los componentes presentes, incluidos ingress, Worker y portal. El trabajo 5 utiliza la fase del manifiesto y conserva el contraste con PowerCLI `13.5.0.25380678` en Fases 1 a 7, sin secretos ni conexión al laboratorio. El trabajo 6 conserva separados los grafos de desarrollo, métricas, Immich runtime/build y Whisper, consulta ORT dentro de runtime OSV y audita los locks npm de raíz, renderer y portal presente. `quality-gates` solo aprueba si los siete terminan correctamente. La protección de rama deberá exigir ese resultado. Preview, salvaguardas, entrega híbrida y verificación posterior se incorporarán al alcanzar sus fases, sin activar `deployment_enabled` anticipadamente.

El control 6 intenta conservar sus informes durante 14 días aun cuando un paso falle: `python-sca.json`, el directorio `gpu-dependencies/`, `renderer-npm-audit.json` y `renderer-npm-receipt.json`, bajo `.tools/evidence/`. El recibo npm vincula los hashes de `package.json`, `package-lock.json` y el reporte con el umbral `high`. La carga usa rutas explícitas y no incluye la bóveda. Revisar esos artefactos y los diagnósticos del trabajo para resolver un fallo; su mera existencia no acredita seguridad aprobada.

Los runners públicos no acceden a `.6`, no reciben el token del puente y no cargan CUDA, pesos ni servicios. El [cierre operativo de Fase 3](../evidence/2026-11-05-fase-3.md) acredita CLIP/rostros y transcripción CUDA, MP4 NVENC y ProRes 4444 con alfa en 1080p/4K, cuatro servicios saludables y consultas LAN autenticadas. La concurrencia final respondió `[200, 200, 429]`, con máximo global NVIDIA observado de dos sesiones. El certificado `gpu-final-health-snapshot.json` verifica fuentes instaladas y hashes NAS; el responsable declaró `phase: 3` tras revisarlo, conservando `deployment_enabled: false`. Estas mediciones operativas separadas no acreditan p95, reinicio automático de Windows ni una corrida remota del workflow.

Los contratos Node del renderer comprueban también compilación estática única por proceso, reutilización concurrente, medios temporales aislados por UUID y props/resolución dinámicas. La regla QA conserva `Y=0..1920` normalizado en 4K y devuelve escala y dimensiones originales. Estas pruebas controladas no miden el rendimiento real de la caché ni publican el bundle del servicio.

Para la primera actualización de un repositorio nuevo, el control 7 obtiene el commit y su árbol completo por API de solo lectura: exige identidad coincidente, respuesta sin truncar y presencia del workflow, esta receta y el diagrama. No necesita historial local ni ejecutar comandos Git.

Si falla la calidad, consultar el registro del trabajo y corregir la causa. Si falla una entrega futura, usar la [receta SRE](sre-rollback.md) y el [índice de incidentes](../README.md).

Para el CTA social, aplica `0020_brief_ad_contacts.sql` antes de desplegar
el Worker. Actualiza la validación de briefs en el despacho y los agentes,
el renderer y después el portal. Verifica que el brief devuelve al titular
los tres campos y que el despacho sólo transfiere el canal seleccionado.
Comprueba los fundidos, el icono y la URL en un MP4 de ensayo.

Para cambios en `resolve-audio-lucas`, coteja la huella del manifiesto de
skills. La conexión real de OpenClaw con Resolve Studio y la revisión del
vídeo exportado se comprueban en el entorno del proyecto.

Para el ProRes 4444 largo, verifica un MOV 4K de varios GiB en el volumen
temporal del renderer. La lectura íntegra de las marcas de los paquetes
tiene hasta 180 segundos dentro del plazo general del render. Comprueba
que se conservan todos los fotogramas, la transparencia y el audio de la entrega.

Para las colas de Mateo y Lucas, verifica dos órdenes listas en el mismo
disparo. Si la primera falla antes de 15 segundos, registra su reintento
y comprueba que la siguiente avance en ese disparo. La unidad conserva
la señal de error y continúa aplicando la espera de cada orden.

Para verificar la medición multipart, abre una carga de ensayo, repite
la misma apertura y comprueba una sola muestra de las tres etapas. Coteja
la preparación, R2 y la confirmación en la serie `ensayo` de Analytics Engine.
