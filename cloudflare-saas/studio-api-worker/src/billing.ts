import { sha256Hex, type PortalIdentity } from "./identity.js";
import {
  MercadoPagoClient, MercadoPagoError, verifyMercadoPagoWebhook,
  type MercadoPagoInvoice, type MercadoPagoPayment, type MercadoPagoSubscription, type MercadoPagoWebhookEvent,
} from "./mercadopago.js";
import { HttpError, readJson, response, type SqlDatabase, type SqlStatement } from "./platform.js";

type PaidPlan = "pro" | "ultra" | "ilimitado";
type PaymentState = "pending" | "paid" | "failed" | "refunded" | "charged_back";
type Terms = Pick<Checkout, "amount_clp" | "frequency" | "frequency_type">;
type SqlValue = string | number | null;

export interface BillingEnv {
  DB: SqlDatabase;
  PUBLIC_ORIGIN: string;
  BILLING_MODE: "sandbox" | "production";
  MP_ENVIRONMENT: string;
  MP_ACCESS_TOKEN: string;
  MP_SITE_ID: string;
  MP_MERCHANT_ID: string;
  MP_SANDBOX_PLAN_CATALOG: string;
  MP_APPLICATION_ID?: string;
  MP_SANDBOX_API_APPLICATION_ID?: string;
  MP_WEBHOOK_SECRET?: string;
  MP_SANDBOX_PAYER_EMAIL?: string;
  MP_SANDBOX_CHECKOUT_ENABLED?: string;
  SOFIA_OWNER_USER_ID?: string;
}

interface Checkout {
  subscription_id: string;
  user_id: string;
  plan_id: PaidPlan;
  external_reference: string;
  provider_subscription_id: string | null;
  status: string;
  billing_status: PaymentState;
  current_period_start: number | null;
  current_period_end: number | null;
  provider_mandate: MercadoPagoSubscription["status"];
  provider_updated_at: number | null;
  provider_version: number | null;
  idempotency_key: string;
  request_sha256: string;
  payer_email: string;
  amount_clp: number;
  frequency: number;
  frequency_type: "months" | "days";
  checkout_url: string | null;
}

interface PaymentFact {
  id: string;
  invoiceId: string;
  state: PaymentState;
  amount: number;
  start: number;
  end: number;
  updatedAt: number;
}

interface WebhookResource {
  event_id: string;
  resource_id: string;
  topic: MercadoPagoWebhookEvent["topic"];
}

const plans = ["pro", "ultra", "ilimitado"] as const;
const MODE = "sandbox";
const merchantId = /^[1-9][0-9]{0,23}$/;
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const checkoutSelect = `SELECT b.subscription_id, b.user_id, s.plan_id, s.external_reference,
  s.provider_subscription_id, s.status, s.billing_status, s.current_period_start, s.current_period_end,
  b.provider_mandate, b.provider_updated_at, b.provider_version, b.idempotency_key, b.request_sha256, b.payer_email,
  b.amount_clp, b.frequency, b.frequency_type, b.checkout_url
  FROM billing_checkout b JOIN subscriptions s ON s.id=b.subscription_id AND s.user_id=b.user_id
  WHERE s.provider='mercadopago' AND s.provider_mode=?`;
const INSERT_SUB = `INSERT INTO subscriptions(id,user_id,plan_id,provider_mode,external_reference,created_at,updated_at)
  VALUES(?,?,?,?,?,?,?)`;
const INSERT_CHECKOUT = `INSERT INTO billing_checkout(subscription_id,user_id,idempotency_key,request_sha256,
  payer_email,amount_clp,frequency,frequency_type,created_at) VALUES(?,?,?,?,?,?,?,?,?)`;
const SAVE_MANDATE = `UPDATE billing_checkout SET
  checkout_url=COALESCE(?,checkout_url), provider_version=?, provider_mandate=?, provider_updated_at=?,
  closed_at=CASE WHEN ?='canceled' THEN COALESCE(closed_at,?) ELSE closed_at END
  WHERE subscription_id=? AND EXISTS (
    SELECT 1 FROM subscriptions WHERE id=billing_checkout.subscription_id
    AND (provider_subscription_id IS NULL OR provider_subscription_id=?)
  ) AND (
    provider_updated_at IS NULL OR provider_updated_at < ? OR
    (provider_updated_at = ? AND COALESCE(provider_version,-1) < COALESCE(?,-1))
  )`;
