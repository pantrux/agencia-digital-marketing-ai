import { sha256Hex } from "./identity.js";
import { brandIdPattern, findBrand } from "./brands.js";
import { HttpError, ownerId, prepareOwnedStatement, readJson, response, type SqlDatabase, type SqlOwner, type SqlSession } from "./platform.js";

const maxIdempotency = 128;
const orderColumns = "o.id,o.plan_id,o.project_slug,o.kind,o.status,o.queue_priority,o.created_at,o.updated_at,o.output_resolution,o.output_format,o.output_edit";
const brandColumns = "m.brand_id,m.display_name AS brand_name";
const brandJoin = "LEFT JOIN order_brand_profiles m ON m.order_id=o.id AND m.user_id=o.user_id AND m.provider_mode=o.provider_mode";
const quotaColumns = "plan_id,monthly_reels,queue_priority,max_resolution,watermark,max_output_seconds,allow_prores4444";
const activePlan = (owner = "?") => `COALESCE((SELECT plan_id FROM subscriptions
  WHERE user_id=${owner} AND provider_mode=? AND status='active' AND current_period_start<=? AND current_period_end>?), 'free')`;

export type PlanId = "free" | "pro" | "ultra" | "ilimitado";
export type OutputResolution = "1080p" | "4k";
export type OutputFormat = "mp4" | "prores4444";
export type OutputEdit = "full" | "reel";

interface Order {
  id: string;
  plan_id: PlanId;
  project_slug: string;
  kind: "production";
  status: string;
  queue_priority: number;
  output_resolution: OutputResolution;
  output_format: OutputFormat;
  output_edit: OutputEdit;
  request_sha256: string;
  created_at: number;
  updated_at: number;
  brand_id?: string | null;
  brand_name?: string | null;
}

interface Quota {
  plan_id: PlanId;
  monthly_reels: number | null;
  queue_priority: number;
  max_resolution: OutputResolution;
  allow_prores4444: number;
  watermark: number;
  max_output_seconds: number | null;
}

export type PlanWithUsage = Quota & { used: number };
export type ListedOrder = Omit<Order, "request_sha256"> & {
  received_at: number | null;
  brief_confirmed_at: number | null;
  production_issue: "qa_rechazado" | "requiere_revision" | null;
};

export interface OrderEnv {
  DB: SqlDatabase;
  BILLING_MODE: "sandbox" | "production";
  SOFIA_OWNER_USER_ID?: string;
}

function productionAvailable(env: OrderEnv, userId: string, plan: PlanId): boolean {
  return env.BILLING_MODE === "sandbox" && (plan !== "free" || userId === env.SOFIA_OWNER_USER_ID);
}

export function month(seconds: number): string {
  return new Date(seconds * 1000).toISOString().slice(0, 7) + "-01";
}

export async function activeQuota(env: OrderEnv, userId: string, time: number): Promise<Quota> {
  const row = await env.DB.withSession("first-primary").prepare(`SELECT ${quotaColumns}
    FROM plan_quotas WHERE plan_id=${activePlan()}`)
    .bind(userId, env.BILLING_MODE, time, time).first<Quota>();
  if (!row) throw new HttpError(503, "PLAN_NO_DISPONIBLE");
  return row;
}

export function preparePlan(db: SqlSession, owner: SqlOwner | string, env: OrderEnv, now: number) {
  return prepareOwnedStatement(db, owner, `SELECT ${quotaColumns},
    (SELECT COALESCE(SUM(u.units),0) FROM usage_ledger u JOIN orders o ON o.id=u.order_id
      WHERE u.user_id=${ownerId} AND u.period_start=? AND u.status IN ('reserved','consumed')
        AND COALESCE(o.provider_mode,'production')=?) AS used
    FROM plan_quotas WHERE plan_id=${activePlan(ownerId)}`, month(now), env.BILLING_MODE, env.BILLING_MODE, now, now);
}

