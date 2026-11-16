import {createServer, type Server} from 'node:http';
import {parseInspect, parseRender, parseStills, WorkerError, type InspectRequest, type RenderRequest, type StillsRequest} from './contracts.ts';
import {authorize, SlotPool} from './security.ts';

export interface Health {service: string; status: string; ready: boolean; version: string; nvenc_available: boolean; [key: string]: unknown}
export interface Backend {
  health(): Health;
  render(request: RenderRequest, signal: AbortSignal): Promise<unknown>;
  stills(request: StillsRequest, signal: AbortSignal): Promise<unknown>;
  inspect(request: InspectRequest, signal: AbortSignal): Promise<unknown>;
}
interface Request {method: string; path: string; remoteAddress: string; authorization: string | undefined; body: Buffer; signal?: AbortSignal}
interface Response {status: number; body: unknown}
export class Application {
  readonly token: string; readonly backend: Backend; readonly slots = new SlotPool(2); readonly shutdown: AbortSignal;
  constructor(token: string, backend: Backend, shutdown = new AbortController().signal) {this.token = token; this.backend = backend; this.shutdown = shutdown;}
  async handle(request: Request): Promise<Response> {
    try {
      authorize(request.remoteAddress, request.authorization, this.token);
      if (request.method === 'GET' && ['/health', '/healthz'].includes(request.path)) {
        const health = this.backend.health(); return {status: health.ready ? 200 : 503, body: health};
      }
      if (request.method !== 'POST' || !['/render', '/stills', '/inspect'].includes(request.path)) throw new WorkerError(404, 'RUTA_NO_ENCONTRADA', 'La ruta o método no está disponible.');
      if (request.body.length > 65536) throw new WorkerError(413, 'CUERPO_EXCESIVO', 'La solicitud supera 64 KiB.');
      let data: unknown;
      try {data = JSON.parse(request.body.toString('utf8'));} catch {throw new WorkerError(422, 'JSON_INVALIDO', 'Se requiere JSON válido.');}
      const signal = AbortSignal.any([request.signal ?? new AbortController().signal, this.shutdown]);
      const release = this.slots.acquire();
      try {
        const body = request.path === '/render' ? await this.backend.render(parseRender(data), signal) : request.path === '/stills' ? await this.backend.stills(parseStills(data), signal) : await this.backend.inspect(parseInspect(data), signal);
        return {status: 200, body};
      } finally {release();}
    } catch (error) {
      return error instanceof WorkerError ? {status: error.status, body: {error: error.code, message: error.message}} : {status: 500, body: {error: 'FALLO_INTERNO', message: 'El trabajo no se completó.'}};
    }
  }
}
// Construir el servidor no abre un puerto; el lanzador es el único que llama a listen.
export function createWorkerServer(app: Application): Server {
  const server = createServer(async (request, response) => {
    const controller = new AbortController();
    request.on('aborted', () => controller.abort());
    response.on('close', () => {if (!response.writableEnded) controller.abort();});
    const chunks: Buffer[] = []; let size = 0;
    try {
      // Rechazar antes de leer o retener datos de clientes ajenos.
      authorize(request.socket.remoteAddress ?? '', request.headers.authorization, app.token);
      for await (const chunk of request) {
        size += (chunk as Buffer).length;
        if (size > 65536) throw new WorkerError(413, 'CUERPO_EXCESIVO', 'La solicitud supera 64 KiB.');
        chunks.push(chunk as Buffer);
      }
      const result = await app.handle({method: request.method ?? '', path: request.url ?? '', remoteAddress: request.socket.remoteAddress ?? '', authorization: request.headers.authorization, body: Buffer.concat(chunks), signal: controller.signal});
      response.writeHead(result.status, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'}); response.end(JSON.stringify(result.body));
    } catch (error) {
      const status = error instanceof WorkerError ? error.status : 400;
      response.writeHead(status, {'Content-Type': 'application/json; charset=utf-8', Connection: 'close'}); response.end(JSON.stringify({error: error instanceof WorkerError ? error.code : 'SOLICITUD_INVALIDA'}));
    }
  });
  server.requestTimeout = 30000; server.headersTimeout = 15000; server.maxHeadersCount = 32;
  return server;
}
