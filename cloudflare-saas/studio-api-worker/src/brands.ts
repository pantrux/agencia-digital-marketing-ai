import { HttpError, readJson, response } from "./platform.js";
import type { OrderEnv, PlanId } from "./orders.js";

const fields = "id,display_name,audience,tone";
const uuid = "[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}";
export const brandIdPattern = new RegExp("^" + uuid + "$", "u");
export const brandRoute = new RegExp("^/api/brands(?:/(" + uuid + "))?$", "u");

export interface BrandProfile {
  id: string;
  display_name: string;
  audience: string;
  tone: string;
}

type ProfileFields = Omit<BrandProfile, "id">;

function text(value: unknown, maximum: number, multiline = false): string {
  if (typeof value !== "string" || value.length > maximum) throw new HttpError(422, "MARCA_INVALIDA");
  const cleaned = value.normalize("NFC").replace(/\r\n?/gu, "\n").trim();
  const checked = multiline ? cleaned.replace(/[\n\t]/gu, "") : cleaned;
  if (cleaned.length > maximum || /[\p{Cc}\p{Cs}]/u.test(checked)) throw new HttpError(422, "MARCA_INVALIDA");
  return cleaned;
}

function profile(input: Record<string, unknown>): ProfileFields {
  if (Object.keys(input).length !== 3) throw new HttpError(422, "MARCA_INVALIDA");
  const display_name = text(input.display_name, 80);
  if (!display_name) throw new HttpError(422, "MARCA_INVALIDA");
  return { display_name, audience: text(input.audience, 480, true), tone: text(input.tone, 240, true) };
}

export function findBrand(env: OrderEnv, userId: string, id: string): Promise<BrandProfile | null> {
  return env.DB.withSession("first-primary").prepare(`SELECT ${fields} FROM brand_profiles
    WHERE id=? AND user_id=? AND provider_mode=?`).bind(id, userId, env.BILLING_MODE).first<BrandProfile>();
}

export async function handleBrands(request: Request, env: OrderEnv, userId: string, plan: PlanId,
  id: string | undefined): Promise<Response> {
  if (id ? request.method !== "PUT" : request.method !== "GET" && request.method !== "POST") {
    throw new HttpError(405, "METODO_NO_PERMITIDO");
  }
  if (plan !== "ilimitado") throw new HttpError(403, "MARCAS_FUERA_DEL_PLAN");
  const mode = env.BILLING_MODE;
  if (request.method === "GET") {
    const list = await env.DB.withSession("first-primary").prepare(`SELECT ${fields} FROM brand_profiles
      WHERE user_id=? AND provider_mode=? ORDER BY name_key,id`).bind(userId, mode).all<BrandProfile>();
    return response({ brands: list.results });
  }
  const data = profile(await readJson(request, 4096));
  const nameKey = data.display_name.toLocaleLowerCase("es").normalize("NFC");
  const values = [nameKey, data.display_name, data.audience, data.tone] as const;
  if (id) {
    const updated = await env.DB.prepare(`UPDATE OR IGNORE brand_profiles SET name_key=?,display_name=?,audience=?,tone=?
      WHERE id=? AND user_id=? AND provider_mode=? RETURNING ${fields}`)
      .bind(...values, id, userId, mode).first<BrandProfile>();
    if (updated) return response({ brand: updated });
    if (!await findBrand(env, userId, id)) throw new HttpError(404, "MARCA_NO_ENCONTRADA");
    throw new HttpError(409, "NOMBRE_DE_MARCA_EN_USO");
  }
  const created = await env.DB.prepare(`INSERT INTO brand_profiles(id,user_id,provider_mode,name_key,display_name,audience,tone)
    VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,provider_mode,name_key) DO NOTHING RETURNING ${fields}`)
    .bind(crypto.randomUUID(), userId, mode, ...values).first<BrandProfile>();
  if (created) return response({ brand: created }, 201);
  const existing = await env.DB.withSession("first-primary").prepare(`SELECT ${fields} FROM brand_profiles
    WHERE user_id=? AND provider_mode=? AND name_key=?`).bind(userId, mode, nameKey).first<BrandProfile>();
  if (existing?.display_name === data.display_name && existing.audience === data.audience && existing.tone === data.tone) {
    return response({ brand: existing });
  }
  throw new HttpError(409, "NOMBRE_DE_MARCA_EN_USO");
}
