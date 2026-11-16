# Estándar CI/CD y confiabilidad SRE

Índice canónico para desarrollar, verificar y operar la Agencia Digital de Marketing AI conforme a la arquitectura v4.1. Responsable: equipo de plataforma / SRE. Última revisión: 2026-11-06.

La ubicación NAS vigente es `/volume2/producciones`, por [ADR-001](../decisions/ADR-001-volumen-producciones.md). El origen NFS es `192.168.0.8:/volume2/producciones`; el recurso SMB conserva el nombre `producciones`. Los ~2753,63 GiB libres observados en `/volume2` el 2026-11-05 no son una reserva exclusiva. El recurso, los dos montajes y la lectura compartida ya tienen [evidencia real de Fase 2](evidence/2026-11-05-fase-2.md); su cierre fue confirmado el 2026-11-05, con 220 pruebas Python y controles finales aprobados. Se conserva el datastore existente `/volume2/NFS`.

## Alcance vigente y base de Fase 0

La Fase 0 preparó controles locales, el workflow de calidad, cinco procedimientos de incidentes y scripts de salud y reversión ensayados con simulación. El manifiesto `config/phase-capabilities.json` declara `phase: 7` con `deployment_enabled: false` y exige las fuentes del ciclo de producción y reversión, además del portal y del Worker. La [evidencia real de Fase 3](evidence/2026-11-05-fase-3.md) acredita inferencia CUDA, MP4 NVENC y ProRes 4444 con alfa en 1080p/4K, salud autenticada y convivencia.

Los siete controles son bloqueantes dentro del workflow. La protección efectiva de `main`, la ejecución en GitHub y los accesos del runner requieren configuración del repositorio en fases posteriores. No se ha autorizado ejecutar comandos de Git en esta sesión. No activar entrega continua ni publicar una vista previa hasta disponer de sus recursos aislados, salvaguardas y pruebas reales.

