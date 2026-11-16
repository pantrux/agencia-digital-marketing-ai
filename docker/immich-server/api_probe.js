// Comprueba el API dentro del contenedor y recibe los secretos por stdin.
const fs = require("node:fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const origin = "http://127.0.0.1:2283/api";

async function call(path, { method = "GET", body, token, timeout = 10000 } = {}) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = "Bearer " + token;
  const response = await fetch(origin + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
    redirect: "error",
    signal: AbortSignal.timeout(timeout),
  });
  const raw = await response.text();
  if (raw.length > 65536) throw new Error("RESPUESTA_EXCESIVA");
  return { status: response.status, body: raw ? JSON.parse(raw) : null };
}

async function login(out) {
  const { email, password } = input;
  const version = await call("/server/version");
  out.version = version.body;
  out.anonymous = (await call("/users/me")).status;
  if (version.status !== 200 || out.anonymous !== 401 ||
      version.body?.major !== 3 || version.body?.minor !== 3 ||
      version.body?.patch !== 6 || version.body?.prerelease !== null) return;
  out.signup = (await call("/auth/admin-sign-up", {
    method: "POST", body: { email, name: "Ensayo privado", password },
  })).status;
  if (out.signup !== 201) return;
  out.wrongPassword = (await call("/auth/login", {
    method: "POST", body: { email, password: "wrong-trial-password" },
  })).status;
  if (out.wrongPassword !== 401) return;
  const result = await call("/auth/login", { method: "POST", body: { email, password } });
  out.login = result.status;
  if (result.status !== 201 || result.body?.isAdmin !== true) return;
  const token = result.body.accessToken;
  const user = result.body.userId;
  if (typeof token !== "string" || typeof user !== "string") return;
  out.token = token;
  out.userId = user;
  out.success = true;
}

async function verify(out) {
  const { token, userId } = input;
  try {
    const user = await call("/users/me", { token });
    out.me = user.status;
    out.sameUser = user.body?.id === userId && user.body?.isAdmin === true;
    if (out.me !== 200 || !out.sameUser) return;
    const search = await call("/search/smart", {
      method: "POST", body: { query: "ensayo privado de color azul V1", size: 1 },
      token, timeout: 40000,
    });
    out.smart = search.status;
    out.assets = search.body?.assets?.items?.length;
  } finally {
    const result = await call("/auth/logout", { method: "POST", token });
    out.logout = result.status;
    out.loggedOut = result.body?.successful === true;
    out.revoked = (await call("/users/me", { token })).status;
  }
  out.success = out.smart === 200 && out.assets === 0 &&
    out.logout === 200 && out.loggedOut && out.revoked === 401;
}

(async () => {
  const out = { success: false };
  try {
    if (input.action === "login") await login(out);
    else if (input.action === "verify") await verify(out);
  } catch (error) {
    out.error = error.name;
  }
  process.stdout.write(JSON.stringify(out) + "\n");
  process.exitCode = out.success ? 0 : 1;
})();
