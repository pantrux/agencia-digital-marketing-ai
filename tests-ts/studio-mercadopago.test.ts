import { createHmac } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import {
  MercadoPagoClient,
  MercadoPagoError,
  verifyMercadoPagoWebhook,
} from '../cloudflare-saas/studio-api-worker/src/mercadopago.js';

const TOKEN = 'opaque-sandbox-fixture';
const SECRET = 'opaque-webhook-fixture';
const NOW = 1_795_000_010;
const TS = '1795000000';
const REF = 'studio-test-901';
const SUB = 'aA938084726fca480172750000000001';
const INVOICE = '6114264375';
const PAYMENT = '19951521071';
const MERCHANT = '100200300';
const APP = '1234567812345678';
const REQUEST = '0468121e-7061-4513-82f4-3feb624cce74';
const CHECKOUT = 'https://www.mercadopago.cl/subscriptions/checkout?preapproval_id=' + SUB;
const INPUT = {
  reference: REF,
  payerEmail: 'payer@example.test',
  reason: 'Studio Pro',
  amountClp: 5000,
  frequency: 1,
  frequencyType: 'months' as const,
  backUrl: 'https://studio.e-scale.cl/billing/return',
};
const SUBSCRIPTION = {
  id: SUB,
  collector_id: Number(MERCHANT),
  application_id: Number(APP),
  external_reference: REF,
  version: 0,
  last_modified: '2026-11-14T13:00:00.500-03:00',
  init_point: CHECKOUT,
  status: 'pending',
  auto_recurring: {
    frequency: 1, frequency_type: 'months', transaction_amount: '5000', currency_id: 'CLP',
  },
  payer_email: 'blank-or-private@example.test',
};
const SELLER = { id: Number(MERCHANT), site_id: 'MLC', tags: ['test_user'] };
const INVOICE_BODY = {
  id: Number(INVOICE),
  preapproval_id: SUB,
  currency_id: 'CLP',
  transaction_amount: 5000,
  status: 'scheduled',
  last_modified: '2026-11-14T16:00:00Z',
  payment: { id: Number(PAYMENT), status: 'approved' },
};
const PAYMENT_BODY = {
  id: Number(PAYMENT), collector_id: Number(MERCHANT), currency_id: 'CLP',
  transaction_amount: 5000, status: 'approved', live_mode: false,
  date_last_updated: '2026-11-14T16:00:00Z',
};

function webhook(options: {
  id?: string; ts?: string; sig?: string; urlExtra?: string; body?: object; req?: string;
} = {}): Request {
  const id = options.id ?? SUB;
  const ts = options.ts ?? TS;
  const requestId = options.req ?? REQUEST;
  const mac = createHmac('sha256', SECRET)
    .update('id:' + id + ';request-id:' + requestId + ';ts:' + ts + ';').digest('hex');
  return new Request(
    'https://studio.e-scale.cl/api/webhooks/mercadopago?type=subscription_preapproval&data.id=' +
    id + (options.urlExtra ?? ''),
    {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-signature': options.sig ?? ('ts=' + ts + ',v1=' + mac),
        'x-request-id': requestId },
      body: JSON.stringify(options.body ?? {
        id: 9020, type: 'subscription_preapproval', data: { id }, live_mode: false,
        application_id: Number(APP), user_id: Number(MERCHANT),
      }),
    },
  );
}

type Reply = (url: URL, init: RequestInit | undefined) => Response | Promise<Response>;

function client(reply: Reply, options: ConstructorParameters<typeof MercadoPagoClient>[0] = {
  accessToken: TOKEN, environment: 'sandbox', merchantId: MERCHANT, applicationId: APP,
}) {
  const fake: typeof fetch = async (value, init) => {
    const url = new URL(String(value));
    expect(url.origin).toBe('https://api.mercadopago.com');
    expect(new Headers(init?.headers).get('authorization')).toBe('Bearer ' + TOKEN);
    expect(init?.redirect).toBe('manual');
    return reply(url, init);
  };
  return new MercadoPagoClient(options, fake);
}