export function planView(plan: PlanWithUsage | null | undefined, userId: string, env: OrderEnv, now: number) {
  if (!plan) throw new HttpError(503, "PLAN_NO_DISPONIBLE");
  return { ...plan, period_start: month(now), production_available: productionAvailable(env, userId, plan.plan_id) };
}

export async function readPlan(userId: string, env: OrderEnv) {
  const now = Math.floor(Date.now() / 1000);
  const row = await preparePlan(env.DB.withSession("first-primary"), userId, env, now).first<PlanWithUsage>();
  return planView(row, userId, env, now);
}

export async function describePlan(userId: string, env: OrderEnv): Promise<Response> {
  return response(await readPlan(userId, env));
}

type InternalFields = "request_sha256" | "brand_id" | "brand_name";

export function publicOrder<T extends Pick<Order, "id"> & Partial<Pick<Order, InternalFields>>>(row: T) {
  const { request_sha256: _, brand_id, brand_name, ...result } = row;
  void _;
  return { ...result, ...(brand_id && brand_name ? { brand: { id: brand_id, display_name: brand_name } } : {}) };
}

export function prepareOrders(db: SqlSession, owner: SqlOwner | string, env: OrderEnv) {
  return prepareOwnedStatement(db, owner, `SELECT o.id,o.plan_id,o.project_slug,o.kind,
    o.status,o.queue_priority,o.created_at,o.updated_at,o.output_resolution,o.output_format,o.output_edit,${brandColumns},
    d.received_at,b.confirmed_at AS brief_confirmed_at,
    CASE WHEN o.qa_rejected_master_sha256 IS NOT NULL THEN 'qa_rechazado'
      WHEN o.status='processing' AND r.order_id IS NOT NULL THEN 'requiere_revision' END AS production_issue
    FROM orders o LEFT JOIN production_dispatch d ON d.order_id=o.id
    LEFT JOIN production_briefs b ON b.order_id=o.id AND b.user_id=o.user_id
    LEFT JOIN production_manual_reviews r ON r.order_id=o.id
    ${brandJoin}
    WHERE o.user_id=${ownerId} AND o.kind='production' AND COALESCE(o.provider_mode,'production')=?
    ORDER BY o.created_at DESC,o.id LIMIT 50`, env.BILLING_MODE);
}

export async function readOrders(userId: string, env: OrderEnv) {
  const list = await prepareOrders(env.DB.withSession("first-primary"), userId, env).all<ListedOrder>();
  return list.results.map(publicOrder);
}

export async function listOrders(userId: string, env: OrderEnv): Promise<Response> {
  return response({ orders: await readOrders(userId, env) });
}

