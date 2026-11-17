import { validateSyncJob } from "../../../src/media/sync-contract.js";
import { HttpError, readJson, response, type SqlDatabase } from "./platform.js";
import type { OutputEdit, OutputFormat, OutputResolution } from "./orders.js";

export interface ProductionDispatchEnv {
  DB: SqlDatabase;
  BILLING_MODE: "sandbox" | "production";
  STUDIO_PRODUCTION_DISPATCH_TOKEN?: string;
}

type Claimed = { order_id: string; attempts: number; lease: string; available_at: number };
type Work = { user_id: string; plan_id: string; upload_id: string; payload: string; sha256: string; bytes: number;
  project_slug: string; output_resolution: OutputResolution; output_format: OutputFormat; output_edit: OutputEdit };
export const productionDispatchPath = "/api/internal/production/dispatch";
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const tokenPattern = /^[a-f0-9]{64}$/u;
const encoder = new TextEncoder();
const leaseSeconds = 120;

function matches(value: unknown, pattern: RegExp, length: number): value is string {
  return typeof value === "string" && value.length === length && pattern.test(value);
}

async function authorized(request: Request, env: ProductionDispatchEnv): Promise<void> {
  const secret = env.STUDIO_PRODUCTION_DISPATCH_TOKEN ?? "";
  if (secret.length !== 64 || !tokenPattern.test(secret)) throw new HttpError(404, "RUTA_NO_DISPONIBLE");
  const header = request.headers.get("Authorization") ?? "";
  if (header.length !== 71 || !header.startsWith("Bearer ") || !tokenPattern.test(header.slice(7))) {
    throw new HttpError(401, "DESPACHO_NO_AUTORIZADO");
  }
  const algorithm = { name: "HMAC", hash: "SHA-256" };
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), algorithm, false, ["verify"]);
  const provided = await crypto.subtle.importKey("raw", encoder.encode(header.slice(7)), algorithm, false, ["sign"]);
  const context = encoder.encode("studio-production-dispatch-v1");
  const proof = await crypto.subtle.sign(algorithm.name, provided, context);
  if (!await crypto.subtle.verify(algorithm.name, key, proof, context)) throw new HttpError(401, "DESPACHO_NO_AUTORIZADO");
}

async function claim(env: ProductionDispatchEnv, now: number): Promise<Response> {
  const lease = crypto.randomUUID();
  const taken = await env.DB.prepare(`UPDATE production_dispatch SET available_at=?, attempts=attempts+1, lease=?
    WHERE order_id = (SELECT d.order_id FROM production_dispatch d JOIN orders o ON o.id=d.order_id
      WHERE d.available_at<=? AND d.received_at IS NULL AND o.kind='production' AND o.status='processing'
        AND COALESCE(o.provider_mode,'production')=?
      ORDER BY o.queue_priority DESC,d.available_at,d.order_id LIMIT 1)
    RETURNING order_id,attempts,lease,available_at`).bind(now + leaseSeconds, lease, now, env.BILLING_MODE).first<Claimed>();
  if (!taken) return response({ job: null });
  const work = await env.DB.withSession("first-primary").prepare(`SELECT o.user_id,o.plan_id,o.project_slug,s.id AS upload_id,
    s.sha256,s.bytes,j.payload,o.output_resolution,o.output_format,o.output_edit FROM orders o JOIN job_outbox j ON j.order_id=o.id
    JOIN usage_ledger u ON u.order_id=o.id AND u.user_id=o.user_id AND u.status='consumed'
    JOIN upload_sessions s ON s.order_id=o.id AND s.user_id=o.user_id AND s.status='completed'
    WHERE o.id=? AND o.kind='production' AND o.status='processing' AND COALESCE(o.provider_mode,'production')=?`)
    .bind(taken.order_id, env.BILLING_MODE).first<Work>();
  if (!work) throw new HttpError(409, "DESPACHO_ORIGEN_CAMBIO");
  const media = validateSyncJob(JSON.parse(work.payload), { maxObjectBytes: 1024 ** 3 });
  if (media.job_id !== taken.order_id || media.sha256 !== work.sha256 || media.bytes !== work.bytes
    || media.project_slug !== work.project_slug) throw new HttpError(409, "DESPACHO_ORIGEN_CAMBIO");
  return response({ job: { order_id: taken.order_id, attempt: taken.attempts, lease: taken.lease,
    lease_expires_at: taken.available_at, user_id: work.user_id, plan_id: work.plan_id, upload_id: work.upload_id,
    mode: env.BILLING_MODE, media, ...(work.output_resolution === "4k" ? { output_resolution: "4k" } : {}),
    ...(work.output_format === "prores4444" ? { output_format: work.output_format } : {}),
    ...(work.output_edit === "reel" ? { output_edit: work.output_edit } : {}) } });
}

