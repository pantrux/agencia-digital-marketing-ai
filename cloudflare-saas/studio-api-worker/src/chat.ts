import { HttpError, readJson, response, type SqlDatabase } from "./platform.js";

const brokerOrigin = "https://api-studio.e-scale.cl";
const brokerPath = "/v1/sofia/chat";
const protocol = "studio-sofia-v1";
const freshnessSeconds = 300;
const deadlineMs = 45_000;
const maxInput = 2048;
const maxReply = 8192;
const maxReceipt = 24 * 1024;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const encoder = new TextEncoder();

// Contrato LAN: POST exclusivo al broker, sin token ni rutas de OpenClaw en el Worker.
// El broker valida firma/frescura, vincula el turno a (actor_id, order_id) y deduplica
// por request_id, comparando el cuerpo autenticado. Un mismo ID con otros datos da 409.
// La operación se limita a producir texto como Sofía, con identidad y política del
// servicio propias. Ni el mensaje ni el portal eligen runtime, herramientas o permisos.
export interface ChatEnv {
  DB: SqlDatabase;
  BILLING_MODE: "sandbox" | "production";
  SOFIA_OWNER_USER_ID?: string;
  SOFIA_BROKER_ORIGIN?: string;
  STUDIO_SOFIA_HMAC_SECRET?: string;
}

interface BrokerRequest {
  agent_id: "agency-account";
  actor_id: string;
  order_id: string;
  billing_mode: "sandbox";
  plan_id: "free";
  request_id: string;
  message: string;
}

interface BrokerReply {
  actor_id: string;
  order_id: string;
  request_id: string;
  text: string;
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && value.length === 36 && uuid.test(value);
}

function validText(value: unknown, maximum: number): value is string {
  if (typeof value !== "string" || value.length > maximum || value.trim().length === 0
    || encoder.encode(value).length > maximum) return false;
  for (const character of value) {
    const code = character.codePointAt(0)!;
    if ((code < 32 && ![9, 10, 13].includes(code)) || code === 127
      || (code >= 0xd800 && code <= 0xdfff)) return false;
  }
  return true;
}

function hex(data: ArrayBuffer): string {
  return Array.from(new Uint8Array(data), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function now(): number { return Math.floor(Date.now() / 1000); }

async function keyFor(env: ChatEnv): Promise<CryptoKey> {
  const value = env.STUDIO_SOFIA_HMAC_SECRET;
  if (env.SOFIA_BROKER_ORIGIN !== brokerOrigin || !value || value === "CHANGE_ME"
    || value.length < 32 || value.length > 128 || /[^!-~]/u.test(value)) {
    throw new HttpError(503, "CHAT_NO_DISPONIBLE");
  }
  const bytes = encoder.encode(value);
  try {
    return await crypto.subtle.importKey("raw", bytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
  } finally { bytes.fill(0); }
}

async function signingMessage(
  direction: "request" | "response", timestamp: string, discriminator: string, raw: Uint8Array<ArrayBuffer>,
): Promise<Uint8Array<ArrayBuffer>> {
  const hash = hex(await crypto.subtle.digest("SHA-256", raw));
  return encoder.encode([protocol, direction, timestamp, discriminator, brokerPath, hash].join("\n"));
}

function readSignature(value: string | null): Uint8Array<ArrayBuffer> | null {
  if (value?.length !== 67 || !/^v1=[a-f0-9]{64}$/u.test(value)) return null;
  const bytes = new Uint8Array(32);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Number.parseInt(value.slice(3 + 2 * index, 5 + 2 * index), 16);
  }
  return bytes;
}

function invalidReply(): never { throw new HttpError(502, "CHAT_RESPUESTA_INVALIDA"); }

async function brokerBytes(receipt: Response): Promise<Uint8Array<ArrayBuffer>> {
  if (!receipt.body || receipt.headers.get("content-type")?.split(";", 1)[0]?.toLowerCase() !== "application/json") {
    await receipt.body?.cancel();
    return invalidReply();
  }
  const reader = receipt.body.getReader();
  const bytes = new Uint8Array(maxReceipt);
  let length = 0;
  try {
    for (;;) {
      const next = await reader.read();
      if (next.done) return bytes.subarray(0, length);
      if (length + next.value.length > maxReceipt) {
        await reader.cancel();
        return invalidReply();
      }
      bytes.set(next.value, length);
      length += next.value.length;
    }
  } finally { reader.releaseLock(); }
}

async function verifyReply(receipt: Response, request: BrokerRequest, key: CryptoKey): Promise<BrokerReply> {
  const timestamp = receipt.headers.get("timestamp") ?? "";
  const resultTime = /^(?:0|[1-9][0-9]{0,15})$/u.exec(timestamp)?.[0] === timestamp
    ? Number(timestamp) : Number.NaN;
  const signature = readSignature(receipt.headers.get("signature"));
  if (!signature || !Number.isSafeInteger(resultTime) || Math.abs(now() - resultTime) > freshnessSeconds) {
    await receipt.body?.cancel();
    return invalidReply();
  }
  const raw = await brokerBytes(receipt);
  if (!await crypto.subtle.verify("HMAC", key, signature, await signingMessage("response", timestamp, "200", raw))) {
    return invalidReply();
  }
  let fields: unknown;
  try { fields = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)); }
  catch { return invalidReply(); }
  if (!fields || typeof fields !== "object" || Array.isArray(fields)) return invalidReply();
  const { actor_id, order_id, request_id, text } = fields as Record<string, unknown>;
  if (Object.keys(fields).length !== 4 || actor_id !== request.actor_id || order_id !== request.order_id
    || request_id !== request.request_id || !validText(text, maxReply)) return invalidReply();
  return { actor_id, order_id, request_id, text };
}

