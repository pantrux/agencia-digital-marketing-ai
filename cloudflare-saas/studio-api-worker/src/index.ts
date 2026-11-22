import { finishGoogle, identity, logout, sessionLookup, startGoogle, type AuthEnv, type PortalIdentity } from "./identity.js";
import { handleBilling, retryWebhookEvents, type BillingEnv } from "./billing.js";
import { brandRoute, handleBrands } from "./brands.js";
import { handleSofiaChat, type ChatEnv } from "./chat.js";
import { readConversation } from "./conversation.js";
import { deliveryRevisionRoute, handleDeliveryRevisions, type DeliveryRevisionEnv } from "./delivery-revisions.js";
import { expireAbandonedReservations, handleIngest, handlePartAccess, handleUploadCreation, initialPartsHeader, partAccessTarget, uploadsConfigured, type IngestEnv, type MultipartObserver } from "./ingest.js";
import { dispatchOutbox, dispatchSourceOutbox, syncQueue, type JobEnv, type JobMessage } from "./jobs.js";
import { handleKits, kitView, prepareKits, type KitRow } from "./kits.js";
import { handleKitProducer, kitProducerPath, type KitProducerEnv } from "./kit-producer.js";
import { handleManualAdjustment, manualAdjustmentRoute } from "./manual-adjustments.js";
import {
  activeQuota, describePlan, listOrders, planView, prepareOrders, preparePlan, publicOrder, reserveOrder,
  type ListedOrder, type OrderEnv, type PlanWithUsage,
} from "./orders.js";
import { handleProductionDispatch, productionDispatchPath, type ProductionDispatchEnv } from "./production-dispatch.js";
import { handleProductionBrief, productionBriefRoute } from "./production-brief.js";
import { handleSourceSet, sourceSetRoute } from "./production-sources.js";
import { handleReference, referenceRoute, type ReferenceEnv } from "./references.js";
import { completeSourceUpload, createSourceUpload, handleSourcePartAccess } from "./source-upload.js";
import { response, HttpError, readJson, type ExecutionTasks } from "./platform.js";

interface AnalyticsDataset {
  writeDataPoint(point: { indexes: string[]; blobs: string[]; doubles: number[] }): void;
}

interface HttpMetricsEnv {
  STUDIO_HTTP_METRICS?: AnalyticsDataset;
  STUDIO_BROWSER_METRICS?: AnalyticsDataset;
}

type WorkerEnv = AuthEnv & IngestEnv & OrderEnv & JobEnv & BillingEnv & ChatEnv & KitProducerEnv & ProductionDispatchEnv & HttpMetricsEnv & ReferenceEnv & DeliveryRevisionEnv;

const measuredRoutes = new Map([
  ["GET /api/auth/google", "auth_inicio"],
  ["GET /api/auth/google/callback", "auth_callback"],
  ["GET /api/auth/me", "auth_me"],
  ["POST /api/auth/logout", "auth_logout"],
  ["POST /api/uploads", "multipart_inicio"],
]);

const browserRoutes = new Set(["auth_me", "panel", "multipart_inicio", "multipart_firma"]);
const sourceUploadRoute = /^\/api\/orders\/([a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12})\/sources\/([a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12})\/(upload|complete|parts(?:\/([1-9][0-9]?)\/sign)?)$/u;
type BrowserSample = [string, number, number, boolean];

function observeMultipart(env: WorkerEnv, route: "multipart_inicio_etapas" | "multifuente_inicio_etapas"): MultipartObserver | undefined {
  const metrics = env.STUDIO_HTTP_METRICS;
  if (!metrics) return undefined;
  return (preparation, storage, confirmation, fromTestSession = false) => {
    const cohort = env.PUBLIC_ORIGIN === "https://studio.e-scale.cl" && !fromTestSession
      ? (env.BILLING_MODE === "production" ? "produccion" : "piloto") : "ensayo";
    try {
      metrics.writeDataPoint({ indexes: [route], blobs: [cohort],
        doubles: [preparation + storage + confirmation, 200, preparation, storage, confirmation] });
    } catch {
      // Una pérdida de telemetría no modifica la carga confirmada.
    }
  };
}

function isBrowserSample(value: unknown): value is BrowserSample {
  if (!Array.isArray(value) || value.length !== 4) return false;
  const [route, status, milliseconds, reused] = value;
  return typeof route === "string" && browserRoutes.has(route)
    && Number.isInteger(status) && status >= 200 && status <= 299
    && Number.isInteger(milliseconds) && milliseconds >= 1 && milliseconds <= 120_000
    && typeof reused === "boolean";
}

