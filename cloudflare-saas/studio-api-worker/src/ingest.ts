import { signR2ListParts, signR2UploadPart } from "../../../src/media/r2-signing.js";
import { validateSyncJob, type SyncJob } from "../../../src/media/sync-contract.js";
import { sessionLookup, type AuthEnv, type PortalIdentity } from "./identity.js";
import {
  HttpError, ownerId, prepareOwnedStatement, readJson, response, type SqlDatabase, type SqlOwner, type SqlSession,
  type SqlStatement,
} from "./platform.js";
import { storageFits, storageQuotaArguments } from "./storage-quota.js";

const maxObjectBytes = 1024 ** 3;
const partSize = 16 * 1024 ** 2;
const maxParts = maxObjectBytes / partSize;
const sessionTtl = 86400;
const operationLease = 600;
const maxXmlBytes = 96 * 1024;
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/u;
const uploadRoute = /^\/api\/uploads\/([a-f0-9-]+)(?:\/(complete|parts|part-observations)(?:\/([1-9][0-9]?)\/sign)?)?$/u;
const mimeTypes = new Map([
  ["mp4", "video/mp4"], ["mov", "video/quicktime"], ["mkv", "video/x-matroska"],
  ["webm", "video/webm"], ["m4v", "video/x-m4v"], ["wav", "audio/wav"],
  ["mp3", "audio/mpeg"], ["m4a", "audio/mp4"], ["flac", "audio/flac"],
  ["png", "image/png"], ["jpg", "image/jpeg"], ["jpeg", "image/jpeg"],
]);

interface ObjectMetadata {
  key: string;
  size: number;
  etag: string;
  httpMetadata?: { contentType?: string };
  customMetadata?: Record<string, string>;
}

interface UploadHandle {
  uploadId: string;
  abort(): Promise<void>;
  complete(parts: Array<{ partNumber: number; etag: string }>): Promise<ObjectMetadata>;
}

export interface IngestBucket {
  createMultipartUpload(key: string, options: {
    httpMetadata: { contentType: string }; customMetadata: Record<string, string>;
  }): Promise<UploadHandle>;
  resumeMultipartUpload(key: string, uploadId: string): UploadHandle;
  head(key: string): Promise<ObjectMetadata | null>;
  get(key: string): Promise<(ObjectMetadata & { body: ReadableStream }) | null>;
  delete(key: string): Promise<void>;
}

export interface IngestEnv {
  DB: SqlDatabase;
  MEDIA: IngestBucket;
  R2_ACCOUNT_ID: string;
  R2_ACCESS_KEY_ID: string;
  R2_SECRET_ACCESS_KEY: string;
  BILLING_MODE: "sandbox" | "production";
}

export type MultipartObserver = (preparation: number, storage: number, confirmation: number, testAccount?: boolean) => void;

export function uploadsConfigured(env: IngestEnv): boolean {
  return /^[a-f0-9]{32}$/u.test(env.R2_ACCOUNT_ID)
    && /^[a-f0-9]{32}$/u.test(env.R2_ACCESS_KEY_ID)
    && /^[a-f0-9]{64}$/u.test(env.R2_SECRET_ACCESS_KEY);
}

interface OrderWithReservation {
  id: string;
  project_slug: string;
  reserved: number;
  has_source_set: number;
}

interface UploadRow {
  id: string;
  user_id: string;
  order_id: string;
  project_slug: string;
  object_key: string;
  sha256: string;
  bytes: number;
  mime_type: string;
  extension: string;
  upload_id: string | null;
  status: "creating" | "open" | "completing" | "completed" | "aborting" | "aborted";
  operation_id: string | null;
  lease_until: number | null;
  object_etag: string | null;
  order_status: string;
  usage_status: string;
  expires_at: number;
}

interface Part {
  PartNumber: number;
  ETag: string;
  Size: number;
}

type RequestedPart = Omit<Part, "Size">;
type Plain = Record<string, unknown>;
type AbortReason = "cancelled" | "metadata_mismatch" | "sha256_mismatch";

function fail(status: number, code: string): never { throw new HttpError(status, code); }
function unix(): number { return Math.floor(Date.now() / 1000); }
function amzDate(): string { return new Date().toISOString().replace(/[-:]|\.\d{3}/gu, ""); }
function isUuid(value: unknown): value is string {
  return typeof value === "string" && value.length === 36 && uuid.test(value);
}
function hasOnly(value: Plain, keys: string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}
function etag(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const matched = value.match(/^(?:"([a-f0-9]{32})"|([a-f0-9]{32}))$/u);
  return matched?.[0] === value ? matched[1] ?? matched[2] ?? null : null;
}

