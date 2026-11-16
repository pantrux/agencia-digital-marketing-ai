import { HttpError, type ExecutionTasks, type SessionCache, type SqlDatabase, type SqlSession } from "./platform.js";

const cookieSession = "__Host-studio_session";
const cookieState = "__Host-studio_oauth";
const stateSeconds = 600;
const sessionSeconds = 8 * 60 * 60;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });
const googleIssuer = "https://accounts.google.com";
const tokenUrl = "https://oauth2.googleapis.com/token";
const jwksUrl = "https://www.googleapis.com/oauth2/v3/certs";
const urlBytes = /^[A-Za-z0-9_-]+$/;

export interface AuthEnv {
  DB: SqlDatabase;
  SESSIONS: SessionCache;
  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;
  STUDIO_JWT_SESSION_SECRET: string;
  STUDIO_SESSION_BATCH_ENABLED?: string;
  PUBLIC_ORIGIN: string;
}

export interface PortalIdentity {
  id: string;
  email: string;
  display_name: string | null;
}

function callback(env: AuthEnv): string {
  return new URL("/api/auth/google/callback", env.PUBLIC_ORIGIN).href;
}

function now(): number {
  return Math.floor(Date.now() / 1000);
}

function base64url(value: Uint8Array): string {
  return btoa(String.fromCharCode(...value)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function unbase64(value: string): Uint8Array<ArrayBuffer> {
  if (!urlBytes.test(value) || value.length % 4 === 1) throw new Error("BASE64_INVALIDO");
  return Uint8Array.from(atob(value.replaceAll("-", "+").replaceAll("_", "/")), (byte) => byte.charCodeAt(0));
}

function random(): string {
  return base64url(crypto.getRandomValues(new Uint8Array(32)));
}

export async function sha256Hex(value: string): Promise<string> {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value))))
    .map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function sessionKey(secret: string): Promise<CryptoKey> {
  if (!secret || secret === "CHANGE_ME" || encoder.encode(secret).length < 32) {
    throw new HttpError(503, "AUTENTICACION_NO_CONFIGURADA");
  }
  return crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

const jwtHeader = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9";

async function seal(payload: object, secret: string): Promise<string> {
  const token = base64url(encoder.encode(JSON.stringify(payload)));
  const signed = jwtHeader + "." + token;
  const signature = await crypto.subtle.sign("HMAC", await sessionKey(secret), encoder.encode(signed));
  return signed + "." + base64url(new Uint8Array(signature));
}

async function unseal(raw: string, secret: string): Promise<Record<string, unknown> | null> {
  if (raw.length > 4096) return null;
  const [header, token, mac, extra] = raw.split(".");
  if (header !== jwtHeader || !token || !mac || extra !== undefined) return null;
  const key = await sessionKey(secret);
  try {
    if (!(await crypto.subtle.verify("HMAC", key, unbase64(mac), encoder.encode(header + "." + token)))) return null;
    const parsed: unknown = JSON.parse(decoder.decode(unbase64(token)));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const value = parsed as Record<string, unknown>;
    return typeof value.exp === "number" && Number.isSafeInteger(value.exp) && value.exp > now() ? value : null;
  } catch {
    return null;
  }
}

function cookie(name: string, value: string, lifetime: number): string {
  return `${name}=${value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${lifetime}`;
}

function readCookie(request: Request, name: string): string {
  const values = (request.headers.get("cookie") ?? "").split(";").map((item) => item.trim())
    .filter((item) => item.startsWith(name + "="));
  return values.length === 1 ? values[0]?.slice(name.length + 1) ?? "" : "";
}

export async function startGoogle(env: AuthEnv): Promise<Response> {
  if (!env.GOOGLE_CLIENT_ID.endsWith(".apps.googleusercontent.com") || !env.GOOGLE_CLIENT_SECRET) {
    throw new HttpError(503, "GOOGLE_NO_CONFIGURADO");
  }
  const state = random();
  const nonce = random();
  const verifier = random();
  const challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(verifier))));
  const url = new URL("/o/oauth2/v2/auth", googleIssuer);
  url.search = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID, redirect_uri: callback(env), response_type: "code",
    scope: "openid email profile", state, nonce, code_challenge: challenge, code_challenge_method: "S256",
  }).toString();
  const signed = await seal({ kind: "oauth", state, nonce, verifier, exp: now() + stateSeconds }, env.STUDIO_JWT_SESSION_SECRET);
  return new Response(null, { status: 302, headers: {
    Location: url.href, "Set-Cookie": cookie(cookieState, signed, stateSeconds),
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
  } });
}

interface GoogleIdentity {
  sub: string;
  email: string;
  name: string | null;
}