const SAVE_SUB_ID = `UPDATE subscriptions SET provider_subscription_id=?,
  provider_updated_at=MAX(COALESCE(provider_updated_at,0),?), updated_at=?
  WHERE id=? AND provider_mode=? AND (provider_subscription_id IS NULL OR provider_subscription_id=?)`;
const SAVE_PAYMENT = `INSERT INTO billing_payments(payment_id,invoice_id,subscription_id,status,amount_clp,
  period_start,period_end,provider_updated_at)
  SELECT ?,?,?,?,?,?,?,? FROM subscriptions WHERE id=? AND provider_subscription_id=? AND provider_mode=?
  ON CONFLICT(payment_id) DO UPDATE SET status=excluded.status,provider_updated_at=excluded.provider_updated_at
  WHERE billing_payments.subscription_id=excluded.subscription_id AND billing_payments.invoice_id=excluded.invoice_id
  AND billing_payments.amount_clp=excluded.amount_clp AND billing_payments.period_start=excluded.period_start
  AND billing_payments.period_end=excluded.period_end AND (
    billing_payments.provider_updated_at<excluded.provider_updated_at OR (
      billing_payments.provider_updated_at=excluded.provider_updated_at AND
      excluded.status IN ('refunded','charged_back') AND billing_payments.status NOT IN ('refunded','charged_back')
    )
  )`;
const APPLY_COVERAGE = `WITH candidate AS (
    SELECT p.status,p.period_start,p.period_end FROM billing_payments p
    WHERE p.subscription_id=? AND p.period_start <= ?
    ORDER BY CASE WHEN p.status='paid' AND p.period_end>? THEN 1 ELSE 0 END DESC,
      p.period_start DESC,
      CASE p.status WHEN 'paid' THEN 2 WHEN 'refunded' THEN 1 WHEN 'charged_back' THEN 1 ELSE 0 END DESC,
      p.provider_updated_at DESC,p.payment_id ASC LIMIT 1
  ), source AS (
    SELECT b.provider_mandate, c.status,c.period_start,c.period_end FROM billing_checkout b
    LEFT JOIN candidate c ON 1=1 WHERE b.subscription_id=?
  )
  UPDATE subscriptions SET
    status=CASE
      WHEN (SELECT provider_mandate FROM source)='canceled' THEN 'cancelled'
      WHEN (SELECT provider_mandate FROM source)='authorized' AND
           (SELECT status FROM source)='paid' AND (SELECT period_end FROM source)>? THEN 'active'
      ELSE (SELECT provider_mandate FROM source)
    END,
    billing_status=COALESCE((SELECT status FROM source),'pending'),
    current_period_start=(SELECT CASE WHEN status='paid' THEN period_start END FROM source),
    current_period_end=(SELECT CASE WHEN status='paid' THEN period_end END FROM source),
    updated_at=? WHERE id=? AND provider_mode=? AND provider_subscription_id=?`;
const INSERT_EVENT = `INSERT INTO webhook_events(provider_mode,event_id,resource_id,topic,received_at)
  VALUES(?,?,?,?,?) ON CONFLICT(provider,provider_mode,event_id) DO NOTHING`;
const SELECT_EVENT = `SELECT topic,processed_at FROM webhook_events
  WHERE provider='mercadopago' AND provider_mode=? AND event_id=?`;
const PROCESS_EVENT = `UPDATE webhook_events SET processed_at=MAX(received_at,?)
  WHERE provider='mercadopago' AND provider_mode=? AND event_id=? AND processed_at IS NULL`;
const PROCESS_LINKED_EVENT = `${PROCESS_EVENT} AND EXISTS (
    SELECT 1 FROM subscriptions WHERE id=? AND provider_mode=webhook_events.provider_mode
      AND provider='mercadopago' AND provider_subscription_id=?
  )`;
const RETRY_DUE = `provider='mercadopago' AND provider_mode=? AND processed_at IS NULL
  AND retry_attempts < 8 AND COALESCE(retry_at, received_at + 120) <= ?`;
const SELECT_RETRY_EVENTS = `SELECT event_id,resource_id,topic,retry_attempts FROM webhook_events
  WHERE ${RETRY_DUE} AND topic IN ('subscription_preapproval','subscription_authorized_payment','payment')
  ORDER BY COALESCE(retry_at, received_at + 120),event_id LIMIT 5`;
const CLAIM_RETRY = `UPDATE webhook_events SET retry_at=?,retry_attempts=retry_attempts+1
  WHERE ${RETRY_DUE} AND event_id=? AND retry_attempts=?`;

function now() { return Math.floor(Date.now() / 1000); }

