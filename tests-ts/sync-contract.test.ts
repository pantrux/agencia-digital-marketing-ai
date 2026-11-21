import { beforeAll, describe, expect, it, vi } from 'vitest';
import {
  encodeSyncJob, importSyncKey, parseSyncBody, signSyncJob, signSourceSyncJob,
  validateSyncJob, verifySyncRequest,
} from '../src/media/sync-contract.js';

// Vector público de prueba: bytes consecutivos, sin credenciales reales.
const rawKey = Uint8Array.from({ length: 32 }, (_, index) => index);
const policy = { maxObjectBytes: 12345 };
const timestamp = '1700000000';
const fixture = () => ({
  job_id: '1e0b3af5-2f67-4bf0-8a20-4b7d0b1c2d3e',
  asset_id: '2f1c4b06-3a78-4ca1-9b31-5c8e1c2d3e4f',
  project_slug: 'campana-2026', extension: 'mp4', bytes: 12345,
  sha256: 'a'.repeat(64),
  object_key: 'ingest/1e0b3af5-2f67-4bf0-8a20-4b7d0b1c2d3e/2f1c4b06-3a78-4ca1-9b31-5c8e1c2d3e4f.mp4',
});
const canonical = JSON.stringify(fixture());
const body = () => new TextEncoder().encode(canonical);
// Calculado independientemente con node:crypto createHash/createHmac.
const fingerprint = 'd46ba757c04bdbc4e20dd939a555409f753b98258554fb6ba22521eedd94b7fd';
const signature = 'v1=75b234bd0eec94e62ce7759905041298cdc510d584bddc27bcf2686dce70e4b3';
let key: CryptoKey;
beforeAll(async () => { key = await importSyncKey(rawKey); });

