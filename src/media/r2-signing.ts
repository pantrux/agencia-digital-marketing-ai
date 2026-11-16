/** Descriptor SigV4 puro: no usa fetch, SDK, entorno ni filesystem. */
import { validateSyncJob, type SyncPolicy } from './sync-contract.js';

export type R2Method = 'GET' | 'HEAD';
export interface R2Credentials {
  readonly accountId: string;
  readonly accessKeyId: string;
  readonly secretAccessKey: string;
}
export interface R2Headers {
  readonly Host: string;
  readonly 'x-amz-date': string;
  readonly 'x-amz-content-sha256': string;
  readonly Authorization: string;
}
export interface R2Diagnostic {
  readonly method: R2Method;
  readonly url: string;
  readonly headers: Readonly<R2Headers>;
}
export interface R2RequestDescriptor extends R2Diagnostic {
  toJSON(): Readonly<R2Diagnostic>;
}

export class R2SigningError extends Error {
  constructor(readonly code: string) {
    super(`Descriptor R2 rechazado: ${code}.`);
    this.name = 'R2SigningError';
  }
}

const encoder = new TextEncoder();
const emptyHash = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
const signedHeaders = 'host;x-amz-content-sha256;x-amz-date';
const credentialFields = ['accountId', 'accessKeyId', 'secretAccessKey'];

function fail(code: string): never { throw new R2SigningError(code); }

function exact(value: unknown, pattern: RegExp): value is string {
  return typeof value === 'string' && value.match(pattern)?.[0] === value;
}

function credentialsSnapshot(value: R2Credentials): Readonly<R2Credentials> {
  if (typeof value !== 'object' || value === null) fail('CREDENCIALES');
  const keys = Reflect.ownKeys(value);
  if (keys.length !== credentialFields.length || keys.some((key) => typeof key !== 'string' || !credentialFields.includes(key))) fail('CREDENCIALES');
  const data: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
  for (const field of credentialFields) {
    const property = Object.getOwnPropertyDescriptor(value, field);
    if (!property || !('value' in property)) fail('CREDENCIALES');
    data[field] = property.value;
  }
  if (!exact(data.accountId, /^[0-9a-f]{32}$/u) ||
      !exact(data.accessKeyId, /^[A-Za-z0-9]{1,128}$/u) ||
      !exact(data.secretAccessKey, /^[\x21-\x7e]{1,256}$/u)) fail('CREDENCIALES');
  return Object.freeze({ accountId: data.accountId, accessKeyId: data.accessKeyId, secretAccessKey: data.secretAccessKey });
}

function checkedDate(value: string): string {
  if (!exact(value, /^[0-9]{8}T[0-9]{6}Z$/u) || value.startsWith('0000')) fail('FECHA');
  const iso = `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)}T${value.slice(9, 11)}:${value.slice(11, 13)}:${value.slice(13, 15)}.000Z`;
  const parsed = new Date(iso);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString() !== iso) fail('FECHA');
  return value;
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function hmac(keyBytes: Uint8Array<ArrayBuffer>, text: string): Promise<Uint8Array<ArrayBuffer>> {
  try {
    const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(text)));
  } finally {
    // Sólo este buffer controlado se limpia; JS no garantiza borrar cadenas/copias.
    keyBytes.fill(0);
  }
}

async function signatureFor(auth: Readonly<R2Credentials>, date: string, canonicalRequest: string): Promise<string> {
  const day = date.slice(0, 8);
  const scope = `${day}/auto/s3/aws4_request`;
  try {
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(canonicalRequest)));
    const toSign = ['AWS4-HMAC-SHA256', date, scope, hex(digest)].join('\n');
    let key = encoder.encode(`AWS4${auth.secretAccessKey}`);
    for (const part of [day, 'auto', 's3', 'aws4_request']) key = await hmac(key, part);
    return hex(await hmac(key, toSign));
  } catch { return fail('CRIPTOGRAFIA'); }
}

function descriptor(method: R2Method, url: string, headers: Readonly<R2Headers>): Readonly<R2RequestDescriptor> {
  const diagnostic = Object.freeze({ method, url, headers: Object.freeze({ ...headers, Authorization: '[REDACTADO]' }) });
  return Object.freeze({ method, url, headers, toJSON: () => diagnostic });
}

