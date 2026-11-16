# Agencia Digital de Marketing AI — Plataforma Audiovisual Autoatendible

Directorio raíz canónico de arquitectura, código de infraestructura, pipelines CI/CD, operación SRE y configuración del equipo de agentes para la **Agencia Digital de Marketing AI (`studio.e-scale.cl`)**.

---

## Índice de Documentación Maestra

| Documento | Propósito |
| :--- | :--- |
| [`docs/ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/docs/ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md) | **Documento Maestro de Arquitectura v4.1:** Topología híbrida Cloudflare Edge + Homelab vSphere 8.0, 4 Planes de Suscripción (`Free`, `Pro`, `Ultra`, `Ilimitado`), Google OAuth 2.0, Pasarela Dual (Stripe + Flow.cl/MercadoPago), Equipo de 5 Agentes IA (`gpt-6.1-sol` / `gpt-6.1-terra`), URLs exactas de Skills, 4 MCPs y Gobierno CI/CD & SRE. |
| [`docs/PLAN-DE-IMPLEMENTACION.md`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/docs/PLAN-DE-IMPLEMENTACION.md) | **Plan de Implementación v4.1:** Desglose ejecutable desde la **Fase 0** hasta la **Fase 7** con verificaciones deterministas. |
| [`docs/PROMPT-MAESTRO-AGENTE-IMPLEMENTACION.md`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/docs/PROMPT-MAESTRO-AGENTE-IMPLEMENTACION.md) | **Prompt Maestro de Arranque:** Instrucciones completas listas para entregar al agente de implementación. |
| [`ESTADO.md`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/ESTADO.md) | Bitácora viva de avance y verificaciones por fase. |
| [`docs/ci/README.md`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/docs/ci/README.md) | Estándar documental de CI/CD, diagramas Mermaid y recetas Dev/SRE. |
| [`.env`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/.env) | **Bóveda Maestra de Credenciales (NO commitear):** Respaldo obligatorio de toda credencial generada y/o utilizada en la plataforma. |
| [`.env.example`](file:///D:/Proyectos/Agencia%20Digital%20de%20Marketing%20AI/.env.example) | Plantilla limpia de variables de entorno para control de versiones. |

## Estado comprobado

La decisión vigente de almacenamiento está registrada en [ADR-001](docs/decisions/ADR-001-volumen-producciones.md).

La Fase 0 dispone de siete controles de calidad, cinco runbooks y pruebas SRE locales. Consultar la [evidencia](docs/ci/evidence/2026-11-05-fase-0.md) y [ESTADO.md](ESTADO.md) antes de operar; los resultados locales no acreditan infraestructura desplegada.

Las Fases 1 y 2 están verificadas; la Fase 3 está en curso, sin servicios GPU acreditados todavía. `/volume2/producciones`, autorizado por el usuario, ya se verificó por NFSv4.1 en `.85` y `.7` y por SMB3 cifrado en Windows `P:`, con el mismo archivo de prueba en los tres clientes. Consultar la [evidencia de Fase 2](docs/ci/evidence/2026-11-05-fase-2.md). La auditoría del 2026-11-05 observó `/volume2` con 6094,56 GiB de capacidad y ~2753,63 GiB libres (2,69 TiB); la previsión anterior de 41 TB queda sustituida por los hechos recogidos en [ADR-001](docs/decisions/ADR-001-volumen-producciones.md). Se conserva el datastore existente `/volume2/NFS`.

El [repositorio público](https://github.com/pantrux/agencia-digital-marketing-ai) contiene únicamente `LICENSE` por indicación del usuario. GitHub la identifica como MIT, [aprobada por OSI](https://opensource.org/license/mit). Se conserva su texto oficial para mantener la identificación de la licencia; las fuentes de la plataforma permanecen locales y no se han ejecutado comandos Git.
