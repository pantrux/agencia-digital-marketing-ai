/** Contrato puro R2 v1; no realiza transporte, persistencia ni acceso al disco. */
export interface SyncPolicy { readonly maxObjectBytes: number }
export interface SyncJob {
  readonly job_id: string;
  readonly asset_id: string;
  readonly project_slug: string;
  readonly extension: string;
  readonly bytes: number;
  readonly sha256: string;
  readonly object_key: string;
}
export interface SignedSyncRequest {
  readonly body: Uint8Array;
  readonly timestamp: string;
  readonly signature: string;
}
export interface VerifiedSyncJob {
  readonly job: Readonly<SyncJob>;
  readonly bodySha256: string;
  readonly destinationRelativePath: string;
}

export class SyncContractError extends Error {
  constructor(readonly code: string) {
    super(`Solicitud de sincronización rechazada: ${code}.`);
    this.name = 'SyncContractError';
  }
}

const fields = ['job_id', 'asset_id', 'project_slug', 'extension', 'bytes', 'sha256', 'object_key'];
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const digest = /^[0-9a-f]{64}$/u;
const slug = /^[a-z0-9][a-z0-9_-]{0,62}$/u;
const reserved = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$/u;
const encoder = new TextEncoder();
export const MAX_SYNC_BODY_BYTES = 4096;
export const SYNC_TIME_TOLERANCE_SECONDS = 300;

function fail(code: string): never { throw new SyncContractError(code); }

// `$` de JavaScript también coincide antes de un salto final: exigir toda la cadena.
function matches(value: unknown, pattern: RegExp): value is string {
  return typeof value === 'string' && value.match(pattern)?.[0] === value;
}

function checkPolicy(policy: SyncPolicy): void {
  if (!policy || !Number.isSafeInteger(policy.maxObjectBytes) || policy.maxObjectBytes <= 0) {
    fail('POLITICA');
  }
}

export function validateSyncJob(value: unknown, policy: SyncPolicy): Readonly<SyncJob> {
  checkPolicy(policy);
  if (typeof value !== 'object' || value === null || Array.isArray(value)) fail('ESQUEMA');
  const keys = Reflect.ownKeys(value);
  if (keys.length !== fields.length || keys.some((field) => typeof field !== 'string' || !fields.includes(field))) {
    fail('ESQUEMA');
  }
  const data: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
  for (const field of fields) {
    const descriptor = Object.getOwnPropertyDescriptor(value, field);
    if (!descriptor || !('value' in descriptor)) fail('ESQUEMA');
    data[field] = descriptor.value;
  }
  if (!matches(data.job_id, uuid) || !matches(data.asset_id, uuid)) fail('UUID');
  if (!matches(data.project_slug, slug) || matches(data.project_slug, reserved)) fail('PROYECTO');
  if (!matches(data.extension, /^[a-z0-9]{1,10}$/u)) fail('EXTENSION');
  if (typeof data.bytes !== 'number' || !Number.isSafeInteger(data.bytes) || data.bytes <= 0 || data.bytes > policy.maxObjectBytes) fail('BYTES');
  if (!matches(data.sha256, digest)) fail('SHA256');
  const expectedKey = `ingest/${data.job_id}/${data.asset_id}.${data.extension}`;
  if (data.object_key !== expectedKey) fail('OBJETO');
  // El orden literal define los bytes del protocolo, sin normalizar la entrada.
  return Object.freeze({
    job_id: data.job_id, asset_id: data.asset_id, project_slug: data.project_slug,
    extension: data.extension, bytes: data.bytes, sha256: data.sha256, object_key: expectedKey,
  });
}

export function encodeSyncJob(value: unknown, policy: SyncPolicy): Uint8Array<ArrayBuffer> {
  const bytes = encoder.encode(JSON.stringify(validateSyncJob(value, policy)));
  if (bytes.byteLength > MAX_SYNC_BODY_BYTES) fail('CUERPO');
  return bytes;
}

function snapshot(body: Uint8Array): Uint8Array<ArrayBuffer> {
  if (!(body instanceof Uint8Array) || body.byteLength === 0 || body.byteLength > MAX_SYNC_BODY_BYTES) fail('CUERPO');
  return Uint8Array.from(body);
}

export function parseSyncBody(body: Uint8Array, policy: SyncPolicy): Readonly<SyncJob> {
  const original = snapshot(body);
  let value: unknown;
  try {
    value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(original));
  } catch { return fail('JSON_UTF8'); }
  const job = validateSyncJob(value, policy);
  const canonical = encodeSyncJob(job, policy);
  // La comparación es de cuerpo público; el MAC siempre usa verify nativo.
  if (original.length !== canonical.length || original.some((byte, index) => byte !== canonical[index])) fail('CANONICO');
  return job;
}