export async function signR2Request(method: R2Method, value: unknown, policy: SyncPolicy, credentials: R2Credentials, amzDate: string): Promise<Readonly<R2RequestDescriptor>> {
  if (method !== 'GET' && method !== 'HEAD') fail('METODO');
  // Capturar toda entrada antes del primer await y reutilizar el contrato cerrado.
  const job = validateSyncJob(value, policy);
  const auth = credentialsSnapshot(credentials);
  const date = checkedDate(amzDate);
  const day = date.slice(0, 8);
  const host = `${auth.accountId}.r2.cloudflarestorage.com`;
  // El esquema del trabajo deja sólo ASCII no reservado y separadores fijos.
  const canonicalUri = `/studio-media-bucket/${job.object_key}`;
  const scope = `${day}/auto/s3/aws4_request`;
  const canonicalHeaders = `host:${host}\nx-amz-content-sha256:${emptyHash}\nx-amz-date:${date}\n`;
  const canonicalRequest = [method, canonicalUri, '', canonicalHeaders, signedHeaders, emptyHash].join('\n');
  const signature = await signatureFor(auth, date, canonicalRequest);
  const headers = Object.freeze({
    Host: host,
    'x-amz-date': date,
    'x-amz-content-sha256': emptyHash,
    Authorization: `AWS4-HMAC-SHA256 Credential=${auth.accessKeyId}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
  });
  return descriptor(method, `https://${host}${canonicalUri}`, headers);
}

export interface R2UploadPartInput {
  readonly uploadId: string;
  readonly partNumber: number;
  readonly bytes: number;
  readonly mimeType: string;
  readonly expires: number;
}

function s3Query(params: Record<string, string>): string {
  return Object.keys(params).sort().map((key) => `${encodeURIComponent(key)}=${encodeURIComponent(params[key]!).replace(/[!'()*]/gu,
    (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`)}`).join('&');
}

export async function signR2UploadPart(
  value: unknown, policy: SyncPolicy, credentials: R2Credentials, amzDate: string, input: R2UploadPartInput,
): Promise<{readonly url: string; readonly headers: Readonly<Record<string, string>>}> {
  const job = validateSyncJob(value, policy);
  const { uploadId, partNumber, bytes, mimeType, expires } = input;
  if (!exact(uploadId, /^[\x21-\x7e]{1,2048}$/u)
    || !Number.isSafeInteger(partNumber) || partNumber < 1 || partNumber > 10_000
    || !Number.isSafeInteger(bytes) || bytes < 1 || bytes > job.bytes
    || !exact(mimeType, /^[a-z0-9.+-]{1,32}\/[a-z0-9.+-]{1,64}$/u)
    || !Number.isSafeInteger(expires) || expires < 1 || expires > 300) fail('MULTIPART');
  const auth = credentialsSnapshot(credentials);
  const date = checkedDate(amzDate);
  const host = `${auth.accountId}.r2.cloudflarestorage.com`;
  const uri = `/studio-media-bucket/${job.object_key}`;
  const scope = `${date.slice(0, 8)}/auto/s3/aws4_request`;
  // El navegador fija Content-Length al transmitir el Blob. El cliente sólo añade Content-Type.
  const headerNames = 'content-length;content-type;host';
  const headers = `content-length:${bytes}\ncontent-type:${mimeType}\nhost:${host}\n`;
  const query = s3Query({
    'X-Amz-Algorithm': 'AWS4-HMAC-SHA256', 'X-Amz-Credential': `${auth.accessKeyId}/${scope}`,
    'X-Amz-Date': date, 'X-Amz-Expires': String(expires), 'X-Amz-SignedHeaders': headerNames,
    partNumber: String(partNumber), uploadId,
  });
  const canonical = ['PUT', uri, query, headers, headerNames, 'UNSIGNED-PAYLOAD'].join('\n');
  const signature = await signatureFor(auth, date, canonical);
  return Object.freeze({
    url: `https://${host}${uri}?${query}&X-Amz-Signature=${signature}`,
    headers: Object.freeze({ 'Content-Type': mimeType }),
  });
}

export async function signR2ListParts(
  value: unknown, policy: SyncPolicy, credentials: R2Credentials, amzDate: string, uploadId: string,
): Promise<Readonly<R2RequestDescriptor>> {
  if (!exact(uploadId, /^[\x21-\x7e]{1,2048}$/u)) fail('MULTIPART');
  const job = validateSyncJob(value, policy);
  const auth = credentialsSnapshot(credentials);
  const date = checkedDate(amzDate);
  const host = `${auth.accountId}.r2.cloudflarestorage.com`;
  const uri = `/studio-media-bucket/${job.object_key}`;
  const scope = `${date.slice(0, 8)}/auto/s3/aws4_request`;
  const query = s3Query({ 'max-parts': '100', uploadId });
  const canonicalHeaders = `host:${host}\nx-amz-content-sha256:${emptyHash}\nx-amz-date:${date}\n`;
  const canonical = ['GET', uri, query, canonicalHeaders, signedHeaders, emptyHash].join('\n');
  const signature = await signatureFor(auth, date, canonical);
  return descriptor('GET', `https://${host}${uri}?${query}`, Object.freeze({
    Host: host, 'x-amz-date': date, 'x-amz-content-sha256': emptyHash,
    Authorization: `AWS4-HMAC-SHA256 Credential=${auth.accessKeyId}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
  }));
}