it('verifica firma original, preserva mayúsculas del ID y proyecta sólo metadatos', async () => {
  await expect(verifyMercadoPagoWebhook(webhook(), SECRET, NOW, 10)).resolves.toEqual({
    topic: 'subscription_preapproval', resourceId: SUB, requestId: REQUEST,
    timestampSeconds: Number(TS), notificationId: '9020', applicationId: APP,
    merchantId: MERCHANT, liveMode: false,
  });
});

describe('aviso Mercado Pago', () => {
  it.each([
    [{ id: SUB.toLowerCase(), body: { type: 'subscription_preapproval', data: { id: SUB } } }, 'webhook_payload'],
    [{ ts: '1795000000000' }, 'webhook_signature'],
    [{ ts: '1794999999' }, 'webhook_timestamp'],
    [{ sig: 'ts=' + TS + ',ts=' + TS + ',v1=' + 'ab'.repeat(32) }, 'webhook_signature'],
    [{ sig: 'ts=' + TS + ',v1=' + 'ñ'.repeat(64) }, 'webhook_signature'],
    [{ urlExtra: '&data.id=' + SUB }, 'webhook_signature'],
    [{ urlExtra: '&type=payment' }, 'webhook_payload'],
    [{ req: REQUEST + ',other' }, 'webhook_signature'],
  ] as const)('rechaza entrada ambigua o fuera de ventana %#', async (input, code) => {
    await expect(verifyMercadoPagoWebhook(webhook(input), SECRET, NOW, 10))
      .rejects.toMatchObject({ code });
  });

  it('rechaza la firma válida para otro secreto', async () => {
    await expect(verifyMercadoPagoWebhook(webhook(), 'another-fixture-secret', NOW, 10))
      .rejects.toMatchObject({ code: 'webhook_signature' });
  });

  it('detiene el stream de un aviso firmado que supera el límite', async () => {
    const signed = webhook();
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array(32 * 1024)); },
      cancel() { cancelled = true; },
    });
    const oversized = new Request(signed.url, {
      method: 'POST', headers: signed.headers, body: stream, duplex: 'half',
    } as RequestInit);
    await expect(verifyMercadoPagoWebhook(oversized, SECRET, NOW, 10))
      .rejects.toMatchObject({ code: 'webhook_payload' });
    expect(cancelled).toBe(true);
  });

  it('acepta un pago con ID numérico en el cuerpo y preserva la evidencia de entorno', async () => {
    const request = webhook({
      id: PAYMENT,
      body: { type: 'payment', data: { id: Number(PAYMENT) }, live_mode: true },
    });
    const forPayment = new Request(request.url.replace('type=subscription_preapproval', 'type=payment'), request);
    await expect(verifyMercadoPagoWebhook(forPayment, SECRET, NOW, 10)).resolves.toEqual({
      topic: 'payment', resourceId: PAYMENT, requestId: REQUEST,
      timestampSeconds: Number(TS), notificationId: null, applicationId: null,
      merchantId: null, liveMode: true,
    });
  });
});

it('crea un único mandato pendiente sólo tras identificar al vendedor de prueba chileno', async () => {
  const calls: string[] = [];
  const api = client((url, init) => {
    calls.push(url.pathname);
    if (url.pathname === '/users/me') return Response.json(SELLER);
    expect(init?.method).toBe('POST');
    expect(new Headers(init?.headers).has('x-idempotency-key')).toBe(false);
    expect(JSON.parse(String(init?.body))).toEqual({
      status: 'pending', external_reference: REF, reason: INPUT.reason,
      payer_email: INPUT.payerEmail, back_url: INPUT.backUrl,
      auto_recurring: {
        currency_id: 'CLP', transaction_amount: 5000, frequency: 1, frequency_type: 'months',
      },
    });
    return Response.json(SUBSCRIPTION, { status: 201 });
  });
  await expect(api.createPending({ ...INPUT, card_token_id: 'must-not-be-sent' } as typeof INPUT))
    .resolves.toEqual({
      id: SUB, reference: REF, status: 'pending', checkoutUrl: CHECKOUT, amountClp: 5000,
      frequency: 1, frequencyType: 'months', updatedAt: Math.floor(Date.parse(SUBSCRIPTION.last_modified) / 1000),
      version: 0,
    });
  expect(calls).toEqual(['/users/me', '/preapproval']);
});