interface GoogleKey extends JsonWebKey {
  kid?: string;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("OBJETO_INVALIDO");
  return value as Record<string, unknown>;
}

async function googleSigningKeys(send: typeof fetch, cancel: AbortSignal): Promise<GoogleKey[]> {
  const jwks = await send(jwksUrl, { redirect: "manual", signal: AbortSignal.any([cancel, AbortSignal.timeout(8000)]) });
  if (!jwks.ok) throw new Error();
  const keys: unknown = object(await jwks.json()).keys;
  if (!Array.isArray(keys) || keys.length > 32) throw new Error();
  return keys as GoogleKey[];
}

async function verifiedGoogleToken(
  jwt: string, client: string, nonce: string, signingKeys: Promise<GoogleKey[] | null>,
): Promise<GoogleIdentity> {
  try {
    if (jwt.length > 8192) throw new Error();
    const [header, claims, signature, extra] = jwt.split(".");
    if (!header || !claims || !signature || extra !== undefined) throw new Error();
    const h = object(JSON.parse(decoder.decode(unbase64(header))));
    if (h.alg !== "RS256" || typeof h.kid !== "string" || !urlBytes.test(h.kid)) throw new Error();
    const keys = await signingKeys;
    if (!keys) throw new Error();
    const choices = keys.filter((k) => k.kid === h.kid && k.kty === "RSA" && k.alg === "RS256" && k.use === "sig");
    const jwk = choices[0];
    if (choices.length !== 1 || !jwk) throw new Error();
    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    if (!(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, unbase64(signature), encoder.encode(header + "." + claims)))) {
      throw new Error();
    }
    const c = object(JSON.parse(decoder.decode(unbase64(claims))));
    const time = now();
    if (
      (c.iss !== googleIssuer && c.iss !== "accounts.google.com") || c.aud !== client ||
      (c.azp !== undefined && c.azp !== client) || c.nonce !== nonce ||
      typeof c.exp !== "number" || !Number.isSafeInteger(c.exp) || c.exp <= time ||
      typeof c.iat !== "number" || !Number.isSafeInteger(c.iat) || c.iat > time + 60 ||
      typeof c.sub !== "string" || !/^[0-9]{1,255}$/.test(c.sub) ||
      c.email_verified !== true || typeof c.email !== "string" || c.email.length > 254 ||
      !/^[^\s@]+@[^\s@]+$/.test(c.email)
    ) throw new Error();
    return { sub: c.sub, email: c.email, name: typeof c.name === "string" ? c.name.slice(0, 120) : null };
  } catch {
    throw new HttpError(401, "TOKEN_GOOGLE_INVALIDO");
  }
}

async function exchangeGoogleCode(code: string, verifier: string, env: AuthEnv, send: typeof fetch): Promise<string> {
  const tokenRequest = new URLSearchParams({ code, client_id: env.GOOGLE_CLIENT_ID, client_secret: env.GOOGLE_CLIENT_SECRET,
    code_verifier: verifier, redirect_uri: callback(env), grant_type: "authorization_code" });
  let result: Response;
  try {
    result = await send(tokenUrl, {
      method: "POST", redirect: "manual", signal: AbortSignal.timeout(10000),
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body: tokenRequest.toString(),
    });
  } catch {
    throw new HttpError(503, "GOOGLE_TOKEN_INACCESIBLE");
  }
  if (!result.ok) throw new HttpError(401, "GOOGLE_INTERCAMBIO_RECHAZADO");
  let token: unknown;
  try {
    token = object(await result.json()).id_token;
  } catch {
    throw new HttpError(502, "GOOGLE_RESPUESTA_INVALIDA");
  }
  if (typeof token !== "string") throw new HttpError(401, "TOKEN_GOOGLE_INVALIDO");
  return token;
}

