# ADR-002: MercadoPago como único proveedor de pagos de la primera versión

## Estado

Aceptada por instrucción explícita del usuario: «Solo mercadopago, a futuro flow y webpay». Sustituye el requisito anterior de pasarela dual Stripe Billing y Flow.cl/MercadoPago de la arquitectura y el plan v4.1. La integración y su verificación pertenecen a Fase 6 y permanecen pendientes.

## Fecha

2026-11-05.

## Contexto

El portal `studio.e-scale.cl` mantiene Google OAuth y los cuatro planes `Free`, `Pro`, `Ultra` e `Ilimitado`. El documento anterior exigía más de un proveedor desde la primera entrega. El usuario redujo ese alcance para comenzar con MercadoPago y dejó Flow y Webpay para una ampliación posterior.

El usuario está obteniendo un token de prueba conforme a la documentación oficial. Esta actividad no acredita todavía una cuenta habilitada, una aplicación configurada, llamadas API satisfactorias, webhooks registrados ni pagos reales. La [documentación oficial de credenciales](https://www.mercadopago.cl/developers/es/docs/checkout-api-orders/resources/credentials?scope=prod) distingue los entornos de prueba y producción y reserva el Access Token para el backend. La modalidad concreta de checkout o suscripción se definirá en la implementación correspondiente; esta decisión no selecciona una API solo por el aspecto de una credencial.

## Decisión

Usar **MercadoPago como único proveedor de cobro inicial** del catálogo de cuatro planes. Esta decisión conserva las identidades de los planes y su control interno de acceso y cuotas; no fija precios, periodicidad ni nuevas cuotas, ni implica que Free deba generar un cobro.

- **MercadoPago:** integrar y probar en Fase 6 con el entorno de prueba verificado. La promoción a producción requiere sus propias credenciales y evidencia.
- **Flow y Webpay:** registrar como trabajo posterior. No implementar fallback, cuentas ni webhooks de esos proveedores en esta entrega.
- **Stripe:** retirar como requisito de la entrega actual. Las reservas históricas de Stripe, Flow y otros proveedores se conservan en la bóveda y plantilla; no deben bloquear el arranque, las pruebas ni la aceptación del alcance inicial de MercadoPago. Conservar una reserva no activa una integración.
- **Gobierno de secretos:** el responsable respalda cada credencial e identificador real antes de inyectarlo. Este ADR no modifica `.env`, su plantilla ni servicios externos, y no publica valores.

## Alternativas consideradas

| Alternativa | Ventaja | Decisión |
|---|---|---|
| Pasarela dual Stripe y Flow/MercadoPago desde el inicio | Mantiene la previsión original de varios proveedores. | Sustituida por la instrucción del usuario; añade cuentas, estados y pruebas fuera del alcance inicial. |
| MercadoPago, Flow y Webpay simultáneamente | Incorpora desde el comienzo los proveedores mencionados para el futuro. | Pospuesta; el usuario pidió únicamente MercadoPago para la primera versión. |
| MercadoPago primero y ampliación posterior | Acota la integración inicial y conserva el catálogo de planes. | Elegida. Cada proveedor futuro tendrá su implementación y aceptación independientes. |

## Consecuencias y aceptación pendiente

La API del Worker, el estado de suscripciones y las pruebas de cuotas deben basarse inicialmente en MercadoPago. El proveedor de pago y el plan interno son identidades distintas: una redirección del navegador no acredita por sí sola un pago ni concede derechos de un plan.

Antes de aceptar Fase 6 se requiere respaldar y comprobar las credenciales del entorno elegido, implementar los estados internos necesarios y probar el flujo de prueba, sus errores y las notificaciones autenticadas e idempotentes según el producto de MercadoPago utilizado. La [guía oficial de primeros pasos](https://www.mercadopago.cl/developers/es/docs/getting-started) sirve para elegir ese producto; su disponibilidad documental no demuestra la habilitación de la cuenta particular.

Flow, Webpay y Stripe no forman parte de esa condición de aceptación. No se ha ejecutado ninguna operación de pagos ni configuración de webhooks mediante este cambio documental.

## Documentos relacionados

- [Arquitectura vigente](../ARQUITECTURA-PLATAFORMA-AUDIOVISUAL.md).
- [Plan de implementación, Fase 6](../PLAN-DE-IMPLEMENTACION.md).

