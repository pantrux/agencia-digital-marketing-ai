import { HttpError, ownerId, prepareOwnedStatement, response, type SqlDatabase, type SqlOwner, type SqlSession } from "./platform.js";
import { storageFits, storageQuotaArguments } from "./storage-quota.js";

type KitMode = "sandbox" | "production";

interface KitObject {
  key: string;
  size: number;
  etag: string;
  version?: string;
  customMetadata?: Record<string, string>;
}

export interface KitsEnv {
  DB: SqlDatabase;
  BILLING_MODE: KitMode;
  MEDIA: {
    head(key: string): Promise<KitObject | null>;
    get(key: string, options?: { range: { offset: number; length: number } }): Promise<(KitObject & { body: ReadableStream }) | null>;
  };
}

export interface DeliveryKitInput {
  id: string;
  orderId: string;
  userId: string;
  title: string;
  bytes: number;
  sha256: string;
  durationMs?: number;
}

export interface ApprovedKitInput extends DeliveryKitInput {
  uploadId: string;
  sourceSha256: string;
  prores?: { bytes: number; sha256: string };
}

export interface DeliveryKit {
  id: string;
  order_id: string;
  title: string;
  bytes: number;
  created_at: number;
  url: string;
  prores?: { bytes: number; url: string };
}

export type KitRow = Omit<DeliveryKit, "url" | "prores"> & {
  sha256: string;
  r2_etag: string;
  r2_version: string;
  duration_ms: number | null;
  prores_bytes: number | null;
  prores_sha256: string | null;
  prores_r2_etag: string | null;
  prores_r2_version: string | null;
};
type GraphicIdentity = [bytes: number | null, digest: string | null, etag: string | null, version: string | null];
const noGraphic: GraphicIdentity = [null, null, null, null];

const ROOT = "/api/kits";
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/u;
const sha256 = /^[a-f0-9]{64}$/u;
const objectIdentity = /^[A-Za-z0-9._=-]{1,128}$/u;
const MAX_BYTES = 5 * 1024 ** 4;
const MAX_LIST = 50;
const scopeForOrder = (owner = "?") => `o.user_id=${owner} AND COALESCE(o.provider_mode,'production')=? AND o.kind='production' AND o.status='completed'`;
const orderScope = scopeForOrder();
const selectPublished = (owner = "?") => `SELECT k.id,k.order_id,k.title,k.bytes,k.sha256,k.r2_etag,k.r2_version,k.created_at,k.duration_ms,
  k.prores_bytes,k.prores_sha256,k.prores_r2_etag,k.prores_r2_version
  FROM delivery_kits k JOIN orders o ON o.id=k.order_id AND o.user_id=k.user_id
  WHERE ${scopeForOrder(owner)} AND k.provider_mode=?`;
const published = selectPublished();
const withinRetention = `AND EXISTS (SELECT 1 FROM plan_quotas p WHERE p.plan_id=o.plan_id
  AND (p.retention_days IS NULL OR k.created_at + p.retention_days * 86400 > ?))`;

function deny(status: number, code: string): never { throw new HttpError(status, code); }

function validId(value: string): boolean { return typeof value === "string" && value.length === 36 && uuid.test(value); }

function mode(value: string): KitMode {
  if (value !== "sandbox" && value !== "production") deny(503, "KITS_NO_CONFIGURADOS");
  return value;
}

export function deliveryKitKey(environment: KitMode, orderId: string, kitId: string, extension: "zip" | "mov" = "zip"): string {
  if (!validId(orderId) || !validId(kitId)) deny(422, "KIT_IDENTIDAD_INVALIDA");
  return "kits/" + mode(environment) + "/" + orderId + "/" + kitId + "." + extension;
}

function openRead(env: KitsEnv) { return env.DB.withSession("first-primary"); }

