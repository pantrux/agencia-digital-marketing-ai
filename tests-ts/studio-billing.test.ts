import { createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { handleBilling, retryWebhookEvents, type BillingEnv } from "../cloudflare-saas/studio-api-worker/src/billing.js";
import type { SqlDatabase, SqlRows, SqlStatement } from "../cloudflare-saas/studio-api-worker/src/platform.js";

const SESSION = { id: "fixture", email: "private-session@example.test", display_name: null };
const MERCHANT = "100200300";
const APP = "123456789";
const ORIGIN = "https://studio.e-scale.cl";
const SECRET = "opaque-fixture-webhook";
const SUB = "sandbox-subscription";
const PAYMENT = "10000000001";
const INVOICE = "10000000002";
const WHEN = new Date("2026-11-14T13:00:00.000Z");
const ANCHOR = "2026-11-14T12:00:00.000Z";
const NEXT = "2026-12-14T12:00:00.000Z";
let database: DatabaseSync;
let env: BillingEnv;

class Statement implements SqlStatement {
  values: (string | number | null)[] = [];
  constructor(readonly sql: string) {}
  bind(...values: (string | number | null)[]): SqlStatement { this.values = values; return this; }
  first<T>(): Promise<T | null> {
    return Promise.resolve(database.prepare(this.sql).get(...this.values) as T | undefined ?? null);
  }
  all<T>(): Promise<SqlRows<T>> {
    return Promise.resolve({ results: database.prepare(this.sql).all(...this.values) as T[], meta: {} });
  }
  run(): Promise<SqlRows<never>> {
    const changes = Number(database.prepare(this.sql).run(...this.values).changes);
    return Promise.resolve({ results: [], meta: { changes } });
  }
}

function binding(): SqlDatabase {
  const db: SqlDatabase = {
    prepare: (sql) => new Statement(sql),
    withSession: () => db,
    async batch<T>(commands: SqlStatement[]): Promise<SqlRows<T>[]> {
      database.exec("BEGIN");
      try {
        const result: SqlRows<T>[] = [];
        for (const command of commands) result.push(await command.run());
        database.exec("COMMIT");
        return result;
      } catch (error) {
        database.exec("ROLLBACK");
        throw error;
      }
    },
  };
  return db;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(WHEN);
  database = new DatabaseSync(":memory:");
  database.exec("PRAGMA foreign_keys=ON");
  for (const name of ["0001_schema.sql", "0002_billing.sql", "0013_billing_webhook_retry.sql"]) {
    database.exec(readFileSync(new URL("../cloudflare-saas/migrations/" + name, import.meta.url), "utf8"));
  }
  database.prepare("INSERT INTO users(id,google_sub,email) VALUES (?,?,?)").run(SESSION.id, "sub", SESSION.email);
  env = {
    DB: binding(), PUBLIC_ORIGIN: ORIGIN, BILLING_MODE: "sandbox", MP_ENVIRONMENT: "sandbox",
    SOFIA_OWNER_USER_ID: SESSION.id,
    MP_SITE_ID: "MLC", MP_MERCHANT_ID: MERCHANT, MP_ACCESS_TOKEN: "opaque-fixture",
    MP_SANDBOX_CHECKOUT_ENABLED: "true",
    MP_APPLICATION_ID: APP, MP_WEBHOOK_SECRET: SECRET, MP_SANDBOX_PAYER_EMAIL: "buyer@example.test",
    MP_SANDBOX_PLAN_CATALOG: JSON.stringify(Object.fromEntries(
      ["pro", "ultra", "ilimitado"].map((plan, index) => [plan, {
        amount_clp: (index + 1) * 5000, frequency: 1, frequency_type: "months",
      }]),
    )),
  };
});

