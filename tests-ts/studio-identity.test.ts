import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { finishGoogle, startGoogle, type AuthEnv } from "../cloudflare-saas/studio-api-worker/src/identity.js";
import type { ExecutionTasks, SessionCache } from "../cloudflare-saas/studio-api-worker/src/platform.js";

const origin = "https://studio.example.invalid";
const oauthCode = "codigo-google-sintetico";
const tokenUrl = "https://oauth2.googleapis.com/token";
const jwksUrl = "https://www.googleapis.com/oauth2/v3/certs";
const googleSubject = "123456789012345678901";
const portalUser = { id: "13dde435-f2f5-4797-a98f-d0affddcd812", email: "titular@example.invalid", display_name: "Titular" };
const encoded = (value: Record<string, unknown>) => Buffer.from(JSON.stringify(value)).toString("base64url");
const sha256 = (value: string) => createHash("sha256").update(value);

async function fixture() {
  const first = vi.fn(async () => portalUser);
  const run = vi.fn(async () => ({ success: true }));
  const bindUser = vi.fn<(...values: unknown[]) => { first: typeof first }>().mockReturnValue({ first });
  const bindSession = vi.fn<(...values: unknown[]) => { run: typeof run }>().mockReturnValue({ run });
  const prepare = vi.fn((sql: string) => {
    if (sql.startsWith("INSERT INTO users")) return { bind: bindUser };
    if (sql.startsWith("INSERT INTO sessions")) return { bind: bindSession };
    throw new Error("SQL de prueba inesperado");
  });
  const withSession = vi.fn<(constraint: string) => { prepare: typeof prepare }>().mockReturnValue({ prepare });
  const batch = vi.fn().mockResolvedValue([{ results: [] }, { results: [{ user_id: portalUser.id }] }]);
  const put = vi.fn<SessionCache["put"]>().mockResolvedValue(undefined);
  const waitUntil = vi.fn<ExecutionTasks["waitUntil"]>();
  const env: AuthEnv = {
    DB: { withSession, prepare, batch } as unknown as AuthEnv["DB"],
    SESSIONS: { put, get: async () => null, delete: async () => undefined },
    GOOGLE_CLIENT_ID: "studio-fixture.apps.googleusercontent.com",
    GOOGLE_CLIENT_SECRET: "secreto-del-cliente-sintetico",
    STUDIO_JWT_SESSION_SECRET: "secreto-de-firma-sintetico-para-prueba-local",
    PUBLIC_ORIGIN: origin,
  };
  const start = await startGoogle(env);
  expect(start.status).toBe(302);
  const auth = new URL(start.headers.get("Location")!);
  const stateCookie = start.headers.getSetCookie()[0]!.split(";", 1)[0]!;
  const callback = new URL(auth.searchParams.get("redirect_uri")!);
  callback.search = new URLSearchParams({ state: auth.searchParams.get("state")!, code: oauthCode }).toString();
  const request = new Request(callback, { headers: { cookie: stateCookie } });
  return { auth, env, request, withSession, prepare, batch, bindUser, bindSession, run, put, waitUntil };
}

function googleToken(client: string, nonce: string) {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const kid = "clave-fixture";
  const jwk = { ...publicKey.export({ format: "jwk" }), kid, use: "sig", alg: "RS256" };
  const now = Math.floor(Date.now() / 1000);
  const payload = encoded({
    iss: "https://accounts.google.com", aud: client, azp: client, nonce, iat: now, exp: now + 600,
    sub: googleSubject, email: portalUser.email, email_verified: true, name: portalUser.display_name,
  });
  const message = `${encoded({ alg: "RS256", kid })}.${payload}`;
  const jwt = `${message}.${sign("sha256", Buffer.from(message), privateKey).toString("base64url")}`;
  return { jwk, jwt };
}

