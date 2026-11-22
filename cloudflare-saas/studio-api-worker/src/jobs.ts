import { importSyncKey, signSyncJob, signSourceSyncJob, validateSyncJob, type SyncJob } from "../../../src/media/sync-contract.js";
import type { SqlDatabase } from "./platform.js";

interface OutboxRow { order_id: string; payload: string; attempts: number }
interface SourceOutboxRow { source_id: string; order_id: string; payload: string; attempts: number }
type QueueValue = SyncJob | { kind: "production_source"; job: SyncJob };
export interface JobMessage {
  body: unknown;
  attempts: number;
  ack(): void;
  retry(options: { delaySeconds: number }): void;
}
export interface JobEnv {
  DB: SqlDatabase;
  JOBS?: { send(message: QueueValue): Promise<void> };
  STUDIO_EDGE_TO_HOMELAB_HMAC_SECRET?: string;
  HOMELAB_SYNC_ORIGIN?: string;
}

const policy = { maxObjectBytes: 1024 ** 3 };
const syncUrl = "https://api-studio.e-scale.cl/v1/sync";
const pollIntervalMs = 10_000;
const outboxLeaseSeconds = 60;
// Un worker local tiene hasta 300 s. Dos mensajes consecutivos caben en los 900 s de Queues.
const syncBudgetMs = 330_000;
const queueBudgetMs = 850_000;
const maxSyncRequests = 45;

async function awaitSync(job: SyncJob, secret: string, send: typeof fetch,
  budget: { deadline: number; requests: number }, source = false): Promise<boolean> {
  const key = await importSyncKey(new TextEncoder().encode(secret));
  const deadline = Math.min(Date.now() + syncBudgetMs, budget.deadline);
  while (budget.requests > 0 && Date.now() < deadline) {
    const signed = await (source ? signSourceSyncJob : signSyncJob)(job, policy, key, String(Math.floor(Date.now() / 1000)));
    const remaining = deadline - Date.now();
    if (remaining <= 0) return false;
    budget.requests -= 1;
    const response = await send(source ? syncUrl.replace("/v1/sync", "/v2/sync") : syncUrl, {
      method: "POST", redirect: "manual", signal: AbortSignal.timeout(Math.min(30_000, remaining)),
      headers: { "content-type": "application/json", timestamp: signed.timestamp, signature: signed.signature },
      body: Uint8Array.from(signed.body),
    });
    const raw = await response.text();
    if (raw.length > 4096) return false;
    const result = JSON.parse(raw) as { status?: string; error?: string; sha256?: string; bytes?: number; job_id?: string; asset_id?: string };
    if (response.status === 200) {
      return result.status === "completed" && result.sha256 === job.sha256 && result.bytes === job.bytes
        && result.job_id === job.job_id && result.asset_id === job.asset_id;
    }
    const pending = (response.status === 202 && result.status === "processing") || (response.status === 503 && result.error === "BUSY");
    if (!pending || budget.requests === 0 || deadline - Date.now() <= pollIntervalMs) return false;
    await new Promise<void>((resolve) => setTimeout(resolve, pollIntervalMs));
  }
  return false;
}

export async function dispatchSourceOutbox(env: JobEnv, sourceId?: string): Promise<void> {
  if (!env.JOBS || env.HOMELAB_SYNC_ORIGIN !== "https://api-studio.e-scale.cl") return;
  // D1 fija available_at al confirmar la carga. Usa su reloj también al seleccionar y reservar.
  const rows = await env.DB.withSession("first-primary").prepare(`SELECT q.source_id,s.order_id,q.payload,q.attempts
    FROM production_source_outbox q JOIN production_sources s ON s.id=q.source_id JOIN orders o ON o.id=s.order_id
    WHERE q.dispatched_at IS NULL AND q.available_at<=unixepoch() AND q.synced_at IS NULL AND o.status='queued'
      ${sourceId === undefined ? "" : "AND q.source_id=?"}
    ORDER BY o.queue_priority DESC,q.available_at,s.position LIMIT 20`)
    .bind(...(sourceId === undefined ? [] : [sourceId])).all<SourceOutboxRow>();
  for (const row of rows.results) {
    const claim = await env.DB.prepare(`UPDATE production_source_outbox SET available_at=unixepoch()+?,attempts=attempts+1
      WHERE source_id=? AND available_at<=unixepoch() AND attempts=? AND dispatched_at IS NULL AND synced_at IS NULL`)
      .bind(outboxLeaseSeconds, row.source_id, row.attempts).run();
    if (claim.meta.changes !== 1) continue;
    try {
      const job = validateSyncJob(JSON.parse(row.payload), policy);
      if (job.job_id !== row.order_id || job.asset_id !== row.source_id) continue;
      await env.JOBS.send({ kind: "production_source", job });
      await env.DB.prepare("UPDATE production_source_outbox SET dispatched_at=unixepoch() WHERE source_id=? AND dispatched_at IS NULL")
        .bind(row.source_id).run();
    } catch { /* El cron reintenta después del vencimiento de la concesión. */ }
  }
}