function syncJob(row: UploadRow): Readonly<SyncJob> {
  return validateSyncJob({
    job_id: row.order_id, asset_id: row.id, project_slug: row.project_slug,
    extension: row.extension, bytes: row.bytes, sha256: row.sha256, object_key: row.object_key,
  }, { maxObjectBytes });
}

const uploadWithReservation = `SELECT s.*, o.project_slug, o.status AS order_status, u.status AS usage_status
    FROM upload_sessions s JOIN orders o ON o.id = s.order_id AND o.user_id = s.user_id
    JOIN usage_ledger u ON u.order_id = o.id AND u.user_id = o.user_id`;

function findUpload(db: SqlSession, mode: IngestEnv["BILLING_MODE"], owner: SqlOwner | string, id: string, by: "id" | "order_id"): SqlStatement {
  return prepareOwnedStatement(db, owner, `${uploadWithReservation}
    WHERE s.${by} = ? AND s.user_id = ${ownerId} AND COALESCE(o.provider_mode, 'production') = ?`, id, mode);
}

function uploadInfo(row: UploadRow, newlyOpened = false): Response {
  if (!row.upload_id || (row.status !== "open" && row.status !== "completed")) {
    return fail(409, "SUBIDA_EN_TRANSICION");
  }
  return response({
    id: row.id, uploadId: row.upload_id, key: row.object_key, status: row.status,
    partSize, partCount: Math.ceil(row.bytes / partSize),
    ...(newlyOpened ? { newlyOpened: true } : {}),
    ...(row.status === "completed" ? { etag: row.object_etag, hash_verified: true } : {}),
  });
}

function requireReserved(row: UploadRow): void {
  if (row.order_status !== "queued" || row.usage_status !== "reserved") fail(409, "ORDEN_NO_RESERVADA");
}

function active(row: UploadRow): asserts row is UploadRow & { upload_id: string } {
  requireReserved(row);
  if (row.expires_at <= unix()) fail(410, "SUBIDA_VENCIDA");
  if (!row.upload_id || row.status !== "open") fail(409, "SUBIDA_NO_ABIERTA");
}

function uploadInput(payload: Plain) {
  const { order_id, project_slug, extension, bytes, sha256, content_type } = payload;
  if (!hasOnly(payload, ["order_id", "project_slug", "extension", "bytes", "sha256", "content_type"])
    || !isUuid(order_id) || typeof extension !== "string" || typeof content_type !== "string"
    || mimeTypes.get(extension) !== content_type) return fail(422, "SUBIDA_ARGUMENTOS");
  return { order_id, project_slug, extension, bytes, sha256, content_type };
}

type UploadOwner = string | Awaited<ReturnType<typeof sessionLookup>>;

