/** Contrato transportable de la entrega previa; las huellas se comprueban en el ejecutor SRE. */
export interface ReleaseManifest {
  schema_version: 1;
  release_sha: string;
  cloudflare: { worker: 'studio-api-worker'; version_id: string };
  compose: { path: string; sha256: string };
  openclaw: { path: string; sha256: string; current_sha256: string; ssh_user: string };
}

function objectWithKeys(value: unknown, keys: string[]): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('El contrato requiere un objeto.');
  }
  const record = value as Record<string, unknown>;
  if (Object.keys(record).length !== keys.length || keys.some((key) => !(key in record))) {
    throw new Error('El contrato contiene campos faltantes o adicionales.');
  }
  return record;
}

function matches(value: unknown, expression: RegExp): string {
  if (typeof value !== 'string' || !expression.test(value)) {
    throw new Error('El contrato contiene un identificador o ruta inválidos.');
  }
  return value;
}

const digest = /^[a-f0-9]{64}$/;
const absolutePath = /^(?:\/(?!.*(?:^|\/)\.\.(?:\/|$))[^\r\n]+|[A-Za-z]:[\\/](?!.*\.\.)[^\r\n]+)$/;

export function parseReleaseManifest(value: unknown): ReleaseManifest {
  const root = objectWithKeys(value, ['schema_version', 'release_sha', 'cloudflare', 'compose', 'openclaw']);
  if (root['schema_version'] !== 1) throw new Error('Versión de contrato desconocida.');
  const cloudflare = objectWithKeys(root['cloudflare'], ['worker', 'version_id']);
  const compose = objectWithKeys(root['compose'], ['path', 'sha256']);
  const openclaw = objectWithKeys(root['openclaw'], ['path', 'sha256', 'current_sha256', 'ssh_user']);
  if (cloudflare['worker'] !== 'studio-api-worker') throw new Error('Destino Cloudflare inválido.');
  return {
    schema_version: 1,
    release_sha: matches(root['release_sha'], /^[a-f0-9]{40}$/),
    cloudflare: {
      worker: 'studio-api-worker',
      version_id: matches(cloudflare['version_id'], /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/),
    },
    compose: { path: matches(compose['path'], absolutePath), sha256: matches(compose['sha256'], digest) },
    openclaw: {
      path: matches(openclaw['path'], absolutePath),
      sha256: matches(openclaw['sha256'], digest),
      current_sha256: matches(openclaw['current_sha256'], digest),
      ssh_user: matches(openclaw['ssh_user'], /^[a-z_][a-z0-9_-]{0,31}$/),
    },
  };
}
