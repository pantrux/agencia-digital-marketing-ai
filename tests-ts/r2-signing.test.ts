import { describe, expect, it, vi } from 'vitest';
import { signR2Request, type R2Credentials, type R2Method } from '../src/media/r2-signing.js';

// Credenciales públicas de prueba, sin relación con la cuenta operativa.
const credentials = (): R2Credentials => ({ accountId: '1'.repeat(32), accessKeyId: 'a'.repeat(32), secretAccessKey: 'b'.repeat(64) });
const policy = { maxObjectBytes: 12345 };
const job = () => ({
  job_id: '1e0b3af5-2f67-4bf0-8a20-4b7d0b1c2d3e',
  asset_id: '2f1c4b06-3a78-4ca1-9b31-5c8e1c2d3e4f',
  project_slug: 'campana-2026', extension: 'mp4', bytes: 12345,
  sha256: 'c'.repeat(64),
  object_key: 'ingest/1e0b3af5-2f67-4bf0-8a20-4b7d0b1c2d3e/2f1c4b06-3a78-4ca1-9b31-5c8e1c2d3e4f.mp4',
});
const amzDate = '20261106T120000Z';
const emptyHash = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
// Vectores calculados con hashlib/hmac de Python stdlib, antes de implementar TS.
const signatures = {
  GET: '787cda65a9f2408e067b767830920a7e49625f2a3beb971b3c945807150b654c',
  HEAD: '64ab793e8654ceecc257af080e852d636b8a6a0cb92d16a648ca124c771e1949',
};