async function createUpload(
  request: Request, env: IngestEnv, owner: UploadOwner, onCommitted?: (orderId: string) => void,
  observe?: MultipartObserver,
): Promise<Response> {
  async function requireSession() {
    if (typeof owner === "string") return;
    if (!await owner.statement(env.DB.withSession("first-primary")).first<PortalIdentity>()) {
      fail(401, "AUTENTICACION_REQUERIDA");
    }
  }
  let input: ReturnType<typeof uploadInput>;
  try {
    input = uploadInput(await readJson(request));
  } catch (error) {
    await requireSession();
    throw error;
  }
  const { order_id, project_slug, extension, bytes, sha256, content_type } = input;
  const orderLookup = (db: SqlSession) => prepareOwnedStatement(db, owner, `
    SELECT o.id, o.project_slug, CASE WHEN o.status = 'queued' AND u.status = 'reserved' THEN 1 ELSE 0 END AS reserved,
      EXISTS(SELECT 1 FROM production_source_sets s WHERE s.order_id=o.id) AS has_source_set
    FROM orders o LEFT JOIN usage_ledger u ON u.order_id = o.id AND u.user_id = o.user_id
    WHERE o.id = ? AND o.user_id = ${ownerId} AND o.kind = 'production'
      AND COALESCE(o.provider_mode, 'production') = ?`, order_id, env.BILLING_MODE);
  const id = crypto.randomUUID();
  const key = `ingest/${order_id}/${id}.${extension}`;
  let job: Readonly<SyncJob>;
  try {
    job = validateSyncJob(
      { job_id: order_id, asset_id: id, project_slug, extension, bytes, sha256, object_key: key },
      { maxObjectBytes },
    );
  } catch {
    await requireSession();
    const order = await orderLookup(env.DB.withSession("first-primary")).first<OrderWithReservation>();
    if (!order || order.project_slug !== project_slug) return fail(404, "ORDEN_NO_ENCONTRADA");
    return fail(422, "SUBIDA_ARGUMENTOS");
  }
  const current = unix();
  const operation = crypto.randomUUID();
  const reserve = prepareOwnedStatement(env.DB, owner, `
    INSERT INTO upload_sessions
      (id, user_id, order_id, object_key, sha256, bytes, mime_type, extension, status,
       operation_id, lease_until, created_at, expires_at)
    SELECT ?, o.user_id, o.id, ?, ?, ?, ?, ?, 'creating', ?, ?, ?, ?
    FROM orders o JOIN usage_ledger u ON u.order_id = o.id
    JOIN plan_quotas p ON p.plan_id = o.plan_id
    WHERE o.id = ? AND o.user_id = ${ownerId} AND o.project_slug = ? AND o.kind = 'production' AND o.status = 'queued'
      AND u.status = 'reserved' AND COALESCE(o.provider_mode, 'production') = ?
      AND NOT EXISTS (SELECT 1 FROM production_source_sets s WHERE s.order_id=o.id)
      AND ${storageFits}
    ON CONFLICT(order_id) DO NOTHING
  `, id, key, job.sha256, job.bytes, content_type, extension, operation,
    current + operationLease, current, current + sessionTtl, order_id, job.project_slug,
    env.BILLING_MODE, ...storageQuotaArguments(job.bytes, current));
  const started = performance.now();
  const result = await env.DB.batch<PortalIdentity | OrderWithReservation | UploadRow>([
    ...(typeof owner === "string" ? [] : [owner.statement(env.DB)]),
    orderLookup(env.DB),
    reserve,
    findUpload(env.DB, env.BILLING_MODE, owner, order_id, "order_id"),
  ]);
  const caller = typeof owner === "string" ? undefined : result.shift()?.results[0];
  if (typeof owner !== "string" && !caller) return fail(401, "AUTENTICACION_REQUERIDA");
  const [matched, inserted, found] = result;
  if (!matched || !inserted || !found) return fail(503, "SUBIDA_NO_DISPONIBLE");
  const order = matched.results[0];
  if (!order || !("reserved" in order) || order.project_slug !== job.project_slug) return fail(404, "ORDEN_NO_ENCONTRADA");
  if ("has_source_set" in order && order.has_source_set) return fail(409, "MATERIALES_EN_PREPARACION");
  const row = found.results[0];
  if (!row) return fail(409, order.reserved ? "ALMACENAMIENTO_AGOTADO" : "ORDEN_NO_RESERVADA");
  if (!("status" in row)) return fail(503, "SUBIDA_NO_DISPONIBLE");
  if (row.extension !== extension || row.bytes !== bytes || row.sha256 !== sha256 || row.mime_type !== content_type) {
    return fail(409, "SUBIDA_DIFERENTE");
  }
  if (row.status === "completed") return uploadInfo(row);
  requireReserved(row);
  if (row.status === "completing") return complete(null, env, row, onCommitted);
  if (row.expires_at <= current) return fail(410, "SUBIDA_VENCIDA");
  if (row.status === "open") return uploadInfo(row);
  if (row.status !== "creating") return fail(409, "SUBIDA_EN_TRANSICION");
  if (inserted.meta.changes !== 1) {
    const renewed = await env.DB.prepare(`
      UPDATE upload_sessions SET operation_id = ?, lease_until = ?
      WHERE id = ? AND user_id = ? AND status = 'creating' AND lease_until <= ?
    `).bind(operation, current + operationLease, row.id, row.user_id, current).run();
    if (renewed.meta.changes !== 1) return fail(409, "SUBIDA_EN_TRANSICION");
  }
  let handle: UploadHandle;
  const storageStarted = performance.now();
  try {
    handle = await env.MEDIA.createMultipartUpload(row.object_key, {
      httpMetadata: { contentType: content_type },
      customMetadata: { agency_asset_id: row.id, agency_sha256: row.sha256 },
    });
  } catch {
    await env.DB.prepare("UPDATE upload_sessions SET lease_until = ? WHERE id = ? AND operation_id = ?")
      .bind(unix(), row.id, operation).run();
    return fail(503, "R2_NO_DISPONIBLE");
  }
  const confirmationStarted = performance.now();
  const opened = await env.DB.prepare(`
    UPDATE upload_sessions SET status = 'open', upload_id = ?, operation_id = NULL, lease_until = NULL
    WHERE id = ? AND user_id = ? AND status = 'creating' AND operation_id = ?
  `).bind(handle.uploadId, row.id, row.user_id, operation).run();
  if (opened.meta.changes !== 1) {
    try { await handle.abort(); } catch { /* La política nativa de R2 expira multipart huérfanos. */ }
    return fail(409, "SUBIDA_EN_TRANSICION");
  }
  const reply = uploadInfo({ ...row, status: "open", upload_id: handle.uploadId }, true);
  observe?.(storageStarted - started, confirmationStarted - storageStarted, performance.now() - confirmationStarted,
    Boolean(caller && "email" in caller && caller.email.toLowerCase().endsWith(".invalid")));
  return reply;
}