async function syncSource(body: unknown, env: JobEnv, secret: string, send: typeof fetch,
  budget: { deadline: number; requests: number }): Promise<"ack" | "retry"> {
  if (!body || typeof body !== "object" || Array.isArray(body)) return "retry";
  const wrapper = body as Record<string, unknown>;
  if (Object.keys(wrapper).sort().join(",") !== "job,kind" || wrapper.kind !== "production_source") return "retry";
  const job = validateSyncJob(wrapper.job, policy);
  const claim = await env.DB.withSession("first-primary").prepare(`SELECT o.status,u.status AS usage_status,q.payload,q.synced_at
    FROM production_source_outbox q JOIN production_sources s ON s.id=q.source_id
    JOIN production_source_uploads upload ON upload.source_id=s.id
    JOIN orders o ON o.id=s.order_id JOIN usage_ledger u ON u.order_id=o.id AND u.user_id=o.user_id
    WHERE s.id=? AND o.id=? AND upload.status='completed'`)
    .bind(job.asset_id, job.job_id).first<{ status: string; usage_status: string; payload: string; synced_at: number | null }>();
  if (!claim || claim.status === "cancelled" || claim.synced_at !== null) return "ack";
  if (claim.status !== "queued" || claim.usage_status !== "reserved" ||
    JSON.stringify(validateSyncJob(JSON.parse(claim.payload), policy)) !== JSON.stringify(job)) return "retry";
  if (!await awaitSync(job, secret, send, budget, true)) return "retry";
  const ready = `EXISTS (SELECT 1 FROM production_sources s WHERE s.order_id=?)
    AND NOT EXISTS (SELECT 1 FROM production_sources s LEFT JOIN production_source_uploads up ON up.source_id=s.id
      LEFT JOIN production_source_outbox pending ON pending.source_id=s.id WHERE s.order_id=?
        AND (up.status IS NULL OR up.status<>'completed' OR pending.synced_at IS NULL))`;
  const now = Math.floor(Date.now() / 1000);
  const saved = await env.DB.batch([
    env.DB.prepare(`UPDATE production_source_outbox SET synced_at=? WHERE source_id=? AND synced_at IS NULL
    AND payload=? AND EXISTS (SELECT 1 FROM production_sources s JOIN orders o ON o.id=s.order_id
      JOIN usage_ledger u ON u.order_id=o.id AND u.user_id=o.user_id
      WHERE s.id=? AND o.id=? AND o.status='queued' AND u.status='reserved')`)
      .bind(now, job.asset_id, claim.payload, job.asset_id, job.job_id),
    env.DB.prepare(`UPDATE usage_ledger SET status='consumed',updated_at=? WHERE order_id=? AND status='reserved'
      AND EXISTS (SELECT 1 FROM orders o WHERE o.id=usage_ledger.order_id AND o.status='queued') AND ${ready}`)
      .bind(now, job.job_id, job.job_id, job.job_id),
    env.DB.prepare(`UPDATE orders SET status='processing',updated_at=? WHERE id=? AND status='queued'
      AND EXISTS (SELECT 1 FROM usage_ledger u WHERE u.order_id=orders.id AND u.user_id=orders.user_id AND u.status='consumed')
      AND ${ready}`).bind(now, job.job_id, job.job_id, job.job_id),
  ]);
  if (saved[0]?.meta.changes === 1) return "ack";
  const current = await env.DB.withSession("first-primary").prepare(`SELECT q.synced_at,o.status
    FROM production_source_outbox q JOIN production_sources s ON s.id=q.source_id
    JOIN orders o ON o.id=s.order_id WHERE q.source_id=? AND o.id=?`)
    .bind(job.asset_id, job.job_id).first<{ synced_at: number | null; status: string }>();
  return !current || current.status === "cancelled" || current.synced_at !== null ? "ack" : "retry";
}

