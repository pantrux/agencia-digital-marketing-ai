import { validateSyncJob } from "../../../src/media/sync-contract.js";
import { signR2UploadPart } from "../../../src/media/r2-signing.js";
import type { PortalIdentity, sessionLookup } from "./identity.js";
import { listMultipartParts, requestedParts, uploadsConfigured, verifyDigest, type IngestEnv, type MultipartObserver } from "./ingest.js";
import { HttpError, ownerId, prepareOwnedStatement, readJson, response } from "./platform.js";
import { storageFits, storageQuotaArguments } from "./storage-quota.js";

const partSize = 16 * 1024 ** 2;
const sessionTtl = 86400;
const leaseDuration = 600;
const mime: Record<string, string> = { mp4: "video/mp4", mov: "video/quicktime", webm: "video/webm" };

type SourceRow = { id: string; order_id: string; project_slug: string; bytes: number; sha256: string;
  extension: string; order_status: string; usage_status: string };
type UploadRow = { source_id: string; object_key: string; upload_id: string | null; status: string;
  operation_id: string | null; lease_until: number | null; expires_at: number; object_etag: string | null };
type SourceWithUpload = SourceRow & { user_id: string } & (UploadRow | { source_id: null });
type SourceOwner = string | Awaited<ReturnType<typeof sessionLookup>>;

