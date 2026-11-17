import {spawn} from 'node:child_process';
import {WorkerError} from './contracts.ts';

export function childEnvironment(source: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const result: NodeJS.ProcessEnv = {};
  for (const name of ['PATH', 'Path', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'HOME']) {
    if (source[name] !== undefined) result[name] = source[name];
  }
  return result;
}
export interface ProcessOptions {
  timeoutMs: number; signal?: AbortSignal; maxOutputBytes?: number;
  hyperframes?: {browser: string; ffmpegDir: string; project: string};
}

export function runProcess(executable: string, args: string[], options: ProcessOptions): Promise<{stdout: string; stderr: string}> {
  if (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0 || options.signal?.aborted) return Promise.reject(new WorkerError(504, 'PROCESO_CANCELADO', 'El proceso agotó su tiempo o fue cancelado.'));
  return new Promise((resolve, reject) => {
    const env = childEnvironment();
    const hf = options.hyperframes;
    if (hf) {
      env.HYPERFRAMES_BROWSER_PATH = hf.browser;
      env.HYPERFRAMES_NO_TELEMETRY = '1';
      env.PRODUCER_BROWSER_GPU_MODE = 'software';
      const pathKey = Object.keys(env).find(name => name.toUpperCase() === 'PATH') ?? 'PATH';
      env[pathKey] = [hf.ffmpegDir, env[pathKey]].filter(Boolean).join(process.platform === 'win32' ? ';' : ':');
    }
    const child = spawn(executable, args, {shell: false, windowsHide: true, env,
      ...(hf && {cwd: hf.project}), stdio: ['ignore', 'pipe', 'pipe']});
    const stdout: Buffer[] = [], stderr: Buffer[] = [];
    let size = 0, failure: Error | undefined;
    const stop = (error: Error): void => {
      if (failure) return;
      failure = error;
      if (hf && child.pid && process.platform === 'win32') {
        const taskkill = spawn('taskkill.exe', ['/pid', String(child.pid), '/t', '/f'], {
          shell: false, windowsHide: true, env: childEnvironment(), stdio: 'ignore',
        });
        taskkill.once('error', () => child.kill('SIGKILL'));
        taskkill.once('close', (code) => {if (code !== 0) child.kill('SIGKILL');});
      } else child.kill('SIGKILL');
    };
    const abort = (): void => stop(new WorkerError(504, 'PROCESO_CANCELADO', 'El proceso agotó su tiempo o fue cancelado.'));
    const timer = setTimeout(abort, options.timeoutMs);
    options.signal?.addEventListener('abort', abort, {once: true});
    const receive = (chunks: Buffer[], chunk: Buffer): void => {
      size += chunk.length;
      if (size > (options.maxOutputBytes ?? 4 * 1024 * 1024)) stop(new WorkerError(502, 'SALIDA_EXCESIVA', 'El proceso produjo demasiada salida.'));
      else chunks.push(chunk);
    };
    child.stdout.on('data', (chunk: Buffer) => receive(stdout, chunk));
    child.stderr.on('data', (chunk: Buffer) => receive(stderr, chunk));
    child.on('error', () => {failure ??= new WorkerError(503, 'HERRAMIENTA_NO_DISPONIBLE', 'No se pudo ejecutar la herramienta fijada.');});
    // La plaza solo se libera después de cerrar proceso y tuberías propios.
    child.on('close', (code) => {
      clearTimeout(timer); options.signal?.removeEventListener('abort', abort);
      if (failure) reject(failure);
      else if (code !== 0) reject(new WorkerError(502, 'HERRAMIENTA_FALLO', 'La herramienta no completó el procesamiento.'));
      else resolve({stdout: Buffer.concat(stdout).toString('utf8'), stderr: Buffer.concat(stderr).toString('utf8')});
    });
  });
}
