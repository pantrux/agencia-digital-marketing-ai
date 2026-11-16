# Revisión de skills públicas de Fase 5

El [manifiesto](manifest.json) fija los 22 puntos de entrada de los seis orígenes públicos de la arquitectura. Incluye sus referencias, ejemplos y utilidades auxiliares, además de las licencias y las instrucciones de instalación disponibles que corresponde revisar. Conserva la disposición del repositorio de origen, incluida la referencia de `ad-creative` a `skills/ads/references/meta-decision-system.md`. Ese archivo es una referencia, y `ads` no se registra como skill. Las pruebas de evaluaciones de Corey quedan fuera del lote.

## Preparar la revisión

Ejecutar desde la raíz con un directorio nuevo y privado fuera del workspace de OpenClaw:

```powershell
$destinoRevisionSkills = Join-Path ([IO.Path]::GetTempPath()) ('agency-skills-' + [guid]::NewGuid().ToString('N'))
python -I -B openclaw-agency-setup/upstream-skills/review_skills.py --transport zip --output $destinoRevisionSkills
```

`--transport raw` consulta cada archivo en `raw.githubusercontent.com`. `--transport zip` consulta el ZIP del mismo commit en `codeload.github.com`, verificado desde Windows cuando el primer host rechazó la conexión. Ambos comprueban SHA-256 y tamaño por archivo. La variante ZIP abre en memoria las entradas exactas declaradas y comprueba su tipo antes de leerlas. El directorio se crea después de validar todo el lote. Las utilidades de origen quedan como archivos de revisión sin conservar sus permisos de ejecución. El recibo `receipt.json` identifica manifiesto, ruta de transporte, commits y número de archivos. Su huella corresponde al JSON canónico del manifiesto con claves ordenadas, sin espacios y codificado como UTF-8.

Para preparar la instalación privada, partir del directorio identificado por cada archivo `SKILL.md` y cotejar que los auxiliares relativos llegan al workspace que corresponda según la arquitectura. Conservar la referencia a `ads/references/meta-decision-system.md` en su posición relativa y sin un `SKILL.md` adicional. El recibo acredita adquisición. La revisión de instrucciones y la disponibilidad efectiva por agente se acreditan en OpenClaw por separado.

## Alcance de la revisión

| Origen | Qué revisar antes de activar |
| --- | --- |
| Instagram | La licencia MIT está incluida. Los programas Python de `ig-reel`, `ig-caption`, `ig-human` y `ig-viral` se mantienen con sus datos auxiliares. Revisar los scripts y evitar el directorio de otro agente para notas de `ig-viral`. [Commit oficial](https://github.com/Jakeschincariol/instagram-agent-skill/tree/d03c56bb598be770c60b201f94237e5d1a4268a6) |
| Marketing | La licencia MIT está incluida. `ad-creative` y `social` remiten a integraciones capaces de publicar o alterar campañas. La plantilla HTML de revisión contiene JavaScript y se trata como código externo. [Commit oficial](https://github.com/coreyhaines31/marketingskills/tree/1f837447b20bb6f0e72d87b80ece1c221c362c7d) |
| Meta Ads | La licencia MIT está incluida y el nombre declarado es `meta-ads`. Las recomendaciones de configuración, seguimiento y presupuesto se revisan con el flujo de aprobación de campañas. [Skill oficial](https://github.com/kostja94/marketing-skills/blob/ac823a127782355339f21aef6c87f1ccff751c84/skills/paid-ads/platforms/meta-ads/SKILL.md) |
| Remotion | La entrada publicada es `skills/remotion-best-practices/SKILL.md`. Remotion documenta la instalación para agentes. El commit carece de una licencia independiente para redistribuir estos archivos. Los ejemplos incluyen código, servicios de mapas y referencias a herramientas de Studio. Preparar la copia privada y mantener sus archivos fuera del repositorio público de la agencia. [Instalación oficial](https://www.remotion.dev/docs/ai/skills) · [Comando y destino oficial](https://www.remotion.dev/docs/cli/skills) · [Consulta en el repositorio](https://github.com/remotion-dev/skills/issues/28) |
| RunComfy | La referencia histórica redirige a `prime-skills/runcomfy-agent-skills`, con licencia MIT. La skill de imagen describe instalación, login y envío de solicitudes a un proveedor externo. Mantener cualquier activación sujeta al proveedor y a la autorización de datos y gasto. [Skill oficial](https://github.com/prime-skills/runcomfy-agent-skills/blob/fca19ae084c2ae4667f5c8affbb99da5103d5832/ai-image-generation/SKILL.md) |
| Inference.sh | La referencia histórica redirige a `inference-sh/skills`. El README declara MIT mediante un distintivo y el árbol del commit carece de archivo `LICENSE`. La entrada de vídeo exige el CLI `belt`; el manifiesto fija también su guía de instalación como texto de revisión. La ejecución puede enviar medios y generar cargos. [README oficial](https://github.com/inference-sh/skills/blob/0cbf91c26ada28702c79962406286f0c14292150/README.md) · [Skill oficial](https://github.com/inference-sh/skills/blob/0cbf91c26ada28702c79962406286f0c14292150/tools/video/ai-video-generation/SKILL.md) |

Los enlaces externos que aparecen en los documentos son referencias de lectura. Tratar los comandos, archivos auxiliares y configuraciones del lote como contenido externo que requiere revisión antes de ejecutarse o darse a un agente con herramientas. La lista fija las fuentes públicas. Las dos skills externas locales y las tres internas se preparan desde sus propias rutas según el plan.
