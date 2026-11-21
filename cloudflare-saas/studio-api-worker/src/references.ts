import {publicUrl} from '../../studio-reference-worker/src/index.js';
import {HttpError, readJson, response, type SqlDatabase} from './platform.js';

export type ReferenceEnv = {DB: SqlDatabase; BILLING_MODE: 'sandbox' | 'production';
  REFERENCE_SERVICE?: {fetch(request: Request): Promise<Response>}};
export const referenceRoute = /^\/api\/orders\/([a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12})\/reference$/u;

export async function handleReference(request: Request, env: ReferenceEnv, user: string, order: string): Promise<Response> {
  const db = env.DB.withSession('first-primary');
  const owner = await db.prepare(`SELECT id FROM orders WHERE id=? AND user_id=? AND kind='production'
    AND COALESCE(provider_mode,'production')=?`).bind(order, user, env.BILLING_MODE).first<{id:string}>();
  if (!owner) throw new HttpError(404, 'ORDEN_NO_ENCONTRADA');
  if (request.method === 'GET') {
    const reference = await db.prepare(`SELECT source_url,status,title,description,excerpt FROM studio_reference_context
      WHERE order_id=? AND user_id=?`).bind(order,user).first();
    return response({reference});
  }
  if (request.method !== 'POST') throw new HttpError(405, 'METODO_NO_PERMITIDO');
  const body = await readJson(request, 4096);
  if (Object.keys(body).length !== 1 || typeof body.url !== 'string') throw new HttpError(422, 'REFERENCIA_INVALIDA');
  const url = publicUrl(body.url);
  if (!url) throw new HttpError(422, 'REFERENCIA_INVALIDA');
  if (!env.REFERENCE_SERVICE) throw new HttpError(503, 'REFERENCIA_NO_DISPONIBLE');
  const now = Math.floor(Date.now()/1000);
  const allowed = await db.prepare(`INSERT INTO studio_reference_rate(user_id,last_started) VALUES(?,?)
    ON CONFLICT(user_id) DO UPDATE SET last_started=excluded.last_started
    WHERE studio_reference_rate.last_started <= excluded.last_started-30 RETURNING user_id`)
    .bind(user,now).first<{user_id:string}>();
  if (!allowed) throw new HttpError(429, 'REFERENCIA_ESPERAR');
  let result: {status:'available'|'unavailable'; title:string; description:string; excerpt:string} = {status:'unavailable',title:'',description:'',excerpt:''};
  try {
    const reply = await env.REFERENCE_SERVICE.fetch(new Request('https://referencias.invalid/extract', {
      method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({url:url.toString()}),
      signal:AbortSignal.timeout(10_000),
    }));
    const text = await reply.text();
    if (reply.ok && text.length <= 8192) {
      const data = JSON.parse(text) as Record<string, unknown>;
      if (data.status === 'available' && typeof data.title === 'string' && data.title.length <= 500 &&
          typeof data.description === 'string' && data.description.length <= 500 &&
          typeof data.excerpt === 'string' && data.excerpt.length <= 1000 && (data.title || data.description || data.excerpt)) {
        result = {status:'available',title:data.title,description:data.description,excerpt:data.excerpt};
      }
    }
  } catch { /* No se inventa contenido si el sitio o el servicio no responde. */ }
  await db.prepare(`INSERT INTO studio_reference_context(order_id,user_id,source_url,status,title,description,excerpt,updated_at)
    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET source_url=excluded.source_url,
    status=excluded.status,title=excluded.title,description=excluded.description,excerpt=excluded.excerpt,updated_at=excluded.updated_at
    WHERE studio_reference_context.user_id=excluded.user_id AND studio_reference_context.updated_at<=excluded.updated_at`)
    .bind(order,user,url.toString(),result.status,result.title,result.description,result.excerpt,now).run();
  return response({reference:{source_url:url.toString(), ...result}});
}