describe('Mensaje R2 v1 cerrado y canónico', () => {
  it('firma fuentes múltiples con un dominio separado del protocolo anterior', async () => {
    const signed = await signSourceSyncJob(fixture(), policy, key, timestamp);
    expect(signed.bodySha256).toBe(fingerprint);
    expect(signed.signature).toBe('v2=dcf43d6de3029e24817d533565d8308702727305a616e7670d668a11ddc2b0b9');
    await expect(verifySyncRequest(signed, policy, key, () => Number(timestamp))).rejects.toMatchObject({ code: 'FIRMA' });
  });
  it('ordena los campos, conserva bytes exactos y congela el trabajo', () => {
    const reversed = Object.fromEntries(Object.entries(fixture()).reverse());
    expect(new TextDecoder().decode(encodeSyncJob(reversed, policy))).toBe(canonical);
    const job = parseSyncBody(body(), policy);
    expect(job).toEqual(fixture());
    expect(Object.isFrozen(job)).toBe(true);
  });

  it.each([null, [], 'dato', 1, true, {}, { ...fixture(), url: 'https://ajeno.invalid/' }])(
    'rechaza esquema incorrecto o campos adicionales: %j', (value) => {
      expect(() => validateSyncJob(value, policy)).toThrow();
    },
  );

  it.each(['.', '..', '../a', 'a/b', 'a\\b', '/a', 'a:b', 'A', 'á', 'a.', 'a ', '_a', '-a', 'a\n', 'a'.repeat(64), 'con', 'prn', 'aux', 'nul', 'com1', 'com9', 'lpt1', 'lpt9'])(
    'rechaza un proyecto peligroso: %j', (project_slug) => {
      expect(() => validateSyncJob({ ...fixture(), project_slug }, policy)).toThrow();
    },
  );

  it('admite un slug de 63 caracteres y nombres que no son dispositivos DOS', () => {
    for (const project_slug of ['a'.repeat(63), 'con-proyecto', 'com10', 'a_b-9']) {
      expect(validateSyncJob({ ...fixture(), project_slug }, policy).project_slug).toBe(project_slug);
    }
  });

  it('rechaza accesores de objetos productores sin ejecutar código de la entrada', () => {
    const value = fixture();
    Object.defineProperty(value, 'bytes', { enumerable: true, get: () => { throw new Error('accesor ejecutado'); } });
    expect(() => validateSyncJob(value, policy)).toThrow('ESQUEMA');
  });

  it.each(['MP4', '.mp4', 'a/b', 'mp4\n', '', 'a'.repeat(11)])('rechaza extensión %j', (extension) => {
    expect(() => validateSyncJob({ ...fixture(), extension }, policy)).toThrow();
  });

  it.each(['https://r2.invalid/a', '../a', 'ingest/otro/archivo.mp4', fixture().object_key + '\n'])(
    'rechaza una clave de objeto que no deriva de los UUID: %j', (object_key) => {
      expect(() => validateSyncJob({ ...fixture(), object_key }, policy)).toThrow();
    },
  );

  it.each([0, -1, 0.5, 12346, Number.MAX_SAFE_INTEGER + 1, Infinity, NaN, '12'])(
    'rechaza bytes no autorizados: %j', (bytes) => {
      expect(() => validateSyncJob({ ...fixture(), bytes }, policy)).toThrow();
    },
  );

  it('exige una política válida sin valor por defecto', () => {
    for (const bad of [undefined, null, {}, { maxObjectBytes: 0 }, { maxObjectBytes: 1.5 }, { maxObjectBytes: Infinity }]) {
      expect(() => validateSyncJob(fixture(), bad as typeof policy)).toThrow();
    }
    expect(validateSyncJob(fixture(), policy).bytes).toBe(policy.maxObjectBytes);
  });

  it.each([
    ' ' + canonical, canonical + '\n', canonical.replace('12345', '1.2345e4'),
    canonical.replace('12345', '12345.0'), canonical.replace('campana', '\\u0063ampana'),
    canonical.replace('{', '{"job_id":"ignorado",'),
    JSON.stringify(Object.fromEntries(Object.entries(fixture()).reverse())),
  ])('rechaza otra representación del mismo JSON', (text) => {
    expect(() => parseSyncBody(new TextEncoder().encode(text), policy)).toThrow();
  });

  it('rechaza UTF8 inválido, BOM, JSON truncado y cuerpos mayores a 4096 bytes', () => {
    for (const bytes of [new Uint8Array([0xc3, 0x28]), new Uint8Array([0xef, 0xbb, 0xbf, ...body()]), body().slice(0, -1), new Uint8Array(4097)]) {
      expect(() => parseSyncBody(bytes, policy)).toThrow();
    }
  });

  it('distingue el límite de 4096 bytes de un exceso de tamaño', () => {
    const atLimit = new TextEncoder().encode(canonical + ' '.repeat(4096 - body().length));
    expect(() => parseSyncBody(atLimit, policy)).toThrow('CANONICO');
    expect(() => parseSyncBody(new Uint8Array(4097), policy)).toThrow('CUERPO');
    expect(() => parseSyncBody(new Uint8Array(), policy)).toThrow('CUERPO');
  });

  it('rechaza UUID no v4/variante RFC, mayúsculas, tipos incorrectos y SHA no canónico', () => {
    for (const field of ['job_id', 'asset_id'] as const) {
      for (const invalid of ['1e0b3af5-2f67-3bf0-8a20-4b7d0b1c2d3e', '1e0b3af5-2f67-4bf0-7a20-4b7d0b1c2d3e', fixture()[field].toUpperCase(), fixture()[field] + '\n', null]) {
        expect(() => validateSyncJob({ ...fixture(), [field]: invalid }, policy)).toThrow('UUID');
      }
    }
    for (const sha256 of ['A'.repeat(64), 'a'.repeat(63), 'a'.repeat(64) + '\n', 42]) {
      expect(() => validateSyncJob({ ...fixture(), sha256 }, policy)).toThrow('SHA256');
    }
  });

  it('admite los extremos seguros de número y de extensión', () => {
    const value = fixture(); value.bytes = Number.MAX_SAFE_INTEGER; value.extension = 'a1b2c3d4e5';
    value.object_key = value.object_key.replace('.mp4', '.' + value.extension);
    const maxPolicy = { maxObjectBytes: Number.MAX_SAFE_INTEGER };
    expect(parseSyncBody(encodeSyncJob(value, maxPolicy), maxPolicy)).toEqual(value);
  });

  it('rechaza símbolo extra, campo ausente y destino suministrado por el cliente', () => {
    const { sha256: omitted, ...missing } = fixture();
    expect(omitted).toHaveLength(64);
    for (const value of [missing, { ...fixture(), destinationRelativePath: '/etc/archivo' }, { ...fixture(), [Symbol('extra')]: true }]) {
      expect(() => validateSyncJob(value, policy)).toThrow('ESQUEMA');
    }
  });
});

