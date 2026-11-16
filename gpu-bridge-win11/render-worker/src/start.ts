import {pathToFileURL} from 'node:url';
import path from 'node:path';
import {loadConfig} from './config.ts';
import {WorkerError} from './contracts.ts';
import {RenderBackend} from './backend.ts';
import {Application, createWorkerServer} from './server.ts';

export async function main(): Promise<void> {
  if (Number(process.versions.node.split('.')[0]) !== 24 || (process.env.GPU_BRIDGE_BIND_ADDRESS && process.env.GPU_BRIDGE_BIND_ADDRESS !== '0.0.0.0')) throw new WorkerError(503, 'RUNTIME_INVALIDO', 'Se requiere Node24 y escucha explícita0.0.0.0.');
  const config = await loadConfig();
  // El SDK hereda process.env al abrir Chromium; el secreto queda solo en memoria del servidor.
  delete process.env.GPU_BRIDGE_BEARER_TOKEN;
  const backend = await RenderBackend.create(config), shutdown = new AbortController();
  const server = createWorkerServer(new Application(config.token, backend, shutdown.signal));
  // Reservar8090 antes de probar la GPU evita inicializaciones simultáneas de dos workers.
  await new Promise<void>((resolve, reject) => {server.once('error', reject); server.listen(8090, '0.0.0.0', () => {server.removeListener('error', reject); resolve();});});
  const stop = (): void => {shutdown.abort(); server.close();};
  process.once('SIGINT', stop); process.once('SIGTERM', stop);
  try {await backend.initialize();}
  catch (error) {stop(); throw error;}
  console.log(JSON.stringify({service: 'remotion', status: 'ok', port: 8090}));
}
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  main().catch((error: unknown) => {
    console.error(JSON.stringify({service: 'remotion', status: 'degraded', error: error instanceof WorkerError ? error.code : 'INICIALIZACION_FALLIDA'})); process.exitCode = 1;
  });
}
