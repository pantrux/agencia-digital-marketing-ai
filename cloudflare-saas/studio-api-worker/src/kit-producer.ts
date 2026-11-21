import { completeOrderWithKit, publishRevisionKit, type KitsEnv } from "./kits.js";
import { HttpError, readJson, response } from "./platform.js";

export interface KitProducerEnv {
  STUDIO_KIT_PRODUCER_HMAC_SECRET?: string;
  STUDIO_DELIVERY_REVISION_ENABLED?: string;
}

export const kitProducerPath = "/api/internal/kits/publish";
const fields = ["id", "orderId", "userId", "title", "bytes", "sha256", "uploadId", "sourceSha256"] as const;
const encoder = new TextEncoder();

export async function handleKitProducer(request: Request, env: KitsEnv & KitProducerEnv): Promise<Response> {
  if (request.method !== "POST") throw new HttpError(405, "METODO_NO_PERMITIDO");
  const secret = env.STUDIO_KIT_PRODUCER_HMAC_SECRET ?? "";
  if (!/^[a-f0-9]{64}$/u.test(secret) || secret.length !== 64) throw new HttpError(404, "RUTA_NO_DISPONIBLE");
  const timestamp = request.headers.get("timestamp") ?? "";
  const signature = request.headers.get("signature") ?? "";
  const now = Math.floor(Date.now() / 1000);
  if (!/^[1-9][0-9]{9}$/u.test(timestamp) || Math.abs(now - Number(timestamp)) > 120
    || signature.length !== 67 || !/^v[12345]=[a-f0-9]{64}$/u.test(signature)) {
    throw new HttpError(401, "PUBLICACION_NO_AUTORIZADA");
  }

  const body = await readJson(request, 4096);
  const version = signature.slice(0, 2);
  const revision = version === "v4" || version === "v5";
  if (revision && env.STUDIO_DELIVERY_REVISION_ENABLED !== "true") throw new HttpError(404, "RUTA_NO_DISPONIBLE");
  const hasDuration = version !== "v1";
  const hasProres = version === "v3" || version === "v5";
  const signedFields = [...fields, ...(hasDuration ? ["durationMs"] : []),
    ...(hasProres ? ["proresBytes", "proresSha256"] : []), ...(revision ? ["revisionId", "lease"] : [])];
  const { id, orderId, userId, title, bytes, sha256, uploadId, sourceSha256, durationMs, proresBytes, proresSha256, revisionId, lease } = body;
  const prores = hasProres && typeof proresBytes === "number" && typeof proresSha256 === "string"
    ? { bytes: proresBytes, sha256: proresSha256 } : undefined;
  if (Object.keys(body).length !== signedFields.length || typeof id !== "string" || typeof orderId !== "string"
    || typeof userId !== "string" || typeof title !== "string" || typeof bytes !== "number" || typeof sha256 !== "string"
    || typeof uploadId !== "string" || typeof sourceSha256 !== "string"
    || (hasDuration && typeof durationMs !== "number") || (hasProres && !prores)
    || (revision && (typeof revisionId !== "string" || typeof lease !== "string"))) {
    throw new HttpError(422, "KIT_DATOS_INVALIDOS");
  }

  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  const supplied = Uint8Array.from(signature.slice(3).match(/.{2}/gu) ?? [], (value) => Number.parseInt(value, 16));
  const context = "studio-kit-" + version;
  const message = JSON.stringify([context, "POST", kitProducerPath, env.BILLING_MODE, timestamp, signedFields.map((field) => body[field])]);
  if (!await crypto.subtle.verify("HMAC", key, supplied, encoder.encode(message))) {
    throw new HttpError(401, "PUBLICACION_NO_AUTORIZADA");
  }

  const input = { id, orderId, userId, title, bytes, sha256, uploadId, sourceSha256,
    ...(hasDuration && typeof durationMs === "number" ? { durationMs } : {}), ...(prores ? { prores } : {}) };
  const kit = revision && typeof revisionId === "string" && typeof lease === "string"
    ? await publishRevisionKit(env, {...input, revisionId, lease}) : await completeOrderWithKit(env, input);
  return response({ kit });
}