describe("Callback OAuth de Google", () => {
  it("solapa la descarga de claves con el intercambio y espera ambos antes de persistir", async () => {
    const { auth, env, request, withSession, waitUntil } = await fixture();
    const { jwt, jwk } = googleToken(env.GOOGLE_CLIENT_ID, auth.searchParams.get("nonce")!);
    const exchange = Promise.withResolvers<Response>();
    const keys = Promise.withResolvers<Response>();
    const send = vi.fn<typeof fetch>((url) => url === tokenUrl ? exchange.promise : keys.promise);
    const pending = finishGoogle(request, env, { waitUntil }, send);
    try {
      await vi.waitFor(() => expect(send.mock.calls.map(([url]) => url)).toStrictEqual([tokenUrl, jwksUrl]));
      exchange.resolve(Response.json({ id_token: jwt }));
      await Promise.resolve();
      expect(withSession).not.toHaveBeenCalled();
    } finally {
      exchange.resolve(Response.json({ id_token: jwt }));
      keys.resolve(Response.json({ keys: [jwk] }));
      await pending;
    }
    expect((await pending).status).toBe(303);
    expect(withSession).toHaveBeenCalledExactlyOnceWith("first-primary");
  });

  it("cancela la consulta de claves si Google rechaza el intercambio", async () => {
    const { env, request, withSession, waitUntil } = await fixture();
    let signal: AbortSignal | undefined;
    const send = vi.fn<typeof fetch>((url, init) => {
      if (url === tokenUrl) return Promise.resolve(new Response(null, { status: 400 }));
      signal = init?.signal ?? undefined;
      return new Promise<Response>((_, reject) => {
        signal?.addEventListener("abort", () => reject(signal?.reason), { once: true });
      });
    });
    await expect(finishGoogle(request, env, { waitUntil }, send)).rejects.toMatchObject({
      status: 401, code: "GOOGLE_INTERCAMBIO_RECHAZADO",
    });
    expect(send.mock.calls.map(([url]) => url)).toStrictEqual([tokenUrl, jwksUrl]);
    expect(signal?.aborted).toBe(true);
    expect(withSession).not.toHaveBeenCalled();
  });

  it("rechaza un estado de OAuth inválido sin consultar a Google", async () => {
    const { env, request, withSession, waitUntil } = await fixture();
    const url = new URL(request.url);
    url.searchParams.set("state", "estado-ajeno");
    const invalid = new Request(url, { headers: request.headers });
    const send = vi.fn<typeof fetch>();
    await expect(finishGoogle(invalid, env, { waitUntil }, send)).rejects.toMatchObject({
      status: 400, code: "SESION_GOOGLE_INVALIDA",
    });
    expect(send).not.toHaveBeenCalled();
    expect(withSession).not.toHaveBeenCalled();
  });

  it("verifica PKCE y RS256, persiste una sesión primaria y devuelve únicamente cookies propias", async () => {
    const { auth, env, request, withSession, bindUser, bindSession, run, put, waitUntil } = await fixture();
    const { jwt, jwk } = googleToken(env.GOOGLE_CLIENT_ID, auth.searchParams.get("nonce")!);
    const send = vi.fn<typeof fetch>((url, init) => {
      expect(init?.redirect).toBe("manual");
      expect(init?.signal).toBeInstanceOf(AbortSignal);
      if (url === tokenUrl) {
        if (!init) throw new Error("Falta RequestInit de prueba");
        expect(init.method).toBe("POST");
        expect(new Headers(init.headers).get("Content-Type")).toBe("application/x-www-form-urlencoded");
        expect(typeof init.body).toBe("string");
        const body = new URLSearchParams(init.body as string);
        expect(Object.fromEntries(body)).toStrictEqual({
          code: oauthCode, client_id: env.GOOGLE_CLIENT_ID, client_secret: env.GOOGLE_CLIENT_SECRET,
          code_verifier: expect.stringMatching(/^[a-zA-Z0-9_-]{43}$/u),
          redirect_uri: origin + "/api/auth/google/callback", grant_type: "authorization_code",
        });
        expect(sha256(body.get("code_verifier")!).digest("base64url")).toBe(auth.searchParams.get("code_challenge"));
        return Promise.resolve(Response.json({ id_token: jwt, access_token: "provider-token-fixture" }));
      }
      expect(url).toBe(jwksUrl);
      expect(init?.body).toBeUndefined();
      return Promise.resolve(Response.json({ keys: [jwk] }));
    });

    const result = await finishGoogle(request, env, { waitUntil }, send);
    expect(send).toHaveBeenCalledTimes(2);
    expect(withSession).toHaveBeenCalledExactlyOnceWith("first-primary");
    expect(bindUser).toHaveBeenCalledExactlyOnceWith(expect.any(String), googleSubject, portalUser.email,
      portalUser.display_name, expect.any(Number), expect.any(Number));
    expect(result.status).toBe(303);
    expect(result.headers.get("Location")).toBe(origin + "/");
    expect(result.headers.get("Cache-Control")).toBe("no-store");
    expect(result.headers.get("Referrer-Policy")).toBe("no-referrer");
    const cookies = result.headers.getSetCookie();
    expect(cookies).toHaveLength(2);
    expect(cookies[0]).toBe("__Host-studio_oauth=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0");
    expect(cookies[1]).toMatch(/^__Host-studio_session=.+; Path=\/; Secure; HttpOnly; SameSite=Lax; Max-Age=28800$/u);
    const session = cookies[1]!.split(";", 1)[0]!.slice("__Host-studio_session=".length);
    const sessionHash = sha256(session).digest("hex");
    expect(bindSession).toHaveBeenCalledExactlyOnceWith(sessionHash, portalUser.id, expect.any(String),
      expect.any(Number), expect.any(Number));
    expect(run).toHaveBeenCalledTimes(1);
    expect(put).toHaveBeenCalledExactlyOnceWith(`session:${sessionHash}`, portalUser.id, { expirationTtl: 28800 });
    expect(waitUntil).toHaveBeenCalledTimes(1);
    expect(await result.text()).toBe("");
    const headers = JSON.stringify([...result.headers]);
    for (const value of [jwt, "provider-token-fixture", env.GOOGLE_CLIENT_SECRET, env.STUDIO_JWT_SESSION_SECRET, oauthCode]) {
      expect(headers).not.toContain(value);
    }
  });

  it.each([tokenUrl, jwksUrl])("rechaza redirecciones del proveedor en %s antes de tocar D1", async (endpoint) => {
    const { auth, env, request, withSession, prepare, waitUntil } = await fixture();
    const { jwt } = googleToken(env.GOOGLE_CLIENT_ID, auth.searchParams.get("nonce")!);
    const send = vi.fn<typeof fetch>((url, init) => {
      expect(init?.redirect).toBe("manual");
      if (url === endpoint) return Promise.resolve(new Response(null, {
        status: 307, headers: { Location: "https://redireccion.example.invalid/" },
      }));
      if (url === tokenUrl) return Promise.resolve(Response.json({ id_token: jwt }));
      expect(url).toBe(jwksUrl);
      return Promise.resolve(Response.json({ keys: [] }));
    });
    await expect(finishGoogle(request, env, { waitUntil }, send)).rejects.toMatchObject({
      status: 401, code: endpoint === tokenUrl ? "GOOGLE_INTERCAMBIO_RECHAZADO" : "TOKEN_GOOGLE_INVALIDO",
    });
    expect(send.mock.calls.map(([url]) => url)).toEqual([tokenUrl, jwksUrl]);
    expect(withSession).not.toHaveBeenCalled();
    expect(prepare).not.toHaveBeenCalled();
    expect(waitUntil).not.toHaveBeenCalled();
  });

  it("agrupa el alta de usuario y sesión y vincula la cookie al usuario que devuelve D1", async () => {
    const { auth, env, request, withSession, batch, bindSession, put, waitUntil } = await fixture();
    const { jwt, jwk } = googleToken(env.GOOGLE_CLIENT_ID, auth.searchParams.get("nonce")!);
    const send = vi.fn<typeof fetch>(async (url) => url === tokenUrl
      ? Response.json({ id_token: jwt }) : Response.json({ keys: [jwk] }));

    const result = await finishGoogle(request, { ...env, STUDIO_SESSION_BATCH_ENABLED: "true" }, { waitUntil }, send);

    expect(result.status).toBe(303);
    expect(batch).toHaveBeenCalledTimes(1);
    expect(batch.mock.calls[0]?.[0]).toHaveLength(2);
    expect(withSession).not.toHaveBeenCalled();
    const token = result.headers.getSetCookie()[1]!.split(";", 1)[0]!.slice("__Host-studio_session=".length);
    const payload = JSON.parse(Buffer.from(token.split(".")[1]!, "base64url").toString("utf8"));
    expect(payload).toEqual({ kind: "session", version: 2, nonce: expect.any(String), exp: expect.any(Number) });
    const hash = sha256(token).digest("hex");
    expect(bindSession).toHaveBeenCalledExactlyOnceWith(hash, payload.nonce, payload.exp, expect.any(Number), googleSubject);
    expect(put).toHaveBeenCalledExactlyOnceWith(`session:${hash}`, portalUser.id, { expirationTtl: 28800 });
  });

  it("omite la cookie y la caché si falla la escritura de la sesión", async () => {
    const { auth, env, request, batch, put, waitUntil } = await fixture();
    const { jwt, jwk } = googleToken(env.GOOGLE_CLIENT_ID, auth.searchParams.get("nonce")!);
    const send = vi.fn<typeof fetch>(async (url) => url === tokenUrl
      ? Response.json({ id_token: jwt }) : Response.json({ keys: [jwk] }));
    batch.mockRejectedValue(new Error("Fallo de D1"));

    await expect(finishGoogle(request, { ...env, STUDIO_SESSION_BATCH_ENABLED: "true" }, { waitUntil }, send))
      .rejects.toThrow("Fallo de D1");
    expect(put).not.toHaveBeenCalled();
    expect(waitUntil).not.toHaveBeenCalled();
  });
});
