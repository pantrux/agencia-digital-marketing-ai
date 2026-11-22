const API_ORIGIN = 'https://api.mercadopago.com';
const CHECKOUT_HOST = 'www.mercadopago.cl';
const PORTAL_ORIGIN = 'https://studio.e-scale.cl';
const WEBHOOK_TOPICS = ['subscription_preapproval', 'subscription_authorized_payment', 'payment'] as const;
const SUBSCRIPTION_STATUSES = ['pending', 'authorized', 'paused', 'cancelled', 'canceled'] as const;
const PAYMENT_STATUSES = [
  'pending', 'approved', 'authorized', 'in_process', 'in_mediation', 'rejected',
  'cancelled', 'refunded', 'charged_back',
] as const;
const MAX_WEBHOOK_BYTES = 16_384;
const MAX_RESPONSE_BYTES = 256 * 1024;
const PAGE_SIZE = 20;
const MAX_PAGES = 10;
const REQUEST_TIMEOUT_MS = 8_000;
const EXTERNAL_REFERENCE = /^[A-Za-z0-9_-]{1,120}$/u;
const RESOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/u;
const NUMERIC_ID = /^[1-9][0-9]{0,23}$/u;
const REQUEST_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const encoder = new TextEncoder();

type ErrorCode =
  | 'invalid_input' | 'sandbox_unverified' | 'provider_http' | 'provider_transport'
  | 'provider_payload' | 'provider_binding' | 'provider_ambiguous' | 'provider_pagination'
  | 'webhook_signature' | 'webhook_timestamp' | 'webhook_payload';
type FrequencyType = 'months' | 'days';
type ProviderAction = 'seller_lookup' | 'mandate_create' | 'mandate_search' | 'provider_read';
type TransportCause = 'timeout' | 'aborted' | 'request_options' | 'network';
type SubscriptionStatus = Exclude<(typeof SUBSCRIPTION_STATUSES)[number], 'cancelled'>;
type PaymentStatus = (typeof PAYMENT_STATUSES)[number];
type JsonObject = Record<string, unknown>;

export class MercadoPagoError extends Error {
  constructor(readonly code: ErrorCode, readonly status: number | null = null,
    readonly action?: ProviderAction, readonly transportCause?: TransportCause) {
    super(code);
    this.name = 'MercadoPagoError';
  }
}

function transportCause(error: unknown): TransportCause {
  if (error instanceof Error) {
    if (error.name === 'TimeoutError') return 'timeout';
    if (error.name === 'AbortError') return 'aborted';
    if (error.name === 'TypeError' && /(?:RequestInit|unsupported|illegal invocation)/iu.test(error.message)) {
      return 'request_options';
    }
  }
  return 'network';
}

export interface MercadoPagoWebhookEvent {
  topic: (typeof WEBHOOK_TOPICS)[number];
  resourceId: string;
  requestId: string;
  timestampSeconds: number;
  notificationId: string | null;
  applicationId: string | null;
  merchantId: string | null;
  liveMode: boolean | null;
}

export interface MercadoPagoOptions {
  accessToken: string;
  environment: 'sandbox';
  merchantId: string;
  applicationId?: string;
  checkoutHost?: typeof CHECKOUT_HOST;
  returnOrigin?: string;
  signal?: AbortSignal;
}

export interface MercadoPagoPendingInput {
  reference: string;
  payerEmail: string;
  reason: string;
  amountClp: number;
  frequency: number;
  frequencyType: FrequencyType;
  backUrl: string;
}

export interface MercadoPagoSubscription {
  id: string;
  reference: string;
  checkoutUrl: string | null;
  status: SubscriptionStatus;
  amountClp: number;
  frequency: number;
  frequencyType: FrequencyType;
  nextPaymentAt?: number;
  updatedAt: number;
  version: number | null;
}

