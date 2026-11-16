# Autorizar el retorno de Google a E-scale Studio

Si Google muestra `Error 400: redirect_uri_mismatch`, revisa las URI autorizadas del cliente que utiliza el portal. La URL pública de retorno coincide con `GOOGLE_REDIRECT_URI` en la bóveda y requiere una entrada idéntica en Google Cloud. [Documentación de Google](https://developers.google.com/identity/protocols/oauth2/web-server#redirect-uri-mismatch)

1. Abre [Google Auth Platform → Clients](https://console.cloud.google.com/auth/clients) y selecciona el proyecto donde creaste el acceso de E-scale Studio.
2. Abre el cliente de tipo **Aplicación web** cuyo ID coincide con `GOOGLE_CLIENT_ID` del `.env`.
3. En **URIs de redireccionamiento autorizados**, elige **Añadir URI**. Pega `https://studio.e-scale.cl/api/auth/google/callback` sin barra final y guarda. Conserva las demás URI existentes.
4. Abre una sesión nueva desde [E-scale Studio](https://studio.e-scale.cl), selecciona **Continuar con Google** y elige la cuenta autorizada. Si el proyecto está en modo de prueba, esa cuenta debe estar en **Audience → Test users**.
5. La prueba termina cuando el portal vuelve a **Producción** y muestra la cuota del plan. Un 302 hacia Google acredita el primer paso del flujo.
