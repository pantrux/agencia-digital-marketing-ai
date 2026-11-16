const origin = "https://studio.e-scale.cl";
const previewHost = /^(?:[a-f0-9]{8}|phase6-validation)\.studio-portal-web\.pages\.dev$/u;

function rejected(error, status) {
  return Response.json({ error }, { status, headers: { "Cache-Control": "no-store" } });
}

export default {
  fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(request);
    if (url.origin === origin) return env.STUDIO_API.fetch(request);
    if (url.protocol === "https:" && previewHost.test(url.hostname) && request.method === "GET") {
      if (url.pathname === "/api/healthz" && !url.search) {
        return env.STUDIO_API.fetch(new Request(origin + "/api/healthz"));
      }
      if (url.pathname === "/api/auth/me" || url.pathname === "/api/dashboard") {
        return rejected("AUTENTICACION_REQUERIDA", 401);
      }
    }
    return rejected("RUTA_NO_DISPONIBLE", 404);
  },
};
