# ADR-001: Usar `/volume2/producciones` para el almacenamiento compartido

## Estado

Aceptada por autorización explícita del usuario. Sustituye la ubicación anterior `/volume1/producciones` del plan y la arquitectura v4.1. Al aceptar esta decisión, su implementación y la verificación de Fase 2 estaban pendientes. La [evidencia posterior](../ci/evidence/2026-11-05-fase-2.md) registra el recurso y el acceso triple observados; el responsable confirmó el cierre el 2026-11-05, con 220 pruebas Python y controles finales aprobados.

## Fecha

2026-11-05.

## Contexto

La plataforma necesita un área común de medios y respaldos locales en Synology DSM `192.168.0.8`, accesible por NFSv4.1 desde `.85` y `.7`, y por SMB3 desde Windows `.6`. El plan anterior ubicaba el área en `/volume1/producciones` y representaba aproximadamente 41 TB de almacenamiento; esa capacidad no fue acreditada por la auditoría real.

La inspección de solo lectura de los sistemas de archivos montados, realizada el 2026-11-05 a las 21:45 UTC, observó:

| Volumen | Capacidad | Espacio libre aproximado |
|---|---:|---:|
| `/volume1` | 182,25 GiB | 33,7 GiB |
| `/volume2` | 6094,56 GiB | 2753,63 GiB (2,69 TiB) |
| `/volume3` | 7935,92 GiB | 145 GiB |

Son capacidades de los sistemas de archivos existentes, no una medición de capacidad física bruta, una certificación del esquema RAID ni una reserva exclusiva para la plataforma. El espacio libre varía con otros consumidores. Evidencia local: `.tools/evidence/synology-all-volume-capacity.json`.

La revisión independiente de dependencias a las 21:41 UTC encontró que ESXi `192.168.0.3` mantiene accesible y montado el datastore `NFS`, servido desde `192.168.0.8:/volume2/NFS`. Su lista de VMs registradas asociadas estaba vacía. Eso no demuestra ausencia de archivos, usos externos u otros clientes. Evidencia local: `.tools/evidence/vcenter-nfs-dependencies.json`.

## Decisión

Usar **`/volume2/producciones`** como raíz del nuevo recurso de la plataforma, por autorización del usuario y por su mayor espacio libre observado frente a las alternativas existentes.

- Origen NFS previsto: `192.168.0.8:/volume2/producciones`; destino local `/mnt/producciones` en `.85` y `.7`, con NFSv4.1.
- Recurso SMB previsto: `producciones` en `192.168.0.8`, respaldado por esa misma raíz; unidad `P:` en el contexto del usuario de Windows `.6`, con cifrado y persistencia exigidos por el mapper.
- Biblioteca y proyectos bajo la misma raíz; respaldos locales bajo `/volume2/producciones/_backups`.
- Preservar `/volume2/NFS`, las exportaciones, clientes y datos ajenos. No desmontar ni reconfigurar el datastore para crear el nuevo recurso.

El responsable actualizó la bóveda y su plantilla **antes de usar** la nueva ubicación: 207 claves y 14 reservas pendientes en este punto. Este cambio documental no modifica esos archivos ni revela valores.

## Alternativas consideradas

| Alternativa | Ventaja | Motivo para descartarla |
|---|---|---|
| Mantener `/volume1/producciones` | Conserva la ruta del plan anterior. | Solo ~33,7 GiB libres observados, insuficientes para el área de medios prevista. Queda sustituida por esta decisión. |
| Usar `/volume3/producciones` | Mayor capacidad total del volumen. | Solo ~145 GiB libres observados, con ocupación cercana al 99 %; ofrece menor margen que `/volume2`. |
| Esperar almacenamiento adicional o redistribuir volúmenes | Podría ampliar la capacidad futura. | No existe capacidad adicional acreditada ni una redistribución autorizada; alteraría recursos ajenos al nuevo directorio. |

## Consecuencias

- Arquitectura, plan, instrucciones de arranque, runbooks y contratos NFS deben usar el mismo origen nuevo. El nombre SMB, `P:` y `/mnt/producciones` se conservan: no permiten distinguir por sí solos un origen antiguo, por lo que debe comprobarse la ruta real del servidor.
- Revalidar espacio y permisos inmediatamente antes de crear el recurso. Los ~2,69 TiB libres observados no son una cuota garantizada ni acreditan 41 TB disponibles.
- `_backups` comparte el volumen de los medios; no constituye una copia independiente frente a la pérdida de ese volumen. La copia adicional en Cloudflare R2 sigue siendo un objetivo de fases posteriores y requiere sus propias pruebas.
- Habilitar NFSv4.1 debe preservar NFS 2/3 y las exportaciones existentes, coordinando el impacto sobre todos los clientes activos, incluido ESXi. Esta decisión no acredita que esa habilitación ya haya ocurrido.
- La Fase 2 permanece abierta hasta crear el recurso y permisos, verificar NFS/SMB, montar en `.85` y `.7`, mapear únicamente una unidad autorizada y comprobar el mismo archivo desde los tres clientes con su hash. Este ADR no acredita ninguna de esas mutaciones ni la prueba triple.

## Documentos relacionados

- [Arquitectura vigente](../ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md).
- [Plan de implementación](../PLAN-DE-IMPLEMENTACION.md).
- [Procedimiento NFS](../../storage/README-NFS-Linux.md).
- [Procedimiento SMB](../../storage/README-SMB-Windows.md).
- [Estado de implementación](../../ESTADO.md).