function fail(status: number, code: string): never { throw new HttpError(status, code); }

function recordProviderFailure(operation: "checkout" | "billing", error: MercadoPagoError): void {
  console.warn("mercadopago_sandbox", { operation, category: error.code,
    provider_action: error.action ?? null, provider_status: error.status,
    transport_cause: error.transportCause ?? null });
}

function sandboxConfigured(env: BillingEnv): boolean {
  return env.BILLING_MODE === MODE && env.MP_ENVIRONMENT === MODE && env.MP_SITE_ID === "MLC";
}

function optional(value?: string): string | undefined {
  return value && value !== "CHANGE_ME" ? value : undefined;
}

function api(env: BillingEnv, signal?: AbortSignal): MercadoPagoClient {
  const applicationId = optional(env.MP_SANDBOX_API_APPLICATION_ID) ?? optional(env.MP_APPLICATION_ID);
  try {
    return new MercadoPagoClient({
      environment: MODE, accessToken: env.MP_ACCESS_TOKEN, merchantId: env.MP_MERCHANT_ID,
      returnOrigin: env.PUBLIC_ORIGIN, ...(applicationId ? { applicationId } : {}),
      ...(signal ? { signal } : {}),
    });
  } catch {
    fail(503, "PAGO_PRUEBA_NO_CONFIGURADO");
  }
}

function settings(env: BillingEnv): Record<PaidPlan, Terms> {
  try {
    if (!env.MP_SANDBOX_PLAN_CATALOG || env.MP_SANDBOX_PLAN_CATALOG.length > 4096) throw new Error();
    const catalog = JSON.parse(env.MP_SANDBOX_PLAN_CATALOG) as Record<PaidPlan, Terms>;
    if (!catalog || Object.keys(catalog).length !== plans.length) throw new Error();
    for (const plan of plans) {
      const terms = catalog[plan];
      if (!terms || Object.keys(terms).length !== 3 || !Number.isSafeInteger(terms.amount_clp)
        || terms.amount_clp <= 0 || !Number.isSafeInteger(terms.frequency)
        || terms.frequency < 1 || terms.frequency > 365
        || !["months", "days"].includes(terms.frequency_type)) throw new Error();
    }
    return catalog;
  } catch {
    fail(503, "CATALOGO_PAGO_PRUEBA_NO_CONFIGURADO");
  }
}

function checkoutBy(env: BillingEnv, where: string, ...args: SqlValue[]): Promise<Checkout | null> {
  return env.DB.withSession("first-primary").prepare(checkoutSelect + where)
    .bind(MODE, ...args).first<Checkout>();
}

function checkoutEnabled(env: BillingEnv, userId: string): boolean {
  return env.MP_SANDBOX_CHECKOUT_ENABLED === "true" && userId === optional(env.SOFIA_OWNER_USER_ID)
    && Boolean(optional(env.MP_SANDBOX_PAYER_EMAIL));
}

function view(row: Checkout, status: number, env: BillingEnv): Response {
  const enabled = checkoutEnabled(env, row.user_id);
  return response({
    checkout_id: row.subscription_id, plan_id: row.plan_id, status: row.status,
    billing_status: row.billing_status, amount_clp: row.amount_clp,
    checkout_enabled: enabled,
    checkout_url: enabled && row.provider_mandate === "pending" ? row.checkout_url : null,
    current_period_start: row.current_period_start, current_period_end: row.current_period_end,
  }, status);
}