async function recordBrowserTimings(request: Request, env: WorkerEnv, testAccount: boolean): Promise<Response> {
  const metrics = env.STUDIO_BROWSER_METRICS;
  if (!metrics) throw new HttpError(404, "RUTA_NO_DISPONIBLE");
  const { samples, ...extra } = await readJson(request, 1024);
  if (Object.keys(extra).length || !Array.isArray(samples) || samples.length === 0 || samples.length > 12
    || !samples.every(isBrowserSample)) throw new HttpError(422, "MUESTRAS_INVALIDAS");
  const host = env.PUBLIC_ORIGIN === "https://studio.e-scale.cl" && !testAccount ? "produccion" : "ensayo";
  try {
    for (const [route, status, milliseconds, reused] of samples) {
      metrics.writeDataPoint({ indexes: [route], blobs: [host, reused ? "reutilizada" : "nueva"], doubles: [milliseconds, status] });
    }
  } catch {
    // Una pérdida de telemetría no afecta al flujo del portal.
  }
  return new Response(null, { status: 204, headers: { "Cache-Control": "no-store" } });
}

async function health(env: WorkerEnv): Promise<Response> {
  const [db, media, kv] = await Promise.allSettled([
    env.DB.withSession("first-primary").prepare("SELECT COUNT(*) AS count FROM plan_quotas").first<{ count: number }>(),
    env.MEDIA.head("health/studio-api-worker"),
    env.SESSIONS.get("health:studio-api-worker"),
  ]);
  const d1 = db.status === "fulfilled" && db.value?.count === 4;
  const r2 = media.status === "fulfilled";
  const sessions = kv.status === "fulfilled";
  const ready = d1 && r2 && sessions;
  return response({ ready, d1, r2, kv: sessions }, ready ? 200 : 503);
}

function validateOrigin(request: Request, url: URL, origin: string): void {
  if (url.origin !== origin) throw new HttpError(404, "RUTA_NO_DISPONIBLE");
  if (request.method !== "GET" && request.method !== "HEAD" && request.headers.get("Origin") !== origin) {
    throw new HttpError(403, "ORIGEN_NO_AUTORIZADO");
  }
}

async function dashboard(request: Request, env: WorkerEnv): Promise<Response> {
  const owner = await sessionLookup(request, env);
  const now = Math.floor(Date.now() / 1000);
  const [people, plans, orders, kits] = await env.DB.batch([
    owner.statement(env.DB), preparePlan(env.DB, owner, env, now),
    prepareOrders(env.DB, owner, env), prepareKits(env.DB, owner, env, now),
  ]);
  const person = people?.results[0] as PortalIdentity | undefined;
  if (!person) throw new HttpError(401, "AUTENTICACION_REQUERIDA");
  return response({
    person, plan: planView(plans?.results[0] as PlanWithUsage | undefined, person.id, env, now),
    orders: (orders!.results as ListedOrder[]).map(publicOrder),
    kits: (kits!.results as KitRow[]).map(kitView),
  });
}