function credentials(env: IngestEnv) {
  return { accountId: env.R2_ACCOUNT_ID, accessKeyId: env.R2_ACCESS_KEY_ID, secretAccessKey: env.R2_SECRET_ACCESS_KEY };
}

function xmlField(source: string, field: "IsTruncated" | "PartNumber" | "ETag" | "Size"): string | null {
  const fields = Array.from(source.matchAll(new RegExp(`<${field}>([^<]*)</${field}>`, "gu")));
  return fields.length === 1 ? fields[0]?.[1]?.trim() ?? null : null;
}

export function parseListedParts(xml: string): Part[] {
  const root = xml.trim().replace(/^<\?xml\s+version=(?:"1\.0"|'1\.0')(?:\s+encoding=(?:"UTF-8"|'UTF-8'))?\s*\?>\s*/iu, "");
  if (!/^<ListPartsResult(?:\s+xmlns="http:\/\/s3\.amazonaws\.com\/doc\/2006-03-01\/")?\s*>[\s\S]*<\/ListPartsResult>$/u.test(root)
    || root.includes("<!") || root.includes("<?") || xmlField(root, "IsTruncated") !== "false") {
    return fail(502, "R2_PARTES_INVALIDAS");
  }
  const parts: Part[] = [];
  const seen = new Set<number>();
  for (const match of root.matchAll(/<Part>([\s\S]*?)<\/Part>/gu)) {
    const body = match[1] ?? "";
    const partNumber = Number(xmlField(body, "PartNumber"));
    const size = Number(xmlField(body, "Size"));
    const tag = etag(xmlField(body, "ETag")?.replace(/&quot;|&#34;/gu, '"'));
    if (!Number.isSafeInteger(partNumber) || partNumber < 1 || partNumber > maxParts
      || !Number.isSafeInteger(size) || size < 1 || !tag || seen.has(partNumber)) {
      return fail(502, "R2_PARTES_INVALIDAS");
    }
    seen.add(partNumber);
    parts.push({ PartNumber: partNumber, Size: size, ETag: tag });
  }
  return parts.sort((left, right) => left.PartNumber - right.PartNumber);
}

async function boundedXml(remote: Response): Promise<string> {
  if (!remote.body) return fail(502, "R2_PARTES_INVALIDAS");
  const reader = remote.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let length = 0;
  let xml = "";
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) return xml + decoder.decode();
      length += next.value.length;
      if (length > maxXmlBytes) {
        await reader.cancel();
        return fail(502, "R2_PARTES_INVALIDAS");
      }
      xml += decoder.decode(next.value, { stream: true });
    }
  } finally { reader.releaseLock(); }
}

async function listParts(env: IngestEnv, row: UploadRow & { upload_id: string }): Promise<Part[]> {
  return listMultipartParts(env, syncJob(row), row.upload_id);
}

export async function listMultipartParts(env: IngestEnv, job: Readonly<SyncJob>, uploadId: string): Promise<Part[]> {
  const signed = await signR2ListParts(job, { maxObjectBytes }, credentials(env), amzDate(), uploadId);
  const remote = await fetch(signed.url, { headers: { ...signed.headers }, redirect: "manual", signal: AbortSignal.timeout(15000) });
  if (remote.status === 404) return fail(409, "R2_MULTIPART_AUSENTE");
  if (!remote.ok) return fail(503, "R2_NO_DISPONIBLE");
  return parseListedParts(await boundedXml(remote));
}