export interface MercadoPagoInvoice {
  id: string;
  subscriptionId: string;
  paymentId: string | null;
  reference: string | null;
  amountClp: number;
  status: string;
  paymentStatus: PaymentStatus | null;
  debitAt?: number;
  updatedAt: number;
}

export interface MercadoPagoPayment {
  id: string;
  status: PaymentStatus;
  amountClp: number;
  reference: string | null;
  updatedAt: number;
  liveMode: false;
}

function requireValue(condition: unknown, code: ErrorCode): asserts condition {
  if (!condition) throw new MercadoPagoError(code);
}

function object(value: unknown, code: ErrorCode = 'provider_payload'): JsonObject {
  requireValue(value !== null && typeof value === 'object' && !Array.isArray(value), code);
  return value as JsonObject;
}

function matches(value: unknown, expression: RegExp): value is string {
  return typeof value === 'string' && expression.test(value);
}

function oneOf<const T extends string>(value: unknown, values: readonly T[]): value is T {
  return typeof value === 'string' && values.some((option) => option === value);
}

function numericId(value: unknown, code: ErrorCode): string {
  const id = typeof value === 'number' && Number.isSafeInteger(value) ? String(value) : value;
  requireValue(matches(id, NUMERIC_ID), code);
  return id;
}

function optionalNumericId(value: unknown, code: ErrorCode): string | null {
  return value == null ? null : numericId(value, code);
}

function idFor(value: unknown, expression = RESOURCE_ID): string {
  requireValue(matches(value, expression), 'invalid_input');
  return value;
}

function frequencyType(value: unknown): value is FrequencyType {
  return value === 'days' || value === 'months';
}

function integer(value: unknown, min: number, max: number): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= min && value <= max;
}

function reference(value: unknown, optional: boolean): string | null {
  if (optional && (value == null || value === '')) return null;
  const ref = typeof value === 'number' && Number.isSafeInteger(value) ? String(value) : value;
  requireValue(matches(ref, EXTERNAL_REFERENCE), 'provider_payload');
  return ref;
}

function email(value: unknown): value is string {
  return matches(value, /^[^\s@]+@[^\s@]+\.[^\s@]+$/u) && value.length <= 254;
}

function amount(data: JsonObject): number {
  const raw = data.transaction_amount;
  const value = matches(raw, /^(?:0|[1-9][0-9]{0,14})(?:\.0{1,3})?$/u) ? Number(raw) : raw;
  requireValue(data.currency_id === 'CLP' && integer(value, 1, Number.MAX_SAFE_INTEGER), 'provider_payload');
  return value;
}

function timestamp(value: unknown): number {
  requireValue(matches(value, /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$/u), 'provider_payload');
  const seconds = Math.floor(Date.parse(value) / 1000);
  requireValue(integer(seconds, 1, Number.MAX_SAFE_INTEGER), 'provider_payload');
  return seconds;
}

async function readJson(message: Request | Response, limit: number, code: ErrorCode): Promise<JsonObject> {
  const reader = message.body?.getReader();
  requireValue(reader, code);
  const type = message.headers.get('content-type')?.split(';', 1)[0]?.trim().toLowerCase();
  const length = message.headers.get('content-length');
  const decoder = new TextDecoder('utf-8', { fatal: true });
  try {
    requireValue(type === 'application/json' && (length === null || (
      /^[0-9]{1,9}$/u.test(length) && Number(length) <= limit
    )), code);
    let size = 0;
    let content = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      requireValue(size <= limit, code);
      content += decoder.decode(value, { stream: true });
    }
    return object(JSON.parse(content + decoder.decode()) as unknown, code);
  } catch {
    void reader.cancel().catch(() => undefined);
    throw new MercadoPagoError(code);
  } finally {
    reader.releaseLock();
  }
}