async function create(request: Request, env: BillingEnv, user: PortalIdentity): Promise<Response> {
  if (!checkoutEnabled(env, user.id)) fail(503, "CHECKOUT_DE_PRUEBA_PAUSADO");
  const key = request.headers.get("idempotency-key") ?? "";
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(key)) fail(400, "IDEMPOTENCIA_INVALIDA");
  const input = await readJson(request, 1024);
  const plan = plans.find((candidate) => candidate === input.plan_id);
  if (Object.keys(input).length !== 1 || !plan) fail(422, "PLAN_PAGO_INVALIDO");
  const terms = settings(env)[plan];
  const email = env.MP_SANDBOX_PAYER_EMAIL;
  if (!email || email === "CHANGE_ME" || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)
    || email.length > 254) fail(503, "COMPRADOR_PRUEBA_NO_CONFIGURADO");
  const client = api(env);
  const fingerprint = await sha256Hex(JSON.stringify([
    MODE, plan, terms.amount_clp, terms.frequency, terms.frequency_type, email,
  ]));
  const existing = () => checkoutBy(env, ` AND b.user_id=? AND (b.idempotency_key=? OR b.closed_at IS NULL)
    ORDER BY (b.idempotency_key=?) DESC LIMIT 1`, user.id, key, key);
  const replay = (row: Checkout) => {
    if (row.idempotency_key !== key) fail(409, "CHECKOUT_EN_CURSO");
    if (row.request_sha256 !== fingerprint) fail(409, "IDEMPOTENCIA_EN_CONFLICTO");
    return view(row, row.provider_subscription_id ? 200 : 202, env);
  };
  const previous = await existing();
  if (previous) return replay(previous);

  const id = crypto.randomUUID();
  const ref = "studio-sandbox-" + id;
  // Verificar al vendedor antes de reservar un intento que todavía puede repetirse.
  const submit = await client.preparePending({
    reference: ref, payerEmail: email, reason: "E-scale Studio " + plan,
    amountClp: terms.amount_clp, frequency: terms.frequency, frequencyType: terms.frequency_type,
    backUrl: new URL("/", env.PUBLIC_ORIGIN).href,
  });
  const time = now();
  try {
    await env.DB.batch([
      env.DB.prepare(INSERT_SUB).bind(id, user.id, plan, MODE, ref, time, time),
      env.DB.prepare(INSERT_CHECKOUT).bind(
        id, user.id, key, fingerprint, email, terms.amount_clp, terms.frequency, terms.frequency_type, time,
      ),
    ]);
  } catch (error) {
    const winner = await existing();
    if (winner) return replay(winner);
    throw error;
  }
  const created = await checkoutBy(env, " AND b.subscription_id=?", id);
  if (!created) fail(503, "PAGO_NO_DISPONIBLE");
  try {
    const mandate = await submit();
    await persist(env, created, mandate, null);
  } catch (error) {
    if (!(error instanceof MercadoPagoError)) throw error;
    recordProviderFailure("checkout", error);
    // El intento ya es durable. Nunca repetir el POST sin conciliación.
    return view(created, 202, env);
  }
  const saved = await checkoutBy(env, " AND b.subscription_id=?", id);
  if (!saved) fail(503, "PAGO_NO_DISPONIBLE");
  return view(saved, saved.checkout_url ? 201 : 202, env);
}

function assertMandate(row: Checkout, remote: MercadoPagoSubscription): void {
  if (row.external_reference !== remote.reference
    || (row.provider_subscription_id !== null && row.provider_subscription_id !== remote.id)
    || row.amount_clp !== remote.amountClp || row.frequency !== remote.frequency
    || row.frequency_type !== remote.frequencyType) fail(502, "SUSCRIPCION_NO_COINCIDE");
}

function paymentFact(
  row: Checkout, remote: MercadoPagoSubscription, invoice: MercadoPagoInvoice, payment: MercadoPagoPayment,
): PaymentFact {
  if (invoice.subscriptionId !== remote.id || invoice.paymentId !== payment.id
    || (invoice.reference !== null && invoice.reference !== row.external_reference)
    || (payment.reference !== null && payment.reference !== row.external_reference)
    || invoice.amountClp !== row.amount_clp || payment.amountClp !== row.amount_clp) {
    fail(502, "PAGO_NO_COINCIDE");
  }
  const start = invoice.debitAt;
  if (start === undefined) fail(502, "PERIODO_PAGO_NO_VERIFICADO");
  const end = cycleEnd(start, row);
  let state: PaymentState = "pending";
  if (payment.status === "approved") state = "paid";
  if (payment.status === "rejected" || payment.status === "cancelled") state = "failed";
  if (payment.status === "refunded" || payment.status === "charged_back") state = payment.status;
  return { id: payment.id, invoiceId: invoice.id, state, amount: payment.amountClp,
    start, end, updatedAt: payment.updatedAt };
}

function cycleEnd(start: number, row: Terms): number {
  if (row.frequency_type === "days") return start + row.frequency * 86_400;
  const date = new Date(start * 1000);
  const sourceDay = date.getUTCDate();
  date.setUTCDate(1);
  date.setUTCMonth(date.getUTCMonth() + row.frequency);
  const lastDay = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  date.setUTCDate(Math.min(sourceDay, lastDay));
  return Math.floor(date.getTime() / 1000);
}