export function requestedParts(body: Plain, count: number): RequestedPart[] {
  if (!hasOnly(body, ["parts"]) || !Array.isArray(body.parts) || body.parts.length !== count) {
    return fail(422, "SUBIDA_PARTES");
  }
  const parts: RequestedPart[] = body.parts.map((value: unknown, index) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return fail(422, "SUBIDA_PARTES");
    const record = value as Plain;
    const tag = etag(record.ETag);
    if (!hasOnly(record, ["PartNumber", "ETag"]) || record.PartNumber !== index + 1 || !tag) {
      return fail(422, "SUBIDA_PARTES");
    }
    return { PartNumber: index + 1, ETag: tag };
  });
  return parts;
}

function bytesForPart(row: UploadRow, number: number): number {
  return Math.min(partSize, row.bytes - (number - 1) * partSize);
}

async function claim(env: IngestEnv, row: UploadRow, mode: "completing" | "aborting"): Promise<string> {
  const current = unix();
  const token = crypto.randomUUID();
  const allowed = mode === "completing" ? "status = 'open'" : "status = 'open' OR (status = 'creating' AND lease_until <= ?)";
  const statement = env.DB.prepare(`
    UPDATE upload_sessions SET status = ?, operation_id = ?, lease_until = ?
    WHERE id = ? AND user_id = ?
      AND ((${allowed}) OR (status = ? AND lease_until <= ?))
      AND EXISTS (SELECT 1 FROM orders o JOIN usage_ledger u ON o.id = u.order_id
                  WHERE o.id = upload_sessions.order_id AND o.status = 'queued' AND u.status = 'reserved')
  `);
  const params: (string | number)[] = [mode, token, current + operationLease, row.id, row.user_id];
  if (mode === "aborting") params.push(current);
  const result = await statement.bind(...params, mode, current).run();
  if (result.meta.changes !== 1) return fail(409, "SUBIDA_EN_TRANSICION");
  return token;
}

function objectMatches(object: ObjectMetadata, row: UploadRow): boolean {
  return object.key === row.object_key && object.size === row.bytes
    && object.httpMetadata?.contentType === row.mime_type
    && object.customMetadata?.agency_asset_id === row.id
    && object.customMetadata.agency_sha256 === row.sha256;
}

function isOwnObject(object: ObjectMetadata, row: UploadRow): boolean {
  return object.key === row.object_key && object.customMetadata?.agency_asset_id === row.id;
}

type DigestWriter = WritableStream & { digest: Promise<ArrayBuffer> };
export async function verifyDigest(body: ReadableStream, expected: string): Promise<boolean> {
  const implementation = crypto as Crypto & { DigestStream?: new (name: string) => DigestWriter };
  if (!implementation.DigestStream) {
    await body.cancel();
    return fail(503, "VERIFICACION_HASH_NO_DISPONIBLE");
  }
  const writer = new implementation.DigestStream("SHA-256");
  const [, bytes] = await Promise.all([body.pipeTo(writer), writer.digest]);
  const digest = Array.from(new Uint8Array(bytes), (value) => value.toString(16).padStart(2, "0")).join("");
  return digest === expected;
}

async function markAborted(env: IngestEnv, row: UploadRow, token: string, reason: AbortReason): Promise<void> {
  const status = reason === "cancelled" ? "aborting" : "completing";
  const ownsOperation = "EXISTS (SELECT 1 FROM upload_sessions s WHERE s.id = ? AND s.user_id = ? AND s.status = ? AND s.operation_id = ?)";
  const proof = [row.id, row.user_id, status, token] as const;
  const current = unix();
  const batch: SqlStatement[] = [
    env.DB.prepare(`UPDATE usage_ledger SET status = 'released', updated_at = ?
      WHERE order_id = ? AND status = 'reserved' AND ${ownsOperation}
        AND EXISTS (SELECT 1 FROM orders WHERE id = ? AND status = 'queued')`)
      .bind(current, row.order_id, ...proof, row.order_id),
    env.DB.prepare(`UPDATE orders SET status = 'cancelled', updated_at = ?
      WHERE id = ? AND status = 'queued' AND ${ownsOperation}
        AND EXISTS (SELECT 1 FROM usage_ledger WHERE order_id = ? AND status = 'released')`)
      .bind(current, row.order_id, ...proof, row.order_id),
    env.DB.prepare(`UPDATE upload_sessions SET status = 'aborted', abort_reason = ?, operation_id = NULL, lease_until = NULL
      WHERE id = ? AND user_id = ? AND status = ? AND operation_id = ?
        AND EXISTS (SELECT 1 FROM orders WHERE id = ? AND status = 'cancelled')`)
      .bind(reason, ...proof, row.order_id),
  ];
  const results = await env.DB.batch(batch);
  if (results[2]?.meta.changes !== 1) fail(409, "SUBIDA_EN_TRANSICION");
}