/** Reserva y abre la carga de una fuente del pedido. */
export async function createSourceUpload(env: IngestEnv, owner: SourceOwner, order: string, sourceId: string,
  observe?: MultipartObserver): Promise<Response> {
  const sourceJoins = `FROM production_sources source
    JOIN production_source_sets source_set ON source_set.order_id=source.order_id
    JOIN orders o ON o.id=source_set.order_id AND o.user_id=source_set.user_id
    JOIN usage_ledger ledger ON ledger.order_id=o.id AND ledger.user_id=o.user_id`;
  const sourceScope = `WHERE source.id=? AND o.id=? AND o.user_id=${ownerId}
    AND COALESCE(o.provider_mode,'production')=?`;
  const started = performance.now();
  const initial = await env.DB.batch<PortalIdentity | SourceWithUpload>([
    ...(typeof owner === "string" ? [] : [owner.statement(env.DB)]),
    prepareOwnedStatement(env.DB, owner, `SELECT source.id,source.order_id,source.bytes,source.sha256,source.extension,
      o.user_id,o.project_slug,o.status AS order_status,ledger.status AS usage_status,upload.*
      ${sourceJoins} LEFT JOIN production_source_uploads upload ON upload.source_id=source.id ${sourceScope}`,
    sourceId, order, env.BILLING_MODE),
  ]);
  const caller = typeof owner === "string" ? undefined : initial.shift()?.results[0];
  if (typeof owner !== "string" && (!caller || !("email" in caller))) throw new HttpError(401, "AUTENTICACION_REQUERIDA");
  if (caller && !uploadsConfigured(env)) throw new HttpError(503, "SUBIDA_NO_DISPONIBLE");
  const source = initial[0]?.results[0];
  if (!source || !("order_id" in source)) throw new HttpError(404, "MATERIAL_NO_ENCONTRADO");
  const user = source.user_id;
  if (source.order_status !== "queued" || source.usage_status !== "reserved") {
    if (source.source_id !== null && source.status === "completed" && source.object_etag && source.upload_id
      && ["processing", "completed"].includes(source.order_status) && source.usage_status === "consumed") {
      return response({ id: source.id, uploadId: source.upload_id, key: source.object_key, status: "completed",
        partSize, partCount: Math.ceil(source.bytes / partSize), hash_verified: true });
    }
    throw new HttpError(409, "ORDEN_NO_RESERVADA");
  }
  const contentType = mime[source.extension];
  if (!contentType) throw new HttpError(422, "MATERIAL_INVALIDO");
  const key = `ingest/${order}/${source.id}.${source.extension}`;
  try {
    validateSyncJob({ job_id: order, asset_id: source.id, project_slug: source.project_slug,
      extension: source.extension, bytes: source.bytes, sha256: source.sha256, object_key: key }, { maxObjectBytes: 1024 ** 3 });
  } catch { throw new HttpError(422, "MATERIAL_INVALIDO"); }
  const now = Math.floor(Date.now() / 1000);
  const info = (value: UploadRow, newlyOpened = false) => response({ id: source.id, uploadId: value.upload_id,
    key: value.object_key, status: value.status, partSize, partCount: Math.ceil(source.bytes / partSize),
    ...(newlyOpened ? { newlyOpened: true } : {}) });
  const reusable = (value: UploadRow): Response | undefined => {
    if (value.object_key !== key) throw new HttpError(409, "MATERIAL_EN_CONFLICTO");
    if (value.status === "completed" && value.upload_id && value.object_etag) return info(value);
    if (value.status === "open" && value.upload_id) {
      if (value.expires_at <= now) throw new HttpError(410, "SUBIDA_VENCIDA");
      return info(value);
    }
  };
  if (source.source_id !== null) {
    const previous = reusable(source);
    if (previous) return previous;
  }
  const token = crypto.randomUUID();
  const [created, found] = await env.DB.batch<UploadRow>([
    env.DB.prepare(`INSERT INTO production_source_uploads(source_id,object_key,status,operation_id,lease_until,created_at,expires_at)
      SELECT source.id,?,'creating',?,?,?,? FROM production_sources source
      JOIN production_source_sets source_set ON source_set.order_id=source.order_id
      JOIN orders o ON o.id=source_set.order_id AND o.user_id=source_set.user_id
      JOIN usage_ledger ledger ON ledger.order_id=o.id AND ledger.user_id=o.user_id
      JOIN plan_quotas p ON p.plan_id=o.plan_id
      WHERE source.id=? AND o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=?
        AND o.status='queued' AND ledger.status='reserved' AND ${storageFits}
      ON CONFLICT(source_id) DO NOTHING`).bind(key, token, now + leaseDuration, now, now + sessionTtl,
      sourceId, order, user, env.BILLING_MODE, ...storageQuotaArguments(source.bytes, now)),
    prepareOwnedStatement(env.DB, user, `SELECT upload.* ${sourceJoins}
      JOIN production_source_uploads upload ON upload.source_id=source.id ${sourceScope}`,
    sourceId, order, env.BILLING_MODE),
  ]);
  const row = found?.results[0];
  if (!row) throw new HttpError(409, "ALMACENAMIENTO_AGOTADO");
  const previous = reusable(row);
  if (previous) return previous;
  if (row.status === "completing") return completeSourceUpload(null, env, user, order, sourceId);
  let ownsLease = created?.meta.changes === 1;
  if (row.status === "aborted") {
    if (await env.MEDIA.head(key)) throw new HttpError(409, "MATERIAL_EN_CONFLICTO");
    const restarted = await env.DB.prepare(`UPDATE production_source_uploads SET status='creating',upload_id=NULL,
      object_etag=NULL,operation_id=?,lease_until=?,created_at=?,expires_at=?
      WHERE source_id=? AND status='aborted' AND EXISTS (
        SELECT 1 FROM production_sources source JOIN production_source_sets s ON s.order_id=source.order_id
        JOIN orders o ON o.id=s.order_id AND o.user_id=s.user_id
        JOIN usage_ledger ledger ON ledger.order_id=o.id AND ledger.user_id=o.user_id
        JOIN plan_quotas p ON p.plan_id=o.plan_id
        WHERE source.id=? AND o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=?
          AND o.status='queued' AND ledger.status='reserved' AND ${storageFits})`)
      .bind(token, now + leaseDuration, now, now + sessionTtl, sourceId, sourceId, order, user, env.BILLING_MODE,
        ...storageQuotaArguments(source.bytes, now)).run();
    if (restarted.meta.changes !== 1) throw new HttpError(409, "SUBIDA_EN_TRANSICION");
    row.status = "creating";
    row.expires_at = now + sessionTtl;
    ownsLease = true;
  }
  if (row.expires_at <= now) throw new HttpError(410, "SUBIDA_VENCIDA");
  if (row.status !== "creating") throw new HttpError(409, "SUBIDA_EN_TRANSICION");
  if (!ownsLease) {
    const claim = await env.DB.prepare(`UPDATE production_source_uploads SET operation_id=?,lease_until=?
      WHERE source_id=? AND status='creating' AND lease_until<=?`).bind(token, now + leaseDuration, sourceId, now).run();
    if (claim.meta.changes !== 1) throw new HttpError(409, "SUBIDA_EN_TRANSICION");
  }
  let upload;
  const storageStarted = performance.now();
  try {
    upload = await env.MEDIA.createMultipartUpload(key, {
      httpMetadata: { contentType }, customMetadata: { agency_asset_id: sourceId, agency_sha256: source.sha256 },
    });
  } catch {
    await env.DB.prepare("UPDATE production_source_uploads SET lease_until=? WHERE source_id=? AND operation_id=?")
      .bind(now, sourceId, token).run();
    throw new HttpError(503, "R2_NO_DISPONIBLE");
  }
  const confirmationStarted = performance.now();
  const opened = await env.DB.prepare(`UPDATE production_source_uploads SET status='open',upload_id=?,operation_id=NULL,lease_until=NULL
    WHERE source_id=? AND status='creating' AND operation_id=? AND EXISTS (
      SELECT 1 FROM production_sources source JOIN production_source_sets source_set ON source_set.order_id=source.order_id
      JOIN orders o ON o.id=source_set.order_id AND o.user_id=source_set.user_id
      JOIN usage_ledger ledger ON ledger.order_id=o.id AND ledger.user_id=o.user_id
      WHERE source.id=? AND o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=?
        AND o.status='queued' AND ledger.status='reserved')`)
    .bind(upload.uploadId, sourceId, token, sourceId, order, user, env.BILLING_MODE).run();
  if (opened.meta.changes !== 1) {
    try { await upload.abort(); } catch { /* R2 expira subidas multipart huérfanas. */ }
    throw new HttpError(409, "SUBIDA_EN_TRANSICION");
  }
  const testAccount = Boolean(caller && "email" in caller && caller.email.toLowerCase().endsWith(".invalid"));
  observe?.(storageStarted - started, confirmationStarted - storageStarted, performance.now() - confirmationStarted, testAccount);
  return info({ ...row, status: "open", upload_id: upload.uploadId }, true);
}