export function kitView(kit: KitRow): DeliveryKit {
  return { id: kit.id, order_id: kit.order_id, title: kit.title, bytes: kit.bytes,
    created_at: kit.created_at, url: ROOT + "/" + kit.id + "/download",
    ...(kit.prores_bytes === null ? {} : { prores: { bytes: kit.prores_bytes, url: ROOT + "/" + kit.id + "/prores" } }) };
}

function videoEntry(bytes: Uint8Array, kitSize: number): { offset: number; size: number } {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (bytes.length < 30 || view.getUint32(0, true) !== 0x04034b50 ||
      view.getUint16(8, true) !== 0 || (view.getUint16(6, true) & 9) !== 0) {
    deny(409, "VIDEO_NO_DISPONIBLE");
  }
  const nameLength = view.getUint16(26, true);
  const extraLength = view.getUint16(28, true);
  const offset = 30 + nameLength + extraLength;
  if (offset > bytes.length || new TextDecoder().decode(bytes.subarray(30, 30 + nameLength)) !== "01-video.mp4") {
    deny(409, "VIDEO_NO_DISPONIBLE");
  }
  let size = view.getUint32(22, true);
  if (size === 0xffffffff) {
    let cursor = 30 + nameLength;
    let found = false;
    while (cursor + 4 <= offset) {
      const type = view.getUint16(cursor, true), length = view.getUint16(cursor + 2, true);
      cursor += 4;
      if (cursor + length > offset) deny(409, "VIDEO_NO_DISPONIBLE");
      if (type === 1 && length >= 16) {
        const uncompressed = view.getBigUint64(cursor, true);
        const compressed = view.getBigUint64(cursor + 8, true);
        if (uncompressed !== compressed || uncompressed > BigInt(Number.MAX_SAFE_INTEGER)) deny(409, "VIDEO_NO_DISPONIBLE");
        size = Number(uncompressed);
        found = true;
        break;
      }
      cursor += length;
    }
    if (!found) deny(409, "VIDEO_NO_DISPONIBLE");
  } else if (size !== view.getUint32(18, true)) deny(409, "VIDEO_NO_DISPONIBLE");
  if (!Number.isSafeInteger(size) || size <= 0 || offset + size > kitSize) deny(409, "VIDEO_NO_DISPONIBLE");
  return { offset, size };
}

function videoRange(value: string | null, size: number): { start: number; end: number; partial: boolean } {
  if (!value) return { start: 0, end: size - 1, partial: false };
  const match = /^bytes=(\d*)-(\d*)$/u.exec(value);
  if (!match || (!match[1] && !match[2])) throw new HttpError(416, "RANGO_INVALIDO");
  const suffix = match[1] === "";
  const first = Number(suffix ? match[2] : match[1]);
  const last = match[2] && !suffix ? Number(match[2]) : size - 1;
  if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || first < 0 ||
      (suffix && first === 0)) throw new HttpError(416, "RANGO_INVALIDO");
  const start = suffix ? Math.max(0, size - first) : first;
  const end = Math.min(size - 1, last);
  if (start >= size || start > end) throw new HttpError(416, "RANGO_INVALIDO");
  return { start, end, partial: true };
}

function metadata(object: KitObject | null, key: string, bytes: number, digest: string): object is KitObject & { version: string } {
  return object !== null && object.key === key && object.size === bytes
    && object.customMetadata?.sha256 === digest && objectIdentity.test(object.etag)
    && typeof object.version === "string" && objectIdentity.test(object.version);
}

function validateKit(input: DeliveryKitInput): void {
  const { userId, title, bytes, sha256: digest, durationMs } = input;
  if (!userId || typeof title !== "string" || title.trim() !== title || title.length < 1
    || title.length > 160 || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(title)
    || !Number.isSafeInteger(bytes) || bytes < 1 || bytes > MAX_BYTES
    || typeof digest !== "string" || digest.length !== 64 || !sha256.test(digest)
    || (durationMs !== undefined && (!Number.isSafeInteger(durationMs) || durationMs < 1 || durationMs > 600100))) {
    deny(422, "KIT_DATOS_INVALIDOS");
  }
}