async function recordQualityOutcome(body: Record<string, unknown>, env: ProductionDispatchEnv, now: number): Promise<Response> {
  const { order_id, user_id, upload_id, source_sha256, revision, brief_confirmed_at } = body;
  const manual = body.action === "quality_manual_review";
  const fingerprint = manual ? body.direction_sha256 : body.master_sha256;
  if (Object.keys(body).length !== 8 || !matches(order_id, uuid, 36) || !matches(user_id, uuid, 36)
    || !matches(upload_id, uuid, 36) || !matches(source_sha256, tokenPattern, 64) || !matches(fingerprint, tokenPattern, 64)
    || typeof revision !== "number" || !Number.isInteger(revision) || revision < 1 || revision > 3
    || typeof brief_confirmed_at !== "number" || !Number.isSafeInteger(brief_confirmed_at)
    || brief_confirmed_at < 0 || brief_confirmed_at > now) throw new HttpError(422, "DICTAMEN_DATOS_INVALIDOS");

  const scope = `o.id=? AND o.user_id=? AND o.kind='production' AND COALESCE(o.provider_mode,'production')=?
    AND EXISTS (SELECT 1 FROM production_briefs b
      JOIN production_dispatch d ON d.order_id=b.order_id AND d.received_at IS NOT NULL
      JOIN upload_sessions s ON s.order_id=b.order_id AND s.user_id=b.user_id AND s.status='completed'
      JOIN usage_ledger u ON u.order_id=b.order_id AND u.user_id=b.user_id AND u.status='consumed'
      JOIN plan_quotas p ON p.plan_id=o.plan_id
      WHERE b.order_id=o.id AND b.user_id=o.user_id AND b.confirmed_at=? AND s.id=? AND s.sha256=?
        AND p.autonomous_qa_rounds${manual ? ">=" : "="}?)`;
  const evidence = [order_id, user_id, env.BILLING_MODE, brief_confirmed_at, upload_id, source_sha256, revision] as const;
  if (manual) {
    const saved = await env.DB.prepare(`INSERT INTO production_manual_reviews(order_id,revision,direction_sha256,recorded_at)
      SELECT o.id,?,?,? FROM orders o WHERE ${scope} AND o.status='processing'
        AND NOT EXISTS (SELECT 1 FROM delivery_kits k WHERE k.order_id=o.id)
        AND NOT EXISTS (SELECT 1 FROM production_manual_adjustments a WHERE a.order_id=o.id
          AND a.revision=? AND a.review_sha256=? AND a.status<>'pending')
      ON CONFLICT(order_id) DO NOTHING`).bind(revision, fingerprint, now, ...evidence, revision, fingerprint).run();
    if (saved.meta.changes === 1) return response({ recorded: true });
    const repeat = await env.DB.withSession("first-primary").prepare(`SELECT 1 FROM orders o
      JOIN production_manual_reviews r ON r.order_id=o.id
      WHERE ${scope} AND o.status='processing' AND r.revision=? AND r.direction_sha256=?`)
      .bind(...evidence, revision, fingerprint).first();
    if (!repeat) throw new HttpError(409, "DICTAMEN_FUERA_DE_ORDEN");
    return response({ recorded: true });
  }
  const saved = await env.DB.prepare(`UPDATE orders AS o SET status='failed',qa_rejected_master_sha256=?,updated_at=?
    WHERE ${scope} AND o.status='processing'
      AND NOT EXISTS (SELECT 1 FROM production_manual_reviews r WHERE r.order_id=o.id)
      AND NOT EXISTS (SELECT 1 FROM delivery_kits k WHERE k.order_id=o.id)`)
    .bind(fingerprint, now, ...evidence).run();
  if (saved.meta.changes === 1) return response({ recorded: true });

  const repeat = await env.DB.withSession("first-primary").prepare(`SELECT 1 FROM orders o
    WHERE ${scope} AND o.status='failed' AND o.qa_rejected_master_sha256=?`)
    .bind(...evidence, fingerprint).first();
  if (!repeat) throw new HttpError(409, "DICTAMEN_FUERA_DE_ORDEN");
  return response({ recorded: true });
}