/** Firma únicamente una parte de una fuente abierta y perteneciente al titular. */
export async function signSourcePart(
  env: IngestEnv, user: string, order: string, sourceId: string, number: number,
): Promise<Response> {
  const row = await env.DB.withSession("first-primary").prepare(`SELECT source.id,source.order_id,source.bytes,
    source.sha256,source.extension,o.project_slug,o.status AS order_status,ledger.status AS usage_status,
    upload.object_key,upload.upload_id,upload.status,upload.expires_at
    FROM production_sources source JOIN production_source_sets source_set ON source_set.order_id=source.order_id
    JOIN orders o ON o.id=source_set.order_id AND o.user_id=source_set.user_id
    JOIN usage_ledger ledger ON ledger.order_id=o.id AND ledger.user_id=o.user_id
    JOIN production_source_uploads upload ON upload.source_id=source.id
    WHERE source.id=? AND o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=?`)
    .bind(sourceId, order, user, env.BILLING_MODE).first<SourceRow & UploadRow>();
  if (!row) throw new HttpError(404, "MATERIAL_NO_ENCONTRADO");
  if (row.order_status !== "queued" || row.usage_status !== "reserved") throw new HttpError(409, "ORDEN_NO_RESERVADA");
  const now = Math.floor(Date.now() / 1000);
  if (row.expires_at <= now) throw new HttpError(410, "SUBIDA_VENCIDA");
  if (row.status !== "open" || !row.upload_id) throw new HttpError(409, "SUBIDA_NO_ABIERTA");
  const count = Math.ceil(row.bytes / partSize);
  if (!Number.isSafeInteger(number) || number < 1 || number > count) throw new HttpError(405, "METODO_INVALIDO");
  const contentType = mime[row.extension];
  if (!contentType) throw new HttpError(422, "MATERIAL_INVALIDO");
  const job = validateSyncJob({ job_id: order, asset_id: row.id, project_slug: row.project_slug,
    extension: row.extension, bytes: row.bytes, sha256: row.sha256, object_key: row.object_key },
  { maxObjectBytes: 1024 ** 3 });
  return response(await signR2UploadPart(job, { maxObjectBytes: 1024 ** 3 }, {
    accountId: env.R2_ACCOUNT_ID, accessKeyId: env.R2_ACCESS_KEY_ID, secretAccessKey: env.R2_SECRET_ACCESS_KEY,
  }, new Date().toISOString().replace(/[-:]|\.\d{3}/gu, ""), {
    uploadId: row.upload_id, partNumber: number, bytes: Math.min(partSize, row.bytes - (number - 1) * partSize),
    mimeType: contentType, expires: Math.min(300, row.expires_at - now),
  }));
}