export async function reserveOrder(request: Request, userId: string, env: OrderEnv): Promise<Response> {
  const key = request.headers.get("idempotency-key") ?? "";
  if (!/^[a-zA-Z0-9_-]+$/.test(key) || key.length > maxIdempotency) throw new HttpError(400, "IDEMPOTENCIA_INVALIDA");
  const input = await readJson(request);
  const resolution = input.output_resolution === undefined ? "1080p" : input.output_resolution;
  const format = input.output_format === undefined ? "mp4" : input.output_format;
  const edit = input.output_edit === undefined ? "full" : input.output_edit;
  const brandId = input.brand_id;
  if (Object.keys(input).some((field) => !["kind", "output_resolution", "output_format", "output_edit", "brand_id"].includes(field))
    || input.kind !== "production" || (resolution !== "1080p" && resolution !== "4k")
    || (format !== "mp4" && format !== "prores4444")
    || (edit !== "full" && edit !== "reel")
    || (brandId !== undefined && (typeof brandId !== "string" || brandId.length !== 36 || !brandIdPattern.test(brandId)))) {
    throw new HttpError(422, "ORDEN_INVALIDA");
  }
  const fingerprint = await sha256Hex(JSON.stringify({ kind: "production", mode: env.BILLING_MODE,
    ...(resolution === "4k" ? { output_resolution: resolution } : {}),
    ...(format === "prores4444" ? { output_format: format } : {}),
    ...(edit === "reel" ? { output_edit: edit } : {}),
    ...(brandId ? { brand_id: brandId } : {}) }));
  const recent = () => env.DB.withSession("first-primary").prepare(`SELECT ${orderColumns},o.request_sha256,${brandColumns}
    FROM orders o ${brandJoin} WHERE o.user_id=? AND o.idempotency_key=?`).bind(userId, key).first<Order>();
  const existing = await recent();
  const replay = (row: Order) => {
    if (row.kind !== "production" || row.request_sha256 !== fingerprint) throw new HttpError(409, "IDEMPOTENCIA_EN_CONFLICTO");
    if (row.status === "cancelled") throw new HttpError(410, "ORDEN_CANCELADA");
    return response(publicOrder(row));
  };
  if (existing) return replay(existing);
  const time = Math.floor(Date.now() / 1000);
  const plan = await activeQuota(env, userId, time);
  if (resolution === "4k" && plan.max_resolution !== "4k") throw new HttpError(422, "RESOLUCION_FUERA_DEL_PLAN");
  if (format === "prores4444" && plan.allow_prores4444 !== 1) throw new HttpError(422, "FORMATO_FUERA_DEL_PLAN");
  if (brandId && plan.plan_id !== "ilimitado") throw new HttpError(403, "MARCAS_FUERA_DEL_PLAN");
  if (!productionAvailable(env, userId, plan.plan_id)) throw new HttpError(403, "PRODUCCION_NO_HABILITADA");
  const brand = brandId ? await findBrand(env, userId, brandId) : null;
  if (brandId && !brand) throw new HttpError(404, "MARCA_NO_ENCONTRADA");
  const id = crypto.randomUUID();
  const slug = "job-" + id.replaceAll("-", "");
  try {
    await env.DB.batch([
      env.DB.prepare(`INSERT INTO orders(id,user_id,plan_id,kind,status,idempotency_key,request_sha256,
        project_slug,queue_priority,provider_mode,created_at,updated_at,portal_started_at,output_resolution,output_format,output_edit)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`)
        .bind(id, userId, plan.plan_id, "production", "queued", key, fingerprint, slug, plan.queue_priority,
          env.BILLING_MODE, time, time, time, resolution, format, edit),
      ...(brand ? [env.DB.prepare(`INSERT INTO order_brand_profiles(
        order_id,user_id,provider_mode,brand_id,display_name,audience,tone) VALUES(?,?,?,?,?,?,?)`)
        .bind(id, userId, env.BILLING_MODE, brand.id, brand.display_name, brand.audience, brand.tone)] : []),
      env.DB.prepare(`INSERT INTO usage_ledger(id,user_id,order_id,period_start,units,status,created_at,updated_at)
        VALUES(?,?,?,?,1,'reserved',?,?)`).bind(crypto.randomUUID(), userId, id, month(time), time, time),
    ]);
  } catch (error) {
    const concurrent = await recent();
    if (concurrent) return replay(concurrent);
    const reason = error instanceof Error ? error.message : "";
    if (reason.includes("QUOTA_EXCEEDED")) throw new HttpError(409, "CUOTA_AGOTADA");
    if (reason.includes("QUOTA_PLAN_MISMATCH")) throw new HttpError(409, "PLAN_CAMBIO_REINTENTAR");
    throw new HttpError(503, "ORDEN_NO_DISPONIBLE");
  }
  const reserved: Order = {
    id, kind: "production", plan_id: plan.plan_id, project_slug: slug, status: "queued",
    queue_priority: plan.queue_priority, request_sha256: fingerprint, created_at: time, updated_at: time,
    output_resolution: resolution,
    output_format: format,
    output_edit: edit,
    brand_id: brand?.id ?? null,
    brand_name: brand?.display_name ?? null,
  };
  return response(publicOrder(reserved), 201);
}
