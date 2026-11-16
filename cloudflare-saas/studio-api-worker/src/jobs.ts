import { importSyncKey, signSyncJob, validateSyncJob, type SyncJob } from "../../../src/media/sync-contract.js";
import type { SqlDatabase } from "./platform.js";

interface OutboxRow { order_id: string; payload: string; attempts: number }
export interface JobMessage {
  body: unknown;
  attempts: number;
  ack(): void;
  retry(options: { delaySeconds: number }): void;
}
export interface JobEnv {
  DB: SqlDatabase;
  JOBS?: { send(message: SyncJob): Promise<void> };
  STUDIO_EDGE_TO_HOMELAB_HMAC_SECRET?: string;
  HOMELAB_SYNC_ORIGIN?: string;
}

const policy = { maxObjectBytes: 1024 ** 3 };
const syncUrl = "https://api-studio.e-scale.cl/v1/sync";
const pollIntervalMs = 10_000;
// Un worker local tiene hasta 300 s. Dos mensajes consecutivos caben en los 900 s de Queues.
const syncBudgetMs = 330_000;
const queueBudgetMs = 850_000;
const maxSyncRequests = 45;

async function awaitSync(job: SyncJob, secret: string, send: typeof fetch,
  budget: { deadline: number; requests: number }): Promise<boolean> {
  const key = await importSyncKey(new TextEncoder().encode(secret));
  const deadline = Math.min(Date.now() + syncBudgetMs, budget.deadline);
  while (budget.requests > 0 && Date.now() < deadline) {
    const signed = await signSyncJob(job, policy, key, String(Math.floor(Date.now() / 1000)));
    const remaining = deadline - Date.now();
    if (remaining <= 0) return false;
    budget.requests -= 1;
    const response = await send(syncUrl, {
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

export async function dispatchOutbox(env: JobEnv, orderId?: string): Promise<void> {
  if (!env.JOBS || env.HOMELAB_SYNC_ORIGIN !== "https://api-studio.e-scale.cl") return;
  const time = Math.floor(Date.now() / 1000);
  const rows = await env.DB.withSession("first-primary").prepare(`SELECT j.order_id,j.payload,j.attempts
    FROM job_outbox j JOIN orders o ON o.id=j.order_id
    WHERE j.dispatched_at IS NULL AND j.available_at<=? AND o.status='queued'
      ${orderId === undefined ? "" : "AND j.order_id=?"}
    ORDER BY o.queue_priority DESC,j.available_at,j.order_id LIMIT 20`)
    .bind(time, ...(orderId === undefined ? [] : [orderId])).all<OutboxRow>();
  for (const row of rows.results) {
    const claimed = await env.DB.prepare(`UPDATE job_outbox SET available_at=?,attempts=attempts+1
      WHERE order_id=? AND available_at<=? AND attempts=? AND dispatched_at IS NULL`)
      .bind(time + 60, row.order_id, time, row.attempts).run();
    if (claimed.meta.changes !== 1) continue;
    try {
      const job = validateSyncJob(JSON.parse(row.payload), policy);
      if (job.job_id !== row.order_id) continue;
      await env.JOBS.send(job);
      await env.DB.prepare("UPDATE job_outbox SET dispatched_at=? WHERE order_id=? AND dispatched_at IS NULL")
        .bind(time, row.order_id).run();
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