async function persist(
  env: BillingEnv, row: Checkout, remote: MercadoPagoSubscription, fact: PaymentFact | null,
  eventId?: string,
): Promise<void> {
  assertMandate(row, remote);
  const time = now();
  const commands: SqlStatement[] = [
    env.DB.prepare(SAVE_MANDATE).bind(
      remote.checkoutUrl, remote.version, remote.status, remote.updatedAt, remote.status, time,
      row.subscription_id, remote.id, remote.updatedAt, remote.updatedAt, remote.version,
    ),
    env.DB.prepare(SAVE_SUB_ID).bind(remote.id, remote.updatedAt, time, row.subscription_id, MODE, remote.id),
  ];
  if (fact) {
    commands.push(env.DB.prepare(SAVE_PAYMENT).bind(
      fact.id, fact.invoiceId, row.subscription_id, fact.state, fact.amount, fact.start, fact.end, fact.updatedAt,
      row.subscription_id, remote.id, MODE,
    ));
  }
  commands.push(env.DB.prepare(APPLY_COVERAGE).bind(
    row.subscription_id, time, time, row.subscription_id, time, time, row.subscription_id, MODE, remote.id,
  ));
  if (eventId) commands.push(env.DB.prepare(PROCESS_LINKED_EVENT).bind(
    time, MODE, eventId, row.subscription_id, remote.id,
  ));
  await env.DB.batch(commands);
  // Si otra solicitud enlazó un ID en paralelo, prevalece ese enlace.
  const saved = await checkoutBy(env, " AND b.subscription_id=?", row.subscription_id);
  if (!saved || saved.provider_subscription_id !== remote.id) fail(502, "SUSCRIPCION_NO_COINCIDE");
}

async function factFor(
  client: MercadoPagoClient, row: Checkout, remote: MercadoPagoSubscription, invoice: MercadoPagoInvoice | null,
): Promise<PaymentFact | null> {
  if (!invoice?.paymentId) return null;
  const payment = await client.getPayment(invoice.paymentId);
  return paymentFact(row, remote, invoice, payment);
}

async function reconcile(request: Request, env: BillingEnv, user: PortalIdentity): Promise<Response> {
  const input = await readJson(request, 1024);
  if (Object.keys(input).length !== 1 || typeof input.checkout_id !== "string" || !uuid.test(input.checkout_id)) {
    fail(422, "CHECKOUT_INVALIDO");
  }
  const row = await checkoutBy(env, " AND b.user_id=? AND b.subscription_id=?", user.id, input.checkout_id);
  if (!row) fail(404, "CHECKOUT_NO_ENCONTRADO");
  const client = api(env);
  const remote = row.provider_subscription_id
    ? await client.getSubscription(row.provider_subscription_id)
    : await client.findSubscription({ payerEmail: row.payer_email, reference: row.external_reference });
  if (!remote) return view(row, 202, env);
  assertMandate(row, remote);
  // La cancelación de un intento sin pago se confirma con el mandato.
  const canceledWithoutCoverage = remote.status === "canceled"
    && row.billing_status === "pending" && row.current_period_end === null;
  const invoice = remote.status === "pending" || canceledWithoutCoverage
    ? null : await client.findLatestInvoice(remote.id);
  await persist(env, row, remote, await factFor(client, row, remote, invoice));
  const saved = await checkoutBy(env, " AND b.user_id=? AND b.subscription_id=?", user.id, row.subscription_id);
  if (!saved) fail(503, "PAGO_NO_DISPONIBLE");
  return view(saved, 200, env);
}

function webhookSettings(env: BillingEnv) {
  const secret = optional(env.MP_WEBHOOK_SECRET);
  const app = optional(env.MP_APPLICATION_ID);
  const sandboxApp = optional(env.MP_SANDBOX_API_APPLICATION_ID) ?? app;
  if (!secret || !app || !merchantId.test(app) || !merchantId.test(env.MP_MERCHANT_ID)
    || !sandboxApp || !merchantId.test(sandboxApp)
    || !/^[\x21-\x7e]{16,512}$/.test(secret)) return null;
  return { secret, app, sandboxApp };
}

async function processWebhookResource(env: BillingEnv, event: WebhookResource): Promise<void> {
  const client = api(env, AbortSignal.timeout(14_000));
  const invoice = event.topic === "subscription_authorized_payment"
    ? await client.getInvoice(event.resource_id)
    : event.topic === "payment" ? await client.findInvoiceByPayment(event.resource_id) : null;
  if (!invoice && event.topic !== "subscription_preapproval") fail(503, "FACTURA_PENDIENTE");
  const remote = await client.getSubscription(invoice?.subscriptionId ?? event.resource_id);
  const row = await checkoutBy(env, " AND s.external_reference=?", remote.reference);
  if (row) {
    assertMandate(row, remote);
    await persist(env, row, remote, await factFor(client, row, remote, invoice), event.event_id);
  } else {
    await env.DB.prepare(PROCESS_EVENT).bind(now(), MODE, event.event_id).run();
  }
}

