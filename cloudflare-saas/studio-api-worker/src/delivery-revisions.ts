import {HttpError, readJson, response, type SqlDatabase} from './platform.js';

export interface DeliveryRevisionEnv {
  DB: SqlDatabase;
  BILLING_MODE: 'sandbox' | 'production';
  STUDIO_DELIVERY_REVISION_ENABLED?: string;
}
export const deliveryRevisionRoute = /^\/api\/orders\/([a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12})\/delivery-revisions$/u;
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/u;
type Revision = {id:string; base_kit_id:string; instructions:string; status:string; result_kit_id:string | null; failure_stage:string | null; created_at:number; updated_at:number};

export async function claimDeliveryRevision(env: DeliveryRevisionEnv, now: number): Promise<Response> {
  if (env.STUDIO_DELIVERY_REVISION_ENABLED !== 'true') throw new HttpError(404,'RUTA_NO_DISPONIBLE');
  const lease = crypto.randomUUID();
  const claimed = await env.DB.prepare(`UPDATE delivery_revision_requests SET status='processing',lease=?,lease_until=?,
    updated_at=?,attempts=attempts+1 WHERE id=(SELECT r.id FROM delivery_revision_requests r
    JOIN orders o ON o.id=r.order_id AND o.user_id=r.user_id WHERE o.kind='production' AND o.status='completed'
      AND COALESCE(o.provider_mode,'production')=? AND (r.status='pending' OR (r.status='processing' AND r.lease_until<=?))
    ORDER BY r.created_at,r.id LIMIT 1)
    RETURNING id,order_id,user_id,base_kit_id,instructions,lease,lease_until,attempts,
      (SELECT sha256 FROM delivery_kits WHERE id=delivery_revision_requests.base_kit_id) AS base_kit_sha256,
      (SELECT title FROM delivery_kits WHERE id=delivery_revision_requests.base_kit_id) AS base_kit_title`)
    .bind(lease,now+3600,now,env.BILLING_MODE,now).first();
  return response({revision:claimed});
}

export async function renewDeliveryRevision(env: DeliveryRevisionEnv, id: unknown, lease: unknown, now: number): Promise<Response> {
  if (env.STUDIO_DELIVERY_REVISION_ENABLED !== 'true') throw new HttpError(404,'RUTA_NO_DISPONIBLE');
  if (typeof id !== 'string' || !uuid.test(id) || typeof lease !== 'string' || !uuid.test(lease)) {
    throw new HttpError(422,'AJUSTE_INVALIDO');
  }
  const saved = await env.DB.prepare(`UPDATE delivery_revision_requests SET lease_until=?,updated_at=?
    WHERE id=? AND status='processing' AND lease=? AND lease_until>? AND lease_until<?
      AND EXISTS (SELECT 1 FROM orders o WHERE o.id=delivery_revision_requests.order_id
        AND o.user_id=delivery_revision_requests.user_id AND o.status='completed'
        AND COALESCE(o.provider_mode,'production')=?) RETURNING id,lease_until`)
    .bind(now+3600,now,id,lease,now,now+3600,env.BILLING_MODE).first();
  if (saved) return response({revision:saved});
  const current = await env.DB.withSession('first-primary').prepare(`SELECT r.id,r.lease_until
    FROM delivery_revision_requests r JOIN orders o ON o.id=r.order_id AND o.user_id=r.user_id
    WHERE r.id=? AND r.status='processing' AND r.lease=? AND r.lease_until>?
      AND o.status='completed' AND COALESCE(o.provider_mode,'production')=?`)
    .bind(id,lease,now,env.BILLING_MODE).first();
  if (!current) throw new HttpError(409,'RESERVA_VENCIDA');
  return response({revision:current});
}

export async function failDeliveryRevision(env: DeliveryRevisionEnv, id: unknown, lease: unknown, stage: unknown, now: number): Promise<Response> {
  if (env.STUDIO_DELIVERY_REVISION_ENABLED !== 'true') throw new HttpError(404,'RUTA_NO_DISPONIBLE');
  if (typeof id !== 'string' || !uuid.test(id) || typeof lease !== 'string' || !uuid.test(lease)
    || (stage !== 'direction' && stage !== 'quality')) throw new HttpError(422,'AJUSTE_INVALIDO');
  const saved = await env.DB.prepare(`UPDATE delivery_revision_requests SET status='failed',failure_stage=?,
    lease=NULL,lease_until=NULL,updated_at=? WHERE id=? AND status='processing' AND lease=? AND lease_until>?
    AND EXISTS (SELECT 1 FROM orders o WHERE o.id=delivery_revision_requests.order_id
      AND o.user_id=delivery_revision_requests.user_id AND o.status='completed'
      AND COALESCE(o.provider_mode,'production')=?) RETURNING id`)
    .bind(stage,now,id,lease,now,env.BILLING_MODE).first();
  if (!saved) throw new HttpError(409,'RESERVA_VENCIDA');
  return response({recorded:true});
}

