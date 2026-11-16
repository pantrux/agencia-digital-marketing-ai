# Configurar el acceso de la plataforma a Cloudflare

Fecha: 2026-11-06. En la comprobación inicial, el token DNS existente estaba activo y permite consultar `e-scale.cl`, con cuenta y zona coincidentes. Las consultas iniciales de R2 y D1 devolvieron 403/401 y código 10000. El usuario completó después `CF_PLATFORM_API_TOKEN`; se verificó acceso 200 y creación de los tres recursos previstos. Los pasos siguientes quedan como procedimiento para una futura configuración o rotación. Conservar `CF_API_TOKEN` y preparar otro token en el campo `CF_PLATFORM_API_TOKEN` de la bóveda local.

1. Entrar en [API Tokens de Cloudflare](https://dash.cloudflare.com/profile/api-tokens), seleccionar **Create Token** y **Create Custom Token**.
2. Asignar un nombre reconocible, por ejemplo **Agencia Digital Studio Platform**.
3. Para las copias y bases de esta fase, agregar permisos de **Account**: **D1 → Edit** y **Workers R2 Storage → Edit**.
4. Para completar la plataforma con la misma credencial, agregar los permisos de cuenta **Workers KV Storage → Edit**, **Cloudflare Pages → Edit** y **Queues → Edit**, además de **Account Settings → Read**. Para crear el Worker se necesita el rol de producto **Workers Admin**; una interfaz anterior puede mostrar el grupo **Workers Scripts → Edit**. No confundirlo con un rol Editor limitado a un Worker existente.
5. Limitar los recursos de cuenta a la cuenta que contiene `e-scale.cl`. Evitar seleccionar todas las cuentas. Si también se incluye gestión de rutas/dominio, limitar los permisos de zona **DNS → Edit**, **Zone → Read** y **Workers Routes → Edit** exclusivamente a `e-scale.cl`.
6. Revisar el resumen y crear el token. Copiarlo únicamente al valor de `CF_PLATFORM_API_TOKEN` en `D:\Proyectos\Agencia Digital de Marketing AI\.env`, conservando sus comentarios. No enviarlo por chat ni ponerlo en `.env.example`.
7. Avisar cuando esté guardado. El responsable validará primero el token y los productos por lectura; solo después lo inyectará en los consumidores necesarios. Un token activo no prueba que tenga los permisos correctos.

La clave S3 `R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY` pertenece a un mecanismo distinto y no debe reemplazarse por este token REST. Su acceso se comprueba por separado. Tampoco se exige permiso para administrar tokens de usuario ni facturación.

Referencias oficiales: [permisos de API](https://developers.cloudflare.com/fundamentals/api/reference/permissions/), [roles Workers y alcance de producto](https://developers.cloudflare.com/workers/authorization/workers/), [Pages API](https://developers.cloudflare.com/pages/configuration/api/) y [autenticación R2](https://developers.cloudflare.com/r2/api/tokens/). Los nombres pueden variar entre permisos heredados y roles nuevos; comprobar el resumen antes de crear la credencial.

## Lectura de la latencia de Studio

El token da acceso a Cloudflare Analytics por GraphQL para las rutas públicas de autenticación y de firma multipart de `studio.e-scale.cl`. Se requiere acceso al perfil que administra la zona `e-scale.cl`. El 16 de noviembre se comprobó que la zona permite consultar recuentos y rechaza el campo `edgeTimeToFirstByteMsP95`. La disponibilidad del campo depende del plan de Cloudflare. [Disponibilidad de campos](https://developers.cloudflare.com/analytics/graphql-api/limits/#node-limits-and-availability)

1. Abre [API Tokens de Cloudflare](https://dash.cloudflare.com/profile/api-tokens) y elige **Create Token → Create Custom Token**.
2. Asigna el nombre **Studio Analytics** y agrega el permiso **Zone → Analytics → Read**.
3. En **Zone Resources**, selecciona **Include → Specific zone → e-scale.cl**. Revisa el alcance en **Continue to summary** y pulsa **Create Token**.
4. Copia el valor en `CF_STUDIO_ZONE_ANALYTICS_TOKEN` del archivo `.env` del proyecto. Guarda el archivo y avisa al responsable de la validación. Mantén el valor en la bóveda local.

La validación consulta los recuentos y, cuando la zona lo permite, `edgeTimeToFirstByteMsP95` por ruta. Registra la ventana, las muestras y el resultado. Una ventana sin muestras queda pendiente de verificación. El tiempo que tarda el origen se registra como una métrica separada. Si una rotación falla, restituye el token anterior en la bóveda mientras siga activo. Ante un error de permisos de la credencial, revisa el alcance de zona en el resumen del token. [Creación de tokens](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/) · [Permisos de GraphQL](https://developers.cloudflare.com/analytics/graphql-api/limits/)