/** Consulta las partes ya almacenadas para reanudar la transferencia. */
export async function listSourceParts(env: IngestEnv, user: string, order: string, sourceId: string): Promise<Response> {
  const row = await env.DB.withSession("first-primary").prepare(`SELECT source.id,source.order_id,source.bytes,
    source.sha256,source.extension,o.project_slug,o.status AS order_status,ledger.status AS usage_status,
    upload.object_key,upload.upload_id,upload.status,upload.expires_at
    FROM production_sources source JOIN production_source_sets source_set ON source_set.order_id=source.order_id
    JOIN orders o ON o.id=source_set.order_id AND o.user_id=source_set.user_id
    JOIN usage_ledger ledger ON ledger.order_id=o.id AND ledger.user_id=o.user_id
    JOIN production_source_uploads upload ON upload.source_id=source.id
    WHERE source.id=? AND o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=?`)
    .bind(sourceId, order, user, env.BILLING_MODE).first<SourceRow & UploadRow>();
  if (!row) throw new HttpError(404, "MATERIAL_NO_ENCONTRADO");
  if (row.order_status !== "queued" || row.usage_status !== "reserved") throw new HttpError(409, "ORDEN_NO_RESERVADA");
  if (row.expires_at <= Math.floor(Date.now() / 1000)) throw new HttpError(410, "SUBIDA_VENCIDA");
  if (row.status !== "open" || !row.upload_id) throw new HttpError(409, "SUBIDA_NO_ABIERTA");
  const job = validateSyncJob({ job_id: order, asset_id: row.id, project_slug: row.project_slug,
    extension: row.extension, bytes: row.bytes, sha256: row.sha256, object_key: row.object_key }, { maxObjectBytes: 1024 ** 3 });
  return response({ parts: await listMultipartParts(env, job, row.upload_id) });
}

