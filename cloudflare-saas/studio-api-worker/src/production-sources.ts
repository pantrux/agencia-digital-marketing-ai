import { HttpError, readJson, response, type SqlDatabase } from "./platform.js";

type SourceEnv = { DB: SqlDatabase; BILLING_MODE: "sandbox" | "production" };
type Source = { name: string; bytes: number; sha256: string; extension: string };
type SourceSet = { manifest: string; manifest_sha256: string };
export const sourceSetRoute = /^\/api\/orders\/([a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12})\/sources$/u;

function sources(value: unknown): Source[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 32) throw new HttpError(422, "MATERIALES_INVALIDOS");
  return value.map((item: unknown) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) throw new HttpError(422, "MATERIALES_INVALIDOS");
    const row = item as Record<string, unknown>;
    if (Object.keys(row).sort().join(",") !== "bytes,extension,name,sha256" ||
        typeof row.name !== "string" || row.name.length < 1 || row.name.length > 255 ||
        /[\p{Cc}\p{Cf}\p{Cs}]/u.test(row.name) || typeof row.bytes !== "number" ||
        !Number.isSafeInteger(row.bytes) || row.bytes < 1 || row.bytes > 1024 ** 3 ||
        typeof row.sha256 !== "string" || !/^[a-f0-9]{64}$/u.test(row.sha256) ||
        typeof row.extension !== "string" || !["mp4", "mov", "webm"].includes(row.extension)) {
      throw new HttpError(422, "MATERIALES_INVALIDOS");
    }
    return { name: row.name, bytes: row.bytes, sha256: row.sha256, extension: row.extension };
  });
}

export async function handleSourceSet(request: Request, env: SourceEnv, user: string, order: string): Promise<Response> {
  if (!["GET", "POST"].includes(request.method)) throw new HttpError(405, "METODO_NO_PERMITIDO");
  const db = env.DB.withSession("first-primary");
  const existing = () => db.prepare(`SELECT s.manifest,s.manifest_sha256 FROM production_source_sets s
    JOIN orders o ON o.id=s.order_id AND o.user_id=s.user_id
    WHERE s.order_id=? AND s.user_id=? AND COALESCE(o.provider_mode,'production')=?`)
    .bind(order, user, env.BILLING_MODE).first<SourceSet>();
  const result = async (saved: SourceSet) => {
    const assets = await db.prepare(`SELECT a.id,a.position,a.name,a.bytes,a.sha256,a.extension,
        COALESCE(upload.status,'pending') AS upload_status, outbox.synced_at
      FROM production_sources a JOIN production_source_sets s ON s.order_id=a.order_id
      JOIN orders o ON o.id=s.order_id AND o.user_id=s.user_id
      LEFT JOIN production_source_uploads upload ON upload.source_id=a.id
      LEFT JOIN production_source_outbox outbox ON outbox.source_id=a.id
      WHERE s.order_id=? AND s.user_id=? AND COALESCE(o.provider_mode,'production')=?
      ORDER BY a.position`).bind(order, user, env.BILLING_MODE).all<Source & { id: string; position: number; upload_status: string; synced_at: number | null }>();
    const declared = JSON.parse(saved.manifest) as Source[];
    if (assets.results.length !== declared.length || assets.results.some((asset, index) => {
      const source = declared[index];
      return !source || asset.position !== index || asset.name !== source.name || asset.bytes !== source.bytes ||
        asset.sha256 !== source.sha256 || asset.extension !== source.extension;
    })) throw new HttpError(503, "MATERIALES_INCONSISTENTES");
    return response({ sources: declared, assets: assets.results });
  };
  if (request.method === "GET") {
    const saved = await existing();
    if (!saved) throw new HttpError(404, "MATERIALES_NO_ENCONTRADOS");
    return result(saved);
  }
  const body = await readJson(request, 32768);
  if (Object.keys(body).join(",") !== "sources") throw new HttpError(422, "MATERIALES_INVALIDOS");
  const manifest = JSON.stringify(sources(body.sources));
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(manifest))),
    (byte) => byte.toString(16).padStart(2, "0")).join("");
  await db.prepare(`INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256)
    SELECT o.id,o.user_id,?,? FROM orders o JOIN usage_ledger u ON u.order_id=o.id AND u.user_id=o.user_id
    WHERE o.id=? AND o.user_id=? AND COALESCE(o.provider_mode,'production')=?
      AND o.kind='production' AND o.status='queued' AND u.status='reserved'
      AND NOT EXISTS(SELECT 1 FROM upload_sessions s WHERE s.order_id=o.id)
    ON CONFLICT(order_id) DO NOTHING`).bind(manifest, hash, order, user, env.BILLING_MODE).run();
  const saved = await existing();
  if (!saved) throw new HttpError(409, "MATERIALES_NO_DISPONIBLES");
  if (saved.manifest_sha256 !== hash || saved.manifest !== manifest) throw new HttpError(409, "MATERIALES_EN_CONFLICTO");
  return result(saved);
}