function sameKit(saved: KitRow | null, input: DeliveryKitInput, object: KitObject, graphic: GraphicIdentity): saved is KitRow {
  return saved !== null && saved.order_id === input.orderId && saved.title === input.title && saved.bytes === input.bytes
    && saved.sha256 === input.sha256 && saved.r2_etag === object.etag && saved.r2_version === object.version
    && (input.durationMs === undefined || saved.duration_ms === input.durationMs)
    && saved.prores_bytes === graphic[0] && saved.prores_sha256 === graphic[1]
    && saved.prores_r2_etag === graphic[2] && saved.prores_r2_version === graphic[3];
}

const ordersWithPlan = "FROM orders o JOIN plan_quotas p ON p.plan_id=o.plan_id";
const quotaRejection = `AND NOT EXISTS (SELECT 1 FROM delivery_kits WHERE id=?) AND NOT ${storageFits}`;

function publicationResult(saved: KitRow | null, quotaRejected: boolean, input: DeliveryKitInput, object: KitObject, graphic = noGraphic): DeliveryKit {
  if (sameKit(saved, input, object, graphic)) return kitView(saved);
  deny(409, quotaRejected ? "ALMACENAMIENTO_AGOTADO" : "KIT_PUBLICACION_EN_CONFLICTO");
}

/** Registra un kit aprobado por el productor autorizado, después de su publicación en R2. */
export async function publishDeliveryKit(env: KitsEnv, input: DeliveryKitInput): Promise<DeliveryKit> {
  const { id, orderId, userId, title, bytes, sha256: digest, durationMs } = input;
  const billing = mode(env.BILLING_MODE);
  const key = deliveryKitKey(billing, orderId, id);
  validateKit(input);
  const scope = orderScope + " AND o.output_format='mp4'";
  const ownOrder = await openRead(env).prepare("SELECT o.id FROM orders o WHERE " + scope + " AND o.id=?")
    .bind(userId, billing, orderId).first<{ id: string }>();
  if (!ownOrder) deny(404, "KIT_NO_ENCONTRADO");
  const object = await env.MEDIA.head(key);
  if (!metadata(object, key, bytes, digest)) deny(409, "KIT_OBJETO_CAMBIO");

  const timestamp = Math.floor(Date.now() / 1000);
  const quota = storageQuotaArguments(bytes, timestamp);
  const scoped = [userId, billing, orderId] as const;
  const insert = env.DB.prepare(`INSERT INTO delivery_kits(id,order_id,user_id,provider_mode,title,bytes,sha256,r2_etag,r2_version,created_at,duration_ms)
    SELECT ?,o.id,o.user_id,?,?,?,?,?,?,?,? ${ordersWithPlan} WHERE ${scope} AND o.id=? AND ${storageFits}
    ON CONFLICT(id) DO NOTHING`).bind(id, billing, title, bytes, digest, object.etag, object.version,
    timestamp, durationMs ?? null, ...scoped, ...quota);
  const select = env.DB.prepare(published + " AND k.id=?").bind(userId, billing, billing, id);
  const rejected = env.DB.prepare(`SELECT o.id ${ordersWithPlan} WHERE ${scope} AND o.id=? ${quotaRejection}`)
    .bind(...scoped, id, ...quota);
  const [, selected, limit] = await env.DB.batch<KitRow | { id: string }>([insert, select, rejected]);
  return publicationResult(selected?.results[0] as KitRow | undefined ?? null, Boolean(limit?.results.length), input, object);
}

