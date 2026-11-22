import { HttpError, readJson, response } from "./platform.js";
import type { OrderEnv } from "./orders.js";

export const manualAdjustmentRoute = /^\/api\/orders\/([a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12})\/manual-adjustment$/u;
const scope = `o.id=? AND o.user_id=? AND o.kind='production' AND COALESCE(o.provider_mode,'production')=?`;
const visible = "number,instructions,status,requested_at,resolved_at";

type Adjustment = { number: number; instructions: string; status: "pending" | "ready" | "needs_review";
  requested_at: number; resolved_at: number | null };
type Current = { status: string; paused: number; allowed: number; limit: number | null; used: number; pending: number;
  review_sha256: string | null; revision: number | null };

function note(input: Record<string, unknown>): string {
  if (Object.keys(input).length !== 1 || typeof input.instructions !== "string") {
    throw new HttpError(422, "AJUSTE_INVALIDO");
  }
  const text = input.instructions.replace(/\r\n?/gu, "\n").trim();
  if (!text || text.length > 800 || /[\p{Cc}\p{Cs}]/u.test(text.replace(/[\n\t]/gu, ""))) {
    throw new HttpError(422, "AJUSTE_INVALIDO");
  }
  return text;
}

function remaining(current: Current): number | null {
  return current.limit === null ? null : Math.max(0, current.limit - current.used);
}

function validateWritable(current: Current): void {
  if (current.allowed !== 1) throw new HttpError(403, "AJUSTES_FUERA_DEL_PLAN");
  if (current.status !== "processing" || current.paused !== 1) throw new HttpError(409, "REVISION_NO_PENDIENTE");
  if (current.pending) throw new HttpError(409, "AJUSTE_EN_CURSO");
  if (remaining(current) === 0) throw new HttpError(409, "AJUSTES_AGOTADOS");
}

export async function handleManualAdjustment(request: Request, env: OrderEnv, userId: string, orderId: string): Promise<Response> {
  if (request.method !== "GET" && request.method !== "POST") throw new HttpError(405, "METODO_NO_PERMITIDO");
  const bound = [orderId, userId, env.BILLING_MODE] as const;
  const session = env.DB.withSession("first-primary");
  const readState = () => session.prepare(`SELECT o.status, r.order_id IS NOT NULL AS paused,
    r.direction_sha256 AS review_sha256,r.revision,
    p.allow_copilot AS allowed, p.human_adjustments AS "limit",
    (SELECT count(*) FROM production_manual_adjustments a WHERE a.order_id=o.id) AS used,
    EXISTS (SELECT 1 FROM production_manual_adjustments a WHERE a.order_id=o.id AND a.status='pending') AS pending
    FROM orders o JOIN plan_quotas p ON p.plan_id=o.plan_id
    LEFT JOIN production_manual_reviews r ON r.order_id=o.id WHERE ${scope}`)
    .bind(...bound).first<Current>();
  const current = await readState();
  if (!current) throw new HttpError(404, "ORDEN_NO_ENCONTRADA");
  if (request.method === "GET") {
    const latest = await session.prepare(`SELECT ${visible} FROM production_manual_adjustments
      WHERE order_id=? ORDER BY number DESC LIMIT 1`).bind(orderId).first<Adjustment>();
    return response({ adjustment: latest, paused: current.paused === 1,
      review_round: current.revision,
      included: current.allowed === 1,
      available: current.allowed === 1 && current.paused === 1 && current.status === "processing"
        && !current.pending && remaining(current) !== 0,
      remaining: remaining(current) });
  }
  const key = request.headers.get("Idempotency-Key") ?? "";
  if (!/^[a-zA-Z0-9_-]{1,128}$/u.test(key)) throw new HttpError(400, "IDEMPOTENCIA_INVALIDA");
  const instructions = note(await readJson(request, 4096));
  const find = () => session.prepare(`SELECT ${visible} FROM production_manual_adjustments
    WHERE order_id=? AND request_key=?`).bind(orderId, key).first<Adjustment>();
  const replay = (saved: Adjustment) => {
    if (saved.instructions !== instructions) throw new HttpError(409, "IDEMPOTENCIA_EN_CONFLICTO");
    return response({ adjustment: saved });
  };
  const existing = await find();
  if (existing) return replay(existing);
  validateWritable(current);
  const inserted = await env.DB.prepare(`INSERT INTO production_manual_adjustments(
    order_id,number,request_key,revision,review_sha256,instructions,requested_at)
    SELECT o.id,1+(SELECT count(*) FROM production_manual_adjustments a WHERE a.order_id=o.id),
      ?,r.revision,r.direction_sha256,?,? FROM orders o
    JOIN production_manual_reviews r ON r.order_id=o.id JOIN plan_quotas p ON p.plan_id=o.plan_id
    WHERE ${scope} AND r.direction_sha256=? AND r.revision=? AND o.status='processing' AND p.allow_copilot=1
      AND (p.human_adjustments IS NULL OR p.human_adjustments >
        (SELECT count(*) FROM production_manual_adjustments a WHERE a.order_id=o.id))
      AND NOT EXISTS (SELECT 1 FROM production_manual_adjustments a WHERE a.order_id=o.id AND a.status='pending')
    ON CONFLICT DO NOTHING RETURNING ${visible}`)
    .bind(key, instructions, Math.floor(Date.now() / 1000), ...bound, current.review_sha256,
      current.revision).first<Adjustment>();
  if (inserted) return response({ adjustment: inserted }, 201);
  const concurrent = await find();
  if (concurrent) return replay(concurrent);
  const updated = await readState();
  if (!updated) throw new HttpError(404, "ORDEN_NO_ENCONTRADA");
  validateWritable(updated);
  throw new HttpError(409, "REVISION_CAMBIO_REINTENTAR");
}