type ManualContext = {
  order_id: string; user_id: string; objective: string; audience: string; tone: string; confirmed_at: number;
  number: number; revision: number; review_sha256: string; instructions: string;
};

async function manualContext(orderId: string, env: ProductionDispatchEnv): Promise<Response> {
  const row = await env.DB.withSession("first-primary").prepare(`SELECT b.order_id,b.user_id,b.objective,b.audience,b.tone,b.confirmed_at,
    a.number,a.revision,a.review_sha256,a.instructions
    FROM production_manual_adjustments a JOIN orders o ON o.id=a.order_id
    JOIN production_manual_reviews r ON r.order_id=o.id AND r.revision=a.revision AND r.direction_sha256=a.review_sha256
    JOIN production_briefs b ON b.order_id=o.id AND b.user_id=o.user_id
    JOIN production_dispatch d ON d.order_id=o.id AND d.received_at IS NOT NULL
    WHERE o.id=? AND o.kind='production' AND o.status='processing' AND COALESCE(o.provider_mode,'production')=?
      AND a.status='pending'`).bind(orderId, env.BILLING_MODE).first<ManualContext>();
  if (!row) return response({ brief: null, adjustment: null });
  const { number, revision, review_sha256, instructions, ...brief } = row;
  return response({ brief, adjustment: { number, revision, review_sha256, instructions } });
}

async function recordManualAdjustment(body: Record<string, unknown>, env: ProductionDispatchEnv, now: number): Promise<Response> {
  const { order_id, user_id, upload_id, source_sha256, revision, brief_confirmed_at,
    number, review_sha256, direction_sha256, status } = body;
  if (Object.keys(body).length !== 11 || !matches(order_id, uuid, 36) || !matches(user_id, uuid, 36)
    || !matches(upload_id, uuid, 36) || !matches(source_sha256, tokenPattern, 64)
    || !matches(review_sha256, tokenPattern, 64) || !matches(direction_sha256, tokenPattern, 64)
    || typeof revision !== "number" || !Number.isInteger(revision) || revision < 1 || revision > 3
    || typeof number !== "number" || !Number.isSafeInteger(number) || number < 1
    || typeof brief_confirmed_at !== "number" || !Number.isSafeInteger(brief_confirmed_at)
    || brief_confirmed_at < 0 || brief_confirmed_at > now
    || (status !== "ready" && status !== "needs_review") || (status === "ready" && direction_sha256 === review_sha256)) {
    throw new HttpError(422, "AJUSTE_RESULTADO_INVALIDO");
  }
  const subject = `EXISTS (SELECT 1 FROM orders o
    JOIN production_briefs b ON b.order_id=o.id AND b.user_id=o.user_id
    JOIN production_dispatch d ON d.order_id=o.id AND d.received_at IS NOT NULL
    JOIN upload_sessions s ON s.order_id=o.id AND s.user_id=o.user_id AND s.status='completed'
    JOIN usage_ledger u ON u.order_id=o.id AND u.user_id=o.user_id AND u.status='consumed'
    WHERE o.id=a.order_id AND o.user_id=? AND o.kind='production' AND COALESCE(o.provider_mode,'production')=?
      AND b.confirmed_at=? AND s.id=? AND s.sha256=?)`;
  const bound = [order_id, number, revision, review_sha256, user_id, env.BILLING_MODE,
    brief_confirmed_at, upload_id, source_sha256] as const;
  const where = `a.order_id=? AND a.number=? AND a.revision=? AND a.review_sha256=? AND ${subject}`;
  const changed = await env.DB.prepare(`UPDATE production_manual_adjustments AS a
    SET status=?,result_sha256=?,resolved_at=? WHERE ${where} AND a.status='pending'
      AND EXISTS (SELECT 1 FROM orders o WHERE o.id=a.order_id AND o.status='processing')
      AND EXISTS (SELECT 1 FROM production_manual_reviews r
        WHERE r.order_id=a.order_id AND r.revision=a.revision AND r.direction_sha256=a.review_sha256)`)
    .bind(status, direction_sha256, now, ...bound).run();
  if (changed.meta.changes === 1) return response({ recorded: true });
  const repeat = await env.DB.withSession("first-primary").prepare(`SELECT 1 FROM production_manual_adjustments a
    WHERE ${where} AND a.status=? AND a.result_sha256=?`)
    .bind(...bound, status, direction_sha256).first();
  if (!repeat) throw new HttpError(409, "AJUSTE_RESULTADO_FUERA_DE_ORDEN");
  return response({ recorded: true });
}

