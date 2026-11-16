import { describe, expect, it } from 'vitest';
import { parseReleaseManifest } from '../src/ci/release-contract.js';

const valid = () => ({
  schema_version: 1,
  release_sha: 'a'.repeat(40),
  cloudflare: { worker: 'studio-api-worker', version_id: '12345678-1234-4234-8234-123456789abc' },
  compose: { path: '/opt/media-platform/releases/prev/compose.yml', sha256: 'b'.repeat(64) },
  openclaw: {
    path: '/opt/media-platform/releases/prev/openclaw.json',
    sha256: 'c'.repeat(64), current_sha256: 'd'.repeat(64), ssh_user: 'jandrade',
  },
});

describe('Contrato de la versión recuperable', () => {
  it('admite una versión explícita de los tres planos', () => {
    expect(parseReleaseManifest(valid()).release_sha).toBe('a'.repeat(40));
  });
  it.each([null, [], 'dato', 1])('rechaza entradas que no son objetos: %s', (value) => {
    expect(() => parseReleaseManifest(value)).toThrow();
  });
  it('rechaza nombres de imagen o versión mutables', () => {
    expect(() => parseReleaseManifest({ ...valid(), release_sha: 'latest' })).toThrow();
  });
  it('rechaza claves de comandos adicionales', () => {
    expect(() => parseReleaseManifest({ ...valid(), command: 'ejecutar' })).toThrow();
  });
  it('rechaza un destino Cloudflare ajeno', () => {
    const value = valid(); value.cloudflare.worker = 'otro';
    expect(() => parseReleaseManifest(value)).toThrow();
  });
  it('rechaza rutas relativas o recorridos fuera de la versión', () => {
    for (const path of ['compose.yml', '/opt/../etc/archivo', '']) {
      const value = valid(); value.compose.path = path;
      expect(() => parseReleaseManifest(value)).toThrow();
    }
  });
  it('rechaza usuarios SSH que introducen opciones', () => {
    const value = valid(); value.openclaw.ssh_user = '-oProxyCommand=algo';
    expect(() => parseReleaseManifest(value)).toThrow();
  });
  it('rechaza digests o identificadores incompletos', () => {
    for (const field of ['sha256', 'current_sha256'] as const) {
      const value = valid(); value.openclaw[field] = 'incorrecto';
      expect(() => parseReleaseManifest(value)).toThrow();
    }
    const value = valid(); value.cloudflare.version_id = 'sin-id';
    expect(() => parseReleaseManifest(value)).toThrow();
  });
  it('rechaza versiones de esquema desconocidas y campos faltantes', () => {
    expect(() => parseReleaseManifest({ ...valid(), schema_version: 2 })).toThrow();
    expect(() => parseReleaseManifest({ ...valid(), compose: {} })).toThrow();
    expect(() => parseReleaseManifest({ ...valid(), openclaw: null })).toThrow();
  });
});