/** Cierra la primera entrega y su pedido en la misma transacción, tras verificar el objeto. */
export async function completeOrderWithKit(env: KitsEnv, input: ApprovedKitInput): Promise<DeliveryKit> {
  const { id, orderId, userId, title, bytes, sha256: digest, uploadId, sourceSha256, durationMs, prores } = input;
  const billing = mode(env.BILLING_MODE);
  const key = deliveryKitKey(billing, orderId, id);
  validateKit(input);
  if (prores && (durationMs === undefined || !Number.isSafeInteger(prores.bytes)
    || prores.bytes < 1 || prores.bytes > MAX_BYTES || !sha256.test(prores.sha256) || prores.sha256.length !== 64)) {
    deny(422, "KIT_DATOS_INVALIDOS");
  }
  if (!validId(userId) || !validId(uploadId) || !sha256.test(sourceSha256) || sourceSha256.length !== 64) {
    deny(422, "KIT_ORIGEN_INVALIDO");
  }
  const scope = `o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=? AND o.kind='production'
    AND o.output_format=?
    AND EXISTS (SELECT 1 FROM production_primary_sources s WHERE s.order_id=o.id AND s.user_id=o.user_id
      AND s.id=? AND s.sha256=? AND s.status='completed')
    AND EXISTS (SELECT 1 FROM usage_ledger u WHERE u.order_id=o.id AND u.user_id=o.user_id AND u.status='consumed')
    AND NOT EXISTS (SELECT 1 FROM production_manual_reviews r WHERE r.order_id=o.id)`;
  const scoped = [orderId, userId, billing, prores ? "prores4444" : "mp4", uploadId, sourceSha256] as const;
  const order = await openRead(env).prepare(`SELECT o.status FROM orders o WHERE ${scope}
    AND o.status IN ('processing','completed')`).bind(...scoped).first<{ status: string }>();
  if (!order) deny(404, "KIT_NO_ENCONTRADO");
  const object = await env.MEDIA.head(key);
  if (!metadata(object, key, bytes, digest)) deny(409, "KIT_OBJETO_CAMBIO");
  let graphic = noGraphic;
  if (prores) {
    const movieKey = deliveryKitKey(billing, orderId, id, "mov");
    const movie = await env.MEDIA.head(movieKey);
    if (!metadata(movie, movieKey, prores.bytes, prores.sha256)) deny(409, "KIT_OBJETO_CAMBIO");
    graphic = [prores.bytes, prores.sha256, movie.etag, movie.version];
  }
  const timestamp = Math.floor(Date.now() / 1000);
  const quota = storageQuotaArguments(bytes + (prores?.bytes ?? 0), timestamp);
  const firstKit = `AND o.status='processing' AND NOT EXISTS (SELECT 1 FROM delivery_kits k WHERE k.order_id=o.id)`;
  const prepared = env.DB.prepare(`INSERT INTO delivery_kits(id,order_id,user_id,provider_mode,title,bytes,sha256,r2_etag,r2_version,
    created_at,duration_ms,prores_bytes,prores_sha256,prores_r2_etag,prores_r2_version)
    SELECT ?,o.id,o.user_id,?,?,?,?,?,?,?,?,?,?,?,? ${ordersWithPlan} WHERE ${scope} ${firstKit}
    AND ${storageFits} ON CONFLICT(id) DO NOTHING`)
    .bind(id, billing, title, bytes, digest, object.etag, object.version, timestamp, durationMs ?? null, ...graphic, ...scoped, ...quota);
  const complete = env.DB.prepare(`UPDATE orders AS o SET status='completed',updated_at=? WHERE ${scope} AND o.status='processing'
    AND EXISTS (SELECT 1 FROM delivery_kits k WHERE k.id=? AND k.order_id=o.id AND k.user_id=o.user_id
      AND k.provider_mode=? AND k.title=? AND k.bytes=? AND k.sha256=? AND k.r2_etag=? AND k.r2_version=?
      AND (? IS NULL OR k.duration_ms=?) AND k.prores_bytes IS ? AND k.prores_sha256 IS ?
      AND k.prores_r2_etag IS ? AND k.prores_r2_version IS ?)`)
    .bind(timestamp, ...scoped, id, billing, title, bytes, digest, object.etag, object.version,
      durationMs ?? null, durationMs ?? null, ...graphic);
  const writes = [prepared];
  if (durationMs !== undefined) {
    writes.push(env.DB.prepare(`UPDATE delivery_kits SET duration_ms=? WHERE id=? AND order_id=? AND user_id=?
      AND provider_mode=? AND title=? AND bytes=? AND sha256=? AND r2_etag=? AND r2_version=? AND duration_ms IS NULL
      AND EXISTS (SELECT 1 FROM orders o WHERE ${scope} AND o.status='completed')`)
      .bind(durationMs, id, orderId, userId, billing, title, bytes, digest, object.etag, object.version, ...scoped));
  }
  writes.push(complete);
  const select = env.DB.prepare(published + " AND k.id=?").bind(userId, billing, billing, id);
  const rejected = env.DB.prepare(`SELECT o.id ${ordersWithPlan} WHERE ${scope} ${firstKit} ${quotaRejection}`)
    .bind(...scoped, id, ...quota);
  const results = await env.DB.batch<KitRow | { id: string }>([...writes, select, rejected]);
  const [selected, limit] = results.slice(-2);
  return publicationResult(selected?.results[0] as KitRow | undefined ?? null, Boolean(limit?.results.length), input, object, graphic);
}