export async function handleProductionDispatch(request: Request, env: ProductionDispatchEnv): Promise<Response> {
  if (request.method !== "POST") throw new HttpError(405, "METODO_NO_PERMITIDO");
  await authorized(request, env);
  const body = await readJson(request, 1024);
  const now = Math.floor(Date.now() / 1000);
  if (body.action === "claim" && Object.keys(body).length === 1) return claim(env, now);
  if (body.action === "quality_rejected" || body.action === "quality_manual_review") return recordQualityOutcome(body, env, now);
  if (body.action === "manual_adjustment_result") return recordManualAdjustment(body, env, now);
  if (body.action === "manual_context" && Object.keys(body).length === 2 && matches(body.order_id, uuid, 36)) {
    return manualContext(body.order_id, env);
  }
  if (body.action === "brief" && Object.keys(body).length === 2 && typeof body.order_id === "string"
    && body.order_id.length === 36 && uuid.test(body.order_id)) {
    const brief = await env.DB.withSession("first-primary").prepare(`SELECT b.order_id,b.user_id,b.objective,b.audience,b.tone,b.confirmed_at
      FROM production_briefs b JOIN orders o ON o.id=b.order_id AND o.user_id=b.user_id
      JOIN production_dispatch d ON d.order_id=o.id AND d.received_at IS NOT NULL
      WHERE o.id=? AND o.kind='production' AND o.status='processing' AND COALESCE(o.provider_mode,'production')=?
        AND NOT EXISTS (SELECT 1 FROM production_manual_reviews r WHERE r.order_id=o.id)`)
      .bind(body.order_id, env.BILLING_MODE).first();
    return response({ brief });
  }
  if (body.action !== "ack" || Object.keys(body).length !== 3 || typeof body.order_id !== "string"
    || typeof body.lease !== "string" || body.order_id.length !== 36 || body.lease.length !== 36
    || !uuid.test(body.order_id) || !uuid.test(body.lease)) throw new HttpError(422, "DESPACHO_SOLICITUD_INVALIDA");
  const saved = await env.DB.prepare(`UPDATE production_dispatch SET received_at=?, lease=NULL
    WHERE order_id=? AND lease=? AND available_at>? AND received_at IS NULL
      AND EXISTS (SELECT 1 FROM orders o WHERE o.id=production_dispatch.order_id AND o.status='processing'
        AND COALESCE(o.provider_mode,'production')=?)`).bind(now, body.order_id, body.lease, now, env.BILLING_MODE).run();
  if (saved.meta.changes !== 1) throw new HttpError(409, "DESPACHO_RESERVA_VENCIDA");
  return response({ received: true });
}