export async function route(request: Request, env: WorkerEnv, ctx: ExecutionTasks): Promise<Response> {
  const url = new URL(request.url);
  try {
    const path = url.pathname;
    if (url.origin === env.PUBLIC_ORIGIN && request.method === "POST" && path === "/api/billing/webhook") {
      return await handleBilling(request, env, path);
    }
    if (url.origin === env.PUBLIC_ORIGIN && path === kitProducerPath && !url.search) return await handleKitProducer(request, env);
    if (url.origin === env.PUBLIC_ORIGIN && path === productionDispatchPath && !url.search) return await handleProductionDispatch(request, env);
    validateOrigin(request, url, env.PUBLIC_ORIGIN);
    if (request.method === "GET") {
      if (path === "/api/healthz") return await health(env);
      if (path === "/api/auth/google") return await startGoogle(env);
      if (path === "/api/auth/google/callback") return await finishGoogle(request, env, ctx);
      if (path === "/api/dashboard") return await dashboard(request, env);
    }
    if (request.method === "POST" && path === "/api/auth/logout") return await logout(request, env, ctx);
    const parts = partAccessTarget(path, request.method);
    if (parts) return await handlePartAccess(request, env, parts);
    const onUploadCommitted = (id: string) => {
      ctx.waitUntil(dispatchOutbox(env, id).catch(() => {
        // El cron recupera una orden si falla el despacho tras confirmar la carga.
      }));
    };
    if (request.method === "POST" && path === "/api/uploads") {
      return await handleUploadCreation(request, env, onUploadCommitted, observeMultipart(env, "multipart_inicio_etapas"));
    }
    const sourceUpload = !url.search ? sourceUploadRoute.exec(path) : null;
    if (request.method === "POST" && sourceUpload?.[3] === "upload") {
      const owner = await sessionLookup(request, env);
      const result = await createSourceUpload(env, owner, sourceUpload[1]!, sourceUpload[2]!,
        observeMultipart(env, "multifuente_inicio_etapas"), request.headers.get(initialPartsHeader) === "1");
      ctx.waitUntil(dispatchSourceOutbox(env).catch(() => {}));
      return result;
    }
    const signingSourcePart = request.method === "POST" && sourceUpload?.[4];
    if (sourceUpload && (signingSourcePart || (request.method === "GET" && sourceUpload[3] === "parts"))
      && uploadsConfigured(env)) {
      const owner = await sessionLookup(request, env);
      return await handleSourcePartAccess(env, owner, sourceUpload[1]!, sourceUpload[2]!,
        signingSourcePart ? Number(signingSourcePart) : undefined);
    }
    const user = await identity(request, env);
    if (path === "/api/metrics/ttfb" && request.method === "POST") {
      return await recordBrowserTimings(request, env, user.email.toLowerCase().endsWith(".invalid"));
    }
    if (request.method === "GET") {
      if (path === "/api/auth/me") return response(user);
      if (path === "/api/plan") return await describePlan(user.id, env);
      if (path === "/api/orders") return await listOrders(user.id, env);
    }
    if (path === "/api/kits" || path.startsWith("/api/kits/")) return await handleKits(request, env, user.id, path);
    const brand = brandRoute.exec(path);
    if (brand && !url.search) {
      const plan = await activeQuota(env, user.id, Math.floor(Date.now() / 1000));
      return await handleBrands(request, env, user.id, plan.plan_id, brand[1]);
    }
    const brief = productionBriefRoute.exec(path);
    if (brief && !url.search) return await handleProductionBrief(request, env, user.id, brief[1]!);
    const reference = referenceRoute.exec(path);
    if (reference && !url.search) return await handleReference(request, env, user.id, reference[1]!);
    const deliveryRevision = deliveryRevisionRoute.exec(path);
    if (deliveryRevision && !url.search) return await handleDeliveryRevisions(request, env, user.id, deliveryRevision[1]!);
    const sourceSet = sourceSetRoute.exec(path);
    if (sourceSet && !url.search) return await handleSourceSet(request, env, user.id, sourceSet[1]!);
    if (sourceUpload) {
      if (!uploadsConfigured(env)) throw new HttpError(503, "SUBIDA_NO_DISPONIBLE");
      const [, orderId, sourceId, action] = sourceUpload;
      if (request.method === "POST" && action === "complete") {
        const result = await completeSourceUpload(request, env, user.id, orderId!, sourceId!);
        ctx.waitUntil(dispatchSourceOutbox(env).catch(() => {}));
        return result;
      }
      throw new HttpError(405, "METODO_NO_PERMITIDO");
    }
    const adjustment = manualAdjustmentRoute.exec(path);
    if (adjustment && !url.search) return await handleManualAdjustment(request, env, user.id, adjustment[1]!);
    if (request.method === "POST" && path === "/api/chat") return await handleSofiaChat(request, env, user.id);
    if (request.method === "GET" && path === "/api/chat/history") return await readConversation(env, user.id, url.searchParams.get("order_id") ?? "");
    if (request.method === "POST" && path === "/api/orders") {
      if (!uploadsConfigured(env)) throw new HttpError(503, "SUBIDA_NO_DISPONIBLE");
      return await reserveOrder(request, user.id, env);
    }
    if (path === "/api/uploads" || path.startsWith("/api/uploads/")) {
      return await handleIngest(request, env, user.id, path, onUploadCommitted);
    }
    if (path.startsWith("/api/billing/")) return await handleBilling(request, env, path, user);
    throw new HttpError(404, "RUTA_NO_DISPONIBLE");
  } catch (error) {
    if (error instanceof HttpError) return response({ error: error.code }, error.status);
    return response({ error: "SERVICIO_NO_DISPONIBLE" }, 503);
  }
}

export default {
  async fetch(request: Request, env: WorkerEnv, ctx: ExecutionTasks): Promise<Response> {
    const metrics = env.STUDIO_HTTP_METRICS;
    if (!metrics) return route(request, env, ctx);
    const url = new URL(request.url);
    const sourceOpening = request.method === "POST" && !url.search && sourceUploadRoute.exec(url.pathname)?.[3] === "upload";
    const label = measuredRoutes.get(`${request.method} ${url.pathname}`) ?? (sourceOpening ? "multifuente_inicio" : null)
      ?? (request.method === "POST" && partAccessTarget(url.pathname, request.method) ? "multipart_firma" : null);
    if (!label || url.origin !== env.PUBLIC_ORIGIN) return route(request, env, ctx);
    const start = performance.now();
    const reply = await route(request, env, ctx);
    try {
      metrics.writeDataPoint({
        indexes: [label],
        blobs: [url.origin === "https://studio.e-scale.cl" ? "produccion" : "ensayo"],
        doubles: [Math.max(0, performance.now() - start), reply.status],
      });
    } catch {
      // La telemetría no condiciona la respuesta del portal.
    }
    return reply;
  },
  scheduled(_event: object, env: WorkerEnv, ctx: ExecutionTasks) {
    ctx.waitUntil(dispatchOutbox(env));
    ctx.waitUntil(dispatchSourceOutbox(env));
    ctx.waitUntil(expireAbandonedReservations(env));
    ctx.waitUntil(retryWebhookEvents(env));
  },
  async queue(batch: { messages: JobMessage[] }, env: WorkerEnv) {
    await syncQueue(batch.messages, env);
  },
};