function signature(header: string | null): { ts: string; mac: Uint8Array<ArrayBuffer> } {
  requireValue(header && header.length <= 256, 'webhook_signature');
  const pieces = header.split(',');
  const fields = new Map<string, string>();
  for (const piece of pieces) {
    const part = /^(ts|v1)\s*=\s*([0-9a-fA-F]+)$/u.exec(piece.trim());
    requireValue(part?.[1] && part[2] && !fields.has(part[1]), 'webhook_signature');
    fields.set(part[1], part[2]);
  }
  const ts = fields.get('ts');
  const hash = fields.get('v1');
  requireValue(matches(ts, /^[1-9][0-9]{9}$/u) && matches(hash, /^[0-9a-fA-F]{64}$/u), 'webhook_signature');
  const mac = new Uint8Array(32);
  for (let index = 0; index < mac.length; index++) {
    mac[index] = Number.parseInt(hash.slice(index * 2, index * 2 + 2), 16);
  }
  return { ts, mac };
}

/** Los campos del cuerpo sirven para enrutar y cotejar. La API decide el estado real. */
export async function verifyMercadoPagoWebhook(
  request: Request,
  secret: string,
  nowSeconds: number,
  toleranceSeconds: number,
): Promise<MercadoPagoWebhookEvent> {
  requireValue(
    request.method === 'POST' && matches(secret, /^[\x21-\x7e]{16,512}$/u)
    && integer(nowSeconds, 1, Number.MAX_SAFE_INTEGER)
    && integer(toleranceSeconds, 0, Number.MAX_SAFE_INTEGER),
    'invalid_input',
  );
  const query = new URL(request.url).searchParams;
  const ids = query.getAll('data.id');
  const resourceId = ids[0];
  const requestId = request.headers.get('x-request-id');
  requireValue(ids.length === 1 && matches(resourceId, RESOURCE_ID) && matches(requestId, REQUEST_ID),
    'webhook_signature');
  const { ts, mac } = signature(request.headers.get('x-signature'));
  const timestampSeconds = Number(ts);
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']);
  const manifest = 'id:' + resourceId + ';request-id:' + requestId + ';ts:' + ts + ';';
  const verified = await crypto.subtle.verify('HMAC', key, mac, encoder.encode(manifest));
  requireValue(verified, 'webhook_signature');
  requireValue(Math.abs(timestampSeconds - nowSeconds) <= toleranceSeconds, 'webhook_timestamp');

  const body = await readJson(request, MAX_WEBHOOK_BYTES, 'webhook_payload');
  const topics = query.getAll('type');
  const topic = body.type;
  requireValue(topics.length === 1 && topic === topics[0] && oneOf(topic, WEBHOOK_TOPICS), 'webhook_payload');
  const bodyId = object(body.data, 'webhook_payload').id;
  const expectedId = topic === 'subscription_preapproval' ? bodyId : numericId(bodyId, 'webhook_payload');
  requireValue(expectedId === resourceId, 'webhook_payload');
  requireValue(body.live_mode == null || typeof body.live_mode === 'boolean', 'webhook_payload');
  return {
    topic, resourceId, requestId, timestampSeconds,
    notificationId: optionalNumericId(body.id, 'webhook_payload'),
    applicationId: optionalNumericId(body.application_id, 'webhook_payload'),
    merchantId: optionalNumericId(body.user_id, 'webhook_payload'),
    liveMode: body.live_mode ?? null,
  };
}

function url(value: unknown, code: ErrorCode): URL {
  requireValue(typeof value === 'string' && value.length <= 2048 && !/[\s\\]/u.test(value), code);
  try {
    const parsed = new URL(value);
    requireValue(parsed.protocol === 'https:' && !parsed.username && !parsed.password && !parsed.hash, code);
    return parsed;
  } catch {
    throw new MercadoPagoError(code);
  }
}

export class MercadoPagoClient {
  readonly #accessToken: string;
  readonly #merchantId: string;
  readonly #applicationId: string | undefined;
  readonly #returnOrigin: string;
  readonly #signal: AbortSignal | undefined;
  readonly #fetch: typeof fetch;