it.each([
  { ...SELLER, tags: [] },
  { ...SELLER, id: 900 },
  { ...SELLER, site_id: 'MLA' },
])('nunca envía el POST cuando no se acredita la identidad sandbox %j', async (seller) => {
  const paths: string[] = [];
  const api = client((url) => {
    paths.push(url.pathname);
    return Response.json(seller);
  });
  await expect(api.createPending(INPUT)).rejects.toMatchObject({ code: 'sandbox_unverified' });
  expect(paths).toEqual(['/users/me']);
});

it.each([
  { init_point: CHECKOUT.replace('www.mercadopago.cl', 'mercadopago.cl.invalid') },
  { init_point: CHECKOUT.replace('/subscriptions/checkout', '/elsewhere') },
  { init_point: CHECKOUT + '&preapproval_id=other' },
  { init_point: CHECKOUT + '&activation=false' },
  { init_point: CHECKOUT + '&activation=true&activation=true' },
  { init_point: CHECKOUT + '&activation=true&other=1' },
  { status: 'authorized' },
  { application_id: 123 },
  { external_reference: 'wrong-attempt' },
  { auto_recurring: { ...SUBSCRIPTION.auto_recurring, currency_id: 'ARS' } },
])('rechaza inconsistencias de vínculo e importe posalta sin reenviar %#', async (changes) => {
  let posts = 0;
  const api = client((url) => {
    if (url.pathname === '/users/me') return Response.json(SELLER);
    posts += 1;
    return Response.json({ ...SUBSCRIPTION, ...changes }, { status: 201 });
  });
  await expect(api.createPending(INPUT)).rejects.toBeInstanceOf(MercadoPagoError);
  expect(posts).toBe(1);
});

it('admite activation=true del checkout alojado de Suscripciones en Chile', async () => {
  const checkout = CHECKOUT + '&activation=true';
  await expect(client(() => Response.json({ ...SUBSCRIPTION, init_point: checkout })).getSubscription(SUB))
    .resolves.toMatchObject({ checkoutUrl: checkout });
});

it('permite reconciliar sin pin de aplicación y exige el mismo ID que se consultó', async () => {
  const api = client((url) => {
    expect(url.pathname).toBe('/preapproval/' + SUB);
    return Response.json({ ...SUBSCRIPTION, status: 'authorized', application_id: undefined });
  }, { accessToken: TOKEN, environment: 'sandbox', merchantId: MERCHANT });
  await expect(api.getSubscription(SUB)).resolves.toMatchObject({ id: SUB, status: 'authorized' });
  await expect(client(() => Response.json({ ...SUBSCRIPTION, id: 'other' })).getSubscription(SUB))
    .rejects.toMatchObject({ code: 'provider_binding' });
});

it('consulta factura y pago por sus IDs separados y nunca interpreta factura como liquidación', async () => {
  const api = client((url) => (
    url.pathname === '/authorized_payments/' + INVOICE ? Response.json(INVOICE_BODY) : Response.json(PAYMENT_BODY)
  ));
  await expect(api.getInvoice(INVOICE)).resolves.toEqual({
    id: INVOICE, subscriptionId: SUB, paymentId: PAYMENT, reference: null, amountClp: 5000,
    status: 'scheduled', paymentStatus: 'approved', updatedAt: Date.parse(INVOICE_BODY.last_modified) / 1000,
  });
  await expect(api.getPayment(PAYMENT)).resolves.toMatchObject({
    id: PAYMENT, liveMode: false, status: 'approved', amountClp: 5000, reference: null,
  });
  await expect(client(() => Response.json({ ...PAYMENT_BODY, live_mode: true })).getPayment(PAYMENT))
    .rejects.toMatchObject({ code: 'provider_binding' });
});