function checkKey(key: CryptoKey, usage: 'sign' | 'verify'): void {
  const algorithm = key?.algorithm as HmacKeyAlgorithm | undefined;
  if (!key || key.type !== 'secret' || key.extractable !== false || algorithm?.name !== 'HMAC' ||
      algorithm.hash?.name !== 'SHA-256' || !Number.isInteger(algorithm.length) ||
      algorithm.length < 256 || algorithm.length > 1024 || !key.usages.includes(usage)) fail('CLAVE');
}

export async function importSyncKey(raw: Uint8Array): Promise<CryptoKey> {
  if (!(raw instanceof Uint8Array) || raw.byteLength < 32 || raw.byteLength > 128) fail('CLAVE');
  try {
    return await crypto.subtle.importKey('raw', Uint8Array.from(raw), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
  } catch { return fail('CLAVE'); }
}

function checkTimestamp(value: string): number {
  if (!matches(value, /^(?:0|[1-9][0-9]{0,15})$/u)) fail('TIMESTAMP');
  const seconds = Number(value);
  if (!Number.isSafeInteger(seconds)) fail('TIMESTAMP');
  return seconds;
}

function hex(data: ArrayBuffer): string {
  return Array.from(new Uint8Array(data), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function bodyHash(body: Uint8Array<ArrayBuffer>): Promise<string> {
  return hex(await crypto.subtle.digest('SHA-256', body));
}

function signedMessage(timestamp: string, hash: string): Uint8Array<ArrayBuffer> {
  return encoder.encode(`v1\n${timestamp}\nPOST\n/v1/sync\n${hash}`);
}

export async function signSyncJob(value: unknown, policy: SyncPolicy, key: CryptoKey, timestamp: string): Promise<SignedSyncRequest & { readonly bodySha256: string }> {
  const body = encodeSyncJob(value, policy);
  checkTimestamp(timestamp);
  checkKey(key, 'sign');
  const bodySha256 = await bodyHash(body);
  let mac: ArrayBuffer;
  try { mac = await crypto.subtle.sign('HMAC', key, signedMessage(timestamp, bodySha256)); }
  catch { return fail('FIRMA'); }
  return Object.freeze({ body, timestamp, signature: `v1=${hex(mac)}`, bodySha256 });
}

/** El dominio v2 impide reutilizar una firma de conjuntos en el protocolo anterior. */
export async function signSourceSyncJob(value: unknown, policy: SyncPolicy, key: CryptoKey, timestamp: string): Promise<SignedSyncRequest & { readonly bodySha256: string }> {
  const body = encodeSyncJob(value, policy);
  checkTimestamp(timestamp);
  checkKey(key, 'sign');
  const bodySha256 = await bodyHash(body);
  let mac: ArrayBuffer;
  try { mac = await crypto.subtle.sign('HMAC', key, encoder.encode(`v2\n${timestamp}\nPOST\n/v2/sync\n${bodySha256}`)); }
  catch { return fail('FIRMA'); }
  return Object.freeze({ body, timestamp, signature: `v2=${hex(mac)}`, bodySha256 });
}

export async function verifySyncRequest(request: SignedSyncRequest, policy: SyncPolicy, key: CryptoKey, nowSeconds: () => number): Promise<Readonly<VerifiedSyncJob>> {
  // Copiar antes del primer await evita que el llamador cambie los bytes autenticados.
  const body = snapshot(request.body);
  const job = parseSyncBody(body, policy);
  const timestamp = request.timestamp;
  const signature = request.signature;
  const seconds = checkTimestamp(timestamp);
  if (!matches(signature, /^v1=[0-9a-f]{64}$/u)) fail('FIRMA');
  let now: number;
  try { now = nowSeconds(); } catch { return fail('RELOJ'); }
  if (!Number.isSafeInteger(now) || now < 0 || Math.abs(now - seconds) > SYNC_TIME_TOLERANCE_SECONDS) fail('RELOJ');
  checkKey(key, 'verify');
  const bodySha256 = await bodyHash(body);
  const mac = new Uint8Array(32);
  for (let index = 0; index < mac.length; index++) mac[index] = Number.parseInt(signature.slice(3 + index * 2, 5 + index * 2), 16);
  let authenticated: boolean;
  try { authenticated = await crypto.subtle.verify('HMAC', key, mac, signedMessage(timestamp, bodySha256)); }
  catch { return fail('FIRMA'); }
  if (!authenticated) fail('FIRMA');
  return Object.freeze({
    job, bodySha256,
    destinationRelativePath: `proyectos/${job.project_slug}/01_raw/${job.asset_id}.${job.extension}`,
  });
}