  constructor(options: MercadoPagoOptions, fetchImpl: typeof fetch = fetch) {
    const origin = url(options.returnOrigin ?? PORTAL_ORIGIN, 'invalid_input');
    requireValue(options.environment === 'sandbox'
      && matches(options.accessToken, /^[\x21-\x7e]{1,2048}$/u) && options.accessToken !== 'CHANGE_ME'
      && matches(options.merchantId, NUMERIC_ID)
      && (options.applicationId === undefined || matches(options.applicationId, NUMERIC_ID))
      && (options.checkoutHost === undefined || options.checkoutHost === CHECKOUT_HOST)
      && origin.pathname === '/' && !origin.search, 'invalid_input');
    this.#accessToken = options.accessToken;
    this.#merchantId = options.merchantId;
    this.#applicationId = options.applicationId;
    this.#returnOrigin = origin.origin;
    this.#signal = options.signal;
    this.#fetch = fetchImpl;
  }

  async #request(path: string, body?: JsonObject, action: ProviderAction = 'provider_read'): Promise<JsonObject> {
    const headers = new Headers({ accept: 'application/json', authorization: 'Bearer ' + this.#accessToken,
      'cache-control': 'no-cache, no-store', pragma: 'no-cache' });
    const deadline = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
    const init: RequestInit = { headers, method: body ? 'POST' : 'GET', redirect: 'manual',
      signal: this.#signal ? AbortSignal.any([deadline, this.#signal]) : deadline };
    if (body) {
      headers.set('content-type', 'application/json');
      init.body = JSON.stringify(body);
    }
    let response: Response;
    try {
      const send = this.#fetch;
      response = await send(API_ORIGIN + path, init);
    } catch (error) {
      throw new MercadoPagoError('provider_transport', null, action, transportCause(error));
    }
    if (response.status !== (body ? 201 : 200)) {
      void response.body?.cancel().catch(() => undefined);
      throw new MercadoPagoError('provider_http', response.status, action);
    }
    try {
      return await readJson(response, MAX_RESPONSE_BYTES, 'provider_payload');
    } catch {
      throw new MercadoPagoError('provider_payload', response.status, action);
    }
  }

  #binding(data: JsonObject, requiredMerchant: boolean, requiredApp: boolean): void {
    requireValue(data.live_mode == null || data.live_mode === false, 'provider_binding');
    if (requiredMerchant || data.collector_id != null) {
      requireValue(numericId(data.collector_id, 'provider_binding') === this.#merchantId, 'provider_binding');
    }
    if (this.#applicationId && (requiredApp || data.application_id != null)) {
      requireValue(numericId(data.application_id, 'provider_binding') === this.#applicationId, 'provider_binding');
    }
  }

  #subscription(data: JsonObject): MercadoPagoSubscription {
    this.#binding(data, true, true);
    const { id, status, version } = data;
    const recurrence = object(data.auto_recurring);
    const frequency = recurrence.frequency;
    const type = recurrence.frequency_type;
    requireValue(matches(id, RESOURCE_ID) && oneOf(status, SUBSCRIPTION_STATUSES)
      && integer(frequency, 1, Number.MAX_SAFE_INTEGER) && frequencyType(type)
      && (version == null || integer(version, 0, Number.MAX_SAFE_INTEGER)), 'provider_payload');
    const ref = reference(data.external_reference, false);
    requireValue(ref !== null, 'provider_payload');
    return {
      id, reference: ref, status: status === 'cancelled' ? 'canceled' : status,
      checkoutUrl: data.init_point == null ? null : this.#checkoutUrl(data.init_point, id),
      amountClp: amount(recurrence), frequency, frequencyType: type,
      updatedAt: timestamp(data.last_modified), version: version ?? null,
      ...(data.next_payment_date == null ? {} : { nextPaymentAt: timestamp(data.next_payment_date) }),
    };
  }