La [Fase 4 de aplicaciones y copias](../specs/FASE-4-APLICACIONES-Y-BACKUPS.md) pasó los criterios operativos del plan. Se acreditaron 27 roles, los logins de Immich y Kitsu, el receptor Prometheus en NAS y la restauración del conjunto real de copias desde NAS y R2. El cron y la retención de R2 quedaron instalados. La observación del primer disparo automático, Grafana y el piloto facial previo a medios de clientes se registran en el estado. Ruff incluye `docker/`. [Estado operativo](../../ESTADO.md#qué-falta-para-cerrar-la-fase-4)

## Documentos por audiencia

| Documento | Audiencia | Cuándo leerlo |
|---|---|---|
| [Flujo del desarrollador](recipes/developer-push-flow.md) | Desarrollo / agente IA | Preparar cambios y entender los siete controles. |
| [Reversión coordinada](recipes/sre-rollback.md) | SRE / guardia | Interpretar simulaciones o recuperar una entrega con artefactos previos verificados. |
| [Diagrama del pipeline](diagrams/pipeline-overview.mmd) | Todo el equipo | Distinguir los siete controles actuales de la futura entrega continua. |
| [Topología de observabilidad](diagrams/observability-sre-topology.mmd) | Plataforma / SRE | Ubicar emisores y colectores previstos en los cinco planos. |
| [rb-01: puente GPU](runbooks/rb-01-gpu-bridge-unreachable.md) | Guardia / GPU | Puertos inaccesibles, CUDA o NVENC degradados. |
| [rb-02: migración D1](runbooks/rb-02-cloudflare-d1-migration-fail.md) | Guardia / datos | SQL fallido o incompatibilidad Worker/esquema. |
| [rb-03: agentes](runbooks/rb-03-agent-loop-or-openai-degraded.md) | Guardia / agentes | Llamadas de más de 120 s, bucles o proveedor degradado. |
| [rb-04: R2–NAS](runbooks/rb-04-r2-nas-sync-backlog.md) | Guardia / almacenamiento | Cola acumulada, NFS ausente o archivos incompletos. |
| [rb-05: PostgreSQL](runbooks/rb-05-kitsu-immich-db-recovery.md) | Guardia / datos | Restaurar Kitsu o Immich desde una copia verificada. |

Cada runbook distingue resultados esperados de evidencia observada. La última ejecución real se registra al realizar el procedimiento; su existencia no equivale a un simulacro aprobado.

Al crear el repositorio, la primera actualización no tiene versión anterior para comparar. El control documental consulta por API de solo lectura el commit recibido y su árbol completo, exige SHA coincidente y `truncated: false`, y comprueba que workflow, receta y diagrama existan como archivos. Una respuesta incompleta o sin esos documentos bloquea. En actualizaciones posteriores compara versiones; en solicitudes de cambio pagina todos los archivos y comprueba que la solicitud no cambió durante la consulta. No ejecuta comandos Git para ninguna de estas verificaciones.

## Los siete controles de calidad

El workflow `.github/workflows/ci-cd-media-platform.yml` ejecuta siete trabajos independientes en runners alojados por GitHub y un trabajo final que exige éxito de todos:

| Control | Comprobación | Límite de lo que demuestra en Fase 0 |
|---|---|---|
| 1. Secretos | Gitleaks y sincronía de la plantilla de variables | No valida accesos externos ni permite publicar `.env`. |
| 2. Estilo | ESLint en raíz, renderer y el Worker presente. Ruff y formato incluyen `gpu-bridge-win11/`, `docker/`, OpenClaw e `ingress/` cuando existe. También se revisan workflow, Bash y PowerShell. | No instala ni arranca servicios en el host GPU ni acredita un build Linux. |
| 3. Tipos | TypeScript estricto de raíz, Worker y renderer. El portal presente genera tipos de Next.js y construye la exportación estática local. | No demuestra un render real, el uso de NVENC o una publicación de Pages. |
| 4. Pruebas | Vitest, pytest y pruebas Node del renderer con dependencias fijadas. Las pruebas de Fase 6 cubren Worker, migraciones SQLite locales, ingress, broker y socket Unix privado de Sofía. | Usa escenarios aislados; no carga pesos ni acredita CUDA, NVENC o producción E2E. |
| 5. Infraestructura | `validate_phase.py` admite mínimos de Fases 0 a 7. En Fases 1 a 7 conserva el contrato PowerCLI fijado. Valida el Compose base con datos sintéticos y ensaya las migraciones D1 con Wrangler local cuando existe su configuración de ejemplo. | Acredita fuentes y configuración sintética. El laboratorio, la seguridad de las imágenes y la restauración real requieren evidencia propia. |
| 6. Seguridad | Auditoría npm de raíz, renderer y portal presente, Trivy, grafo Python de desarrollo y grafos GPU separados con pip-audit 2.10.1 aislado y cobertura exacta. | Un hallazgo alto/crítico Node o cualquier vulnerabilidad Python bloquea. Una omisión de cobertura también bloquea. No acredita inferencia ni revisión de permisos reales. |
| 7. Documentación | Enlaces, archivos obligatorios y coherencia documental | Exige actualizar recetas y diagramas cuando evolucione el pipeline. |

El objetivo arquitectónico de menos de seis minutos requiere medir una ejecución real en GitHub; no se deduce de las pruebas locales. Al avanzar de fase, los validadores deben exigir los artefactos de esa fase y fallar si faltan, sin omisiones silenciosas.

`requirements-dev.txt` fija PyYAML 6.0.3 para la configuración de red, Ruff 0.17.3 para estilo, packaging 26.2 para metadatos de paquetes y pytest 9.1.0 con sus dependencias de pruebas. Instala ese archivo con `python -m pip install -r requirements-dev.txt` antes de ejecutar los analizadores y las pruebas locales. El control 4 prepara la fuente pública de VectorChord con tamaño y SHA-256 fijados y ejecuta `python -m pytest tests -m 'not evidencia_operativa' -p no:cacheprovider --disable-plugin-autoload`. Recoge contratos de pytest y `unittest.TestCase` reproducibles desde la copia publicable. Las pruebas del finalizador de red usan archivos temporales y comandos controlados.

Las pruebas marcadas `evidencia_operativa` cotejan insumos fijados de MoneyPrinter, PostgreSQL, n8n y Compose con evidencia del laboratorio. Para la aceptación operativa, ejecuta `python -m pytest tests -m evidencia_operativa -p no:cacheprovider --disable-plugin-autoload` en el entorno local que conserva esos insumos. El comando sin filtro ejecuta ambos grupos.

El grafo incluye `storage/requirements-synology.txt` con Paramiko 5.1.0, sus seis dependencias transitivas de `requirements-python-constraints.txt`, NumPy y `gpu-bridge-win11/requirements-tests-cpu.txt` para contratos HTTP e imagen sin CUDA. La auditoría exige 33 paquetes en Linux y 34 en Windows, donde también aplica el pin de `tzdata`. El control 6 instala `pip-audit==2.10.1` en el entorno separado `.tools/venvs/python-audit` y audita los requisitos del proyecto con `--strict`, el servicio PyPI y salida JSON. En el mismo entorno, `scripts/ci/check_python_audit.py` aplica los marcadores de plataforma y exige cobertura exacta de los pines y ausencia de vulnerabilidades conocidas.

La corrida Trivy observada en Fase 2 cubrió `package-lock.json`; el cierre de Fase 3 incorporó también el lock del renderer. Trivy no acreditó Python: su auditoría independiente y el validador cerraron esa falta de cobertura, con 27 pines sin vulnerabilidades conocidas en `python-sca-phase3-complete.json`. El resultado limpio solo se registra al terminar ambos comandos satisfactoriamente; los informes y paquetes contabilizados deben conservarse como evidencia. Las [instrucciones locales](recipes/developer-push-flow.md) distinguen Windows de Linux.

La Fase 3 conserva separados el grafo de desarrollo y los entornos de cada servicio. `scripts/ci/check_gpu_dependencies.py prepare` genera inventario y requisitos de métricas (cuatro paquetes), Immich runtime (70 artefactos: 69 consultas OSV y una excepción de fuente), Immich build (ocho paquetes) y Whisper (94 ruedas Windows/Python 3.12). Cada grafo se consulta con pip-audit 2.10.1 aislado, `--strict --no-deps --disable-pip --vulnerability-service osv`; no se instalan los runtimes para consultarlos. El modo `validate` recompone las fuentes y hashes y exige correspondencia exacta entre inventario e informes antes de generar un recibo. Exige también los cuatro diagnósticos `stderr.txt`, rechaza avisos OSV con esquema incompatible o sin `affected` y registra sus hashes junto con los JSON; salida cero de pip-audit por sí sola no basta. La separación evita fusionar, por ejemplo, `packaging==23.2` de runtime y `packaging==26.3` de build.

Las dos distribuciones de fuente de Immich se convierten en ruedas en un entorno de construcción independiente, y el entorno de ejecución recibe ruedas locales verificadas sin resolver dependencias adicionales. La excepción `immich-model==0.2.0` queda fijada por fuente oficial, commit, URL y SHA256: no está indexada y no se presenta como consultada satisfactoriamente en PyPI u OSV; sus dependencias sí deben quedar cubiertas. `onnxruntime-gpu==1.27.1` procede del feed Microsoft CUDA 12 y queda incluido en las 69 consultas OSV de runtime por nombre y versión, conservando su hash de procedencia. El renderer conserva su propio `package-lock.json` y su auditoría npm. Los comandos y la cobertura exigida figuran en la [receta local](recipes/developer-push-flow.md).

El trabajo de seguridad conserva durante 14 días los informes disponibles mediante una acción fijada por commit y ejecutada con `if: always()`. Las rutas admitidas son `.tools/evidence/python-sca.json`, `.tools/evidence/gpu-dependencies/` y los pares de informe/recibo npm `renderer-npm-{audit,receipt}.json` y `studio-portal-npm-{audit,receipt}.json` bajo `.tools/evidence/`. Los recibos vinculan con SHA256 cada manifiesto npm, su lock y el reporte, con umbral `high`. Interpretar los artefactos junto con la salida de cada comando. La publicación del código y la corrida remota del workflow siguen pendientes.

La licencia del código no acredita permiso comercial para los pesos. ADR-003 selecciona ahora YuNet y únicamente AuraFace `glintr100`, bajo las licencias MIT/Apache-2.0 publicadas, con ID propio `agency_yunet_auraface_v1`; Buffalo_L conserva su registro histórico y sigue sin autorización comercial acreditada. La prueba operativa `.tools/evidence/immich-inference-real-commercial.json` registra `verified=true`, un rostro y 14,047 segundos, con perfiles CUDA de CLIP textual/visual, detección y reconocimiento y sin modelos residentes al terminar. Esa evidencia técnica y los avisos/licencias fijados se distinguen de las consultas SCA: estos trabajos públicos no cargan GPU. El cierre posterior de Fase 3 reunió además los formatos, la convivencia y la salud final; aprobar SCA por sí solo no declara una fase.

## Validación declarativa y cierres operativos

El validador admite las fases 0 a 7. Usa el número declarado en `config/phase-capabilities.json` y exige que cualquier `--phase N` coincida. Rechaza fases posteriores sin implementación, rutas ausentes o fuera del proyecto, listas que omitan los mínimos y `deployment_enabled=true`. La Fase 1 incorpora todos los archivos de Fase 0 y exige aprovisionamiento vCenter, confianza TLS, prueba aislada de cloud-init, contratos PowerCLI, snapshots, finalizador de red, bootstrap y sus pruebas. La Fase 2 hereda esos mínimos y añade auditoría/plan/aplicación NAS, clientes NFS y SMB, verificador de sesión cifrada, dependencia fijada, procedimientos, ADR y pruebas de almacenamiento. La Fase 3 hereda todo y exige fuentes, instaladores, supervisión y parada segura, firewall, manifiestos/locks, renderer, contratos y ADR facial del puente GPU.

La Fase 4 incorpora el Compose base y de aplicaciones, sus unidades, observabilidad y copias 3-2-1. El paso Compose de CI ejecuta `docker compose config --quiet` sobre la base con datos sintéticos. Las pruebas de configuración real por proyecto se describen en el [inventario operativo](../../docker/README-phase4-runtime.md).

El control de infraestructura exige las fuentes, las unidades y los verificadores de la copia integral y de las copias independientes de D1 y OpenClaw. También exige el Worker y la configuración de ejemplo que programan la orden de D1. El [estado de Fase 4](../../ESTADO.md#verificación-y-seguimientos-de-fase-4) conserva la evidencia de los disparos y de las lecturas en ambos repositorios.

El mismo control exige el aprovisionador del NAS, el monitor de respaldos D1 y los archivos de la cola y recuperación de la bóveda Windows. Sus pruebas comprueban la renumeración del dispositivo ext4, la verificación de los recibos de D1 y la conservación de las capturas pendientes.

La Fase 5 exige los cuatro servidores MCP, el plugin, catálogo, instalador y manifiestos de skills. Ruff incluye sus fuentes y ShellCheck revisa `setup.sh`. `--phase 5` comprueba los mínimos acumulativos con CD deshabilitado. La invocación de los agentes y las llamadas a servicios se verificaron en OpenClaw. [Evidencia de Fase 5](../../ESTADO.md#verificación-de-fase-5)

La Fase 6 hereda esos mínimos y exige las especificaciones del portal, facturación y entregas, la configuración de ejemplo del Worker, el portal Pages con lock propio, los perfiles de marca, las migraciones `0000`–`0015`, el renderizador ingress, el broker de Sofía y sus pruebas. El manifiesto y la suite sintética ejercitan el contrato declarativo de Fase 6. La aceptación de DNS, TLS, D1, R2, Queue, OAuth y pagos se acredita con evidencia operativa. [Especificación de Fase 6](../specs/FASE-6-CLOUDFLARE-Y-PORTAL.md) · [Procedimiento local](recipes/developer-push-flow.md#controles-del-portal-de-fase-6).

La Fase 7 hereda los mínimos de la Fase 6 y exige las fuentes y unidades del relevo, las cinco etapas de agentes, los ajustes del titular (`0016`), las observaciones de carga multipart (`0017`), el aprovisionamiento de Kitsu, el publicador, el controlador de sondas y reversión y el verificador de la bóveda. Las pruebas sintéticas cubren estos contratos. Los resultados de producción, la recuperación y el cotejo del inventario activo se registran en el [estado del piloto](../../ESTADO.md#primera-producción-del-portal).

En Fases 1 a 7, el trabajo 5 instala desde PSGallery los módulos fijados que no encuentre e importa la cadena exacta del SDK: `VMware.VimAutomation.Sdk`, `VMware.VimAutomation.Common`, `VMware.Vcf.Sso`, `VMware.Vim`, `VMware.OpenAPI`, `VMware.VimAutomation.Cis.Core` y `VMware.VimAutomation.Core`. Usa `13.5.0.25380678`, salvo `VMware.Vim`, que usa `9.1.0.25380678`. Se comprueban también las versiones cargadas porque los manifiestos originales declaran dependencias mínimas. El [paquete Core fijado en PSGallery](https://www.powershellgallery.com/packages/VMware.VimAutomation.Core/13.5.0.25380678) identifica la versión de referencia.

`Test-PowerCLIContracts.ps1` analiza el código y los parámetros del SDK instalado; no invoca los comandos de infraestructura. CI exige resultado válido, comandos comprobados y `InfrastructureContacted=false`, sin credenciales de producción ni red hacia el laboratorio. Mientras el manifiesto declare Fase 0, este paso de Fase 1 permanece sin ejecutar. La instalación y ejecución en un runner Linux remoto todavía requieren una corrida real; la validación local del SDK no demuestra esa corrida ni el objetivo de tiempo del pipeline.

El cierre operativo de VM acredita hardware 6 vCPU/16 GB/80 GB, arranque cloud-init, IP y SSH, Docker Compose, runner registrado con UID 1000 y snapshot `01-vm-limpia` en vCenter. Los IDs, credenciales y archivos de identidad se respaldan antes de utilizarlos. La declaración `phase: 7` conserva los mínimos acumulados desde la Fase 0.

El certificado funcional final `gpu-final-health-snapshot.json` registra `functional_verified=true`, 17 fuentes nativas y 20 del renderer verificadas, 14 artefactos NAS con SHA256 y cuatro servicios saludables. Las pruebas LAN conservaron ocho respuestas 200 autenticadas y ocho 401 con Bearer incorrecto. El renderer final completó MP4 y ProRes 4444 en 1080p/4K con audio conforme y alfa real en ProRes; la concurrencia respondió `[200, 200, 429]`, con máximo global NVIDIA observado de dos sesiones. Son resultados operativos separados del CI y mediciones puntuales, sin acreditar p95, reinicio automático de Windows ni entrega continua. El responsable certificó el cierre tras revisarlos; el verificador no modifica la fase por sí mismo.

## Gobernanza de secretos

La fuente maestra es `D:\Proyectos\Agencia Digital de Marketing AI\.env`. Toda credencial o ID nuevo se persiste allí con `# Para qué:`, `# Dónde:` y `# Riesgo al rotar:`, después se añade su clave con `CHANGE_ME` a `.env.example`, y finalmente se inyecta en el servicio destino. La plantilla no contiene valores reales.

El control CI usa `python scripts/ci/check_env.py --template-only` sin leer credenciales de producción; la comprobación local sin esa opción revisa la bóveda y su sincronía sin imprimir valores. No cargar credenciales de producción en trabajos de solicitudes de cambio. El token del puente, del gateway y de APIs internas debe entrar al verificador mediante un entorno protegido; no usar trazas de shell (`set -x`) ni publicar cuerpos HTTP sin depuración de datos sensibles.

## Salud, recuperación y evidencia

Los scripts `sre/smoke-test-e2e.sh` y `sre/auto-rollback.sh` delegan el control a Python 3. El presupuesto conjunto es de 85 segundos, para mantener margen respecto al objetivo arquitectónico menor a 90 s; incluye sondeo, reversión y verificación. Si se consume el presupuesto o una acción falla, la salida es de error, no una recuperación presunta. El cumplimiento real requiere medir servicios y artefactos de producción en Fase 7.

La reversión v2 usa un manifiesto de la entrega previa y su hash de confianza aportado por separado. Activa la versión estable de Cloudflare, restaura servicios Compose acotados y verifica la recarga en caliente de las etiquetas de OpenClaw que enumera el manifiesto. La versión 1 mantiene bloqueado el reemplazo del archivo completo. Los datos, Pages y paquetes Windows tienen procedimientos específicos. [Alcance, ensayo y operación](recipes/sre-rollback.md)

Conservar en `ESTADO.md` el comando ejecutado, su resultado, fecha y limitaciones. Para incidentes incluir ID de pedido, componente, versión y duración; ocultar secretos y datos privados antes de adjuntar registros.