export async function finishGoogle(request: Request, env: AuthEnv, ctx: ExecutionTasks, send: typeof fetch = fetch): Promise<Response> {
  const query = new URL(request.url).searchParams;
  const state = await unseal(readCookie(request, cookieState), env.STUDIO_JWT_SESSION_SECRET);
  const code = query.get("code");
  if (
    state?.kind !== "oauth" || typeof state.nonce !== "string" || typeof state.verifier !== "string" ||
    query.getAll("state").length !== 1 || query.get("state") !== state.state ||
    query.getAll("code").length !== 1 || !code || code.length > 2048
  ) throw new HttpError(400, "SESION_GOOGLE_INVALIDA");
  const cancelKeys = new AbortController();
  const token = exchangeGoogleCode(code, state.verifier, env, send);
  const signingKeys = googleSigningKeys(send, cancelKeys.signal).catch(() => null);
  let person: GoogleIdentity;
  try {
    person = await verifiedGoogleToken(await token, env.GOOGLE_CLIENT_ID, state.nonce, signingKeys);
  } finally {
    cancelKeys.abort();
  }
  const time = now();
  const upsertUser = `INSERT INTO users(id,google_sub,email,display_name,created_at,updated_at)
    VALUES(?,?,?,?,?,?) ON CONFLICT(google_sub) DO UPDATE SET
    email=excluded.email,display_name=excluded.display_name,updated_at=excluded.updated_at`;
  const userParams = [crypto.randomUUID(), person.sub, person.email, person.name, time, time];
  const nonce = random();
  const exp = time + sessionSeconds;
  let userId: string;
  let signed: string;
  let id: string;
  if (env.STUDIO_SESSION_BATCH_ENABLED === "true") {
    signed = await seal({ kind: "session", version: 2, nonce, exp }, env.STUDIO_JWT_SESSION_SECRET);
    id = await sha256Hex(signed);
    const [, sessions] = await env.DB.batch<{ user_id: string }>([
      env.DB.prepare(upsertUser).bind(...userParams),
      env.DB.prepare(`INSERT INTO sessions(id,user_id,nonce,expires_at,created_at)
        SELECT ?,id,?,?,? FROM users WHERE google_sub=? RETURNING user_id`).bind(id, nonce, exp, time, person.sub),
    ]);
    const user = sessions?.results[0];
    if (!user) throw new HttpError(503, "SESION_NO_DISPONIBLE");
    userId = user.user_id;
  } else {
    const active = env.DB.withSession("first-primary");
    const user = await active.prepare(`${upsertUser} RETURNING id,email,display_name`).bind(...userParams).first<PortalIdentity>();
    if (!user) throw new HttpError(503, "SESION_NO_DISPONIBLE");
    userId = user.id;
    signed = await seal({ kind: "session", nonce, sub: userId, exp }, env.STUDIO_JWT_SESSION_SECRET);
    id = await sha256Hex(signed);
    await active.prepare("INSERT INTO sessions(id,user_id,nonce,expires_at,created_at) VALUES(?,?,?,?,?)")
      .bind(id, userId, nonce, exp, time).run();
  }
  ctx.waitUntil(env.SESSIONS.put("session:" + id, userId, { expirationTtl: sessionSeconds }).catch(() => undefined));
  const headers = new Headers({ Location: new URL("/", env.PUBLIC_ORIGIN).href, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" });
  headers.append("Set-Cookie", cookie(cookieState, "", 0));
  headers.append("Set-Cookie", cookie(cookieSession, signed, sessionSeconds));
  return new Response(null, { status: 303, headers });
}

export async function sessionLookup(request: Request, env: AuthEnv) {
  const raw = readCookie(request, cookieSession);
  const claims = await unseal(raw, env.STUDIO_JWT_SESSION_SECRET);
  const legacy = claims?.version === undefined && typeof claims?.sub === "string" ? claims.sub : null;
  const current = claims?.version === 2 && claims.sub === undefined;
  if (claims?.kind !== "session" || typeof claims.nonce !== "string" || (legacy === null && !current)) {
    throw new HttpError(401, "AUTENTICACION_REQUERIDA");
  }
  const id = await sha256Hex(raw);
  const ownerQuery = `SELECT user_id FROM sessions WHERE id=? AND nonce=? AND revoked_at IS NULL AND expires_at>?
    ${legacy === null ? "" : "AND user_id=?"}`;
  const ownerParams = [id, claims.nonce, now(), ...(legacy === null ? [] : [legacy])];
  return {
    ownerQuery,
    ownerParams,
    statement: (db: SqlSession) => db.prepare(`SELECT id,email,display_name FROM users WHERE id=(${ownerQuery})`)
      .bind(...ownerParams),
  };
}

export async function identity(request: Request, env: AuthEnv): Promise<PortalIdentity> {
  const lookup = await sessionLookup(request, env);
  const result = await lookup.statement(env.DB.withSession("first-primary")).first<PortalIdentity>();
  if (!result) throw new HttpError(401, "AUTENTICACION_REQUERIDA");
  return result;
}

export async function logout(request: Request, env: AuthEnv, ctx: ExecutionTasks): Promise<Response> {
  const raw = readCookie(request, cookieSession);
  const id = await sha256Hex(raw);
  if (raw) {
    await env.DB.prepare("UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL").bind(now(), id).run();
    ctx.waitUntil(env.SESSIONS.delete("session:" + id).catch(() => undefined));
  }
  return new Response(null, { status: 204, headers: { "Cache-Control": "no-store", "Set-Cookie": cookie(cookieSession, "", 0) } });
}