/** Confirma una fuente solo después de comprobar tamaño, metadatos y SHA-256. */
export async function completeSourceUpload(
  request: Request | null, env: IngestEnv, user: string, order: string, sourceId: string,
): Promise<Response> {
  const row = await env.DB.withSession("first-primary").prepare(`SELECT source.id,source.order_id,source.bytes,
    source.sha256,source.extension,o.project_slug,o.status AS order_status,ledger.status AS usage_status,
    upload.* FROM production_sources source JOIN production_source_sets source_set ON source_set.order_id=source.order_id
    JOIN orders o ON o.id=source_set.order_id AND o.user_id=source_set.user_id
    JOIN usage_ledger ledger ON ledger.order_id=o.id AND ledger.user_id=o.user_id
    JOIN production_source_uploads upload ON upload.source_id=source.id
    WHERE source.id=? AND o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=?`)
    .bind(sourceId, order, user, env.BILLING_MODE).first<SourceRow & UploadRow>();
  if (!row) throw new HttpError(404, "MATERIAL_NO_ENCONTRADO");
  const count = Math.ceil(row.bytes / partSize);
  const parts = request ? requestedParts(await readJson(request), count) : null;
  const readiness = async () => {
    const incomplete = await env.DB.withSession("first-primary").prepare(`SELECT COUNT(*) AS count
      FROM production_sources source LEFT JOIN production_source_uploads upload ON upload.source_id=source.id
      WHERE source.order_id=? AND (upload.status IS NULL OR upload.status<>'completed')`)
      .bind(order).first<{ count: number }>();
    return response({ id: sourceId, status: "completed", hash_verified: true, all_sources_complete: incomplete?.count === 0 });
  };
  if (row.status === "completed") return readiness();
  if (row.order_status !== "queued" || row.usage_status !== "reserved") throw new HttpError(409, "ORDEN_NO_RESERVADA");
  const now = Math.floor(Date.now() / 1000);
  if (!row.upload_id || !["open", "completing"].includes(row.status)) throw new HttpError(409, "SUBIDA_NO_ABIERTA");
  if (row.status === "open" && row.expires_at <= now) throw new HttpError(410, "SUBIDA_VENCIDA");
  const token = crypto.randomUUID();
  const claim = await env.DB.prepare(`UPDATE production_source_uploads SET status='completing',operation_id=?,lease_until=?
    WHERE source_id=? AND (status='open' OR (status='completing' AND lease_until<=?))
      AND EXISTS (SELECT 1 FROM production_sources source JOIN production_source_sets s ON s.order_id=source.order_id
        JOIN orders o ON o.id=s.order_id AND o.user_id=s.user_id JOIN usage_ledger l ON l.order_id=o.id AND l.user_id=o.user_id
        WHERE source.id=? AND o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=?
          AND o.status='queued' AND l.status='reserved')`)
    .bind(token, now + leaseDuration, sourceId, now, sourceId, order, user, env.BILLING_MODE).run();
  if (claim.meta.changes !== 1) throw new HttpError(409, "SUBIDA_EN_TRANSICION");
  const contentType = mime[row.extension];
  if (!contentType) throw new HttpError(422, "MATERIAL_INVALIDO");
  const job = validateSyncJob({ job_id: order, asset_id: row.id, project_slug: row.project_slug,
    extension: row.extension, bytes: row.bytes, sha256: row.sha256, object_key: row.object_key }, { maxObjectBytes: 1024 ** 3 });
  let object = await env.MEDIA.get(row.object_key);
  if (!object) {
    const present = await listMultipartParts(env, job, row.upload_id);
    if (present.length !== count || present.some((part, index) => part.PartNumber !== index + 1 ||
      part.Size !== Math.min(partSize, row.bytes - index * partSize) || (parts && part.ETag !== parts[index]?.ETag))) {
      const reopened = await env.DB.prepare(`UPDATE production_source_uploads SET status='open',operation_id=NULL,lease_until=NULL
        WHERE source_id=? AND status='completing' AND operation_id=?`).bind(sourceId, token).run();
      if (reopened.meta.changes !== 1) throw new HttpError(409, "SUBIDA_EN_TRANSICION");
      if (!request) return response({ id: row.id, uploadId: row.upload_id, key: row.object_key, status: "open",
        partSize, partCount: count });
      throw new HttpError(409, "SUBIDA_PARTES_INCOMPLETAS");
    }
    await env.MEDIA.resumeMultipartUpload(row.object_key, row.upload_id)
      .complete(present.map((part) => ({ partNumber: part.PartNumber, etag: part.ETag })));
    object = await env.MEDIA.get(row.object_key);
  }
  if (!object) throw new HttpError(503, "R2_OBJETO_AUSENTE");
  const own = object.key === row.object_key && object.customMetadata?.agency_asset_id === row.id;
  const matches = own && object.size === row.bytes && object.httpMetadata?.contentType === contentType &&
    object.customMetadata?.agency_sha256 === row.sha256;
  const valid = matches ? await verifyDigest(object.body, row.sha256) : (await object.body.cancel(), false);
  if (!valid) {
    if (own) await env.MEDIA.delete(row.object_key);
    await env.DB.prepare(`UPDATE production_source_uploads SET status='aborted',operation_id=NULL,lease_until=NULL
      WHERE source_id=? AND status='completing' AND operation_id=?`).bind(sourceId, token).run();
    throw new HttpError(422, matches ? "SUBIDA_HASH_DISTINTO" : "SUBIDA_METADATOS");
  }
  const committed = await env.DB.prepare(`UPDATE production_source_uploads SET status='completed',object_etag=?,operation_id=NULL,lease_until=NULL
    WHERE source_id=? AND status='completing' AND operation_id=? AND EXISTS (
      SELECT 1 FROM production_sources source JOIN production_source_sets s ON s.order_id=source.order_id
      JOIN orders o ON o.id=s.order_id AND o.user_id=s.user_id JOIN usage_ledger l ON l.order_id=o.id AND l.user_id=o.user_id
      WHERE source.id=? AND o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=?
        AND o.status='queued' AND l.status='reserved') RETURNING source_id`)
    .bind(object.etag, sourceId, token, sourceId, order, user, env.BILLING_MODE).first<{ source_id: string }>();
  if (!committed) throw new HttpError(409, "SUBIDA_EN_TRANSICION");
  return readiness();
}