  #checkoutUrl(value: unknown, id: string): string {
    const checkout = url(value, 'provider_binding');
    const ids = checkout.searchParams.getAll('preapproval_id');
    const activation = checkout.searchParams.getAll('activation');
    requireValue(checkout.host === CHECKOUT_HOST && checkout.pathname === '/subscriptions/checkout'
      && ids.length === 1 && ids[0] === id && [...checkout.searchParams].length === 1 + activation.length
      && (activation.length === 0 || (activation.length === 1 && activation[0] === 'true')), 'provider_binding');
    return checkout.href;
  }

  async createPending(input: MercadoPagoPendingInput): Promise<MercadoPagoSubscription> {
    const submit = await this.preparePending(input);
    return submit();
  }

  async preparePending(input: MercadoPagoPendingInput): Promise<() => Promise<MercadoPagoSubscription>> {
    const { reference: ref, payerEmail, reason, amountClp, frequency, frequencyType: type, backUrl } = input;
    const back = url(backUrl, 'invalid_input');
    requireValue(back.origin === this.#returnOrigin && matches(ref, EXTERNAL_REFERENCE)
      && email(payerEmail) && matches(reason, /^[^\p{Cc}\p{Cf}]{1,140}$/u)
      && reason.trim() === reason && integer(amountClp, 1, Number.MAX_SAFE_INTEGER)
      && integer(frequency, 1, Number.MAX_SAFE_INTEGER) && frequencyType(type), 'invalid_input');
    const seller = await this.#request('/users/me', undefined, 'seller_lookup');
    requireValue(numericId(seller.id, 'sandbox_unverified') === this.#merchantId
      && seller.site_id === 'MLC' && Array.isArray(seller.tags) && seller.tags.includes('test_user'),
      'sandbox_unverified');
    return async () => {
      const body = await this.#request('/preapproval', {
        status: 'pending', external_reference: ref, payer_email: payerEmail, reason, back_url: back.href,
        auto_recurring: { currency_id: 'CLP', transaction_amount: amountClp, frequency, frequency_type: type },
      }, 'mandate_create');
      const result = this.#subscription(body);
      requireValue(result.status === 'pending' && result.checkoutUrl !== null && result.reference === ref
        && result.amountClp === amountClp && result.frequency === frequency && result.frequencyType === type
        && body.preapproval_plan_id == null && (body.back_url == null || body.back_url === back.href),
        'provider_binding');
      return result;
    };
  }

  async getSubscription(id: string): Promise<MercadoPagoSubscription> {
    const subscription = this.#subscription(await this.#request('/preapproval/' + idFor(id)));
    requireValue(subscription.id === id, 'provider_binding');
    return subscription;
  }

  async findSubscription(input: { payerEmail: string; reference: string }): Promise<MercadoPagoSubscription | null> {
    const { payerEmail, reference: ref } = input;
    requireValue(email(payerEmail) && matches(ref, EXTERNAL_REFERENCE), 'invalid_input');
    const result = await this.#search('/preapproval/search', { payer_email: payerEmail },
      (row) => row.external_reference === ref);
    return result ? this.#subscription(result) : null;
  }

  #invoice(data: JsonObject): MercadoPagoInvoice {
    this.#binding(data, false, false);
    const payment = data.payment == null ? null : object(data.payment);
    const status = data.status;
    const paymentStatus = payment?.status ?? null;
    requireValue(matches(data.preapproval_id, RESOURCE_ID) && matches(status, /^[a-z][a-z_]{0,39}$/u)
      && (paymentStatus === null || oneOf(paymentStatus, PAYMENT_STATUSES)), 'provider_payload');
    return {
      id: numericId(data.id, 'provider_payload'), subscriptionId: data.preapproval_id,
      paymentId: optionalNumericId(payment?.id, 'provider_payload'), reference: reference(data.external_reference, true),
      amountClp: amount(data), status, paymentStatus, updatedAt: timestamp(data.last_modified),
      ...(data.debit_date == null ? {} : { debitAt: timestamp(data.debit_date) }),
    };
  }

  async getInvoice(id: string): Promise<MercadoPagoInvoice> {
    const invoice = this.#invoice(await this.#request('/authorized_payments/' + idFor(id, NUMERIC_ID)));
    requireValue(invoice.id === id, 'provider_binding');
    return invoice;
  }

  async findLatestDueInvoiceWithPayment(subscriptionId: string, asOf: number): Promise<MercadoPagoInvoice | null> {
    idFor(subscriptionId);
    requireValue(integer(asOf, 1, Number.MAX_SAFE_INTEGER), 'invalid_input');
    const records = await this.#searchRows('/authorized_payments/search', { preapproval_id: subscriptionId });
    const due: Array<MercadoPagoInvoice & { debitAt: number }> = [];
    for (const record of records) {
      const invoice = this.#invoice(record);
      requireValue(invoice.subscriptionId === subscriptionId, 'provider_binding');
      if (invoice.paymentId === null) continue;
      const { debitAt } = invoice;
      requireValue(debitAt !== undefined, 'provider_payload');
      if (debitAt <= asOf) due.push({ ...invoice, debitAt });
    }
    due.sort((left, right) => right.debitAt - left.debitAt);
    const [latest] = due;
    requireValue(!latest || !due.some(invoice => invoice.debitAt === latest.debitAt && invoice.id !== latest.id),
      'provider_ambiguous');
    return latest ?? null;
  }

  async findInvoiceByPayment(id: string): Promise<MercadoPagoInvoice | null> {
    idFor(id, NUMERIC_ID);
    const result = await this.#search('/authorized_payments/search', { payment_id: id }, (row) =>
      row.payment != null && optionalNumericId(object(row.payment).id, 'provider_payload') === id);
    return result ? this.#invoice(result) : null;
  }

  async getPayment(id: string): Promise<MercadoPagoPayment> {
    const data = await this.#request('/v1/payments/' + idFor(id, NUMERIC_ID));
    this.#binding(data, true, false);
    requireValue(numericId(data.id, 'provider_payload') === id && data.live_mode === false, 'provider_binding');
    requireValue(oneOf(data.status, PAYMENT_STATUSES), 'provider_payload');
    return {
      id, status: data.status, amountClp: amount(data), reference: reference(data.external_reference, true),
      updatedAt: timestamp(data.date_last_updated), liveMode: false,
    };
  }

  async #search(path: string, filters: Record<string, string>, match: (row: JsonObject) => boolean): Promise<JsonObject | null> {
    const candidates = (await this.#searchRows(path, filters)).filter(match);
    requireValue(candidates.length <= 1, 'provider_ambiguous');
    return candidates[0] ?? null;
  }

  async #searchRows(path: string, filters: Record<string, string>): Promise<JsonObject[]> {
    let offset = 0;
    let expectedTotal: number | undefined;
    const found: JsonObject[] = [];
    for (let page = 0; page < MAX_PAGES; page++) {
      const query = new URLSearchParams({ ...filters, offset: String(offset), limit: String(PAGE_SIZE) });
      const action = path === '/preapproval/search' ? 'mandate_search' : 'provider_read';
      const response = await this.#request(path + '?' + query.toString(), undefined, action);
      const { total, limit, offset: actualOffset } = object(response.paging);
      const results = response.results;
      requireValue(actualOffset === offset && integer(total, 0, MAX_PAGES * PAGE_SIZE)
        && (expectedTotal === undefined || total === expectedTotal)
        && integer(limit, 1, PAGE_SIZE) && Array.isArray(results)
        && results.length === Math.min(limit, total - offset), 'provider_pagination');
      expectedTotal = total;
      found.push(...results.map((entry: unknown) => object(entry)));
      offset += results.length;
      if (offset >= total) return found;
    }
    throw new MercadoPagoError('provider_pagination');
  }
}
