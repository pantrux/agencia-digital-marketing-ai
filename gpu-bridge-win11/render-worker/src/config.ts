import {readFile, lstat} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {WorkerError} from './contracts.ts';
import {authorize} from './security.ts';

export interface RuntimeConfig {token: string; ffmpeg: string; ffprobe: string; browser: string; cacheRoot: string; nasRoot: string}
const names = ['GPU_BRIDGE_BEARER_TOKEN', 'GPU_FFMPEG_PATH', 'GPU_FFPROBE_PATH', 'GPU_REMOTION_BROWSER_PATH', 'GPU_MODEL_CACHE_ROOT', 'GPU_SMB_DRIVE'] as const;
export async function loadConfig(environment: NodeJS.ProcessEnv = process.env): Promise<RuntimeConfig> {
  let privateValues: Record<string, unknown> = {};
  if (environment.GPU_RUNTIME_CONFIG_PATH) {
    const file = environment.GPU_RUNTIME_CONFIG_PATH;
    const info = await lstat(file);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 65536) throw new WorkerError(503, 'CONFIGURACION_INVALIDA', 'La configuración privada no es un archivo válido.');
    const data: unknown = JSON.parse((await readFile(file, 'utf8')).replace(/^\uFEFF/, ''));
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new WorkerError(503, 'CONFIGURACION_INVALIDA', 'La configuración privada no es válida.');
    privateValues = data as Record<string, unknown>;
  }
  const values = Object.fromEntries(names.map((name) => {
    const value = privateValues[name] ?? environment[name];
    if (typeof value !== 'string' || !value || /[\r\n\0]/.test(value)) throw new WorkerError(503, 'CONFIGURACION_INCOMPLETA', 'Falta una variable obligatoria del renderizador.');
    return [name, value];
  })) as Record<typeof names[number], string>;
  authorize('127.0.0.1', `Bearer ${values.GPU_BRIDGE_BEARER_TOKEN}`, values.GPU_BRIDGE_BEARER_TOKEN);
  for (const name of ['GPU_FFMPEG_PATH', 'GPU_FFPROBE_PATH', 'GPU_REMOTION_BROWSER_PATH', 'GPU_MODEL_CACHE_ROOT'] as const) {
    if (!path.win32.isAbsolute(values[name])) throw new WorkerError(503, 'RUTA_CONFIGURACION_INVALIDA', 'Se requieren rutas absolutas de Windows.');
  }
  if (!/^[a-z]:\\?$/i.test(values.GPU_SMB_DRIVE)) throw new WorkerError(503, 'NAS_NO_VERIFICADO', 'Se requiere una unidad SMB explícita.');
  const selectedRoot = Object.hasOwn(privateValues, 'GPU_RENDER_CACHE_ROOT') ? privateValues.GPU_RENDER_CACHE_ROOT : environment.GPU_RENDER_CACHE_ROOT;
  const cacheRoot = selectedRoot === undefined ? values.GPU_MODEL_CACHE_ROOT : selectedRoot;
  if (typeof cacheRoot !== 'string' || !/^[a-z]:[\\/]/i.test(cacheRoot) || !path.win32.isAbsolute(cacheRoot) || /[\r\n\0]/.test(cacheRoot) || cacheRoot.split(/[\\/]/).includes('..')) throw new WorkerError(503, 'CACHE_INVALIDA', 'La caché de render requiere una ruta local absoluta sin escapes.');
  return {token: values.GPU_BRIDGE_BEARER_TOKEN, ffmpeg: values.GPU_FFMPEG_PATH, ffprobe: values.GPU_FFPROBE_PATH, browser: values.GPU_REMOTION_BROWSER_PATH, cacheRoot, nasRoot: values.GPU_SMB_DRIVE[0]!.toUpperCase() + ':\\'};
}
export async function verifyBinary(file: string, expectedSha256: string): Promise<void> {
  const info = await lstat(file);
  if (!info.isFile() || info.isSymbolicLink() || !/^[a-f0-9]{64}$/.test(expectedSha256) || createHash('sha256').update(await readFile(file)).digest('hex') !== expectedSha256) throw new WorkerError(503, 'BINARIO_NO_VERIFICADO', 'El hash de una herramienta no coincide con el manifiesto.');
}