export async function expireAbandonedReservations(env: IngestEnv): Promise<void> {
  const now = unix();
  const aged = `o.kind = 'production' AND o.status = 'queued'
    AND COALESCE(o.provider_mode, 'production') = ? AND o.created_at <= ?
    AND NOT EXISTS (SELECT 1 FROM job_outbox j WHERE j.order_id = o.id)
    AND NOT EXISTS (SELECT 1 FROM upload_sessions s WHERE s.order_id = o.id
      AND (s.status NOT IN ('creating', 'open', 'aborting') OR s.expires_at > ? OR s.lease_until > ?))
    AND NOT EXISTS (SELECT 1 FROM production_sources source JOIN production_source_uploads upload ON upload.source_id=source.id
      WHERE source.order_id=o.id AND upload.status <> 'completed'
        AND (upload.status NOT IN ('creating', 'open', 'aborting', 'aborted')
          OR upload.expires_at > ? OR upload.lease_until > ?))
    AND (NOT EXISTS (SELECT 1 FROM production_sources source WHERE source.order_id=o.id)
      OR EXISTS (SELECT 1 FROM production_sources source LEFT JOIN production_source_uploads upload ON upload.source_id=source.id
        WHERE source.order_id=o.id AND (upload.status IS NULL OR upload.status <> 'completed')))`;
  const scope = [env.BILLING_MODE, now - sessionTtl, now, now, now, now] as const;
  const due = await env.DB.withSession("first-primary").prepare(`
    SELECT o.id FROM orders o JOIN usage_ledger u ON u.order_id = o.id AND u.user_id = o.user_id
    WHERE ${aged} AND u.status = 'reserved'
    ORDER BY o.created_at, o.id LIMIT 20
  `).bind(...scope).all<{ id: string }>();
  const ids = due.results.map(({ id }) => id);
  if (ids.length === 0) return;
  const slots = ids.map(() => "?").join(",");
  await env.DB.batch([
    env.DB.prepare(`UPDATE usage_ledger SET status = 'released', updated_at = ?
      WHERE status = 'reserved' AND order_id IN (SELECT o.id FROM orders o
        WHERE o.id IN (${slots}) AND ${aged})`).bind(now, ...ids, ...scope),
    env.DB.prepare(`UPDATE orders AS o SET status = 'cancelled', updated_at = ?
      WHERE o.id IN (${slots}) AND ${aged}
        AND EXISTS (SELECT 1 FROM usage_ledger u WHERE u.order_id = o.id AND u.status = 'released')`)
      .bind(now, ...ids, ...scope),
    // R2 retira las partes incompletas mediante el ciclo de vida del bucket.
    env.DB.prepare(`UPDATE upload_sessions SET status = 'aborted', abort_reason = 'expired', operation_id = NULL, lease_until = NULL
      WHERE order_id IN (${slots}) AND status IN ('creating', 'open', 'aborting')
        AND EXISTS (SELECT 1 FROM orders o JOIN usage_ledger u ON u.order_id = o.id
          WHERE o.id = upload_sessions.order_id AND o.status = 'cancelled' AND u.status = 'released')`).bind(...ids),
    env.DB.prepare(`UPDATE production_source_uploads SET status='aborted',operation_id=NULL,lease_until=NULL
      WHERE source_id IN (SELECT source.id FROM production_sources source JOIN orders o ON o.id=source.order_id
        JOIN usage_ledger u ON u.order_id=o.id WHERE source.order_id IN (${slots})
          AND o.status='cancelled' AND u.status='released') AND status IN ('creating','open','aborting')`).bind(...ids),
  ]);
}