describe('Descriptor puro R2 SigV4', () => {
  it.each(['GET', 'HEAD'] as const)('coincide con el vector independiente %s', async (method) => {
    const result = await signR2Request(method, job(), policy, credentials(), amzDate);
    const host = '1'.repeat(32) + '.r2.cloudflarestorage.com';
    expect(result.method).toBe(method);
    expect(result.url).toBe(`https://${host}/studio-media-bucket/${job().object_key}`);
    expect(result.headers).toEqual({
      Host: host, 'x-amz-date': amzDate, 'x-amz-content-sha256': emptyHash,
      Authorization: `AWS4-HMAC-SHA256 Credential=${'a'.repeat(32)}/20261106/auto/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=${signatures[method]}`,
    });
    expect(Object.isFrozen(result)).toBe(true);
    expect(Object.isFrozen(result.headers)).toBe(true);
  });

  it.each(['POST', 'get', 'GET\n', 'DELETE'])('rechaza método %j', async (method) => {
    await expect(signR2Request(method as R2Method, job(), policy, credentials(), amzDate)).rejects.toThrow('METODO');
  });

  it.each(['', '20260229T120000Z', '20260231T120000Z', '20260431T120000Z', '20261301T120000Z', '20260001T120000Z', '20261100T120000Z', '20261106T240000Z', '20261106T126000Z', '20261106T120060Z', '20261106t120000z', '2026-11-06T12:00:00Z', '20261106T120000Z\n', '20261106T120000+0000', '00001106T120000Z'])(
    'rechaza fecha UTC inválida o no canónica %j', async (date) => {
      await expect(signR2Request('GET', job(), policy, credentials(), date)).rejects.toThrow('FECHA');
    },
  );

  it('acepta el día bisiesto real y vincula el scope a ese día UTC', async () => {
    const result = await signR2Request('HEAD', job(), policy, credentials(), '20240229T235959Z');
    expect(result.headers['x-amz-date']).toBe('20240229T235959Z');
    expect(result.headers.Authorization).toContain('/20240229/auto/s3/aws4_request');
    await expect(signR2Request('HEAD', job(), policy, credentials(), '19000229T235959Z')).rejects.toThrow('FECHA');
  });

  it.each(['https://ajeno.invalid', '1'.repeat(32) + '@ajeno.invalid', '1'.repeat(32) + ':443', 'A'.repeat(32), '1'.repeat(31), '1'.repeat(32) + '\r\nX: uno'])(
    'rechaza inyección o formato inválido del endpoint %j', async (accountId) => {
      await expect(signR2Request('GET', job(), policy, { ...credentials(), accountId }, amzDate)).rejects.toThrow('CREDENCIALES');
    },
  );

  it('rechaza credenciales vacías, controles, Unicode y campos de destino adicionales', async () => {
    for (const field of ['accessKeyId', 'secretAccessKey'] as const) {
      for (const invalid of ['', '\r\nHost: ajeno', 'uno\tdos', 'con espacio', 'á']) {
        await expect(signR2Request('GET', job(), policy, { ...credentials(), [field]: invalid }, amzDate)).rejects.toThrow('CREDENCIALES');
      }
    }
    for (const extra of [{ host: 'ajeno.invalid' }, { bucket: 'otro' }, { url: 'https://ajeno.invalid/' }]) {
      await expect(signR2Request('GET', job(), policy, { ...credentials(), ...extra }, amzDate)).rejects.toThrow('CREDENCIALES');
    }
  });

  it('rechaza registros incompletos y accesores sin ejecutar la entrada', async () => {
    for (const invalid of [null, [], {}, 'dato', { accountId: credentials().accountId }]) {
      await expect(signR2Request('GET', job(), policy, invalid as unknown as R2Credentials, amzDate)).rejects.toThrow('CREDENCIALES');
    }
    const accessor = { ...credentials() };
    Object.defineProperty(accessor, 'secretAccessKey', { get: () => { throw new Error('accesor privado'); } });
    await expect(signR2Request('GET', job(), policy, accessor, amzDate)).rejects.toThrow('CREDENCIALES');
  });

  it('acota los campos de credenciales sin recortar su contenido', async () => {
    await expect(signR2Request('GET', job(), policy, { ...credentials(), accessKeyId: 'a'.repeat(129) }, amzDate)).rejects.toThrow('CREDENCIALES');
    await expect(signR2Request('GET', job(), policy, { ...credentials(), secretAccessKey: 'b'.repeat(257) }, amzDate)).rejects.toThrow('CREDENCIALES');
  });

  it.each(['../otro', 'https://ajeno.invalid/archivo', job().object_key + '?x=1', job().object_key.replace('ingest/', 'ingest/%2e%2e/')])(
    'rechaza object_key malformada %j', async (object_key) => {
      await expect(signR2Request('GET', { ...job(), object_key }, policy, credentials(), amzDate)).rejects.toThrow('OBJETO');
    },
  );

  it('revalida el trabajo y exige la política de tamaño', async () => {
    await expect(signR2Request('GET', job(), undefined as unknown as typeof policy, credentials(), amzDate)).rejects.toThrow('POLITICA');
    await expect(signR2Request('GET', job(), { maxObjectBytes: 1 }, credentials(), amzDate)).rejects.toThrow('BYTES');
    await expect(signR2Request('GET', { ...job(), url: 'https://ajeno.invalid/' }, policy, credentials(), amzDate)).rejects.toThrow('ESQUEMA');
  });

  it('redacta Authorization en JSON sin retornar el secreto', async () => {
    const result = await signR2Request('GET', job(), policy, credentials(), amzDate);
    const json = JSON.stringify(result);
    expect(JSON.parse(json).headers.Authorization).toBe('[REDACTADO]');
    expect(json).not.toContain(credentials().secretAccessKey);
    expect(json).not.toContain(signatures.GET);
    expect(Object.keys(result)).not.toContain('secretAccessKey');
  });

  it('captura los argumentos antes de los awaits criptográficos', async () => {
    const mutable = { ...credentials() };
    const pending = signR2Request('GET', job(), policy, mutable, amzDate);
    mutable.accountId = '2'.repeat(32); mutable.secretAccessKey = 'c'.repeat(64);
    expect((await pending).headers.Authorization).toContain(signatures.GET);
  });

  it('importa cada clave de derivación no extraíble', async () => {
    const spy = vi.spyOn(crypto.subtle, 'importKey');
    try {
      await signR2Request('GET', job(), policy, credentials(), amzDate);
      expect(spy.mock.results.length).toBeGreaterThan(0);
      for (const result of spy.mock.results) {
        if (result.type !== 'return') throw new Error('Importación fallida');
        const key = await result.value;
        expect(key.extractable).toBe(false);
        await expect(crypto.subtle.exportKey('raw', key)).rejects.toThrow();
      }
    } finally { spy.mockRestore(); }
  });

  it('no expone credenciales ni firmas cuando falla el proveedor criptográfico', async () => {
    const spy = vi.spyOn(crypto.subtle, 'sign').mockRejectedValueOnce(new Error(credentials().secretAccessKey + signatures.GET));
    try {
      let error: unknown;
      try { await signR2Request('GET', job(), policy, credentials(), amzDate); } catch (caught) { error = caught; }
      expect(String(error)).toContain('CRIPTOGRAFIA');
      expect(String(error)).not.toContain(credentials().secretAccessKey);
      expect(JSON.stringify(error)).not.toContain(signatures.GET);
    } finally { spy.mockRestore(); }
  });
});