export async function dispatchOutbox(env: JobEnv, orderId?: string): Promise<void> {
  if (!env.JOBS || env.HOMELAB_SYNC_ORIGIN !== "https://api-studio.e-scale.cl") return;
  const rows = await env.DB.withSession("first-primary").prepare(`SELECT j.order_id,j.payload,j.attempts
    FROM job_outbox j JOIN orders o ON o.id=j.order_id
    WHERE j.dispatched_at IS NULL AND j.available_at<=unixepoch() AND o.status='queued'
      ${orderId === undefined ? "" : "AND j.order_id=?"}
    ORDER BY o.queue_priority DESC,j.available_at,j.order_id LIMIT 20`)
    .bind(...(orderId === undefined ? [] : [orderId])).all<OutboxRow>();
  for (const row of rows.results) {
    const claimed = await env.DB.prepare(`UPDATE job_outbox SET available_at=unixepoch()+?,attempts=attempts+1
      WHERE order_id=? AND available_at<=unixepoch() AND attempts=? AND dispatched_at IS NULL`)
      .bind(outboxLeaseSeconds, row.order_id, row.attempts).run();
    if (claimed.meta.changes !== 1) continue;
    try {
      const job = validateSyncJob(JSON.parse(row.payload), policy);
      if (job.job_id !== row.order_id) continue;
      await env.JOBS.send(job);
      await env.DB.prepare("UPDATE job_outbox SET dispatched_at=unixepoch() WHERE order_id=? AND dispatched_at IS NULL")
        .bind(row.order_id).run();
    } catch {
      // Vuelve a ser elegible al vencer la reserva. El consumidor es idempotente.
    }
  }
}

export async function syncQueue(messages: JobMessage[], env: JobEnv, send: typeof fetch = fetch): Promise<void> {
  const budget = { deadline: Date.now() + queueBudgetMs, requests: maxSyncRequests };
  for (const message of messages) {
    const retry = () => message.retry({ delaySeconds: Math.min(300, 2 ** Math.min(message.attempts, 8)) });
    const secret = env.STUDIO_EDGE_TO_HOMELAB_HMAC_SECRET;
    if (!secret || env.HOMELAB_SYNC_ORIGIN !== "https://api-studio.e-scale.cl") {
      retry();
      continue;
    }
    try {
      if (message.body && typeof message.body === "object" && "kind" in message.body &&
          (message.body as { kind?: unknown }).kind === "production_source") {
        if (await syncSource(message.body, env, secret, send, budget) === "ack") message.ack(); else retry();
        continue;
      }
      const job = validateSyncJob(message.body, policy);
      const claim = await env.DB.withSession("first-primary").prepare(`SELECT o.status,u.status AS usage_status,j.payload
        FROM orders o JOIN usage_ledger u ON u.order_id=o.id JOIN job_outbox j ON j.order_id=o.id WHERE o.id=?`)
        .bind(job.job_id).first<{ status: string; usage_status: string; payload: string }>();
      if (!claim || claim.status === "cancelled") {
        message.ack();
        continue;
      }
      if (claim.usage_status === "consumed") {
        message.ack();
        continue;
      }
      if (claim.status !== "queued" || claim.usage_status !== "reserved" ||
        JSON.stringify(validateSyncJob(JSON.parse(claim.payload), policy)) !== JSON.stringify(job)) {
        retry();
        continue;
      }
      if (!await awaitSync(job, secret, send, budget)) {
        retry();
        continue;
      }
      const t = Math.floor(Date.now() / 1000);
      await env.DB.batch([
        env.DB.prepare("UPDATE usage_ledger SET status='consumed',updated_at=? WHERE order_id=? AND status='reserved'")
          .bind(t, job.job_id),
        env.DB.prepare("UPDATE orders SET status='processing',updated_at=? WHERE id=? AND status='queued'")
          .bind(t, job.job_id),
      ]);
      message.ack();
    } catch {
      retry();
    }
  }
}