it('recorre páginas, vincula referencia exacta y rechaza dos suscripciones candidatas', async () => {
  const offsets: string[] = [];
  let duplicate = false;
  const api = client((url) => {
    expect(url.pathname).toBe('/preapproval/search');
    expect(url.searchParams.get('payer_email')).toBe(INPUT.payerEmail);
    const offset = url.searchParams.get('offset')!;
    offsets.push(offset);
    const row = offset === '0' ? { ...SUBSCRIPTION, id: 'other',
      external_reference: duplicate ? REF : 'another-attempt' } : SUBSCRIPTION;
    return Response.json({ paging: { offset: Number(offset), limit: 1, total: 2 }, results: [row] });
  });
  await expect(api.findSubscription({ payerEmail: INPUT.payerEmail, reference: REF }))
    .resolves.toMatchObject({ id: SUB });
  expect(offsets).toEqual(['0', '1']);
  duplicate = true;
  await expect(api.findSubscription({ payerEmail: INPUT.payerEmail, reference: REF }))
    .rejects.toMatchObject({ code: 'provider_ambiguous' });
});

it('proyecta factura por payment_id y rechaza una respuesta 403 sin filtrar contenido', async () => {
  const api = client((url) => {
    expect(url.pathname).toBe('/authorized_payments/search');
    expect(url.searchParams.get('payment_id')).toBe(PAYMENT);
    return Response.json({ paging: { offset: 0, limit: 20, total: 1 }, results: [INVOICE_BODY] });
  });
  await expect(api.findInvoiceByPayment(PAYMENT)).resolves.toMatchObject({ id: INVOICE });
  await expect(client(() => new Response('proveedor con datos privados', { status: 403 })).getPayment(PAYMENT))
    .rejects.toMatchObject({ status: 403, code: 'provider_http', message: 'provider_http' });
});

it('falla con transporte ambiguo sin emitir otro POST ni revelar su excepción', async () => {
  let posts = 0;
  const api = client((url) => {
    if (url.pathname === '/users/me') return Response.json(SELLER);
    posts += 1;
    throw new Error('private transport data');
  });
  await expect(api.createPending(INPUT)).rejects.toMatchObject({
    code: 'provider_transport', status: null, message: 'provider_transport',
  });
  expect(posts).toBe(1);
});

it('invoca el transporte sin usar el cliente de Mercado Pago como receptor de fetch', async () => {
  function transport(this: unknown) {
    expect(this).toBeUndefined();
    return Promise.resolve(Response.json({ paging: { offset: 0, limit: 20, total: 0 }, results: [] }));
  }
  const api = new MercadoPagoClient({ environment: 'sandbox', accessToken: TOKEN,
    merchantId: MERCHANT, applicationId: APP }, transport);
  await expect(api.findSubscription({ payerEmail: INPUT.payerEmail, reference: REF })).resolves.toBeNull();
});

it('no deduce ausencia si el catálogo paginado cambió durante la reconciliación', async () => {
  const api = client((url) => {
    const offset = Number(url.searchParams.get('offset'));
    return Response.json({
      paging: { offset, limit: 1, total: offset === 0 ? 2 : 3 },
      results: [{ ...SUBSCRIPTION, external_reference: 'otro' }],
    });
  });
  await expect(api.findSubscription({ payerEmail: INPUT.payerEmail, reference: REF }))
    .rejects.toMatchObject({ code: 'provider_pagination' });
});

it('rechaza rutas y URL de regreso ajenas antes de enviar solicitudes', async () => {
  const api = client(() => { throw new Error('Fixture no debe contactar API'); });
  await expect(api.getSubscription('../users/me')).rejects.toMatchObject({ code: 'invalid_input' });
  await expect(api.createPending({ ...INPUT, backUrl: 'https://studio.e-scale.cl.invalid/' }))
    .rejects.toMatchObject({ code: 'invalid_input' });
  await expect(api.getSubscription(SUB + '\n')).rejects.toMatchObject({ code: 'invalid_input' });
  await expect(api.createPending({ ...INPUT, reference: REF + '\n' }))
    .rejects.toMatchObject({ code: 'invalid_input' });
});