async function complete(
  request: Request | null, env: IngestEnv, row: UploadRow, onCommitted?: (orderId: string) => void,
): Promise<Response> {
  const job = syncJob(row);
  const count = Math.ceil(row.bytes / partSize);
  const parts = request ? requestedParts(await readJson(request), count) : null;
  if (row.status === "completed") return uploadInfo(row);
  requireReserved(row);
  if (!row.upload_id || (row.status !== "open" && row.status !== "completing")) return fail(409, "SUBIDA_NO_ABIERTA");
  if (row.status === "open" && row.expires_at <= unix()) return fail(410, "SUBIDA_VENCIDA");
  const bound = { ...row, upload_id: row.upload_id };
  const token = await claim(env, row, "completing");
  let object = await env.MEDIA.get(row.object_key);
  if (!object) {
    const present = await listParts(env, bound);
    if (present.length !== count || present.some((value, index) => value.PartNumber !== index + 1
      || value.Size !== bytesForPart(row, value.PartNumber) || (parts && value.ETag !== parts[index]?.ETag))) {
      const reopened = await env.DB.prepare(`UPDATE upload_sessions SET status = 'open', operation_id = NULL, lease_until = NULL
        WHERE id = ? AND operation_id = ? AND status = 'completing'`).bind(row.id, token).run();
      if (reopened.meta.changes !== 1) return fail(409, "SUBIDA_EN_TRANSICION");
      if (parts) return fail(409, "SUBIDA_PARTES_INCOMPLETAS");
      if (row.expires_at <= unix()) return fail(410, "SUBIDA_VENCIDA");
      return uploadInfo({ ...row, status: "open" });
    }
    const handle = env.MEDIA.resumeMultipartUpload(row.object_key, row.upload_id);
    await handle.complete(present.map((part) => ({ partNumber: part.PartNumber, etag: part.ETag })));
    object = await env.MEDIA.get(row.object_key);
  }
  if (!object) return fail(503, "R2_OBJETO_AUSENTE");
  if (!objectMatches(object, row)) {
    await object.body.cancel();
    if (isOwnObject(object, row)) await env.MEDIA.delete(row.object_key);
    await markAborted(env, row, token, "metadata_mismatch");
    return fail(422, "SUBIDA_METADATOS");
  }
  if (!await verifyDigest(object.body, row.sha256)) {
    await env.MEDIA.delete(row.object_key);
    await markAborted(env, row, token, "sha256_mismatch");
    return fail(422, "SUBIDA_HASH_DISTINTO");
  }
  const committed = await env.DB.batch([
    env.DB.prepare(`UPDATE upload_sessions SET status = 'completed', object_etag = ?,
                  operation_id = NULL, lease_until = NULL
      WHERE id = ? AND user_id = ? AND status = 'completing' AND operation_id = ?
        AND EXISTS (SELECT 1 FROM orders o JOIN usage_ledger u ON o.id = u.order_id
                    WHERE o.id = upload_sessions.order_id AND o.status = 'queued' AND u.status = 'reserved')`)
      .bind(object.etag, row.id, row.user_id, token),
    env.DB.prepare(`INSERT INTO job_outbox (order_id, payload)
      SELECT order_id, ? FROM upload_sessions WHERE id = ? AND user_id = ?
            AND status = 'completed' AND object_etag = ? ON CONFLICT(order_id) DO NOTHING`)
      .bind(JSON.stringify(job), row.id, row.user_id, object.etag),
  ]);
  if (committed[0]?.meta.changes !== 1) return fail(409, "SUBIDA_EN_TRANSICION");
  onCommitted?.(row.order_id);
  return uploadInfo({ ...row, status: "completed", object_etag: object.etag });
}

async function abort(env: IngestEnv, row: UploadRow): Promise<Response> {
  if (row.status === "aborted") return new Response(null, { status: 204 });
  requireReserved(row);
  const token = await claim(env, row, "aborting");
  if (row.upload_id) {
    try {
      await env.MEDIA.resumeMultipartUpload(row.object_key, row.upload_id).abort();
    } catch (error) {
      if (!(error instanceof Error) || !error.message.endsWith("(10024)")) return fail(503, "R2_NO_DISPONIBLE");
    }
  }
  await markAborted(env, row, token, "cancelled");
  return new Response(null, { status: 204 });
}

