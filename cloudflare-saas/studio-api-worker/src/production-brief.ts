import { HttpError, readJson, response, type SqlDatabase } from "./platform.js";

type BriefEnv = { DB: SqlDatabase; BILLING_MODE: "sandbox" | "production" };
type Confirmed = { objective: string; audience: string; tone: string; confirmed_at: number; ad_cta?: string; ad_brand?: string };
type BriefRow = { status: string; received: number; ad_cta: string | null; ad_brand: string | null; output_edit: string; output_format: string; objective: string | null; audience: string | null; tone: string | null;
  confirmed_at: number | null; brand_id: string | null; brand_name: string | null; brand_audience: string | null; brand_tone: string | null };
export const productionBriefRoute = /^\/api\/orders\/([a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12})\/brief$/u;

function normalized(value: unknown, limit: number, optional = false): string {
  if (typeof value !== "string" || value.length > limit) {
    throw new HttpError(422, "BRIEF_INVALIDO");
  }
  for (const character of value) {
    const code = character.charCodeAt(0);
    if (code === 127 || (code < 32 && code !== 9 && code !== 10 && code !== 13)) {
      throw new HttpError(422, "BRIEF_INVALIDO");
    }
  }
  const text = value.trim().replace(/\r\n?/gu, "\n");
  if (!optional && !text) throw new HttpError(422, "BRIEF_INVALIDO");
  return text;
}

function confirmed(row: BriefRow): Confirmed | null {
  if (row.objective === null || row.audience === null || row.tone === null || row.confirmed_at === null) return null;
  return { objective: row.objective, audience: row.audience, tone: row.tone, confirmed_at: row.confirmed_at, ...(row.ad_cta ? { ad_cta: row.ad_cta, ad_brand: row.ad_brand ?? "" } : {}) };
}

export async function handleProductionBrief(request: Request, env: BriefEnv, user: string, order: string): Promise<Response> {
  if (request.method !== "GET" && request.method !== "POST") throw new HttpError(405, "METODO_NO_PERMITIDO");
  const db = env.DB.withSession("first-primary");
  const get = () => db.prepare(`SELECT o.status,o.output_edit,o.output_format, d.received_at IS NOT NULL AS received,
    b.objective,b.audience,b.tone,b.confirmed_at,b.ad_cta,b.ad_brand,
    m.brand_id,m.display_name AS brand_name,m.audience AS brand_audience,m.tone AS brand_tone FROM orders o
    LEFT JOIN production_dispatch d ON d.order_id=o.id
    LEFT JOIN production_briefs b ON b.order_id=o.id AND b.user_id=o.user_id
    LEFT JOIN order_brand_profiles m ON m.order_id=o.id AND m.user_id=o.user_id AND m.provider_mode=o.provider_mode
    WHERE o.id=? AND o.user_id=? AND o.kind='production' AND COALESCE(o.provider_mode,'production')=?`)
    .bind(order, user, env.BILLING_MODE).first<BriefRow>();
  const row = await get();
  if (!row) throw new HttpError(404, "ORDEN_NO_ENCONTRADA");
  const ready = row.status === "processing" && row.received === 1;
  const brandFields = row.brand_id && row.brand_name && row.brand_audience !== null && row.brand_tone !== null
    ? { brand: { id: row.brand_id, display_name: row.brand_name, audience: row.brand_audience, tone: row.brand_tone } } : {};
  const adAvailable = row.output_edit === "reel" && row.output_format === "mp4";
  if (request.method === "GET") return response({ brief: confirmed(row), ready, ...brandFields, ...(adAvailable ? { ad_available: true } : {}) });
  const body = await readJson(request, 8192);
  if (Object.keys(body).some(key => !["objective", "audience", "tone", "ad_cta", "ad_brand"].includes(key))) throw new HttpError(422, "BRIEF_INVALIDO");
  const objective = normalized(body.objective, 800);
  const audience = normalized(body.audience, 480);
  const tone = normalized(body.tone, 240, true);
  const cta = body.ad_cta === undefined ? "" : normalized(body.ad_cta, 70);
  const brand = body.ad_brand === undefined ? row.brand_name?.trim() || "" : normalized(body.ad_brand, 80);
  const singleLine = /[\p{Cc}\p{Cf}\p{Cs}]/u;
  if ((!cta && body.ad_brand !== undefined)
    || (cta && (!adAvailable || !brand || brand.length > 80 || singleLine.test(cta) || singleLine.test(brand)))) {
    throw new HttpError(422, "BRIEF_INVALIDO");
  }
  const adBrand = cta ? brand : "";
  if (ready && !confirmed(row)) {
    await env.DB.prepare(`INSERT INTO production_briefs(order_id,user_id,objective,audience,tone,ad_cta,ad_brand)
      SELECT o.id,o.user_id,?,?,?,?,? FROM orders o JOIN production_dispatch d ON d.order_id=o.id
      WHERE o.id=? AND o.user_id=? AND o.kind='production' AND COALESCE(o.provider_mode,'production')=?
        AND o.status='processing' AND d.received_at IS NOT NULL
      ON CONFLICT(order_id) DO NOTHING`).bind(objective, audience, tone, cta, adBrand, order, user, env.BILLING_MODE).run();
  }
  const saved = await get();
  const brief = saved && confirmed(saved);
  if (!brief) throw new HttpError(409, "BRIEF_ESPERA_CRUDO");
  if (brief.objective !== objective || brief.audience !== audience || brief.tone !== tone || (brief.ad_cta ?? "") !== cta || (brief.ad_brand ?? "") !== adBrand) {
    throw new HttpError(409, "BRIEF_YA_CONFIRMADO");
  }
  return response({ brief, ready: saved.status === "processing" && saved.received === 1, ...brandFields, ...(adAvailable ? { ad_available: true } : {}) });
}