export async function retryWebhookEvents(env: BillingEnv): Promise<void> {
  if (!sandboxConfigured(env) || !webhookSettings(env)) return;
  const rows = await env.DB.withSession("first-primary").prepare(SELECT_RETRY_EVENTS)
    .bind(MODE, now()).all<WebhookResource & { retry_attempts: number }>();
  for (const row of rows.results) {
    const time = now();
    const delay = Math.min(3600, 120 * 2 ** row.retry_attempts);
    // Reservar el intento antes de consultar el proveedor permite recuperarlo si la tarea se interrumpe.
    const claim = await env.DB.prepare(CLAIM_RETRY)
      .bind(time + delay, MODE, time, row.event_id, row.retry_attempts).run();
    if (claim.meta.changes !== 1) continue;
    try {
      await processWebhookResource(env, row);
    } catch (error) {
      if (error instanceof MercadoPagoError) recordProviderFailure("billing", error);
      console.warn("mercadopago_reintento_webhook", {
        intento: row.retry_attempts + 1, causa: error instanceof HttpError ? error.code : "consulta_o_cotejo",
      });
    }
  }
}

async function webhook(request: Request, env: BillingEnv): Promise<Response> {
  const settings = webhookSettings(env);
  if (!settings) fail(503, "WEBHOOK_PAGO_NO_CONFIGURADO");
  const { secret, app, sandboxApp } = settings;
  const time = now();
  let event;
  try {
    event = await verifyMercadoPagoWebhook(request, secret, time, 300);
  } catch {
    fail(401, "WEBHOOK_PAGO_NO_AUTENTICADO");
  }
  // El mandato consultado acredita la aplicación cuando el cuerpo omite el identificador.
  if ((event.applicationId !== null && event.applicationId !== app && event.applicationId !== sandboxApp)
    || (event.merchantId !== null && event.merchantId !== env.MP_MERCHANT_ID)
    || event.liveMode === true) fail(401, "WEBHOOK_PAGO_NO_AUTENTICADO");
  // El tópico procede del cuerpo/URL y no participa de la firma.
  const id = await sha256Hex(JSON.stringify([event.resourceId, event.requestId, event.timestampSeconds]));
  await env.DB.prepare(INSERT_EVENT).bind(MODE, id, event.resourceId, event.topic, time).run();
  const saved = await env.DB.withSession("first-primary").prepare(SELECT_EVENT)
    .bind(MODE, id).first<{ topic: string; processed_at: number | null }>();
  if (!saved || saved.topic !== event.topic) fail(409, "WEBHOOK_PAGO_EN_CONFLICTO");
  if (saved.processed_at !== null) return response({ received: true });

  await processWebhookResource(env, { event_id: id, resource_id: event.resourceId, topic: event.topic });
  return response({ received: true });
}

export async function handleBilling(
  request: Request, env: BillingEnv, path: string, user?: PortalIdentity,
): Promise<Response> {
  if (!sandboxConfigured(env)) fail(503, "PAGO_PRUEBA_NO_CONFIGURADO");
  try {
    if (path === "/api/billing/webhook") {
      if (request.method !== "POST") fail(405, "METODO_NO_PERMITIDO");
      return await webhook(request, env);
    }
    if (!user) fail(401, "SESION_REQUERIDA");
    if (path === "/api/billing/checkout") {
      if (request.method === "POST") return await create(request, env, user);
      if (request.method === "GET") {
        const row = await checkoutBy(env, " AND b.user_id=? ORDER BY b.created_at DESC,b.subscription_id DESC LIMIT 1", user.id);
        return row ? view(row, 200, env)
          : response({ checkout: null, checkout_enabled: checkoutEnabled(env, user.id) });
      }
      fail(405, "METODO_NO_PERMITIDO");
    }
    if (path === "/api/billing/reconcile") {
      if (request.method !== "PUT") fail(405, "METODO_NO_PERMITIDO");
      return await reconcile(request, env, user);
    }
    fail(404, "RUTA_NO_ENCONTRADA");
  } catch (error) {
    if (error instanceof HttpError) throw error;
    if (error instanceof MercadoPagoError) recordProviderFailure("billing", error);
    fail(503, "PAGO_NO_DISPONIBLE");
  }
}