afterEach(() => {
  database.close();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function request(method: string, route: string, body?: object, key = "fixture-checkout"): Request {
  return new Request(ORIGIN + route, {
    method, headers: { "content-type": "application/json", "idempotency-key": key },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
}

const checkoutPath = "/api/billing/checkout";
const webhookPath = "/api/billing/webhook";
const reconcilePath = "/api/billing/reconcile";
function checkout(plan = "pro", key?: string) {
  return handleBilling(request("POST", checkoutPath, { plan_id: plan }, key), env, checkoutPath, SESSION);
}

function state() {
  return database.prepare(
    "SELECT subscriptions.*,billing_checkout.checkout_url,billing_checkout.payer_email " +
    "FROM subscriptions JOIN billing_checkout ON billing_checkout.subscription_id=subscriptions.id",
  ).get();
}

function emulate(options: {
  abort?: boolean;
  authorized?: boolean;
  cancelled?: boolean;
  paymentStatus?: string;
  paymentTime?: string;
  livePayment?: boolean;
  invoiceUnavailable?: boolean;
  invoiceStart?: string;
  providerId?: string;
  providerApplication?: string;
} = {}) {
  let reference: string;
  const upstream = vi.fn<typeof fetch>(async (url, init) => {
    const path = new URL(String(url)).pathname;
    if (path === "/users/me") return Response.json({ id: MERCHANT, site_id: "MLC", tags: ["test_user"] });
    const subscription = () => ({
      id: options.providerId ?? SUB, collector_id: MERCHANT,
      application_id: options.providerApplication ?? APP, external_reference: reference,
      status: options.cancelled ? "cancelled" : options.authorized ? "authorized" : "pending",
      version: options.cancelled ? 2 : Number(options.authorized ?? false),
      last_modified: ANCHOR, next_payment_date: NEXT,
      init_point: "https://www.mercadopago.cl/subscriptions/checkout?preapproval_id=" + (options.providerId ?? SUB),
      auto_recurring: { frequency: 1, frequency_type: "months", currency_id: "CLP", transaction_amount: 5000 },
    });
    if (path === "/preapproval") {
      expect(init?.method).toBe("POST");
      const body = JSON.parse(String(init?.body));
      expect(body.payer_email).toBe("buyer@example.test");
      reference = body.external_reference;
      expect(state()?.external_reference).toBe(reference);
      if (options.abort) throw new Error("unknown upstream POST outcome");
      return Response.json(subscription(), { status: 201 });
    }
    if (path === "/preapproval/" + (options.providerId ?? SUB)) return Response.json(subscription());
    if (path === "/preapproval/search") return Response.json({
      paging: { limit: 20, offset: 0, total: 1 }, results: [subscription()],
    });
    const invoice = {
      id: INVOICE, preapproval_id: SUB, external_reference: reference, status: "processed",
      last_modified: ANCHOR, debit_date: options.invoiceStart ?? ANCHOR, currency_id: "CLP", transaction_amount: 5000,
      payment: { id: PAYMENT, status: options.paymentStatus ?? "approved" },
    };
    if (path === "/authorized_payments/" + INVOICE) {
      return options.invoiceUnavailable ? new Response(null, { status: 503 }) : Response.json(invoice);
    }
    if (path === "/authorized_payments/search") return Response.json({
      paging: { limit: 20, offset: 0, total: 1 }, results: [invoice],
    });
    expect(path).toBe("/v1/payments/" + PAYMENT);
    return Response.json({
      id: PAYMENT, collector_id: MERCHANT, currency_id: "CLP", transaction_amount: 5000, external_reference: reference,
      status: options.paymentStatus ?? "approved", live_mode: options.livePayment ?? false,
      date_last_updated: options.paymentTime ?? ANCHOR,
    });
  });
  vi.stubGlobal("fetch", upstream);
  return upstream;
}

function message(topic: string, id: string, reqId = "fixture-message-1", application: string | null = APP): Request {
  const ts = Math.floor(Date.now() / 1000);
  const v1 = createHmac("sha256", SECRET).update("id:" + id + ";request-id:" + reqId + ";ts:" + ts + ";").digest("hex");
  return new Request(ORIGIN + webhookPath + "?type=" + topic + "&data.id=" + id, {
    method: "POST", headers: {
      "content-type": "application/json", "x-request-id": reqId, "x-signature": "ts=" + ts + ",v1=" + v1,
    },
    body: JSON.stringify({ type: topic, data: { id }, id: 123,
      application_id: application ?? undefined, user_id: MERCHANT, live_mode: false }),
  });
}

it("reserva checkout en D1 antes del POST y reutiliza la misma respuesta sin otra alta", async () => {
  const upstream = emulate();
  const first = await checkout();
  const row = await first.json();
  expect(first.status).toBe(201);
  expect(row).toMatchObject({ plan_id: "pro", status: "pending", checkout_url: expect.stringContaining(SUB) });
  expect(await (await checkout()).json()).toEqual(row);
  await expect(checkout("ultra")).rejects.toMatchObject({ status: 409 });
  await expect(checkout("pro", "different-key")).rejects.toMatchObject({ status: 409 });
  expect(upstream).toHaveBeenCalledTimes(2);
});

it("pausa las altas y oculta enlaces previos sin impedir consultar el intento", async () => {
  const upstream = emulate();
  const { checkout_id } = await (await checkout()).json();
  delete env.MP_SANDBOX_CHECKOUT_ENABLED;
  upstream.mockClear();
  await expect(checkout()).rejects.toMatchObject({ status: 503, code: "CHECKOUT_DE_PRUEBA_PAUSADO" });
  expect(upstream).not.toHaveBeenCalled();
  const status = await handleBilling(request("GET", checkoutPath), env, checkoutPath, SESSION);
  expect(await status.json()).toMatchObject({ checkout_id, checkout_enabled: false, checkout_url: null });
  const reconciled = await handleBilling(request("PUT", reconcilePath, { checkout_id }), env, reconcilePath, SESSION);
  expect(await reconciled.json()).toMatchObject({ checkout_id, checkout_enabled: false, checkout_url: null });
  expect(state()?.checkout_url).toEqual(expect.stringContaining(SUB));
});

it("reserva el checkout de ensayo al titular del piloto", async () => {
  const upstream = emulate();
  const other = { id: "other", email: "other@example.test", display_name: null };
  database.prepare("INSERT INTO users(id,google_sub,email) VALUES (?,?,?)").run(other.id, "other-sub", other.email);
  const post = request("POST", checkoutPath, { plan_id: "pro" });
  await expect(handleBilling(post, env, checkoutPath, other))
    .rejects.toMatchObject({ status: 503, code: "CHECKOUT_DE_PRUEBA_PAUSADO" });
  const availability = await handleBilling(request("GET", checkoutPath), env, checkoutPath, other);
  expect(await availability.json()).toEqual({ checkout: null, checkout_enabled: false });
  expect(upstream).not.toHaveBeenCalled();
  expect(database.prepare("SELECT count(*) AS n FROM subscriptions").get()?.n).toBe(0);
});

it("devuelve un intento pendiente tras timeout, y PUT sólo lee para recuperar el mismo mandato", async () => {
  const upstream = emulate({ abort: true });
  const ambiguous = await checkout();
  const row = await ambiguous.json();
  expect(ambiguous.status).toBe(202);
  expect((await checkout()).status).toBe(202);
  const recovered = await handleBilling(request("PUT", reconcilePath, { checkout_id: row.checkout_id }),
    env, reconcilePath, SESSION);
  expect((await recovered.json()).checkout_url).toContain(SUB);
  expect(upstream.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it("conserva la notificación pendiente cuando otra conciliación enlaza un mandato distinto", async () => {
  emulate({ abort: true });
  const attempt = await checkout();
  const { checkout_id } = await attempt.json();
  const batch = env.DB.batch.bind(env.DB);
  env.DB.batch = (commands) => {
    database.prepare("UPDATE subscriptions SET provider_subscription_id=?, updated_at=? WHERE id=?")
      .run("other-sandbox-subscription", 1122, checkout_id);
    return batch(commands);
  };

  await expect(handleBilling(message("subscription_preapproval", SUB), env, webhookPath))
    .rejects.toMatchObject({ status: 502, code: "SUSCRIPCION_NO_COINCIDE" });

  expect(database.prepare("SELECT processed_at FROM webhook_events").get()).toEqual({ processed_at: null });
  expect(state()).toMatchObject({ provider_subscription_id: "other-sandbox-subscription", updated_at: 1122 });
});

it("un mandato autorizado mantiene cuota sin pago y sólo una factura+pago cotejados activan el plan", async () => {
  const scenario = { authorized: false };
  const upstream = emulate(scenario);
  await checkout();
  scenario.authorized = true;
  const activated = await handleBilling(message("subscription_preapproval", SUB), env, webhookPath);
  expect(activated.status).toBe(200);
  expect(state()).toMatchObject({ status: "authorized", billing_status: "pending", current_period_end: null });
  const event = () => message("subscription_authorized_payment", INVOICE, "fixture-invoice");
  expect((await handleBilling(event(), env, webhookPath)).status).toBe(200);
  expect(state()).toMatchObject({
    status: "active", billing_status: "paid", current_period_start: Date.parse(ANCHOR) / 1000,
    current_period_end: Date.parse(NEXT) / 1000,
  });
  const calls = upstream.mock.calls.length;
  await handleBilling(event(), env, webhookPath);
  expect(upstream.mock.calls).toHaveLength(calls);
  expect(database.prepare("SELECT count(*) AS n FROM billing_payments").get()?.n).toBe(1);
});

it("una factura aprobada con pago real no concede derechos en el entorno de prueba", async () => {
  const scenario = { authorized: false, livePayment: true };
  emulate(scenario);
  const { checkout_id } = await (await checkout()).json();
  scenario.authorized = true;
  await expect(handleBilling(request("PUT", reconcilePath, { checkout_id }), env, reconcilePath, SESSION))
    .rejects.toMatchObject({ status: 503 });
  await expect(handleBilling(message("subscription_authorized_payment", INVOICE), env, webhookPath))
    .rejects.toMatchObject({ status: 503 });
  expect(state()).toMatchObject({ status: "pending", billing_status: "pending", current_period_end: null });
  expect(database.prepare("SELECT count(*) AS n FROM billing_payments").get()?.n).toBe(0);
});

it("conciliar un intento sin cobertura registra la cancelación aunque su factura esté fuera del entorno de prueba", async () => {
  const scenario = { cancelled: false, livePayment: true };
  const upstream = emulate(scenario);
  const { checkout_id } = await (await checkout()).json();
  env.MP_SANDBOX_CHECKOUT_ENABLED = "false";
  scenario.cancelled = true;
  for (let attempt = 0; attempt < 2; attempt++) {
    const result = await handleBilling(request("PUT", reconcilePath, { checkout_id }), env, reconcilePath, SESSION);
    expect(await result.json()).toMatchObject({
      status: "cancelled", billing_status: "pending", checkout_enabled: false, checkout_url: null,
    });
  }
  expect(state()).toMatchObject({ status: "cancelled", current_period_end: null });
  expect(database.prepare("SELECT count(*) AS n FROM billing_payments").get()?.n).toBe(0);
  expect(upstream.mock.calls.some(([url]) => String(url).includes("/v1/payments/"))).toBe(false);
});

it("rechaza webhook de otra aplicación y catálogo ausente sin red ni escrituras", async () => {
  const upstream = emulate();
  await expect(handleBilling(message("subscription_preapproval", SUB, "fixture", "333"), env, webhookPath))
    .rejects.toMatchObject({ status: 401 });
  env.MP_SANDBOX_PLAN_CATALOG = "";
  await expect(checkout()).rejects.toMatchObject({ status: 503 });
  expect(upstream).not.toHaveBeenCalled();
  expect(state()).toBeUndefined();
});

it("un pago reembolsado invalida cobertura, y un evento antiguo no la reactiva", async () => {
  const scenario = { authorized: false, paymentStatus: "approved", paymentTime: ANCHOR };
  emulate(scenario);
  await checkout();
  scenario.authorized = true;
  const event = (requestId: string) => handleBilling(
    message("subscription_authorized_payment", INVOICE, requestId), env, webhookPath,
  );
  await event("paid");
  expect(state()?.status).toBe("active");
  scenario.paymentStatus = "refunded";
  scenario.paymentTime = WHEN.toISOString();
  await event("refunded");
  expect(state()).toMatchObject({ status: "authorized", billing_status: "refunded", current_period_end: null });
  scenario.paymentStatus = "approved";
  scenario.paymentTime = ANCHOR;
  await event("old-payment");
  expect(state()).toMatchObject({ status: "authorized", billing_status: "refunded" });
});

it("una factura de renovación futura no revoca la cobertura vigente ni la concede anticipadamente", async () => {
  const scenario = { authorized: false, invoiceStart: NEXT };
  emulate(scenario);
  await checkout();
  database.prepare("INSERT INTO billing_payments VALUES (?,?,?,?,?,?,?,?)").run(
    "previous-payment", "previous-invoice", state()?.id ?? "", "paid", 5000, Date.parse(ANCHOR) / 1000,
    Date.parse(NEXT) / 1000, Date.parse(ANCHOR) / 1000,
  );
  scenario.authorized = true;
  await handleBilling(message("subscription_authorized_payment", INVOICE, "future"), env, webhookPath);
  expect(state()).toMatchObject({
    status: "active", current_period_start: Date.parse(ANCHOR) / 1000, current_period_end: Date.parse(NEXT) / 1000,
  });
});

it("rechaza registrar derechos si la referencia local estaba ligada a otro ID proveedor", async () => {
  const scenario = { authorized: false, providerId: SUB };
  emulate(scenario);
  await checkout();
  scenario.providerId = "otro-id";
  await expect(handleBilling(message("subscription_preapproval", scenario.providerId), env, webhookPath))
    .rejects.toMatchObject({ status: 502 });
  expect(state()).toMatchObject({ provider_subscription_id: SUB, status: "pending" });
});

it("coteja la aplicación de prueba del proveedor y admite el ID del panel en un webhook firmado", async () => {
  const apiApplication = "123456798";
  env.MP_SANDBOX_API_APPLICATION_ID = apiApplication;
  emulate({ providerApplication: apiApplication });
  expect((await checkout()).status).toBe(201);
  expect((await handleBilling(message("subscription_preapproval", SUB, "panel", APP), env, webhookPath)).status).toBe(200);
  expect((await handleBilling(message("subscription_preapproval", SUB, "sandbox", apiApplication), env, webhookPath)).status).toBe(200);
  expect(state()).toMatchObject({ provider_subscription_id: SUB, status: "pending" });
});

it("procesa el webhook de pago firmado sin application_id y coteja la aplicación del mandato", async () => {
  const apiApplication = "123456798";
  const scenario = { authorized: false, providerApplication: apiApplication };
  env.MP_SANDBOX_API_APPLICATION_ID = apiApplication;
  const upstream = emulate(scenario);
  await checkout();
  scenario.authorized = true;
  const event = () => message("payment", PAYMENT, "pago-sin-aplicacion", null);
  expect((await handleBilling(event(), env, webhookPath)).status).toBe(200);
  expect(upstream.mock.calls.some(([url]) => new URL(String(url)).pathname === "/preapproval/" + SUB)).toBe(true);
  expect((await handleBilling(event(), env, webhookPath)).status).toBe(200);
  expect(state()).toMatchObject({ status: "active", billing_status: "paid" });
  expect(database.prepare("SELECT count(*) AS n FROM billing_payments").get()?.n).toBe(1);
});

it("un webhook sin application_id no concede cobertura si el mandato consultado pertenece a otra aplicación", async () => {
  const scenario = { authorized: false, providerApplication: APP };
  emulate(scenario);
  await checkout();
  scenario.authorized = true;
  scenario.providerApplication = "333";
  await expect(handleBilling(message("payment", PAYMENT, "pago-otra-aplicacion", null), env, webhookPath))
    .rejects.toMatchObject({ status: 503 });
  expect(state()).toMatchObject({ status: "pending", billing_status: "pending", current_period_end: null });
  expect(database.prepare("SELECT count(*) AS n FROM billing_payments").get()?.n).toBe(0);
});

it("recupera en D1 una notificación autenticada tras un fallo del proveedor y reclama un solo intento", async () => {
  const scenario = { authorized: false, invoiceUnavailable: true };
  const upstream = emulate(scenario);
  await checkout();
  scenario.authorized = true;
  await expect(handleBilling(message("subscription_authorized_payment", INVOICE), env, webhookPath))
    .rejects.toMatchObject({ status: 503 });
  const events = database.prepare("SELECT processed_at FROM webhook_events");
  expect(events.get()).toMatchObject({ processed_at: null });

  upstream.mockClear();
  scenario.invoiceUnavailable = false;
  await retryWebhookEvents(env);
  expect(upstream).not.toHaveBeenCalled();

  vi.setSystemTime(WHEN.getTime() + 120_000);
  await Promise.all([retryWebhookEvents(env), retryWebhookEvents(env)]);
  expect(upstream.mock.calls.filter(([url]) => new URL(String(url)).pathname === "/authorized_payments/" + INVOICE))
    .toHaveLength(1);
  expect(events.get()?.processed_at).toBe(Math.floor(Date.now() / 1000));
  expect(state()).toMatchObject({ status: "active", billing_status: "paid" });
  expect(database.prepare("SELECT count(*) AS n FROM billing_payments").get()?.n).toBe(1);
});

it("reintenta notificaciones anteriores con espera creciente y conserva las agotadas para revisión", async () => {
  const upstream = emulate({ invoiceUnavailable: true });
  const insert = database.prepare(`INSERT INTO webhook_events(provider_mode,event_id,resource_id,topic,received_at)
    VALUES(?,?,?,?,?)`);
  const time = Math.floor(WHEN.getTime() / 1000);
  insert.run("sandbox", "anterior", INVOICE, "subscription_authorized_payment", time - 120);
  insert.run("production", "real", INVOICE, "subscription_authorized_payment", time - 120);

  const attempts = database.prepare("SELECT retry_attempts, retry_at, processed_at FROM webhook_events WHERE event_id=?");
  for (let attempt = 1; attempt <= 8; attempt++) {
    await retryWebhookEvents(env);
    const row = attempts.get("anterior");
    expect(row).toMatchObject({ retry_attempts: attempt, processed_at: null });
    expect(row?.retry_at).toBe(Math.floor(Date.now() / 1000) + Math.min(3600, 120 * 2 ** (attempt - 1)));
    await retryWebhookEvents(env);
    expect(upstream).toHaveBeenCalledTimes(attempt);
    vi.setSystemTime(Number(row?.retry_at) * 1000);
  }
  await retryWebhookEvents(env);
  expect(upstream).toHaveBeenCalledTimes(8);
  expect(attempts.get("real")).toMatchObject({ retry_attempts: 0, retry_at: null, processed_at: null });
});