/** El router ya verificó el origen y la identidad autenticada de la petición. */
export async function handleSofiaChat(
  request: Request, env: ChatEnv, userId: string, send: typeof fetch = fetch,
): Promise<Response> {
  if (request.method !== "POST") throw new HttpError(405, "METODO_INVALIDO");
  if (!isUuid(userId)) throw new HttpError(401, "AUTENTICACION_REQUERIDA");
  if (env.BILLING_MODE !== "sandbox" || !isUuid(env.SOFIA_OWNER_USER_ID)) {
    throw new HttpError(503, "CHAT_NO_DISPONIBLE");
  }
  if (userId !== env.SOFIA_OWNER_USER_ID) throw new HttpError(403, "CHAT_NO_AUTORIZADO");
  const requestId = request.headers.get("idempotency-key");
  if (!isUuid(requestId)) throw new HttpError(400, "IDEMPOTENCIA_INVALIDA");
  const body = await readJson(request, 12288);
  const { order_id, message } = body;
  if (Object.keys(body).length !== 2 || !isUuid(order_id) || !validText(message, maxInput)) {
    throw new HttpError(422, "CHAT_ARGUMENTOS_INVALIDOS");
  }
  const key = await keyFor(env);
  const row = await env.DB.withSession("first-primary").prepare(`
    SELECT o.id, o.plan_id FROM orders o
    JOIN upload_sessions s ON s.order_id = o.id AND s.user_id = o.user_id
    JOIN usage_ledger u ON u.order_id = o.id AND u.user_id = o.user_id
    WHERE o.id = ? AND o.user_id = ? AND COALESCE(o.provider_mode, 'production') = ?
      AND o.kind = 'production' AND o.status IN ('queued', 'processing', 'completed')
      AND u.status IN ('reserved', 'consumed') AND s.status = 'completed' AND length(s.object_etag) > 0
  `).bind(order_id, userId, env.BILLING_MODE).first<{ id: string; plan_id: string }>();
  if (!row || row.id !== order_id) throw new HttpError(404, "ORDEN_NO_ENCONTRADA");
  if (row.plan_id !== "free") throw new HttpError(403, "CHAT_NO_AUTORIZADO");
  const actor = hex(await crypto.subtle.sign("HMAC", key, encoder.encode(`${protocol}\nactor\n${userId}`)));
  const outbound: BrokerRequest = {
    agent_id: "agency-account", actor_id: actor, order_id,
    billing_mode: "sandbox", plan_id: "free", request_id: requestId, message: message.trim(),
  };
  const raw = JSON.stringify(outbound);
  const timestamp = String(now());
  const signature = hex(await crypto.subtle.sign(
    "HMAC", key, await signingMessage("request", timestamp, "POST", encoder.encode(raw)),
  ));
  let receipt: Response;
  try {
    receipt = await send(brokerOrigin + brokerPath, {
      method: "POST", redirect: "manual", cache: "no-store",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(deadlineMs)]),
      headers: { "content-type": "application/json", accept: "application/json", timestamp, signature: `v1=${signature}` },
      body: raw,
    });
  } catch { throw new HttpError(503, "CHAT_NO_DISPONIBLE"); }
  if (receipt.status !== 200) {
    await receipt.body?.cancel().catch(() => undefined);
    if (receipt.status === 409) throw new HttpError(409, "CHAT_EN_CONFLICTO");
    if (receipt.status === 429) throw new HttpError(429, "CHAT_OCUPADO");
    throw new HttpError(503, "CHAT_NO_DISPONIBLE");
  }
  const reply = await verifyReply(receipt, outbound, key);
  return response({ text: reply.text, request_id: reply.request_id });
}