/** Registra otra entrega sin alterar la original; el caller autentica al productor y su dictamen. */
export async function publishRevisionKit(env: KitsEnv, input: ApprovedKitInput & {
  revisionId:string; lease:string; prores?:{bytes:number; sha256:string};
}): Promise<DeliveryKit> {
  const {id,orderId,userId,title,bytes,sha256:digest,durationMs,revisionId,lease,prores,uploadId,sourceSha256} = input;
  const billing = mode(env.BILLING_MODE);
  validateKit(input);
  if (!validId(id) || !validId(orderId) || !validId(userId) || !validId(revisionId) || !validId(lease)
    || !validId(uploadId) || !sha256.test(sourceSha256)
    || durationMs === undefined || (prores && (!Number.isSafeInteger(prores.bytes) || prores.bytes < 1
      || prores.bytes > MAX_BYTES || !sha256.test(prores.sha256)))) deny(422,"KIT_DATOS_INVALIDOS");
  const now = Math.floor(Date.now()/1000);
  const state = await openRead(env).prepare(`SELECT r.status,r.result_kit_id FROM delivery_revision_requests r
    JOIN orders o ON o.id=r.order_id AND o.user_id=r.user_id
    WHERE r.id=? AND r.order_id=? AND r.user_id=? AND o.status='completed'
      AND COALESCE(o.provider_mode,'production')=? AND o.output_format=?
      AND EXISTS (SELECT 1 FROM production_primary_sources s WHERE s.order_id=o.id AND s.user_id=o.user_id
        AND s.id=? AND s.sha256=? AND s.status='completed')
      AND EXISTS (SELECT 1 FROM usage_ledger u WHERE u.order_id=o.id AND u.user_id=o.user_id AND u.status='consumed')`)
    .bind(revisionId,orderId,userId,billing,prores ? "prores4444" : "mp4",uploadId,sourceSha256)
    .first<{status:string; result_kit_id:string | null}>();
  if (!state) deny(404,"REVISION_NO_ENCONTRADA");
  if (state.status === "completed" && state.result_kit_id !== id) deny(409,"KIT_PUBLICACION_EN_CONFLICTO");
  const key = deliveryKitKey(billing,orderId,id);
  const object = await env.MEDIA.head(key);
  if (!metadata(object,key,bytes,digest)) deny(409,"KIT_OBJETO_CAMBIO");
  let graphic = noGraphic;
  if (prores) {
    const movieKey = deliveryKitKey(billing,orderId,id,"mov");
    const movie = await env.MEDIA.head(movieKey);
    if (!metadata(movie,movieKey,prores.bytes,prores.sha256)) deny(409,"KIT_OBJETO_CAMBIO");
    graphic = [prores.bytes,prores.sha256,movie.etag,movie.version];
  }
  if (state.status === "completed") {
    const saved = await openRead(env).prepare(published + " AND k.id=?").bind(userId,billing,billing,id).first<KitRow>();
    return publicationResult(saved,false,input,object,graphic);
  }
  const scope = `o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=? AND o.kind='production'
    AND o.status='completed' AND o.output_format=?
    AND EXISTS (SELECT 1 FROM production_primary_sources s WHERE s.order_id=o.id AND s.user_id=o.user_id
      AND s.id=? AND s.sha256=? AND s.status='completed')
    AND EXISTS (SELECT 1 FROM usage_ledger u WHERE u.order_id=o.id AND u.user_id=o.user_id AND u.status='consumed')
    AND EXISTS (SELECT 1 FROM delivery_revision_requests r
      WHERE r.id=? AND r.order_id=o.id AND r.user_id=o.user_id AND r.status='processing'
        AND r.lease=? AND r.lease_until>? AND r.base_kit_id<>?)`;
  const scoped = [orderId,userId,billing,prores ? "prores4444" : "mp4",uploadId,sourceSha256,revisionId,lease,now,id] as const;
  const quota = storageQuotaArguments(bytes+(prores?.bytes ?? 0),now);
  const insert = env.DB.prepare(`INSERT INTO delivery_kits(id,order_id,user_id,provider_mode,title,bytes,sha256,r2_etag,r2_version,
    created_at,duration_ms,prores_bytes,prores_sha256,prores_r2_etag,prores_r2_version)
    SELECT ?,o.id,o.user_id,?,?,?,?,?,?,?,?,?,?,?,? ${ordersWithPlan} WHERE ${scope}
      AND ${storageFits} ON CONFLICT(id) DO NOTHING`)
    .bind(id,billing,title,bytes,digest,object.etag,object.version,now,durationMs,...graphic,...scoped,...quota);
  const finish = env.DB.prepare(`UPDATE delivery_revision_requests AS r SET status='completed',result_kit_id=?,
    lease=NULL,lease_until=NULL,updated_at=? WHERE r.id=? AND r.order_id=? AND r.user_id=?
    AND r.status='processing' AND r.lease=? AND r.lease_until>? AND EXISTS (SELECT 1 FROM delivery_kits k
      WHERE k.id=? AND k.order_id=r.order_id AND k.user_id=r.user_id AND k.provider_mode=? AND k.title=?
        AND k.bytes=? AND k.sha256=? AND k.r2_etag=? AND k.r2_version=? AND k.duration_ms=?
        AND k.prores_bytes IS ? AND k.prores_sha256 IS ? AND k.prores_r2_etag IS ? AND k.prores_r2_version IS ?)`)
    .bind(id,now,revisionId,orderId,userId,lease,now,id,billing,title,bytes,digest,object.etag,object.version,durationMs,...graphic);
  const select = env.DB.prepare(published + " AND k.id=?").bind(userId,billing,billing,id);
  const completed = env.DB.prepare("SELECT result_kit_id FROM delivery_revision_requests WHERE id=? AND status='completed' AND result_kit_id=?")
    .bind(revisionId,id);
  const [, , selected, done] = await env.DB.batch<KitRow | {result_kit_id:string}>([insert,finish,select,completed]);
  if (!done?.results.length) deny(409,"REVISION_NO_PUBLICADA");
  return publicationResult(selected?.results[0] as KitRow | undefined ?? null,false,input,object,graphic);
}