/** En pruebas aisladas; el interruptor debe permanecer cerrado hasta activar el consumidor. */
export async function handleDeliveryRevisions(request: Request, env: DeliveryRevisionEnv, user: string, order: string): Promise<Response> {
  if (env.STUDIO_DELIVERY_REVISION_ENABLED !== 'true') throw new HttpError(404,'RUTA_NO_DISPONIBLE');
  if (request.method !== 'GET' && request.method !== 'POST') throw new HttpError(405,'METODO_NO_PERMITIDO');
  const db = env.DB.withSession('first-primary');
  const owner = await db.prepare(`SELECT id FROM orders WHERE id=? AND user_id=? AND kind='production'
    AND status='completed' AND COALESCE(provider_mode,'production')=?`).bind(order,user,env.BILLING_MODE).first();
  if (!owner) throw new HttpError(404,'ORDEN_NO_ENCONTRADA');
  const fields = 'id,base_kit_id,instructions,status,result_kit_id,failure_stage,created_at,updated_at';
  if (request.method === 'GET') {
    const rows = await db.prepare(`SELECT ${fields} FROM delivery_revision_requests WHERE order_id=? AND user_id=?
      ORDER BY created_at DESC,id DESC LIMIT 50`).bind(order,user).all<Revision>();
    return response({revisions:rows.results});
  }
  const key = request.headers.get('Idempotency-Key') ?? '';
  if (!/^[a-zA-Z0-9_-]{1,128}$/u.test(key)) throw new HttpError(400,'IDEMPOTENCIA_INVALIDA');
  const body = await readJson(request,4096);
  if (Object.keys(body).sort().join(',') !== 'base_kit_id,instructions' ||
      typeof body.base_kit_id !== 'string' || !uuid.test(body.base_kit_id) ||
      typeof body.instructions !== 'string') throw new HttpError(422,'AJUSTE_INVALIDO');
  const instructions = body.instructions.replace(/\r\n?/gu,'\n').trim();
  if (!instructions || instructions.length > 800 || /[\p{Cc}\p{Cs}]/u.test(instructions.replace(/[\n\t]/gu,''))) {
    throw new HttpError(422,'AJUSTE_INVALIDO');
  }
  const find = () => db.prepare(`SELECT ${fields} FROM delivery_revision_requests WHERE order_id=? AND user_id=? AND request_key=?`)
    .bind(order,user,key).first<Revision>();
  const replay = (saved:Revision) => {
    if (saved.instructions !== instructions || saved.base_kit_id !== body.base_kit_id) throw new HttpError(409,'IDEMPOTENCIA_EN_CONFLICTO');
    return response({revision:saved});
  };
  const saved = await find();
  if (saved) return replay(saved);
  const now = Math.floor(Date.now()/1000);
  const created = await db.prepare(`INSERT INTO delivery_revision_requests
    (id,order_id,user_id,base_kit_id,request_key,instructions,created_at,updated_at)
    SELECT ?,o.id,o.user_id,k.id,?,?,?,? FROM orders o JOIN delivery_kits k ON k.order_id=o.id AND k.user_id=o.user_id
    JOIN plan_quotas p ON p.plan_id=o.plan_id WHERE o.id=? AND o.user_id=? AND o.kind='production'
      AND o.status='completed' AND COALESCE(o.provider_mode,'production')=? AND k.provider_mode=? AND k.id=?
      AND (p.retention_days IS NULL OR k.created_at+p.retention_days*86400>?)
    ON CONFLICT DO NOTHING RETURNING ${fields}`).bind(crypto.randomUUID(),key,instructions,now,now,order,user,
      env.BILLING_MODE,env.BILLING_MODE,body.base_kit_id,now).first<Revision>();
  if (created) return response({revision:created},201);
  const concurrent = await find();
  if (concurrent) return replay(concurrent);
  const active = await db.prepare(`SELECT id FROM delivery_revision_requests WHERE order_id=? AND user_id=?
    AND status IN ('pending','processing')`).bind(order,user).first();
  if (active) throw new HttpError(409,'AJUSTE_EN_CURSO');
  throw new HttpError(404,'KIT_NO_ENCONTRADO');
}