describe('Autenticación WebCrypto e identidad persistible', () => {
  it('produce y verifica el vector conocido, derivando únicamente el destino permitido', async () => {
    const signed = await signSyncJob(fixture(), policy, key, timestamp);
    expect(signed.signature).toBe(signature);
    expect(signed.bodySha256).toBe(fingerprint);
    expect(signed.body).toEqual(body());
    const result = await verifySyncRequest({ body: body(), timestamp, signature }, policy, key, () => 1700000000);
    expect(result).toEqual({ job: fixture(), bodySha256: fingerprint, destinationRelativePath: 'proyectos/campana-2026/01_raw/2f1c4b06-3a78-4ca1-9b31-5c8e1c2d3e4f.mp4' });
    expect(Object.isFrozen(result.job)).toBe(true);
  });

  it.each([-300, 300])('admite el límite de reloj inclusivo %s s', async (offset) => {
    await expect(verifySyncRequest({ body: body(), timestamp, signature }, policy, key, () => 1700000000 + offset)).resolves.toHaveProperty('bodySha256', fingerprint);
  });

  it.each([-301, 301])('rechaza fuera del margen %s s', async (offset) => {
    await expect(verifySyncRequest({ body: body(), timestamp, signature }, policy, key, () => 1700000000 + offset)).rejects.toThrow();
  });

  it('rechaza MAC alterado, clave distinta y cambios de contenido', async () => {
    const anotherKey = await importSyncKey(Uint8Array.from(rawKey, (value) => value + 1));
    const altered = encodeSyncJob({ ...fixture(), bytes: 12344 }, policy);
    for (const request of [{ body: body(), timestamp, signature: signature.slice(0, -1) + '0' }, { body: altered, timestamp, signature }]) {
      await expect(verifySyncRequest(request, policy, key, () => 1700000000)).rejects.toThrow();
    }
    await expect(verifySyncRequest({ body: body(), timestamp, signature }, policy, anotherKey, () => 1700000000)).rejects.toThrow();
  });

  it('permite renovar timestamp conservando fingerprint del mismo cuerpo', async () => {
    const signed = await signSyncJob(fixture(), policy, key, '1700000001');
    expect(signed.bodySha256).toBe(fingerprint);
    expect(signed.signature).not.toBe(signature);
    await expect(verifySyncRequest(signed, policy, key, () => 1700000001)).resolves.toHaveProperty('bodySha256', fingerprint);
  });

  it('importa claves no extraíbles y limita su tamaño a 32..128 bytes', async () => {
    expect(key.extractable).toBe(false);
    await expect(crypto.subtle.exportKey('raw', key)).rejects.toThrow();
    for (const size of [0, 31, 129]) await expect(importSyncKey(new Uint8Array(size))).rejects.toThrow();
    expect((await importSyncKey(new Uint8Array(128))).extractable).toBe(false);
  });

  it.each(['', '01700000000', '+1700000000', '-1', '1.7e9', '1700000000.0', '1700000000\n', '9007199254740992'])(
    'rechaza timestamp no canónico o inseguro %j', async (invalid) => {
      await expect(signSyncJob(fixture(), policy, key, invalid)).rejects.toThrow('TIMESTAMP');
      await expect(verifySyncRequest({ body: body(), timestamp: invalid, signature }, policy, key, () => 1700000000)).rejects.toThrow('TIMESTAMP');
    },
  );

  it.each(['', signature.toUpperCase(), signature + '\n', 'v2=' + 'a'.repeat(64), 'v1=' + 'a'.repeat(63)])(
    'rechaza formato de firma %j', async (invalid) => {
      await expect(verifySyncRequest({ body: body(), timestamp, signature: invalid }, policy, key, () => 1700000000)).rejects.toThrow('FIRMA');
    },
  );

  it('admite época cero y rechaza un reloj inválido o que falla', async () => {
    const signed = await signSyncJob(fixture(), policy, key, '0');
    await expect(verifySyncRequest(signed, policy, key, () => 0)).resolves.toHaveProperty('bodySha256', fingerprint);
    for (const now of [NaN, Infinity, -1, 1700000000.5, Number.MAX_SAFE_INTEGER + 1]) {
      await expect(verifySyncRequest({ body: body(), timestamp, signature }, policy, key, () => now)).rejects.toThrow('RELOJ');
    }
    await expect(verifySyncRequest(signed, policy, key, () => { throw new Error('diagnóstico privado'); })).rejects.toThrow('RELOJ');
  });

  it('aísla el cuerpo y las cabeceras frente a mutación durante WebCrypto', async () => {
    const request = { body: body(), timestamp, signature };
    const pending = verifySyncRequest(request, policy, key, () => 1700000000);
    request.body.fill(0); request.timestamp = '1'; request.signature = '';
    expect((await pending).bodySha256).toBe(fingerprint);
  });

  it('rechaza claves extraíbles, otro algoritmo/hash y uso no permitido', async () => {
    const extractable = await crypto.subtle.importKey('raw', rawKey, { name: 'HMAC', hash: 'SHA-256' }, true, ['sign', 'verify']);
    const otherHash = await crypto.subtle.importKey('raw', rawKey, { name: 'HMAC', hash: 'SHA-384' }, false, ['sign', 'verify']);
    const aes = await crypto.subtle.importKey('raw', rawKey, 'AES-GCM', false, ['encrypt']);
    for (const invalid of [extractable, otherHash, aes]) {
      await expect(verifySyncRequest({ body: body(), timestamp, signature }, policy, invalid, () => 1700000000)).rejects.toThrow('CLAVE');
    }
    const verifyOnly = await crypto.subtle.importKey('raw', rawKey, { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']);
    await expect(signSyncJob(fixture(), policy, verifyOnly, timestamp)).rejects.toThrow('CLAVE');
    await expect(verifySyncRequest({ body: body(), timestamp, signature }, policy, verifyOnly, () => 1700000000)).resolves.toHaveProperty('bodySha256', fingerprint);
  });

  it('convierte fallos del proveedor criptográfico en errores sin datos privados', async () => {
    for (const method of ['importKey', 'sign', 'verify'] as const) {
      const spy = vi.spyOn(crypto.subtle, method).mockRejectedValueOnce(new Error('diagnóstico privado'));
      try {
        const operation = method === 'importKey' ? importSyncKey(rawKey) : method === 'sign' ? signSyncJob(fixture(), policy, key, timestamp) : verifySyncRequest({ body: body(), timestamp, signature }, policy, key, () => 1700000000);
        await expect(operation).rejects.toThrow(method === 'importKey' ? 'CLAVE' : 'FIRMA');
      } finally { spy.mockRestore(); }
    }
  });
});