export function prepareKits(db: SqlSession, owner: SqlOwner | string, env: KitsEnv, now: number) {
  const billing = mode(env.BILLING_MODE);
  return prepareOwnedStatement(db, owner, selectPublished(ownerId) + " " + withinRetention + " ORDER BY k.created_at DESC,k.id DESC LIMIT ?",
    billing, billing, now, MAX_LIST);
}

export async function readKits(env: KitsEnv, userId: string): Promise<DeliveryKit[]> {
  if (!userId) deny(401, "SESION_REQUERIDA");
  const list = await prepareKits(openRead(env), userId, env, Math.floor(Date.now() / 1000)).all<KitRow>();
  return list.results.map(kitView);
}

export async function handleKits(request: Request, env: KitsEnv, userId: string, path: string): Promise<Response> {
  if (!userId) deny(401, "SESION_REQUERIDA");
  const billing = mode(env.BILLING_MODE);
  const now = Math.floor(Date.now() / 1000);
  if (path === ROOT) {
    if (request.method !== "GET") deny(405, "METODO_NO_PERMITIDO");
    return response({ kits: await readKits(env, userId) });
  }
  const parts = path.split("/");
  const id = parts[3];
  const prores = parts[4] === "prores";
  const video = parts[4] === "video";
  if (parts.length !== 5 || parts[1] !== "api" || parts[2] !== "kits" || (!prores && !video && parts[4] !== "download")
    || id === undefined || !validId(id)) deny(404, "KIT_NO_ENCONTRADO");
  if (request.method !== "GET" && request.method !== "HEAD") deny(405, "METODO_NO_PERMITIDO");
  const kit = await openRead(env).prepare(published + " " + withinRetention + " AND k.id=?")
    .bind(userId, billing, billing, now, id).first<KitRow>();
  if (!kit) deny(404, "KIT_NO_ENCONTRADO");
  const bytes = prores ? kit.prores_bytes : kit.bytes;
  const digest = prores ? kit.prores_sha256 : kit.sha256;
  if (bytes === null || digest === null) deny(404, "KIT_NO_ENCONTRADO");
  const etag = prores ? kit.prores_r2_etag : kit.r2_etag;
  const version = prores ? kit.prores_r2_version : kit.r2_version;
  const key = deliveryKitKey(billing, kit.order_id, id, prores ? "mov" : "zip");
  if (video) {
    const header = await env.MEDIA.get(key, { range: { offset: 0, length: Math.min(kit.bytes, 4096) } });
    if (!metadata(header, key, kit.bytes, kit.sha256) || header.etag !== kit.r2_etag || header.version !== kit.r2_version) {
      await header?.body.cancel();
      deny(409, "KIT_OBJETO_CAMBIO");
    }
    const entry = videoEntry(new Uint8Array(await new Response(header.body).arrayBuffer()), kit.bytes);
    const range = videoRange(request.headers.get("range"), entry.size);
    const length = range.end - range.start + 1;
    const resultHeaders = new Headers({
      "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
      "Cross-Origin-Resource-Policy": "same-origin", "Content-Type": "video/mp4",
      "Content-Length": String(length), "Accept-Ranges": "bytes",
    });
    if (range.partial) resultHeaders.set("Content-Range", `bytes ${range.start}-${range.end}/${entry.size}`);
    if (request.method === "HEAD") return new Response(null, { status: range.partial ? 206 : 200, headers: resultHeaders });
    const object = await env.MEDIA.get(key, { range: { offset: entry.offset + range.start, length } });
    if (!metadata(object, key, kit.bytes, kit.sha256) || object.etag !== kit.r2_etag || object.version !== kit.r2_version) {
      await object?.body.cancel();
      deny(409, "KIT_OBJETO_CAMBIO");
    }
    return new Response(object.body, { status: range.partial ? 206 : 200, headers: resultHeaders });
  }
  const object = await (request.method === "HEAD" ? env.MEDIA.head(key) : env.MEDIA.get(key));
  const body = object && "body" in object ? object.body as ReadableStream : null;
  if (!metadata(object, key, bytes, digest) || object.etag !== etag
    || object.version !== version || (request.method === "GET" && !body)) {
    if (body) void body.cancel().catch(() => undefined);
    deny(409, "KIT_OBJETO_CAMBIO");
  }
  return new Response(body, { headers: {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Disposition": 'attachment; filename="' + (prores ? "grafica-" + id + ".mov" : "kit-" + id + ".zip") + '"',
    "Content-Type": prores ? "video/quicktime" : "application/zip",
    "Content-Length": String(bytes),
    "Accept-Ranges": "none",
  } });
}
