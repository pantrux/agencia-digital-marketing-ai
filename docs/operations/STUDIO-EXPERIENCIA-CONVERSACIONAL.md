# Preparación operativa: experiencia conversacional

Este documento describe los componentes del cambio propuesto. Su existencia no acredita un despliegue ni una prueba integral. El despliegue se realiza únicamente después de la revisión requerida y de comprobar los controles en un entorno aislado.

## Dependencias

- Consulta `d1_migrations` antes de preparar el despliegue. Si el historial termina en `0018`, aplica primero `0019` a `0021` para incorporar los campos de anuncio, contactos y motor de edición. Las migraciones `0022` a `0026` agregan conversación, conjuntos de fuentes, despacho múltiple, referencias y solicitudes de revisión. Verifica la secuencia completa en una copia restaurable, coteja las filas previas y prueba el Worker activo contra el esquema ampliado. Conserva el respaldo y el registro de migraciones.
- La consulta de referencias requiere el Worker `studio-reference-worker`, con `global_fetch_strictly_public`, y el enlace `REFERENCE_SERVICE` desde el Worker de Studio. Si no está disponible, el portal debe informar que no pudo consultar la referencia; el título, la descripción y un fragmento del texto público no acreditan que se haya aprendido el estilo completo de un perfil.
- La carga múltiple requiere el consumidor R2–NAS y el receptor de ingesta compatibles con el contrato de conjuntos. El despacho del encargo espera a que todas las fuentes se hayan copiado y verificado. Antes de habilitarlo, comprobar la compatibilidad de productores y consumidores y probar una orden de varias fuentes, incluida la recuperación de una transferencia interrumpida.
- El plugin de Sofía necesita acceso a los recibos de transcripción de la orden. La transcripción puede estar incompleta mientras se procesa el material y no representa lo que aparece en pantalla.
- Las revisiones posteriores a la entrega permanecen deshabilitadas si `STUDIO_DELIVERY_REVISION_ENABLED` no es `true`. La implementación actual solo acepta órdenes en modo `sandbox`; todavía no habilita revisiones de órdenes en modo `production`. Requiere además instalar y verificar `agency-production-revision.service` y su temporizador, sus dependencias, credenciales mediante `LoadCredential`, acceso al NAS y los servicios de dirección, render, control de calidad y publicación. No habilitar la variable hasta comprobar el circuito de extremo a extremo en un entorno aislado.

## Verificación previa a activación

1. Confirmar en el PR la revisión de Greptile y los controles requeridos, sin publicar `.env`, claves SSH ni credenciales.
2. Verificar respaldo y migraciones en una copia, y desplegar los servicios compatibles siguiendo el procedimiento operativo vigente del entorno.
3. Probar con dos videos propios: carga, copia al NAS, transcripción, conversación con Sofía, brief confirmado y selección del montaje. Cotejar los estados visibles con los recibos reales.
4. Comprobar que Elena aprueba la salida antes de publicar el kit, que el MP4 se reproduce en el chat y que el enlace de ProRes aparece cuando corresponde. Registrar también los casos de rechazo.
5. Para habilitar revisiones, solicitar un cambio sobre una entrega aprobada, comprobar aislamiento por usuario, concesión y recuperación ante fallos, y verificar que la nueva entrega no sobrescribe la anterior. Deshabilitar la función si no se acredita el circuito completo.

Estas comprobaciones deben recoger evidencia del entorno probado y evitar datos personales y secretos en los registros.