async function recordPartObservations(request: Request, env: IngestEnv, row: UploadRow): Promise<Response> {
  if (!row.upload_id) return fail(409, "SUBIDA_EN_TRANSICION");
  const payload = await readJson(request, 256);
  const { http_403: rejected, sin_respuesta: noResponse } = payload;
  if (!hasOnly(payload, ["http_403", "sin_respuesta"])
    || typeof rejected !== "boolean" || typeof noResponse !== "boolean" || !(rejected || noResponse)) {
    return fail(422, "OBSERVACION_MULTIPART_INVALIDA");
  }
  await env.DB.prepare(`
    UPDATE upload_sessions SET direct_put_403_observed=MAX(direct_put_403_observed, ?),
        direct_put_no_response_observed=MAX(direct_put_no_response_observed, ?)
    WHERE id=? AND user_id=? AND upload_id=?
  `).bind(Number(rejected), Number(noResponse), row.id, row.user_id, row.upload_id).run();
  return new Response(null, { status: 204 });
}

export function partAccessTarget(path: string, method: string): { id: string; number?: number } | null {
  if (method !== "GET" && method !== "POST") return null;
  const route = uploadRoute.exec(path);
  if (route?.[0] !== path || route[2] !== "parts" || !isUuid(route[1])) return null;
  if (method === "GET" && !route[3]) return { id: route[1] };
  if (method === "POST" && route[3]) return { id: route[1], number: Number(route[3]) };
  return null;
}

async function signPart(env: IngestEnv, row: UploadRow, number: number): Promise<Response> {
  active(row);
  if (number > Math.ceil(row.bytes / partSize)) return fail(405, "METODO_INVALIDO");
  const expires = Math.min(300, row.expires_at - unix());
  if (expires < 1) return fail(410, "SUBIDA_VENCIDA");
  return response(await signR2UploadPart(syncJob(row), { maxObjectBytes }, credentials(env), amzDate(), {
    uploadId: row.upload_id, partNumber: number, bytes: bytesForPart(row, number), mimeType: row.mime_type, expires,
  }));
}

async function listUploadParts(env: IngestEnv, row: UploadRow): Promise<Response> {
  active(row);
  return response({ parts: await listParts(env, row) });
}

export async function handlePartAccess(request: Request, env: AuthEnv & IngestEnv,
  { id, number }: { id: string; number?: number }): Promise<Response> {
  const lookup = await sessionLookup(request, env);
  const [sessions, uploads] = await env.DB.batch<PortalIdentity | UploadRow>([
    lookup.statement(env.DB),
    findUpload(env.DB, env.BILLING_MODE, lookup, id, "id"),
  ]);
  if (!sessions?.results[0]) return fail(401, "AUTENTICACION_REQUERIDA");
  const upload = uploads?.results[0];
  if (!upload || !("upload_id" in upload)) return fail(404, "SUBIDA_NO_ENCONTRADA");
  return number === undefined ? listUploadParts(env, upload) : signPart(env, upload, number);
}

export async function handleUploadCreation(
  request: Request, env: AuthEnv & IngestEnv, onCommitted?: (orderId: string) => void, observe?: MultipartObserver,
): Promise<Response> {
  return await createUpload(request, env, await sessionLookup(request, env), onCommitted, observe);
}

export async function handleIngest(
  request: Request, env: IngestEnv, userId: string, path: string, onCommitted?: (orderId: string) => void,
): Promise<Response> {
  const { method } = request;
  if (path === "/api/uploads" && method === "POST") return createUpload(request, env, userId, onCommitted);
  const route = uploadRoute.exec(path);
  const id = route?.[1];
  if (!isUuid(id)) return fail(404, "SUBIDA_NO_ENCONTRADA");
  const action = route?.[2];
  const number = Number(route?.[3]);
  const aborting = !action && method === "DELETE";
  const completing = action === "complete" && method === "POST" && !number;
  const observing = action === "part-observations" && method === "POST" && !number;
  const listing = action === "parts" && method === "GET" && !number;
  const signing = action === "parts" && method === "POST" && number >= 1;
  if (!(aborting || completing || observing || listing || signing) || route?.[0] !== path) {
    return fail(405, "METODO_INVALIDO");
  }
  const row = await findUpload(env.DB.withSession("first-primary"), env.BILLING_MODE, userId, id, "id").first<UploadRow>();
  if (!row) return fail(404, "SUBIDA_NO_ENCONTRADA");
  if (aborting) return abort(env, row);
  if (completing) return complete(request, env, row, onCommitted);
  if (observing) return recordPartObservations(request, env, row);
  if (signing) return signPart(env, row, number);
  return listUploadParts(env, row);
}
