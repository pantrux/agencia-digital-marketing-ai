import { createHash, createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { describe, expect, it, vi } from "vitest";
import {
  expireAbandonedReservations, handleIngest, parseListedParts, type IngestBucket, type IngestEnv,
} from "../cloudflare-saas/studio-api-worker/src/ingest.js";
import { signR2UploadPart } from "../src/media/r2-signing.js";
import type { SqlDatabase, SqlRows, SqlStatement } from "../cloudflare-saas/studio-api-worker/src/platform.js";
import { handleProductionDispatch, productionDispatchPath } from "../cloudflare-saas/studio-api-worker/src/production-dispatch.js";
import { handleProductionBrief } from "../cloudflare-saas/studio-api-worker/src/production-brief.js";
import worker from "../cloudflare-saas/studio-api-worker/src/index.js";
import { describePlan, listOrders, reserveOrder } from "../cloudflare-saas/studio-api-worker/src/orders.js";
import { completeSourceUpload, createSourceUpload, listSourceParts, signSourcePart } from "../cloudflare-saas/studio-api-worker/src/source-upload.js";
import { dispatchSourceOutbox, syncQueue, type JobEnv, type JobMessage } from "../cloudflare-saas/studio-api-worker/src/jobs.js";

const order = "10b4d244-3ae5-4ea8-b56e-ddd35bfbdeed";
const asset = "2037baa2-5189-4616-b6cc-cd9819e75543";
const credentials = { accountId: "1".repeat(32), accessKeyId: "a".repeat(32), secretAccessKey: "b".repeat(64) };
const digest = "c".repeat(64);
const uploadId = "a+upload/=id";
const etag = "f".repeat(32);
const job = {
  job_id: order, asset_id: asset, project_slug: "job-fixture", extension: "mp4", bytes: 7,
  sha256: digest, object_key: `ingest/${order}/${asset}.mp4`,
};

function request(path: string, method: string, body?: Record<string, unknown>): Request {
  return new Request(`https://studio.invalid${path}`, {
    method, ...(body ? { body: JSON.stringify(body), headers: { "content-type": "application/json" } } : {}),
  });
}

type SqlValue = string | number | null;

function sqliteAdapter(database: DatabaseSync, countTriggerChanges = false): SqlDatabase {
  type Executable = SqlStatement & { values<T>(): SqlRows<T> };
  function prepared(sql: string, values: SqlValue[] = []): Executable {
    const statement = database.prepare(sql);
    return {
      bind: (...params) => prepared(sql, params),
      first: async <T>() => (statement.get(...values) as T | undefined) ?? null,
      all: async <T>() => ({ results: statement.all(...values) as T[], meta: {} }),
      run: async () => {
        const before = countTriggerChanges ? Number((database.prepare('SELECT total_changes() AS n').get() as {n:number}).n) : 0;
        const direct = Number(statement.run(...values).changes);
        const changes = countTriggerChanges ? Number((database.prepare('SELECT total_changes() AS n').get() as {n:number}).n) - before : direct;
        return { results: [], meta: { changes } };
      },
      values: <T>() => statement.columns().length > 0
        ? { results: statement.all(...values) as T[], meta: { changes: 0 } }
        : { results: [], meta: { changes: Number(statement.run(...values).changes) } },
    };
  }
  return {
    prepare: prepared,
    withSession: () => ({ prepare: prepared }),
    batch: async <T>(statements: SqlStatement[]) => {
      database.exec("BEGIN");
      try {
        const output = statements.map((value) => (value as Executable).values<T>());
        database.exec("COMMIT");
        return output;
      } catch (error) {
        database.exec("ROLLBACK");
        throw error;
      }
    },
  };
}

function databaseForIngest(output: "1080p" | "4k" = "1080p", format: "mp4" | "prores4444" = "mp4", edit: "full" | "reel" = "full"): DatabaseSync {
  const db = new DatabaseSync(":memory:");
  const schema = new URL("../cloudflare-saas/migrations/0001_schema.sql", import.meta.url);
  db.exec(readFileSync(schema, "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0003_delivery_kits.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0009_production_resolution.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0011_production_output_format.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0012_delivery_prores.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0014_upload_abort_reason.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0015_brand_profiles.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0018_production_edit.sql", import.meta.url), "utf-8"));
  const now = Math.floor(Date.now() / 1000);
  const plan = format === "prores4444" ? "ilimitado" : output === "4k" ? "ultra" : "free";
  db.exec("INSERT INTO users (id, google_sub, email) VALUES ('user', 'google-user', 'fixture@example.invalid')");
  if (plan !== "free") {
    db.prepare(`INSERT INTO subscriptions(id,user_id,plan_id,status,billing_status,provider_mode,external_reference,
      provider_subscription_id,current_period_start,current_period_end)
      VALUES('paid-test','user',?,'active','paid','production','ref-fixture','mandato-sintetico',?,?)`).run(plan, now - 60, now + 60);
  }
  db.prepare(`INSERT INTO orders (id,user_id,plan_id,kind,status,idempotency_key,request_sha256,
    project_slug,queue_priority,created_at,output_resolution,output_format,output_edit)
    SELECT ?, 'user', plan_id,'production','queued','key',?,'job-fixture',queue_priority,?,?,?,?
    FROM plan_quotas WHERE plan_id=?`).run(order, digest, now, output, format, edit, plan);
  db.prepare(`INSERT INTO usage_ledger (id,user_id,order_id,period_start,created_at)
    VALUES ('ledger','user',?,strftime('%Y-%m-01',?,'unixepoch'),?)`).run(order, now, now);
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0006_production_portal_origin.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0008_production_qa_rejection.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0010_production_manual_review.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0016_production_manual_adjustments.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0017_upload_put_observations.sql", import.meta.url), "utf-8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0023_production_sources.sql", import.meta.url), "utf-8"));
  return db;
}

it("aísla los perfiles de marca y conserva la versión que tomó cada pedido", async () => {
  const db = databaseForIngest();
  try {
    for (const file of ["0004_production_dispatch.sql", "0005_production_briefs.sql", "0019_brief_ad_cta.sql", "0020_brief_ad_contacts.sql", "0021_brief_ad_engine.sql"]) {
      db.exec(readFileSync(new URL(`../cloudflare-saas/migrations/${file}`, import.meta.url), "utf8"));
    }
    db.exec(`INSERT INTO users (id,google_sub,email) VALUES
      ('otra','google-otra','otra@example.invalid'),('free','google-free','free@example.invalid');
      DELETE FROM usage_ledger; DELETE FROM orders`);
    const now = Math.floor(Date.now() / 1000);
    const grant = db.prepare(`INSERT INTO subscriptions(id,user_id,plan_id,status,billing_status,provider_mode,
      external_reference,provider_subscription_id,current_period_start,current_period_end)
      VALUES(?,?,'ilimitado','active','paid',?,?,?, ?,?)`);
    for (const [subject, mode] of [["user", "sandbox"], ["otra", "sandbox"], ["user", "production"]] as const) {
      const id = subject + "-" + mode;
      grant.run(id, subject, mode, id, id, now - 300, now + 300);
    }
    const origin = "https://studio.invalid";
    const secret = "clave-aislada-de-las-pruebas-por-marca";
    const tokens = new Map<string, string>();
    for (const subject of ["user", "otra", "free"]) {
      const nonce = "nonce-" + subject;
      const exp = now + 300;
      const encoded = Buffer.from(JSON.stringify({ kind: "session", sub: subject, nonce, exp })).toString("base64url");
      const payload = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + encoded;
      const token = payload + "." + createHmac("sha256", secret).update(payload).digest("base64url");
      db.prepare("INSERT INTO sessions(id,user_id,nonce,expires_at) VALUES (?,?,?,?)")
        .run(createHash("sha256").update(token).digest("hex"), subject, nonce, exp);
      tokens.set(subject, token);
    }
    function send(path: string, method = "GET", body?: Record<string, unknown>, subject = "user",
      mode = "sandbox" as "sandbox" | "production", retry = "marca-pedido") {
      const env = {
        DB: sqliteAdapter(db), PUBLIC_ORIGIN: origin, BILLING_MODE: mode, SOFIA_OWNER_USER_ID: "user",
        STUDIO_JWT_SESSION_SECRET: secret, R2_ACCOUNT_ID: credentials.accountId,
        R2_ACCESS_KEY_ID: credentials.accessKeyId, R2_SECRET_ACCESS_KEY: credentials.secretAccessKey,
      } as Parameters<typeof worker.fetch>[1];
      const token = tokens.get(subject);
      return worker.fetch(new Request(origin + path, {
        method, headers: { Origin: origin, "Content-Type": "application/json", "Idempotency-Key": retry,
          ...(token ? { Cookie: "__Host-studio_session=" + token } : {}) },
        body: body ? JSON.stringify(body) : null,
      }), env, { waitUntil: vi.fn() });
    }
    expect((await send("/api/brands", "GET", undefined, "sin-sesion")).status).toBe(401);
    expect((await send("/api/brands", "GET", undefined, "free")).status).toBe(403);
    const first = { display_name: "Café Norte", audience: "Tiendas del norte", tone: "Directo" };
    const create = await send("/api/brands", "POST", first);
    expect(create.status).toBe(201);
    const { brand } = await create.json();
    expect(brand).toMatchObject(first);
    expect(await (await send("/api/brands", "POST", first)).json()).toEqual({ brand });
    expect((await send("/api/brands", "POST", { ...first, display_name: "CAFÉ NORTE", tone: "Otro" })).status).toBe(409);
    const second = { display_name: "Café Sur", audience: "Consumidores del sur", tone: "Cercano" };
    expect((await send("/api/brands", "POST", second)).status).toBe(201);
    expect(await (await send("/api/brands")).json()).toMatchObject({ brands: [first, second] });
    expect(await (await send("/api/brands", "GET", undefined, "otra")).json()).toEqual({ brands: [] });
    expect(await (await send("/api/brands", "GET", undefined, "user", "production")).json()).toEqual({ brands: [] });
    expect((await send(`/api/brands/${brand.id}`, "PUT", first, "otra")).status).toBe(404);

    const request = { kind: "production", brand_id: brand.id };
    expect((await send("/api/orders", "POST", request, "otra")).status).toBe(404);
    expect((await send("/api/orders", "POST", request, "free")).status).toBe(403);
    const reserve = await send("/api/orders", "POST", request);
    expect(reserve.status).toBe(201);
    const saved = await reserve.json();
    expect(saved).toMatchObject({ brand: { id: brand.id, display_name: first.display_name } });
    expect(db.prepare("SELECT COUNT(*) AS n FROM usage_ledger WHERE order_id=?").get(saved.id)?.n).toBe(1);
    const update = { ...first, display_name: "Café Renovado", tone: "Actualizado" };
    expect((await send(`/api/brands/${brand.id}`, "PUT", update)).status).toBe(200);
    expect(await (await send("/api/orders", "POST", request)).json()).toEqual(saved);
    expect((await send("/api/orders", "POST", { kind: "production" })).status).toBe(409);
    expect(await (await send("/api/orders")).json()).toMatchObject({ orders: [saved] });
    const brief = await (await send(`/api/orders/${saved.id}/brief`)).json();
    expect(brief).toMatchObject({ brand: { id: brand.id, ...first }, brief: null });
    expect((await send(`/api/orders/${saved.id}/brief`, "GET", undefined, "otra")).status).toBe(404);
    db.prepare("UPDATE usage_ledger SET status='consumed' WHERE order_id=?").run(saved.id);
    db.prepare("UPDATE orders SET status='processing' WHERE id=?").run(saved.id);
    db.prepare("INSERT INTO production_dispatch(order_id,available_at,received_at) VALUES (?,unixepoch(),unixepoch())")
      .run(saved.id);
    const picked = { objective: "Presentar la colección", audience: first.audience, tone: first.tone };
    const confirm = await send(`/api/orders/${saved.id}/brief`, "POST", picked);
    expect(confirm.status).toBe(200);
    expect(await confirm.json()).toMatchObject({ brief: picked, brand: first });
    expect(db.prepare("SELECT audience,tone FROM production_briefs WHERE order_id=?").get(saved.id))
      .toMatchObject({ audience: first.audience, tone: first.tone });
  } finally { db.close(); }
});

it.each([["full", "mp4"], ["reel", "mp4"], ["reel", "prores4444"]] as const)("confirma el brief de una orden %s con %s sin duplicarlo ni editarlo después", async (edit, format) => {
  const db = databaseForIngest("1080p", format, edit);
  try {
    for (const file of ["0004_production_dispatch.sql", "0005_production_briefs.sql", "0019_brief_ad_cta.sql", "0020_brief_ad_contacts.sql", "0021_brief_ad_engine.sql"]) {
      db.exec(readFileSync(new URL(`../cloudflare-saas/migrations/${file}`, import.meta.url), "utf8"));
    }
    db.prepare("UPDATE orders SET provider_mode='sandbox' WHERE id=?").run(order);
    const env = { DB: sqliteAdapter(db), BILLING_MODE: "sandbox" as const };
    const brief = { objective: "Invitar a reservar una demo", audience: "Dueños de negocios", tone: "Directo",
      ...(edit === "reel" ? { ad_cta: "Escríbenos para reservar", ad_brand: "Vivero de prueba" } : {}) };
    const send = (who: string, data?: typeof brief & { ad_contacts?: unknown; ad_engine?: string }) => handleProductionBrief(
      request(`/api/orders/${order}/brief`, data ? "POST" : "GET", data), env, who, order,
    );
    const owned = async () => (await (await listOrders("user", env)).json()).orders[0];
    expect(await owned()).toMatchObject({ status: "queued", received_at: null, brief_confirmed_at: null });
    await expect(send("otro", brief)).rejects.toMatchObject({ status: 404 });
    await expect(send("user", brief)).rejects.toMatchObject({ status: 409 });
    db.prepare("UPDATE usage_ledger SET status='consumed' WHERE order_id=?").run(order);
    db.prepare("UPDATE orders SET status='processing' WHERE id=?").run(order);
    await expect(send("user", { ...brief, audience: " " })).rejects.toMatchObject({ status: 422 });
    await expect(send("user", brief)).rejects.toMatchObject({ status: 409 });
    db.prepare(`INSERT INTO production_dispatch(order_id,available_at,received_at) VALUES (?,unixepoch(),unixepoch())`).run(order);
    expect(await owned()).toMatchObject({ status: "processing", received_at: expect.any(Number), brief_confirmed_at: null });
    if (edit === "full") {
      await expect(send("user", { ...brief, ad_cta: "Escríbenos" })).rejects.toMatchObject({ status: 422 });
    } else {
      expect(await (await send("user")).json()).toMatchObject({ ad_available: true });
      await expect(send("user", { ...brief, ad_cta: "Escríbenos\nAhora" })).rejects.toMatchObject({ status: 422 });
    }
    const contacts = { channel: "instagram", facebook: "https://facebook.com/viverodeprueba",
      instagram: "https://instagram.com/vivero.prueba/", whatsapp: "https://wa.me/56911112222" };
    const valid = edit === "reel" ? { ...brief, ad_contacts: contacts, ad_engine: "hyperframes" } : brief;
    if (edit === "reel") {
      await expect(send("user", { ...brief, ad_engine: "otro" })).rejects.toMatchObject({ status: 422 });
      for (const ad_contacts of [
        { ...contacts, channel: "otro" },
        { channel: "facebook", instagram: contacts.instagram },
        { ...contacts, facebook: "https://www.facebook.com.ejemplo.invalid/perfil" },
        { ...contacts, instagram: "https://instagram.com/p/video" },
        { ...contacts, whatsapp: "https://wa.me/056911112222" },
        { ...contacts, instagram: "https://instagram.com/perfil?redireccion=1" },
      ]) await expect(send("user", { ...brief, ad_contacts })).rejects.toMatchObject({ status: 422, code: "CTA_DESTINO_INVALIDO" });
    } else {
      await expect(send("user", { ...brief, ad_contacts: contacts, ad_brand: "Vivero" })).rejects.toMatchObject({ status: 422 });
    }
    const stored = await (await send("user", valid)).json();
    if (edit === "reel") {
      expect(stored.brief.ad_contacts).toEqual({
        channel: "instagram", facebook: "https://www.facebook.com/viverodeprueba",
        instagram: "https://www.instagram.com/vivero.prueba", whatsapp: contacts.whatsapp,
      });
      await expect(send("user", { ...brief, ad_contacts: { ...contacts, channel: "facebook" } }))
        .rejects.toMatchObject({ status: 409 });
    }
    expect(stored).toMatchObject({ brief: { ...brief, confirmed_at: expect.any(Number) } });
    expect(await owned()).toMatchObject({ brief_confirmed_at: stored.brief.confirmed_at });
    expect(await (await listOrders("otro", env)).json()).toEqual({ orders: [] });
    expect(await (await send("user", valid)).json()).toEqual(stored);
    expect(await (await send("user")).json()).toEqual(stored);
    const lookup = () => handleProductionDispatch(new Request(`https://studio.invalid${productionDispatchPath}`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${"a".repeat(64)}` },
      body: JSON.stringify({ action: "brief", order_id: order }),
    }), { ...env, STUDIO_PRODUCTION_DISPATCH_TOKEN: "a".repeat(64) });
    const { ad_contacts, ...dispatchBrief } = stored.brief;
    expect(await (await lookup()).json()).toEqual({ brief: { ...dispatchBrief, order_id: order, user_id: "user",
      ...(ad_contacts ? { ad_destination: { channel: "instagram", url: "https://www.instagram.com/vivero.prueba" } } : {}) } });
    await expect(send("user", { ...brief, objective: "Otro" })).rejects.toMatchObject({ status: 409 });
    await expect(send("otro")).rejects.toMatchObject({ status: 404 });
    expect(db.prepare("SELECT COUNT(*) AS total FROM production_briefs").get()).toMatchObject({ total: 1 });
    db.prepare("UPDATE orders SET status='cancelled' WHERE id=?").run(order);
    expect(await (await lookup()).json()).toEqual({ brief: null });
  } finally { db.close(); }
});

it.each([["1080p", "mp4", "free", "full"], ["4k", "mp4", "ultra", "full"], ["4k", "prores4444", "ilimitado", "reel"]] as const)(
  "despacha una sola producción %s %s del plan %s con montaje %s", async (output, format, plan, edit) => {
  const db = databaseForIngest(output, format, edit);
  try {
    const migration = new URL("../cloudflare-saas/migrations/0004_production_dispatch.sql", import.meta.url);
    db.exec(readFileSync(migration, "utf8"));
    db.prepare(`INSERT INTO upload_sessions (id,user_id,order_id,object_key,sha256,bytes,mime_type,extension,
      upload_id,status,object_etag,created_at,expires_at) VALUES
      ('upload','user',?,?,?,7,'video/mp4','mp4','multipart','completed','etag',unixepoch(),unixepoch()+60)`)
      .run(order, job.object_key, digest);
    db.prepare("INSERT INTO job_outbox (order_id,payload) VALUES (?,?)").run(order, JSON.stringify(job));
    const secret = "a".repeat(64);
    const env = { DB: sqliteAdapter(db), BILLING_MODE: "production" as const, STUDIO_PRODUCTION_DISPATCH_TOKEN: secret };
    const send = (value: object, token = secret) => handleProductionDispatch(new Request(`https://studio.invalid${productionDispatchPath}`, {
      method: "POST", body: JSON.stringify(value), headers: { "content-type": "application/json", Authorization: `Bearer ${token}` },
    }), env);
    await expect(send({ action: "claim" }, "b".repeat(64))).rejects.toMatchObject({ status: 401 });
    expect(await (await send({ action: "claim" })).json()).toEqual({ job: null });
    db.exec("UPDATE usage_ledger SET status='consumed'; UPDATE orders SET status='processing'");
    const result = await (await send({ action: "claim" })).json() as { job: { lease: string; order_id: string } };
    expect(result.job).toMatchObject({ order_id: order, upload_id: "upload",
      plan_id: plan, mode: "production", media: job });
    if (output === "4k") expect(result.job).toHaveProperty("output_resolution", "4k");
    else expect(result.job).not.toHaveProperty("output_resolution");
    if (format === "prores4444") expect(result.job).toHaveProperty("output_format", "prores4444");
    else expect(result.job).not.toHaveProperty("output_format");
    if (edit === "reel") expect(result.job).toHaveProperty("output_edit", "reel");
    else expect(result.job).not.toHaveProperty("output_edit");
    expect(() => db.prepare("UPDATE orders SET output_edit=?").run(edit === "reel" ? "full" : "reel")).toThrow("OUTPUT_EDIT_IMMUTABLE");
    expect(await (await send({ action: "claim" })).json()).toEqual({ job: null });
    db.exec("UPDATE production_dispatch SET available_at=0");
    const next = await (await send({ action: "claim" })).json() as { job: { lease: string } };
    await expect(send({ action: "ack", order_id: order, lease: result.job.lease })).rejects.toMatchObject({ status: 409 });
    expect(await (await send({ action: "ack", order_id: order, lease: next.job.lease })).json()).toEqual({ received: true });
    expect(await (await send({ action: "claim" })).json()).toEqual({ job: null });
    expect(db.prepare("SELECT attempts, received_at FROM production_dispatch").get()).toMatchObject({ attempts: 2, received_at: expect.any(Number) });
  } finally { db.close(); }
});

it.each([false, true])("el despacho múltiple exige que todas las fuentes lleguen al NAS: %s", async (allSynced) => {
  const db = databaseForIngest();
  try {
    for (const file of ["0004_production_dispatch.sql", "0024_multiple_production_dispatch.sql"]) {
      db.exec(readFileSync(new URL(`../cloudflare-saas/migrations/${file}`, import.meta.url), "utf8"));
    }
    const manifest = ["uno.mp4", "dos.mp4"].map((name) => ({ name, bytes: 7, sha256: digest, extension: "mp4" }));
    db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
      .run(order, JSON.stringify(manifest), digest);
    const sources = db.prepare("SELECT id FROM production_sources WHERE order_id=? ORDER BY position").all(order) as { id: string }[];
    for (const { id } of sources) {
      db.prepare(`INSERT INTO production_source_uploads(source_id,object_key,upload_id,status,created_at,expires_at)
        VALUES (?,?,'multipart','open',unixepoch(),unixepoch()+3600)`).run(id, `ingest/${order}/${id}.mp4`);
      db.prepare("UPDATE production_source_uploads SET status='completed',object_etag=? WHERE source_id=?").run(etag, id);
    }
    db.prepare("UPDATE production_source_outbox SET synced_at=unixepoch() WHERE source_id=?").run(sources[0]!.id);
    if (allSynced) db.prepare("UPDATE production_source_outbox SET synced_at=unixepoch() WHERE source_id=?").run(sources[1]!.id);
    db.exec("UPDATE usage_ledger SET status='consumed'; UPDATE orders SET status='processing'");
    const token = "a".repeat(64);
    const env = { DB: sqliteAdapter(db), BILLING_MODE: "production" as const, STUDIO_PRODUCTION_DISPATCH_TOKEN: token };
    const result = await (await handleProductionDispatch(new Request(`https://studio.invalid${productionDispatchPath}`, {
      method: "POST", body: JSON.stringify({ action: "claim" }),
      headers: { "content-type": "application/json", Authorization: `Bearer ${token}` },
    }), env)).json() as { job: null | { upload_id: string; media: { asset_id: string }; additional_media: { asset_id: string }[] } };
    if (!allSynced) expect(result.job).toBeNull();
    else {
      expect(result.job?.upload_id).toBe(sources[0]!.id);
      expect(result.job?.media.asset_id).toBe(sources[0]!.id);
      expect(result.job?.additional_media.map((item) => item.asset_id)).toEqual([sources[1]!.id]);
    }
  } finally { db.close(); }
});

it.each([false, true])("registra el dictamen y expone el motivo al titular sin la huella (manual: %s)", async (manual) => {
  const db = databaseForIngest();
  try {
    for (const file of ["0004_production_dispatch.sql", "0005_production_briefs.sql", "0019_brief_ad_cta.sql", "0020_brief_ad_contacts.sql", "0021_brief_ad_engine.sql"]) {
      db.exec(readFileSync(new URL(`../cloudflare-saas/migrations/${file}`, import.meta.url), "utf8"));
    }
    const id = "c4eec711-eb76-4bd2-a6a3-d2bb3a0aae61";
    const user = "f34d0993-3850-4c43-9ea5-7f5a863818de";
    const upload = "0dc21bb9-0263-4d2a-81fe-8c2931d00245";
    const master = "3".repeat(64);
    db.prepare("INSERT INTO users(id,google_sub,email) VALUES (?,'qa-user','qa@example.invalid')").run(user);
    db.prepare(`INSERT INTO orders(id,user_id,plan_id,kind,status,idempotency_key,request_sha256,
      project_slug,queue_priority,provider_mode) VALUES (?,?,'free','production','queued','qa-1',?,'job-qa',0,'sandbox')`)
      .run(id, user, digest);
    db.prepare(`INSERT INTO upload_sessions(id,user_id,order_id,object_key,sha256,bytes,mime_type,extension,
      upload_id,status,object_etag,created_at,expires_at) VALUES (?,?,?,'raw',?,7,'video/mp4','mp4','tok','completed','etag',1,2)`)
      .run(upload, user, id, digest);
    db.prepare(`INSERT INTO usage_ledger(id,user_id,order_id,period_start,created_at)
      SELECT 'qa-ledger',user_id,id,strftime('%Y-%m-01',created_at,'unixepoch'),created_at FROM orders WHERE id=?`).run(id);
    db.prepare("UPDATE usage_ledger SET status='consumed' WHERE order_id=?").run(id);
    db.prepare("UPDATE orders SET status='processing' WHERE id=?").run(id);
    db.prepare("INSERT INTO production_dispatch(order_id,available_at) VALUES (?,10)").run(id);
    db.prepare(`INSERT INTO production_briefs(order_id,user_id,objective,audience,tone,confirmed_at)
      VALUES (?,?,'Autor','Audiencia','',15)`).run(id, user);
    const secret = "a".repeat(64);
    const env = { DB: sqliteAdapter(db), BILLING_MODE: "sandbox" as const, STUDIO_PRODUCTION_DISPATCH_TOKEN: secret };
    const common = { order_id: id, user_id: user, upload_id: upload, source_sha256: digest, revision: 1, brief_confirmed_at: 15 };
    const report = manual ? { ...common, action: "quality_manual_review", direction_sha256: master }
      : { ...common, action: "quality_rejected", master_sha256: master };
    const send = (overrides: object = {}, token = secret, mode: "sandbox" | "production" = "sandbox") =>
      handleProductionDispatch(new Request(`https://studio.invalid${productionDispatchPath}`, {
        method: "POST", headers: { "content-type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ ...report, ...overrides }),
      }), { ...env, BILLING_MODE: mode });
    await expect(send({}, "b".repeat(64))).rejects.toMatchObject({ status: 401 });
    await expect(send()).rejects.toMatchObject({ status: 409 });
    db.prepare("UPDATE production_dispatch SET received_at=10 WHERE order_id=?").run(id);
    await expect(send({}, secret, "production")).rejects.toMatchObject({ status: 409 });
    await expect(send({ upload_id: asset })).rejects.toMatchObject({ status: 409 });
    await expect(send({ source_sha256: "0".repeat(64) })).rejects.toMatchObject({ status: 409 });
    await expect(send({ brief_confirmed_at: 16 })).rejects.toMatchObject({ status: 409 });
    await expect(send({ revision: 2 })).rejects.toMatchObject({ status: 409 });
    await expect(send({ revision: "1" })).rejects.toMatchObject({ status: 422 });
    db.prepare("UPDATE orders SET status='completed' WHERE id=?").run(id);
    await expect(send()).rejects.toMatchObject({ status: 409 });
    db.prepare("UPDATE orders SET status='processing' WHERE id=?").run(id);
    expect(await (await send()).json()).toEqual({ recorded: true });
    await expect(send({ revision: 2 })).rejects.toMatchObject({ status: 409 });
    if (manual) {
      const query = db.prepare("SELECT * FROM production_manual_reviews WHERE order_id=?");
      const saved = query.get(id);
      expect(saved).toMatchObject({ direction_sha256: master, revision: 1 });
      expect(await (await send()).json()).toEqual({ recorded: true });
      await expect(send({ direction_sha256: "4".repeat(64) })).rejects.toMatchObject({ status: 409 });
      const action = (body: object) => handleProductionDispatch(new Request(`https://studio.invalid${productionDispatchPath}`, {
        method: "POST", headers: { "content-type": "application/json", Authorization: `Bearer ${secret}` }, body: JSON.stringify(body),
      }), env);
      await expect(action({ ...common, action: "quality_rejected", master_sha256: master })).rejects.toMatchObject({ status: 409 });
      expect(await (await action({ action: "brief", order_id: id })).json()).toEqual({ brief: null });
      expect(query.get(id)).toEqual(saved);
      expect(db.prepare("SELECT status FROM usage_ledger WHERE order_id=?").get(id)).toEqual({ status: "consumed" });
      const own = await (await listOrders(user, env)).json();
      expect(own.orders[0]).toMatchObject({ id, status: "processing", production_issue: "requiere_revision" });
      expect(JSON.stringify(own)).not.toContain(master);
      expect(await (await listOrders(asset, env)).json()).toEqual({ orders: [] });
      return;
    }
    const recorded = db.prepare("SELECT status,qa_rejected_master_sha256,updated_at FROM orders WHERE id=?").get(id);
    expect(recorded).toMatchObject({ status: "failed", qa_rejected_master_sha256: master });
    expect(await (await send()).json()).toEqual({ recorded: true });
    await expect(send({ master_sha256: "4".repeat(64) })).rejects.toMatchObject({ status: 409 });
    expect(db.prepare("SELECT status,qa_rejected_master_sha256,updated_at FROM orders WHERE id=?").get(id)).toEqual(recorded);
    expect(db.prepare("SELECT status FROM usage_ledger WHERE order_id=?").get(id)).toEqual({ status: "consumed" });
    const own = await (await listOrders(user, env)).json();
    expect(own.orders[0]).toMatchObject({ id, status: "failed", production_issue: "qa_rechazado" });
    expect(JSON.stringify(own)).not.toContain(master);
    expect(await (await listOrders(asset, env)).json()).toEqual({ orders: [] });
  } finally { db.close(); }
});

it.each([
  { plan: "free", priority: 0, limit: 0, outcome: "needs_review" },
  { plan: "pro", priority: 1, limit: 1, outcome: "needs_review" },
  { plan: "ultra", priority: 2, limit: 3, outcome: "ready" },
  { plan: "ultra", priority: 2, limit: 3, outcome: "needs_review" },
  { plan: "ilimitado", priority: 3, limit: null, outcome: "needs_review" },
])("aplica el cupo del ajuste y espera el resultado autenticado en $plan", async ({ plan, priority, limit, outcome }) => {
  const db = databaseForIngest();
  try {
    for (const file of ["0004_production_dispatch.sql", "0005_production_briefs.sql", "0019_brief_ad_cta.sql", "0020_brief_ad_contacts.sql", "0021_brief_ad_engine.sql"]) {
      db.exec(readFileSync(new URL(`../cloudflare-saas/migrations/${file}`, import.meta.url), "utf8"));
    }
    const userId = "9b7eb194-c462-40de-b44c-f37a100601b1";
    const orderId = "2b7d52d2-3700-4b97-ab29-726c233a0001";
    const uploadId = "8b51ae8d-0859-47a1-8c01-3f6feda26731";
    const direction = "1".repeat(64);
    const corrected = "2".repeat(64);
    const secret = "e".repeat(64);
    const jwtSecret = "clave-local-de-pruebas-de-ajustes";
    const now = Math.floor(Date.now() / 1000);
    db.prepare("INSERT INTO users(id,google_sub,email) VALUES (?,'manual-owner','guia@example.invalid')").run(userId);
    if (plan !== "free") db.prepare(`INSERT INTO subscriptions(id,user_id,plan_id,status,billing_status,provider_mode,
      external_reference,provider_subscription_id,current_period_start,current_period_end)
      VALUES('manual-plan',?,?,'active','paid','sandbox','manual-ref','manual-fixture',?,?)`)
      .run(userId, plan, now - 600, now + 600);
    db.prepare(`INSERT INTO orders(id,user_id,plan_id,kind,status,idempotency_key,request_sha256,
      project_slug,queue_priority,provider_mode,created_at) VALUES (?,?,?,'production','queued','manual-orden',?,'job-ajuste',?,'sandbox',?)`)
      .run(orderId, userId, plan, digest, priority, now);
    db.prepare(`INSERT INTO usage_ledger(id,user_id,order_id,period_start,created_at)
      SELECT 'manual-ledger',user_id,id,strftime('%Y-%m-01',created_at,'unixepoch'),created_at FROM orders WHERE id=?`).run(orderId);
    db.prepare("UPDATE usage_ledger SET status='consumed' WHERE order_id=?").run(orderId);
    db.prepare("UPDATE orders SET status='processing' WHERE id=?").run(orderId);
    db.prepare(`INSERT INTO upload_sessions(id,user_id,order_id,object_key,sha256,bytes,mime_type,extension,
      upload_id,status,object_etag,created_at,expires_at) VALUES (?,?,?,'ajuste-crudo',?,7,'video/mp4','mp4','tok','completed','etag',1,2)`)
      .run(uploadId, userId, orderId, digest);
    db.prepare("INSERT INTO production_dispatch(order_id,available_at,received_at) VALUES (?,10,10)").run(orderId);
    db.prepare(`INSERT INTO production_briefs(order_id,user_id,objective,audience,tone,confirmed_at)
      VALUES (?,?,'Presentar colección','Tiendas','Directo',15)`).run(orderId, userId);
    db.prepare("INSERT INTO production_manual_reviews VALUES (?,1,?,16)").run(orderId, direction);

    const origin = "https://studio.invalid";
    const env = {
      DB: sqliteAdapter(db), BILLING_MODE: "sandbox", PUBLIC_ORIGIN: origin,
      STUDIO_PRODUCTION_DISPATCH_TOKEN: secret, STUDIO_JWT_SESSION_SECRET: jwtSecret,
    } as Parameters<typeof worker.fetch>[1];
    function session(user: string) {
      const expires = now + 300;
      const nonce = "ajuste-" + user;
      const header = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.";
      const payload = header + Buffer.from(JSON.stringify({ kind: "session", sub: user, nonce, exp: expires })).toString("base64url");
      const token = payload + "." + createHmac("sha256", jwtSecret).update(payload).digest("base64url");
      db.prepare("INSERT INTO sessions(id,user_id,nonce,expires_at) VALUES (?,?,?,?)")
        .run(createHash("sha256").update(token).digest("hex"), user, nonce, expires);
      return "__Host-studio_session=" + token;
    }
    const ownCookie = session(userId);
    const otherCookie = session("user");
    const dispatch = (body: object, authorization = secret) => worker.fetch(new Request(origin + productionDispatchPath, {
      method: "POST", headers: { Authorization: "Bearer " + authorization, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }), env, { waitUntil: vi.fn() });
    const ask = (text: string, key = "ajuste-1", cookie = ownCookie) =>
      worker.fetch(new Request(origin + "/api/orders/" + orderId + "/manual-adjustment", {
        method: "POST", headers: { Origin: origin, Cookie: cookie, "Content-Type": "application/json", "Idempotency-Key": key },
        body: JSON.stringify({ instructions: text }),
      }), env, { waitUntil: vi.fn() });
    const text = "Usar el título de la colección y subtítulos grandes.";
    expect((await ask(text, "ajuste-1", otherCookie)).status).toBe(404);
    const current = () => worker.fetch(new Request(origin + "/api/orders/" + orderId + "/manual-adjustment", {
      headers: { Cookie: ownCookie },
    }), env, { waitUntil: vi.fn() });
    expect(await (await current()).json()).toMatchObject({ remaining: limit, paused: true, available: plan !== "free" });
    if (plan === "free") {
      expect((await ask(text)).status).toBe(403);
      return;
    }
    const keys = ["ajuste-1", "ajuste-concurrente"];
    const attempts = await Promise.all(keys.map((key) => ask(text, key)));
    expect(attempts.map((reply) => reply.status).sort()).toEqual([201, 409]);
    const acceptedAt = attempts.findIndex((reply) => reply.status === 201);
    const accepted = attempts[acceptedAt]!;
    const originalKey = keys[acceptedAt]!;
    expect(accepted.status).toBe(201);
    expect(await accepted.json()).toMatchObject({ adjustment: { number: 1, status: "pending" } });
    expect((await ask(text, originalKey)).status).toBe(200);
    expect((await ask("Otro texto", originalKey)).status).toBe(409);
    expect((await ask(text, "ajuste-2")).status).toBe(409);
    expect(await (await dispatch({ action: "brief", order_id: orderId })).json()).toEqual({ brief: null });
    const context = { action: "manual_context", order_id: orderId };
    expect((await dispatch(context, "f".repeat(64))).status).toBe(401);
    expect(await (await dispatch(context)).json()).toMatchObject({
      adjustment: { number: 1, review_sha256: direction, instructions: text, revision: 1 },
      brief: { order_id: orderId, confirmed_at: 15, objective: "Presentar colección" },
    });
    const result = { action: "manual_adjustment_result", order_id: orderId, user_id: userId, upload_id: uploadId,
      source_sha256: digest, revision: 1, brief_confirmed_at: 15, number: 1, review_sha256: direction,
      direction_sha256: corrected, status: outcome };
    expect((await dispatch({ ...result, upload_id: asset })).status).toBe(409);
    expect(await (await dispatch(result)).json()).toEqual({ recorded: true });
    expect(await (await dispatch(result)).json()).toEqual({ recorded: true });
    const opposite = outcome === "ready" ? "needs_review" : "ready";
    expect((await dispatch({ ...result, status: opposite })).status).toBe(409);
    if (outcome === "ready") {
      expect(await (await dispatch({ action: "brief", order_id: orderId })).json()).toMatchObject({
        brief: { confirmed_at: 15, objective: "Presentar colección" },
      });
      expect((await dispatch({ action: "quality_manual_review", order_id: orderId, user_id: userId,
        upload_id: uploadId, source_sha256: digest, direction_sha256: direction,
        revision: 1, brief_confirmed_at: 15 })).status).toBe(409);
      expect(db.prepare("SELECT * FROM production_manual_reviews WHERE order_id=?").get(orderId)).toBeUndefined();
    } else {
      expect(await (await dispatch({ action: "brief", order_id: orderId })).json()).toEqual({ brief: null });
      expect(db.prepare("SELECT direction_sha256 FROM production_manual_reviews WHERE order_id=?").get(orderId))
        .toEqual({ direction_sha256: corrected });
      const limitForTest = limit ?? 4;
      for (let number = 2; number <= limitForTest; number += 1) {
        expect((await ask(text, "ajuste-" + number)).status).toBe(201);
        expect((await dispatch({ ...result, number, review_sha256: corrected })).status).toBe(200);
      }
      if (limit !== null) {
        const excess = await ask(text, "ajuste-" + (limit + 1));
        expect(excess.status).toBe(409);
        expect(await excess.json()).toEqual({ error: "AJUSTES_AGOTADOS" });
      } else {
        expect(await (await current()).json()).toMatchObject({ remaining: null, available: true });
      }
    }
    expect(db.prepare("SELECT status FROM usage_ledger WHERE order_id=?").get(orderId)?.status).toBe("consumed");
  } finally { db.close(); }
});

function mockMedia(body: Uint8Array, invalidMime = false) {
  let key: string;
  let metadata: Parameters<IngestBucket["createMultipartUpload"]>[1];
  let committed = false;
  const object = () => ({
    key, size: body.length, etag: "f".repeat(32) + "-1",
    ...metadata,
    httpMetadata: invalidMime ? { contentType: "video/webm" } : metadata.httpMetadata,
    body: new ReadableStream({ start(controller) { controller.enqueue(body); controller.close(); } }),
  });
  const complete = vi.fn(async () => { committed = true; return object(); });
  const abort = vi.fn(async () => {});
  const handle = { uploadId, complete, abort };
  const bucket: IngestBucket = {
    createMultipartUpload: vi.fn(async (objectKey, options) => { key = objectKey; metadata = options; return handle; }),
    resumeMultipartUpload: vi.fn(() => handle),
    head: vi.fn(async () => committed ? object() : null),
    get: vi.fn(async () => committed ? object() : null),
    delete: vi.fn(async () => { committed = false; }),
  };
  return { bucket, handle };
}

function installDigestStream(): () => void {
  const original = Object.getOwnPropertyDescriptor(crypto, "DigestStream");
  class DigestStream extends WritableStream<Uint8Array> {
    digest: Promise<ArrayBuffer>;
    constructor() {
      const hash = createHash("sha256");
      let done: (value: ArrayBuffer) => void = () => {};
      const digestPromise = new Promise<ArrayBuffer>((resolve) => { done = resolve; });
      super({ write: (chunk) => { hash.update(chunk); }, close: () => { done(Uint8Array.from(hash.digest()).buffer); } });
      this.digest = digestPromise;
    }
  }
  Object.defineProperty(crypto, "DigestStream", { configurable: true, value: DigestStream });
  return () => {
    if (original) Object.defineProperty(crypto, "DigestStream", original);
    else Reflect.deleteProperty(crypto, "DigestStream");
  };
}

describe("Ingesta R2 multipart", () => {
  it("registra una vez los rechazos observados por el titular y conserva el estado de la carga", async () => {
    const db = databaseForIngest();
    try {
      const { bucket } = mockMedia(new Uint8Array(7));
      const env: IngestEnv = { DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production",
        R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
        R2_SECRET_ACCESS_KEY: credentials.secretAccessKey };
      const submitted = await handleIngest(request("/api/uploads", "POST", {
        order_id: order, project_slug: job.project_slug, extension: "mp4", bytes: 7,
        sha256: digest, content_type: "video/mp4",
      }), env, "user", "/api/uploads");
      const { id } = await submitted.json() as { id: string };
      const path = "/api/uploads/" + id + "/part-observations";
      const submit = (body: Record<string, unknown>, user = "user", settings = env) =>
        handleIngest(request(path, "POST", body), settings, user, path);
      const seen = { http_403: true, sin_respuesta: false };
      await expect(submit(seen, "otra")).rejects.toMatchObject({ status: 404 });
      await expect(submit(seen, "user", { ...env, BILLING_MODE: "sandbox" })).rejects.toMatchObject({ status: 404 });
      await expect(submit({ ...seen, url: "valor-ajeno" })).rejects.toMatchObject({ status: 422 });
      expect((await submit(seen)).status).toBe(204);
      db.exec("UPDATE upload_sessions SET status='completed',object_etag='etag'");
      expect((await submit({ http_403: false, sin_respuesta: true })).status).toBe(204);
      expect((await submit(seen)).status).toBe(204);
      expect(db.prepare(`SELECT status, direct_put_403_observed, direct_put_no_response_observed
        FROM upload_sessions`).get()).toMatchObject({
        status: "completed", direct_put_403_observed: 1, direct_put_no_response_observed: 1,
      });
      expect(db.prepare("SELECT status FROM usage_ledger").get()?.status).toBe("reserved");
      expect(db.prepare("SELECT COUNT(*) AS total FROM job_outbox").get()?.total).toBe(0);
    } finally { db.close(); }
  });

  it("recupera por cron las cuotas vencidas sin interrumpir confirmaciones ni otros entornos", async () => {
    const db = databaseForIngest();
    try {
      const now = Math.floor(Date.now() / 1000);
      db.exec("DELETE FROM usage_ledger; DELETE FROM orders");
      for (const [id, state, age, mode, activeLease] of [
        ["sin-sesion", null, 90000, "production", false],
        ["vencida", "open", 90000, "production", false],
        ["vigente", "open", 60, "production", false],
        ["creando", "creating", 90000, "production", true],
        ["confirmando", "completing", 90000, "production", false],
        ["confirmada", "completed", 90000, "production", false],
        ["otro-entorno", null, 90000, "sandbox", false],
      ] as const) {
        const created = now - age;
        db.prepare("INSERT INTO users(id,google_sub,email) VALUES (?,?,?)")
          .run(id, id, id + "@fixture.invalid");
        db.prepare(`INSERT INTO orders (id,user_id,plan_id,kind,status,idempotency_key,request_sha256,
          project_slug,queue_priority,provider_mode,created_at) VALUES (?,?,'free','production','queued',
          'fixture',?,?,0,?,?)`).run(id, id, digest, "job-" + id, mode, created);
        db.prepare(`INSERT INTO usage_ledger (id,user_id,order_id,period_start,created_at)
          VALUES (?,?,?,strftime('%Y-%m-01',?,'unixepoch'),?)`).run(id, id, id, created, created);
        if (state) {
          const transition = state === "creating" || state === "completing";
          db.prepare(`INSERT INTO upload_sessions (
            id,user_id,order_id,object_key,sha256,bytes,mime_type,extension,upload_id,status,
            operation_id,lease_until,object_etag,created_at,expires_at
          ) VALUES (?,?,?,?,?,7,'video/mp4','mp4',?,?,?,?,?,?,?)`)
            .run(id, id, id, "ingest/" + id, digest, state === "creating" ? null : id, state,
              transition ? id : null, transition ? now + (activeLease ? 60 : -60) : null,
              state === "completed" ? etag : null, created, created + 86400);
        }
      }
      const env = { DB: sqliteAdapter(db), BILLING_MODE: "production" } as Parameters<typeof worker.scheduled>[1];
      const invoke = async () => {
        const tasks: Promise<unknown>[] = [];
        worker.scheduled({}, env, { waitUntil: (task) => { tasks.push(task); } });
        await Promise.all(tasks);
      };
      await invoke();
      await invoke();
      expect(db.prepare("SELECT id FROM usage_ledger WHERE status='released' ORDER BY id").all())
        .toEqual([{ id: "sin-sesion" }, { id: "vencida" }]);
      expect(db.prepare("SELECT id FROM orders WHERE status='cancelled' ORDER BY id").all())
        .toEqual([{ id: "sin-sesion" }, { id: "vencida" }]);
      expect(db.prepare("SELECT id,abort_reason FROM upload_sessions WHERE status='aborted'").all())
        .toEqual([{ id: "vencida", abort_reason: "expired" }]);
    } finally { db.close(); }
  });

  it("distingue una reserva cancelada para que el cliente pueda iniciar otra carga", async () => {
    const db = databaseForIngest();
    try {
      expect(db.prepare("SELECT portal_started_at FROM orders").get()?.portal_started_at).toBeNull();
      db.exec("DELETE FROM usage_ledger; DELETE FROM orders");
      const env = { DB: sqliteAdapter(db), BILLING_MODE: "sandbox" as const, SOFIA_OWNER_USER_ID: "user" };
      const create = (key: string) => reserveOrder(new Request("https://studio.invalid/api/orders", {
        method: "POST", headers: { "content-type": "application/json", "idempotency-key": key },
        body: JSON.stringify({ kind: "production" }),
      }), "user", env);
      expect((await create("primer-intento")).status).toBe(201);
      expect((await create("primer-intento")).status).toBe(200);
      db.exec("UPDATE usage_ledger SET status='released'; UPDATE orders SET status='cancelled'");
      await expect(create("primer-intento")).rejects.toMatchObject({ status: 410, code: "ORDEN_CANCELADA" });
      expect((await create("nuevo-intento")).status).toBe(201);
      expect(db.prepare("SELECT status FROM usage_ledger ORDER BY status").all())
        .toEqual([{ status: "released" }, { status: "reserved" }]);
    } finally { db.close(); }
  });

  it("conserva la resolución predeterminada en reintentos y rechaza 4K en Free sin reservar cuota", async () => {
    const db = databaseForIngest();
    try {
      db.exec("DELETE FROM usage_ledger; DELETE FROM orders");
      const env = { DB: sqliteAdapter(db), BILLING_MODE: "sandbox" as const, SOFIA_OWNER_USER_ID: "user" };
    const create = (key: string, output?: string, format?: string) => reserveOrder(new Request("https://studio.invalid/api/orders", {
        method: "POST", headers: { "content-type": "application/json", "idempotency-key": key },
      body: JSON.stringify({ kind: "production", ...(output === undefined ? {} : { output_resolution: output }),
        ...(format === undefined ? {} : { output_format: format }) }),
      }), "user", env);
      await expect(create("four", "4k")).rejects.toMatchObject({ status: 422, code: "RESOLUCION_FUERA_DEL_PLAN" });
      expect(db.prepare("SELECT COUNT(*) AS n FROM usage_ledger").get()?.n).toBe(0);
      expect((await create("default")).status).toBe(201);
      const replay = await create("default", "1080p", "mp4");
      expect(replay.status).toBe(200);
      expect(await replay.json()).toMatchObject({ output_resolution: "1080p", output_format: "mp4" });
      const legacyHash = createHash("sha256").update('{"kind":"production","mode":"sandbox"}').digest("hex");
      expect(db.prepare("SELECT request_sha256 FROM orders").get()?.request_sha256).toBe(legacyHash);
      await expect(create("default", "4k")).rejects.toMatchObject({ status: 409 });
      expect(() => db.exec("UPDATE orders SET output_resolution='4k'")).toThrow();
    } finally { db.close(); }
  });

  it("reserva Ultra 4K para la cuenta con cobertura activa de ensayo y mantiene la resolución", async () => {
    const db = databaseForIngest();
    try {
      db.exec("DELETE FROM usage_ledger; DELETE FROM orders");
      const now = Math.floor(Date.now() / 1000);
      db.prepare(`INSERT INTO subscriptions(id,user_id,plan_id,status,billing_status,provider_mode,external_reference,
        provider_subscription_id,current_period_start,current_period_end)
        VALUES('suscripcion-ultra','user','ultra','active','paid','sandbox','ensayo','mandato-sintetico',?,?)`).run(now - 60, now + 60);
      const env = { DB: sqliteAdapter(db), BILLING_MODE: "sandbox" as const, SOFIA_OWNER_USER_ID: "another-owner" };
      expect(await (await describePlan("user", env)).json()).toMatchObject({ plan_id: "ultra", production_available: true });
      const create = (resolution: string) => reserveOrder(new Request("https://studio.invalid/api/orders", {
        method: "POST", headers: { "content-type": "application/json", "idempotency-key": "ultra4k" },
        body: JSON.stringify({ kind: "production", output_resolution: resolution }),
      }), "user", env);
      expect(await (await create("4k")).json()).toMatchObject({ plan_id: "ultra", output_resolution: "4k" });
      expect((await create("4k")).status).toBe(200);
      await expect(create("1080p")).rejects.toMatchObject({ status: 409 });
      expect(db.prepare("SELECT status FROM usage_ledger").all()).toEqual([{status: "reserved"}]);
    } finally { db.close(); }
  });

  it("reserva ProRes solo para Ilimitado y exige el formato original en un reintento", async () => {
    const db = databaseForIngest();
    try {
      db.exec("DELETE FROM usage_ledger; DELETE FROM orders");
      const now = Math.floor(Date.now() / 1000);
      const env = { DB: sqliteAdapter(db), BILLING_MODE: "sandbox" as const, SOFIA_OWNER_USER_ID: "user" };
      const create = (format?: string) => reserveOrder(new Request("https://studio.invalid/api/orders", {
        method: "POST", headers: { "content-type": "application/json", "idempotency-key": "prores" },
        body: JSON.stringify({ kind: "production", ...(format === undefined ? {} : { output_format: format }) }),
      }), "user", env);
      await expect(create("mov")).rejects.toMatchObject({ status: 422, code: "ORDEN_INVALIDA" });
      await expect(create("prores4444")).rejects.toMatchObject({ status: 422, code: "FORMATO_FUERA_DEL_PLAN" });
      expect(db.prepare("SELECT COUNT(*) AS n FROM usage_ledger").get()?.n).toBe(0);
      db.prepare(`INSERT INTO subscriptions(id,user_id,plan_id,status,billing_status,provider_mode,external_reference,
        provider_subscription_id,current_period_start,current_period_end)
        VALUES('suscripcion-ilimitado','user','ilimitado','active','paid','sandbox','ensayo','mandato-sintetico',?,?)`)
        .run(now - 60, now + 60);
      expect(await (await describePlan("user", env)).json()).toMatchObject({ allow_prores4444: 1 });
      const initial = await create("prores4444");
      expect(initial.status).toBe(201);
      expect(await initial.json()).toMatchObject({ plan_id: "ilimitado", output_format: "prores4444" });
      expect((await create("prores4444")).status).toBe(200);
      await expect(create()).rejects.toMatchObject({ status: 409, code: "IDEMPOTENCIA_EN_CONFLICTO" });
      expect(db.prepare("SELECT status FROM usage_ledger").all()).toEqual([{ status: "reserved" }]);
    } finally { db.close(); }
  });

  it("informa si el piloto puede producir y no reserva cuota para otras cuentas o entornos", async () => {
    const db = databaseForIngest();
    try {
      db.exec("DELETE FROM usage_ledger; DELETE FROM orders");
      db.exec("INSERT INTO users(id,google_sub,email) VALUES('other','google-other','other@example.invalid')");
      const env = { DB: sqliteAdapter(db), BILLING_MODE: "sandbox" as const, SOFIA_OWNER_USER_ID: "user" };
      const create = (user: string, settings: Parameters<typeof reserveOrder>[2]) => reserveOrder(
        new Request("https://studio.invalid/api/orders", {
          method: "POST", headers: { "content-type": "application/json", "idempotency-key": "alta" },
          body: JSON.stringify({ kind: "production" }),
        }), user, settings,
      );
      expect(await (await describePlan("other", env)).json()).toMatchObject({ production_available: false, used: 0 });
      await expect(create("other", env)).rejects.toMatchObject({ status: 403, code: "PRODUCCION_NO_HABILITADA" });
      await expect(create("user", { ...env, BILLING_MODE: "production" })).rejects.toMatchObject({ status: 403 });
      expect(db.prepare("SELECT COUNT(*) AS n FROM usage_ledger").get()?.n).toBe(0);
      expect(await (await describePlan("user", env)).json()).toMatchObject({ production_available: true });
      expect((await create("user", env)).status).toBe(201);
      const saved = db.prepare("SELECT created_at,portal_started_at FROM orders").get();
      expect(saved?.portal_started_at).toBe(saved?.created_at);
      expect((await create("user", env)).status).toBe(200);
      expect(db.prepare("SELECT created_at,portal_started_at FROM orders").all()).toEqual([saved]);
      expect(() => db.exec("UPDATE orders SET portal_started_at=created_at+1")).toThrow();
    } finally { db.close(); }
  });

  it("renueva el cupo Free exactamente al iniciar el mes en UTC", async () => {
    const db = databaseForIngest();
    const clock = vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-11-30T23:59:59.999Z"));
    try {
      db.exec("DELETE FROM usage_ledger; DELETE FROM orders");
      const env = { DB: sqliteAdapter(db), BILLING_MODE: "sandbox" as const, SOFIA_OWNER_USER_ID: "user" };
      const create = (key: string) => reserveOrder(new Request("https://studio.invalid/api/orders", {
        method: "POST", headers: { "content-type": "application/json", "idempotency-key": key },
        body: JSON.stringify({ kind: "production" }),
      }), "user", env);
      expect((await create("noviembre")).status).toBe(201);
      db.exec("UPDATE usage_ledger SET status='consumed'");
      expect(await (await describePlan("user", env)).json()).toMatchObject({ period_start: "2026-11-01", used: 1 });
      await expect(create("diciembre")).rejects.toMatchObject({ status: 409, code: "CUOTA_AGOTADA" });

      clock.mockReturnValue(Date.parse("2026-12-01T00:00:00.000Z"));
      expect(await (await describePlan("user", env)).json()).toMatchObject({ period_start: "2026-12-01", used: 0 });
      expect((await create("diciembre")).status).toBe(201);
      expect(db.prepare("SELECT period_start,status FROM usage_ledger ORDER BY period_start").all()).toEqual([
        { period_start: "2026-11-01", status: "consumed" }, { period_start: "2026-12-01", status: "reserved" },
      ]);
    } finally {
      clock.mockRestore();
      db.close();
    }
  });

  it("lee el plan y el consumo del entorno solicitado en una sola sesión D1", async () => {
    const db = databaseForIngest("4k");
    try {
      const env = { DB: sqliteAdapter(db), BILLING_MODE: "production" as const };
      const sessions = vi.spyOn(env.DB, "withSession");
      expect(await (await describePlan("user", env)).json()).toMatchObject({
        plan_id: "ultra", monthly_reels: 40, used: 1,
      });
      expect(sessions).toHaveBeenCalledExactlyOnceWith("first-primary");

      sessions.mockClear();
      expect(await (await describePlan("user", { ...env, BILLING_MODE: "sandbox" })).json())
        .toMatchObject({ plan_id: "free", monthly_reels: 1, used: 0 });
      expect(sessions).toHaveBeenCalledExactlyOnceWith("first-primary");
    } finally { db.close(); }
  });

  it("sólo la ruta con una sesión válida crea un pedido con procedencia de portal", async () => {
    const db = databaseForIngest();
    try {
      db.exec("DELETE FROM usage_ledger; DELETE FROM orders");
      const origin = "https://studio.invalid";
      const secret = "clave-aislada-de-la-prueba-de-pedidos";
      const nonce = "nonce-aislado";
      const exp = Math.floor(Date.now() / 1000) + 300;
      const data = Buffer.from(JSON.stringify({ kind: "session", sub: "user", nonce, exp })).toString("base64url");
      const payload = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + data;
      const cookie = payload + "." + createHmac("sha256", secret).update(payload).digest("base64url");
      const env = {
        DB: sqliteAdapter(db), PUBLIC_ORIGIN: origin, BILLING_MODE: "sandbox", SOFIA_OWNER_USER_ID: "user",
        STUDIO_JWT_SESSION_SECRET: secret, R2_ACCOUNT_ID: credentials.accountId,
        R2_ACCESS_KEY_ID: credentials.accessKeyId, R2_SECRET_ACCESS_KEY: credentials.secretAccessKey,
      } as Parameters<typeof worker.fetch>[1];
      const post = () => worker.fetch(new Request(origin + "/api/orders", {
        method: "POST", body: JSON.stringify({ kind: "production" }),
        headers: { Origin: origin, "Content-Type": "application/json", "Idempotency-Key": "portal-auth",
          Cookie: "__Host-studio_session=" + cookie },
      }), env, { waitUntil: vi.fn() });
      expect((await post()).status).toBe(401);
      expect(db.prepare("SELECT COUNT(*) AS n FROM orders").get()?.n).toBe(0);
      db.prepare("INSERT INTO sessions(id,user_id,nonce,expires_at) VALUES (?,'user',?,?)")
        .run(createHash("sha256").update(cookie).digest("hex"), nonce, exp);
      expect((await post()).status).toBe(201);
      expect(db.prepare("SELECT portal_started_at=created_at AS marcado FROM orders").get()?.marcado).toBe(1);
    } finally { db.close(); }
  });

  it("procesa de forma cerrada el XML S3 oficial", () => {
    expect(parseListedParts(`<?xml version="1.0"?><ListPartsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <IsTruncated>false</IsTruncated><Part><PartNumber>1</PartNumber><Size>7</Size>
      <ETag>&quot;${etag}&quot;</ETag></Part></ListPartsResult>`)).toEqual([{ PartNumber: 1, Size: 7, ETag: etag }]);
    expect(() => parseListedParts("<!DOCTYPE x SYSTEM 'https://example.invalid'><ListPartsResult></ListPartsResult>")).toThrow();
    expect(() => parseListedParts("<ListPartsResult><IsTruncated>true</IsTruncated></ListPartsResult>")).toThrow();
  });

  it.each([1, 2])("carga el panel con sesión de formato %s y una sola llamada a D1", async (version) => {
    const db = databaseForIngest();
    try {
      for (const file of ["0004_production_dispatch.sql", "0005_production_briefs.sql", "0007_delivery_duration.sql", "0019_brief_ad_cta.sql", "0020_brief_ad_contacts.sql", "0021_brief_ad_engine.sql"]) {
        db.exec(readFileSync(new URL(`../cloudflare-saas/migrations/${file}`, import.meta.url), "utf8"));
      }
      db.exec("INSERT INTO users(id,google_sub,email) VALUES('otro','google-otro','otro@example.invalid')");
      const origin = "https://studio.invalid";
      const secret = "clave-aislada-del-panel-inicial-solo-pruebas";
      const expiry = Math.floor(Date.now() / 1000) + 300;
      function session(user: string) {
        const owner = version === 1 ? { sub: user } : { version };
        const data = Buffer.from(JSON.stringify({ kind: "session", ...owner, nonce: user, exp: expiry })).toString("base64url");
        const payload = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + data;
        const cookie = payload + "." + createHmac("sha256", secret).update(payload).digest("base64url");
        db.prepare("INSERT INTO sessions(id,user_id,nonce,expires_at) VALUES (?,?,?,?)")
          .run(createHash("sha256").update(cookie).digest("hex"), user, user, expiry);
        return "__Host-studio_session=" + cookie;
      }
      const main = session("user");
      const other = session("otro");
      const env = { DB: sqliteAdapter(db), PUBLIC_ORIGIN: origin, BILLING_MODE: "production",
        STUDIO_JWT_SESSION_SECRET: secret } as Parameters<typeof worker.fetch>[1];
      const get = (path: string, Cookie = main) => worker.fetch(new Request(origin + "/api" + path, { headers: { Cookie } }),
        env, { waitUntil: vi.fn() });
      const [person, plan, { orders }, { kits }] = await Promise.all(
        ["/auth/me", "/plan", "/orders", "/kits"].map(async (path) => {
          const result = await get(path);
          expect(result.status, path).toBe(200);
          return result.json();
        }),
      );
      expect(orders).toHaveLength(1);
      const grouped = vi.spyOn(env.DB, "batch");
      const sequential = vi.spyOn(env.DB, "withSession");
      const dashboard = await get("/dashboard");
      expect(dashboard.status).toBe(200);
      expect(dashboard.headers.get("Cache-Control")).toBe("no-store");
      expect(await dashboard.json()).toEqual({ person, plan, orders, kits });
      expect(grouped).toHaveBeenCalledTimes(1);
      expect(grouped.mock.calls[0]?.[0]).toHaveLength(4);
      expect(sequential).not.toHaveBeenCalled();
      expect(await (await get("/dashboard", other)).json()).toMatchObject({ orders: [], kits: [], plan: { used: 0 } });
      grouped.mockClear();
      expect((await get("/dashboard", "")).status).toBe(401);
      expect(grouped).not.toHaveBeenCalled();
      db.exec("UPDATE sessions SET revoked_at=unixepoch() WHERE user_id='user'");
      expect((await get("/dashboard")).status).toBe(401);
    } finally { db.close(); }
  });

  it("firma sólo el part exacto y sus encabezados, sin exponer el secreto", async () => {
    const value = await signR2UploadPart(job, { maxObjectBytes: 1024 }, credentials,
      "20261114T060000Z", { uploadId, partNumber: 1, bytes: 7, mimeType: "video/mp4", expires: 300 });
    const url = new URL(value.url);
    expect(url.searchParams.get("partNumber")).toBe("1");
    expect(url.searchParams.get("uploadId")).toBe(uploadId);
    expect(url.searchParams.get("X-Amz-SignedHeaders")).toBe("content-length;content-type;host");
    expect(url.searchParams.get("X-Amz-Signature")).toMatch(/^[a-f0-9]{64}$/);
    expect(value.headers).toEqual({ "Content-Type": "video/mp4" });
    expect(value.url).not.toContain(credentials.secretAccessKey);
  });

  it("no consulta R2 cuando la orden no pertenece al usuario o al proyecto anunciado", async () => {
    const db = databaseForIngest();
    try {
      const { bucket } = mockMedia(new Uint8Array(7));
      const env = { DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production",
        R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
        R2_SECRET_ACCESS_KEY: credentials.secretAccessKey } as IngestEnv;
      const path = "/api/uploads";
      const payload = {
        order_id: order, project_slug: job.project_slug, extension: "mp4", bytes: 7, sha256: digest, content_type: "video/mp4",
      };
      for (const [subject, slug, mode, bytes] of [
        ["otro", job.project_slug, "production", 7], ["user", "job-ajeno", "production", 7],
        ["user", job.project_slug, "sandbox", 7], ["otro", job.project_slug, "production", 0],
      ] as const) {
        await expect(handleIngest(request(path, "POST", { ...payload, project_slug: slug, bytes }),
          { ...env, BILLING_MODE: mode }, subject, path))
          .rejects.toMatchObject({ status: 404, code: "ORDEN_NO_ENCONTRADA" });
      }
      await expect(handleIngest(request(path, "POST", { ...payload, bytes: 0 }), env, "user", path))
        .rejects.toMatchObject({ status: 422, code: "SUBIDA_ARGUMENTOS" });
      expect(bucket.createMultipartUpload).not.toHaveBeenCalled();
      expect(db.prepare("SELECT COUNT(*) AS total FROM upload_sessions").get()).toEqual({ total: 0 });
    } finally { db.close(); }
  });

  it("no despacha el primer archivo de una orden con materiales registrados", async () => {
    const db = databaseForIngest();
    try {
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?, 'user', ?, ?)")
        .run(order, JSON.stringify(["uno.mp4", "dos.mp4"].map((name) => ({
          name, bytes: 7, sha256: digest, extension: "mp4",
        }))), "a".repeat(64));
      const { bucket } = mockMedia(new Uint8Array(7));
      const env = { DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production" } as IngestEnv;
      const path = "/api/uploads";
      await expect(handleIngest(request(path, "POST", {
        order_id: order, project_slug: job.project_slug, extension: "mp4", bytes: 7, sha256: digest, content_type: "video/mp4",
      }), env, "user", path)).rejects.toMatchObject({ status: 409, code: "MATERIALES_EN_PREPARACION" });
      expect(bucket.createMultipartUpload).not.toHaveBeenCalled();
      expect(db.prepare("SELECT COUNT(*) AS total FROM upload_sessions").get()).toEqual({ total: 0 });
      expect(db.prepare("SELECT COUNT(*) AS total FROM job_outbox").get()).toEqual({ total: 0 });
    } finally { db.close(); }
  });

  it("agrupa titularidad, reserva y sesión en una sola llamada a D1 antes de abrir R2", async () => {
    const db = databaseForIngest();
    try {
      const adapter = sqliteAdapter(db);
      const batch = vi.spyOn(adapter, "batch");
      const session = vi.spyOn(adapter, "withSession");
      const { bucket } = mockMedia(new Uint8Array(7));
      const env = { DB: adapter, MEDIA: bucket, BILLING_MODE: "production" } as IngestEnv;
      const path = "/api/uploads";
      const upload = () => handleIngest(request(path, "POST", {
        order_id: order, project_slug: job.project_slug, extension: "mp4", bytes: 7, sha256: digest, content_type: "video/mp4",
      }), env, "user", path);

      const first = await (await upload()).json();
      expect(first).toMatchObject({ status: "open", uploadId, newlyOpened: true });
      expect(batch).toHaveBeenCalledTimes(1);
      expect(batch.mock.calls[0]?.[0]).toHaveLength(3);
      expect(session).not.toHaveBeenCalled();
      const replay = await (await upload()).json();
      expect(replay).not.toHaveProperty("newlyOpened");
      expect({ ...replay, newlyOpened: true }).toEqual(first);
      expect(bucket.createMultipartUpload).toHaveBeenCalledTimes(1);
    } finally { db.close(); }
  });

  const measuredAccounts = [
    [1, "fixture@example.invalid", "sandbox", "ensayo"],
    [2, "fixture@example.org", "sandbox", "piloto"],
    [2, "fixture@example.org", "production", "produccion"],
    [2, "fixture@example.invalid", "production", "ensayo"],
  ] as const;

  function measuredSession(db: DatabaseSync, version: number, email: string, mode: string, secret: string): string {
    db.prepare("UPDATE users SET email=? WHERE id='user'").run(email);
    db.prepare("UPDATE orders SET provider_mode=? WHERE id=?").run(mode, order);
    const nonce = "nonce-apertura";
    const exp = Math.floor(Date.now() / 1000) + 300;
    const owner = version === 1 ? { sub: "user" } : { version };
    const encoded = Buffer.from(JSON.stringify({ kind: "session", ...owner, nonce, exp })).toString("base64url");
    const signed = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + encoded;
    const token = signed + "." + createHmac("sha256", secret).update(signed).digest("base64url");
    db.prepare("INSERT INTO sessions(id,user_id,nonce,expires_at) VALUES (?,'user',?,?)")
      .run(createHash("sha256").update(token).digest("hex"), nonce, exp);
    return "__Host-studio_session=" + token;
  }

  it.each(measuredAccounts)("abre multipart con la sesión %s de %s en %s y mide %s", async (version, email, mode, cohort) => {
    const db = databaseForIngest();
    try {
      const origin = "https://studio.e-scale.cl";
      const secret = "clave-ficticia-para-la-apertura-multipart";
      const cookie = measuredSession(db, version, email, mode, secret);
      const { bucket } = mockMedia(new Uint8Array(7));
      const adapter = sqliteAdapter(db);
      const batch = vi.spyOn(adapter, "batch");
      const separate = vi.spyOn(adapter, "withSession");
      const writeDataPoint = vi.fn<(point: { indexes: string[] }) => void>();
      if (version === 2) writeDataPoint.mockImplementation(() => { throw new Error("METRICAS_NO_DISPONIBLES"); });
      const env = { DB: adapter, MEDIA: bucket, PUBLIC_ORIGIN: origin, BILLING_MODE: mode,
        STUDIO_JWT_SESSION_SECRET: secret, STUDIO_HTTP_METRICS: { writeDataPoint },
        R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
        R2_SECRET_ACCESS_KEY: credentials.secretAccessKey,
      } as unknown as Parameters<typeof worker.fetch>[1];
      const send = (body: string) => worker.fetch(new Request(origin + "/api/uploads", {
        method: "POST", headers: { Origin: origin, Cookie: cookie, "Content-Type": "application/json",
          "X-Studio-Prefirmar-Partes": "1" }, body,
      }), env, { waitUntil: vi.fn() });
      const body = JSON.stringify({ order_id: order, project_slug: job.project_slug, extension: "mp4", bytes: 7,
        sha256: digest, content_type: "video/mp4" });
      const first = await send(body);
      expect(first.status).toBe(200);
      const opened = await first.json();
      expect(opened).toMatchObject({ status: "open", uploadId, newlyOpened: true });
      expect(opened.initialParts).toEqual([{ partNumber: 1, url: expect.any(String), headers: { "Content-Type": "video/mp4" } }]);
      const signed = new URL(opened.initialParts[0].url);
      expect(signed.searchParams.get("uploadId")).toBe(uploadId);
      expect(signed.searchParams.get("partNumber")).toBe("1");
      expect(signed.searchParams.get("X-Amz-Expires")).toBe("60");
      expect(signed.pathname).toBe(`/studio-media-bucket/${opened.key}`);
      expect(batch).toHaveBeenCalledTimes(1);
      expect(batch.mock.calls[0]?.[0]).toHaveLength(4);
      expect(separate).not.toHaveBeenCalled();
      const replay = await (await send(body)).json();
      expect(replay).not.toHaveProperty("newlyOpened");
      expect(replay).not.toHaveProperty("initialParts");
      expect({ ...replay, newlyOpened: true, initialParts: opened.initialParts }).toEqual(opened);
      db.exec("UPDATE sessions SET revoked_at=unixepoch() WHERE user_id='user'");
      db.exec("DELETE FROM upload_sessions");
      expect((await send(body)).status).toBe(401);
      expect((await send("{")).status).toBe(401);
      expect(writeDataPoint.mock.calls.filter(([point]) => point.indexes[0] === "multipart_inicio_etapas"))
        .toEqual([[{ indexes: ["multipart_inicio_etapas"], blobs: [cohort],
          doubles: [expect.any(Number), 200, expect.any(Number), expect.any(Number), expect.any(Number)] }]]);
      expect(bucket.createMultipartUpload).toHaveBeenCalledTimes(1);
      expect(db.prepare("SELECT count(*) AS n FROM upload_sessions").get()).toEqual({ n: 0 });
    } finally { db.close(); }
  });

  it.each(measuredAccounts)("mide fuentes de la sesión %s de %s en %s como %s", async (version, email, mode, cohort) => {
    const db = databaseForIngest();
    try {
      const origin = "https://studio.e-scale.cl";
      const secret = "clave-ficticia-para-la-apertura-multifuente";
      const cookie = measuredSession(db, version, email, mode, secret);
      const manifest = JSON.stringify([{ name: "fuente.mp4", bytes: 7, sha256: digest, extension: "mp4" }]);
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(order, manifest, digest);
      const source = db.prepare("SELECT id FROM production_sources WHERE order_id=?").get(order) as { id: string };
      const { bucket } = mockMedia(new Uint8Array(7));
      const writeDataPoint = vi.fn<(point: { indexes: string[] }) => void>();
      const adapter = sqliteAdapter(db);
      const batch = vi.spyOn(adapter, "batch");
      const separate = vi.spyOn(adapter, "withSession");
      const env = { DB: adapter, MEDIA: bucket, PUBLIC_ORIGIN: origin, BILLING_MODE: mode,
        STUDIO_JWT_SESSION_SECRET: secret, STUDIO_HTTP_METRICS: { writeDataPoint },
        R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
        R2_SECRET_ACCESS_KEY: credentials.secretAccessKey,
      } as unknown as Parameters<typeof worker.fetch>[1];
      const send = () => worker.fetch(new Request(`${origin}/api/orders/${order}/sources/${source.id}/upload`, {
        method: "POST", headers: { Origin: origin, Cookie: cookie, "X-Studio-Prefirmar-Partes": "1" },
      }), env, { waitUntil: vi.fn() });
      const first = await send();
      expect(first.status).toBe(200);
      const opened = await first.json();
      expect(opened).toMatchObject({ id: source.id, newlyOpened: true });
      expect(opened.initialParts).toEqual([{ partNumber: 1, url: expect.any(String), headers: { "Content-Type": "video/mp4" } }]);
      const signed = new URL(opened.initialParts[0].url);
      expect(signed.pathname).toBe(`/studio-media-bucket/ingest/${order}/${source.id}.mp4`);
      expect(signed.searchParams.get("uploadId")).toBe(uploadId);
      expect(signed.searchParams.get("X-Amz-Expires")).toBe("60");
      expect(batch).toHaveBeenCalledTimes(2);
      expect(batch.mock.calls[0]?.[0]).toHaveLength(2);
      batch.mockClear();
      const replay = await send();
      expect(replay.status).toBe(200);
      const restored = await replay.json();
      expect(restored).not.toHaveProperty("newlyOpened");
      expect(restored).not.toHaveProperty("initialParts");
      expect({ ...restored, newlyOpened: true, initialParts: opened.initialParts }).toEqual(opened);
      expect(batch).toHaveBeenCalledTimes(1);
      expect(separate).not.toHaveBeenCalled();
      const totals = writeDataPoint.mock.calls.filter(([point]) => point.indexes[0] === "multifuente_inicio");
      expect(totals).toHaveLength(2);
      expect(writeDataPoint.mock.calls.filter(([point]) => point.indexes[0] === "multifuente_inicio_etapas"))
        .toEqual([[{ indexes: ["multifuente_inicio_etapas"], blobs: [cohort],
          doubles: [expect.any(Number), 200, expect.any(Number), expect.any(Number), expect.any(Number)] }]]);
      db.exec("UPDATE sessions SET revoked_at=unixepoch() WHERE user_id='user'");
      expect((await send()).status).toBe(401);
      expect(bucket.createMultipartUpload).toHaveBeenCalledTimes(1);
    } finally { db.close(); }
  });

  it.each([1, 2])("coteja la sesión de versión %s y la carga en una llamada a D1 para firmar o listar", async (version) => {
    const db = databaseForIngest();
    try {
      const origin = "https://studio.invalid";
      const secret = "clave-aislada-de-firma-multipart-solo-pruebas";
      const exp = Math.floor(Date.now() / 1000) + 300;
      const nonce = "nonce-firma-multipart";
      const subject = version === 1 ? { sub: "user" } : { version };
      const values = Buffer.from(JSON.stringify({ kind: "session", ...subject, nonce, exp })).toString("base64url");
      const payload = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + values;
      const token = payload + "." + createHmac("sha256", secret).update(payload).digest("base64url");
      db.prepare("INSERT INTO sessions(id,user_id,nonce,expires_at) VALUES (?,'user',?,?)")
        .run(createHash("sha256").update(token).digest("hex"), nonce, exp);
      const { bucket } = mockMedia(new Uint8Array(7));
      const env = { DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production", PUBLIC_ORIGIN: origin,
        STUDIO_JWT_SESSION_SECRET: secret, R2_ACCOUNT_ID: credentials.accountId,
        R2_ACCESS_KEY_ID: credentials.accessKeyId, R2_SECRET_ACCESS_KEY: credentials.secretAccessKey,
      } as Parameters<typeof worker.fetch>[1];
      const created = await handleIngest(request("/api/uploads", "POST", {
        order_id: order, project_slug: job.project_slug, extension: "mp4", bytes: 7, sha256: digest, content_type: "video/mp4",
      }), env, "user", "/api/uploads");
      const { id } = await created.json();
      const batch = vi.spyOn(env.DB, "batch");
      const sessions = vi.spyOn(env.DB, "withSession");
      const sign = (part: number, settings = env, cookie = "__Host-studio_session=" + token) => worker.fetch(new Request(
        origin + `/api/uploads/${id}/parts/${part}/sign`, { method: "POST", headers: { Origin: origin, Cookie: cookie } },
      ), settings, { waitUntil: vi.fn() });
      const list = (settings = env, cookie = "__Host-studio_session=" + token) => worker.fetch(new Request(
        origin + `/api/uploads/${id}/parts`, { headers: { Cookie: cookie } },
      ), settings, { waitUntil: vi.fn() });
      const signed = await sign(1);
      expect(signed.status).toBe(200);
      expect(new URL((await signed.json()).url).searchParams.get("partNumber")).toBe("1");
      expect(batch).toHaveBeenCalledTimes(1);
      expect(sessions).not.toHaveBeenCalled();
      const r2 = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
        `<ListPartsResult><IsTruncated>false</IsTruncated><Part><PartNumber>1</PartNumber>
        <Size>7</Size><ETag>"${etag}"</ETag></Part></ListPartsResult>`,
      ));
      batch.mockClear();
      const listed = await list();
      expect(listed.status).toBe(200);
      expect(await listed.json()).toEqual({ parts: [{ PartNumber: 1, ETag: etag, Size: 7 }] });
      expect(batch).toHaveBeenCalledTimes(1);
      expect(sessions).not.toHaveBeenCalled();
      expect(r2).toHaveBeenCalledTimes(1);
      db.exec("INSERT INTO users(id,google_sub,email) VALUES('otro','google-otro','otro@example.invalid')");
      const otherNonce = "nonce-firma-multipart-otro";
      const otherSubject = version === 1 ? { sub: "otro" } : { version };
      const otherValue = Buffer.from(JSON.stringify({ kind: "session", ...otherSubject, nonce: otherNonce, exp })).toString("base64url");
      const otherPayload = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + otherValue;
      const otherToken = otherPayload + "." + createHmac("sha256", secret).update(otherPayload).digest("base64url");
      db.prepare("INSERT INTO sessions(id,user_id,nonce,expires_at) VALUES (?,'otro',?,?)")
        .run(createHash("sha256").update(otherToken).digest("hex"), otherNonce, exp);
      const forbidden = await sign(1, env, "__Host-studio_session=" + otherToken);
      expect(forbidden.status).toBe(404);
      expect(await forbidden.json()).toEqual({ error: "SUBIDA_NO_ENCONTRADA" });
      expect((await list(env, "__Host-studio_session=" + otherToken)).status).toBe(404);
      expect((await list({ ...env, BILLING_MODE: "sandbox" })).status).toBe(404);
      expect((await sign(2)).status).toBe(405);
      expect((await sign(1, { ...env, BILLING_MODE: "sandbox" })).status).toBe(404);
      batch.mockClear();
      expect((await sign(1, env, "")).status).toBe(401);
      expect((await list(env, "")).status).toBe(401);
      expect(batch).not.toHaveBeenCalled();
      db.exec("UPDATE sessions SET revoked_at=unixepoch() WHERE user_id='user'");
      const revoked = await sign(1);
      expect(revoked.status).toBe(401);
      expect(await revoked.json()).toEqual({ error: "AUTENTICACION_REQUERIDA" });
      expect((await list()).status).toBe(401);
      expect(r2).toHaveBeenCalledTimes(1);
    } finally {
      vi.restoreAllMocks();
      db.close();
    }
  });

  it("reserva el almacenamiento al abrir una carga y lo recupera cuando vence el kit anterior", async () => {
    const db = databaseForIngest();
    try {
      const { bucket } = mockMedia(new Uint8Array(7));
      const env: IngestEnv = {
        DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production",
        R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
        R2_SECRET_ACCESS_KEY: credentials.secretAccessKey,
      };
      const oldOrder = "ae8ca550-12a5-4a4c-a50a-1aa11616c8f1";
      db.prepare(`INSERT INTO orders (id,user_id,plan_id,kind,status,idempotency_key,request_sha256,
        project_slug,queue_priority) VALUES (?,'user','free','production','completed','vieja',?,'job-vieja',0)`)
        .run(oldOrder, digest);
      db.prepare(`INSERT INTO delivery_kits(id,order_id,user_id,provider_mode,title,bytes,
        sha256,r2_etag,r2_version,created_at) VALUES (?,?,'user','production','Anterior',1999999995,?,?,?,unixepoch())`)
        .run(asset, oldOrder, digest, etag, "version1");
      const start = "/api/uploads";
      const upload = () => handleIngest(request(start, "POST", {
        order_id: order, project_slug: "job-fixture", extension: "mp4",
        bytes: 7, sha256: digest, content_type: "video/mp4",
      }), env, "user", start);
      await expect(upload()).rejects.toMatchObject({ status: 409, code: "ALMACENAMIENTO_AGOTADO" });
      expect(bucket.createMultipartUpload).not.toHaveBeenCalled();
      db.exec("UPDATE delivery_kits SET created_at=unixepoch()-7*86400");
      const first = await (await upload()).json();
      expect(first).toMatchObject({ status: "open", newlyOpened: true });
      const resumed = await (await upload()).json();
      expect(resumed).not.toHaveProperty("newlyOpened");
      expect({ ...resumed, newlyOpened: true }).toEqual(first);
      expect(bucket.createMultipartUpload).toHaveBeenCalledTimes(1);
    } finally { db.close(); }
  });

  it("computa cargas múltiples activas al reservar espacio para otra producción", async () => {
    const db = databaseForIngest();
    try {
      const { bucket } = mockMedia(new Uint8Array(7));
      const env = { DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production" } as IngestEnv;
      const previous = "ae8ca550-12a5-4a4c-a50a-1aa11616c8f1";
      db.exec("UPDATE plan_quotas SET storage_bytes=10,storage_limit_bytes=10 WHERE plan_id='free'");
      db.prepare(`INSERT INTO orders(id,user_id,plan_id,kind,status,idempotency_key,request_sha256,project_slug,queue_priority)
        VALUES (?,'user','free','production','queued','multi',?,'job-multi',0)`).run(previous, digest);
      const manifest = [{ name: "detalle.mp4", bytes: 4, sha256: digest, extension: "mp4" }];
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(previous, JSON.stringify(manifest), digest);
      const item = db.prepare("SELECT id FROM production_sources WHERE order_id=?").get(previous) as { id: string };
      db.prepare(`INSERT INTO production_source_uploads(source_id,object_key,upload_id,status,created_at,expires_at)
        VALUES (?,'ingest-fixture','multipart','open',unixepoch(),unixepoch()+3600)`).run(item.id);
      const path = "/api/uploads";
      const upload = () => handleIngest(request(path, "POST", {
        order_id: order, project_slug: "job-fixture", extension: "mp4", bytes: 7, sha256: digest, content_type: "video/mp4",
      }), env, "user", path);
      await expect(upload()).rejects.toMatchObject({ status: 409, code: "ALMACENAMIENTO_AGOTADO" });
      expect(bucket.createMultipartUpload).not.toHaveBeenCalled();
      db.prepare("UPDATE production_source_uploads SET status='aborted' WHERE source_id=?").run(item.id);
      expect(await (await upload()).json()).toMatchObject({ status: "open" });
    } finally { db.close(); }
  });

  it("abre fuentes independientes con cuota atómica y reanuda la misma carga", async () => {
    const db = databaseForIngest();
    try {
      db.exec("UPDATE plan_quotas SET storage_bytes=10,storage_limit_bytes=10 WHERE plan_id='free'");
      const manifest = ["primero.mp4", "segundo.mp4"].map((name) => ({ name, bytes: 7, sha256: digest, extension: "mp4" }));
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(order, JSON.stringify(manifest), digest);
      const sources = db.prepare("SELECT id FROM production_sources WHERE order_id=? ORDER BY position").all(order) as { id: string }[];
      const { bucket } = mockMedia(new Uint8Array(7));
      const env = { DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production",
        R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
        R2_SECRET_ACCESS_KEY: credentials.secretAccessKey } as IngestEnv;
      const observe = vi.fn();
      await expect(createSourceUpload(env, "otro", order, sources[0]!.id, observe)).rejects.toMatchObject({ status: 404 });
      const attempts = await Promise.allSettled(sources.map(({ id }) => createSourceUpload(env, "user", order, id, observe)));
      expect(attempts.filter((attempt) => attempt.status === "fulfilled")).toHaveLength(1);
      expect(attempts.filter((attempt) => attempt.status === "rejected"))
        .toEqual([{ status: "rejected", reason: expect.objectContaining({ code: "ALMACENAMIENTO_AGOTADO" }) }]);
      const first = attempts.find((attempt) => attempt.status === "fulfilled") as PromiseFulfilledResult<Response>;
      const opened = await first.value.json() as { id: string; uploadId: string; status: string };
      expect(opened.status).toBe("open");
      expect(await (await createSourceUpload(env, "user", order, opened.id, observe)).json())
        .toMatchObject({ id: opened.id, uploadId: opened.uploadId, status: "open" });
      expect(observe.mock.calls).toEqual([[expect.any(Number), expect.any(Number), expect.any(Number), false]]);
      await expect(signSourcePart(env, "otro", order, opened.id, 1)).rejects.toMatchObject({ status: 404 });
      await expect(signSourcePart(env, "user", order, opened.id, 2)).rejects.toMatchObject({ status: 405 });
      const signed = await (await signSourcePart(env, "user", order, opened.id, 1)).json() as { url: string; headers: object };
      expect(new URL(signed.url).searchParams.get("partNumber")).toBe("1");
      expect(signed.headers).toEqual({ "Content-Type": "video/mp4" });
      expect(signed.url).not.toContain(credentials.secretAccessKey);
      const remote = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
        `<ListPartsResult><IsTruncated>false</IsTruncated><Part><PartNumber>1</PartNumber>
        <Size>7</Size><ETag>"${etag}"</ETag></Part></ListPartsResult>`));
      try {
        await expect(listSourceParts(env, "otro", order, opened.id)).rejects.toMatchObject({ status: 404 });
        expect(await (await listSourceParts(env, "user", order, opened.id)).json())
          .toEqual({ parts: [{ PartNumber: 1, Size: 7, ETag: etag }] });
      } finally { remote.mockRestore(); }
      expect(bucket.createMultipartUpload).toHaveBeenCalledTimes(1);
    } finally { db.close(); }
  });

  it("verifica cada fuente y solo declara completo el conjunto cuando están todas", async () => {
    const db = databaseForIngest();
    const bytes = new TextEncoder().encode("fixture");
    const sha256 = createHash("sha256").update(bytes).digest("hex");
    const restore = installDigestStream();
    const listing = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
      `<ListPartsResult><IsTruncated>false</IsTruncated><Part><PartNumber>1</PartNumber>
       <Size>7</Size><ETag>"${etag}"</ETag></Part></ListPartsResult>`));
    try {
      const manifest = ["primero.mp4", "segundo.mp4"].map((name) => ({ name, bytes: 7, sha256, extension: "mp4" }));
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(order, JSON.stringify(manifest), digest);
      const sources = db.prepare("SELECT id FROM production_sources WHERE order_id=? ORDER BY position").all(order) as { id: string }[];
      for (let index = 0; index < sources.length; index += 1) {
        const { bucket } = mockMedia(bytes);
        const env = { DB: sqliteAdapter(db, true), MEDIA: bucket, BILLING_MODE: "production",
          R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
          R2_SECRET_ACCESS_KEY: credentials.secretAccessKey } as IngestEnv;
        const id = sources[index]!.id;
        await createSourceUpload(env, "user", order, id);
        const complete = () => completeSourceUpload(request("/unused", "POST", { parts: [{ PartNumber: 1, ETag: etag }] }),
          env, "user", order, id);
        expect(await (await complete()).json()).toEqual({ id, status: "completed", hash_verified: true,
          all_sources_complete: index === 1 });
        expect((await (await complete()).json()).all_sources_complete).toBe(index === 1);
        expect(db.prepare("SELECT COUNT(*) AS n FROM production_source_outbox").get()).toEqual({ n: index + 1 });
      }
      expect(db.prepare("SELECT COUNT(*) AS n FROM job_outbox").get()).toEqual({ n: 0 });
      const queued = db.prepare(`SELECT source_id,payload FROM production_source_outbox ORDER BY source_id`).all() as
        { source_id: string; payload: string }[];
      expect(queued.map(({ source_id, payload }) => {
        const job = JSON.parse(payload) as { job_id: string; asset_id: string; sha256: string };
        return [job.job_id, job.asset_id === source_id, job.sha256];
      })).toEqual([[order, true, sha256], [order, true, sha256]]);
    } finally { listing.mockRestore(); restore(); db.close(); }
  });

  it("sincroniza el primer archivo sin despachar la orden antes de recibir el segundo", async () => {
    const db = databaseForIngest();
    try {
      for (const file of ["0004_production_dispatch.sql", "0024_multiple_production_dispatch.sql"]) {
        db.exec(readFileSync(new URL(`../cloudflare-saas/migrations/${file}`, import.meta.url), "utf8"));
      }
      const manifest = ["uno.mp4", "dos.mp4"].map((name) => ({name, bytes:7, sha256:digest, extension:"mp4"}));
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(order,JSON.stringify(manifest),digest);
      const sources = db.prepare("SELECT id FROM production_sources WHERE order_id=? ORDER BY position").all(order) as {id:string}[];
      const sent: unknown[] = [];
      const env: JobEnv = {DB:sqliteAdapter(db), HOMELAB_SYNC_ORIGIN:"https://api-studio.e-scale.cl",
        STUDIO_EDGE_TO_HOMELAB_HMAC_SECRET:"x".repeat(32), JOBS:{send:async (body) => {sent.push(body);}}};
      const send = vi.fn<typeof fetch>(async (_url, init) => {
        const job = JSON.parse(new TextDecoder().decode(init?.body as Uint8Array));
        return Response.json({status:"completed",job_id:job.job_id,asset_id:job.asset_id,bytes:job.bytes,sha256:job.sha256});
      });
      const finish = async (id:string) => {
        db.prepare(`INSERT INTO production_source_uploads(source_id,object_key,upload_id,status,created_at,expires_at)
          VALUES (?,?,'multipart','open',unixepoch(),unixepoch()+3600)`).run(id,`ingest/${order}/${id}.mp4`);
        db.prepare("UPDATE production_source_uploads SET status='completed',object_etag=? WHERE source_id=?").run(etag,id);
        await dispatchSourceOutbox(env);
        const message: JobMessage = {body:sent.at(-1),attempts:1,ack:vi.fn(),retry:vi.fn()};
        await syncQueue([message],env,send);
        expect(message.ack).toHaveBeenCalledOnce();
      };
      await finish(sources[0]!.id);
      expect(db.prepare("SELECT status FROM orders WHERE id=?").get(order)).toEqual({status:"queued"});
      expect(db.prepare("SELECT status FROM usage_ledger WHERE order_id=?").get(order)).toEqual({status:"reserved"});
      expect(db.prepare("SELECT COUNT(*) AS n FROM production_dispatch").get()).toEqual({n:0});
      await finish(sources[1]!.id);
      expect(db.prepare("SELECT status FROM orders WHERE id=?").get(order)).toEqual({status:"processing"});
      expect(db.prepare("SELECT COUNT(*) AS n FROM production_dispatch").get()).toEqual({n:1});
    } finally {db.close();}
  });

  it("reanuda una confirmación múltiple interrumpida sin abrir otra carga", async () => {
    const db = databaseForIngest();
    const bytes = new TextEncoder().encode("fixture");
    const sha256 = createHash("sha256").update(bytes).digest("hex");
    const { bucket, handle } = mockMedia(bytes);
    const restore = installDigestStream();
    const listing = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
      `<ListPartsResult><IsTruncated>false</IsTruncated><Part><PartNumber>1</PartNumber>
       <Size>7</Size><ETag>"${etag}"</ETag></Part></ListPartsResult>`));
    try {
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(order, JSON.stringify([{ name: "uno.mp4", bytes: 7, sha256, extension: "mp4" }]), digest);
      const source = db.prepare("SELECT id FROM production_sources WHERE order_id=?").get(order) as { id: string };
      const env = { DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production",
        R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
        R2_SECRET_ACCESS_KEY: credentials.secretAccessKey } as IngestEnv;
      await createSourceUpload(env, "user", order, source.id);
      handle.complete.mockRejectedValueOnce(new Error("interrupción"));
      await expect(completeSourceUpload(request("/unused", "POST", { parts: [{ PartNumber: 1, ETag: etag }] }),
        env, "user", order, source.id)).rejects.toThrow("interrupción");
      db.prepare("UPDATE production_source_uploads SET lease_until=unixepoch()-1 WHERE source_id=?").run(source.id);
      expect(await (await createSourceUpload(env, "user", order, source.id)).json())
        .toMatchObject({ id: source.id, status: "completed", all_sources_complete: true });
      expect(bucket.createMultipartUpload).toHaveBeenCalledOnce();
    } finally { listing.mockRestore(); restore(); db.close(); }
  });

  it("despacha todas las fuentes y activa la producción únicamente al confirmar la última", async () => {
    const db = databaseForIngest();
    try {
      for (const file of ["0004_production_dispatch.sql", "0024_multiple_production_dispatch.sql"]) {
        db.exec(readFileSync(new URL(`../cloudflare-saas/migrations/${file}`, import.meta.url), "utf8"));
      }
      const manifest = ["uno.mp4", "dos.mp4"].map((name) => ({ name, bytes: 7, sha256: digest, extension: "mp4" }));
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(order, JSON.stringify(manifest), digest);
      const sources = db.prepare("SELECT id FROM production_sources WHERE order_id=? ORDER BY position").all(order) as { id: string }[];
      for (const { id } of sources) {
        db.prepare(`INSERT INTO production_source_uploads(source_id,object_key,upload_id,status,created_at,expires_at)
          VALUES (?,?,'multipart','open',unixepoch(),unixepoch()+3600)`).run(id, `ingest/${order}/${id}.mp4`);
        db.prepare("UPDATE production_source_uploads SET status='completed',object_etag=? WHERE source_id=?").run(etag, id);
      }
      const sent: unknown[] = [];
      const env: JobEnv = { DB: sqliteAdapter(db), HOMELAB_SYNC_ORIGIN: "https://api-studio.e-scale.cl",
        STUDIO_EDGE_TO_HOMELAB_HMAC_SECRET: "x".repeat(32), JOBS: { send: async (body) => { sent.push(body); } } };
      await dispatchSourceOutbox(env);
      expect(sent).toHaveLength(2);
      const send = vi.fn<typeof fetch>(async (url, init) => {
        expect(url).toBe("https://api-studio.e-scale.cl/v2/sync");
        expect(new Headers(init?.headers).get("signature")).toMatch(/^v2=[a-f0-9]{64}$/u);
        const job = JSON.parse(new TextDecoder().decode(init?.body as Uint8Array));
        return Response.json({ status: "completed", job_id: job.job_id, asset_id: job.asset_id, bytes: job.bytes, sha256: job.sha256 });
      });
      const message = (body: unknown): JobMessage => ({ body, attempts: 1, ack: vi.fn(), retry: vi.fn() });
      send.mockResolvedValueOnce(Response.json({ status: "completed", job_id: order, asset_id: "ajeno", bytes: 7, sha256: digest }));
      const rejected = message(sent[0]);
      await syncQueue([rejected], env, send);
      expect(rejected.retry).toHaveBeenCalledOnce();
      expect(db.prepare("SELECT COUNT(*) AS n FROM production_source_outbox WHERE synced_at IS NOT NULL").get()).toEqual({ n: 0 });
      const first = message(sent[0]);
      await syncQueue([first], env, send);
      expect(first.ack).toHaveBeenCalledOnce();
      expect(db.prepare("SELECT COUNT(*) AS n FROM production_source_outbox WHERE synced_at IS NOT NULL").get()).toEqual({ n: 1 });
      expect(db.prepare("SELECT status FROM orders WHERE id=?").get(order)).toEqual({ status: "queued" });
      const second = message(sent[1]);
      await syncQueue([second], env, send);
      expect(second.ack).toHaveBeenCalledOnce();
      expect(db.prepare("SELECT COUNT(*) AS n FROM production_source_outbox WHERE synced_at IS NOT NULL").get()).toEqual({ n: 2 });
      expect(db.prepare("SELECT status FROM orders WHERE id=?").get(order)).toEqual({ status: "processing" });
      expect(db.prepare("SELECT status FROM usage_ledger WHERE order_id=?").get(order)).toEqual({ status: "consumed" });
      expect(db.prepare("SELECT COUNT(*) AS n FROM production_dispatch WHERE order_id=?").get(order)).toEqual({ n: 1 });
      const uploadEnv = { ...env, BILLING_MODE: "production", MEDIA: mockMedia(new Uint8Array(7)).bucket } as unknown as IngestEnv;
      expect(await (await createSourceUpload(uploadEnv, "user", order, sources[0]!.id)).json())
        .toMatchObject({ id: sources[0]!.id, status: "completed", hash_verified: true });
      await syncQueue([message(sent[0])], env, send);
      expect(send).toHaveBeenCalledTimes(3);
    } finally { db.close(); }
  });

  it("conserva una orden antigua mientras una carga múltiple siga vigente y la libera al vencer", async () => {
    const db = databaseForIngest();
    try {
      db.prepare("UPDATE orders SET created_at=unixepoch()-90000 WHERE id=?").run(order);
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(order, JSON.stringify([{ name: "uno.mp4", bytes: 7, sha256: digest, extension: "mp4" }]), digest);
      const source = db.prepare("SELECT id FROM production_sources WHERE order_id=?").get(order) as { id: string };
      db.prepare(`INSERT INTO production_source_uploads(source_id,object_key,upload_id,status,created_at,expires_at)
        VALUES (?,'objeto','multipart','open',unixepoch()-100,unixepoch()+100)`).run(source.id);
      const env = { DB: sqliteAdapter(db), BILLING_MODE: "production" } as IngestEnv;
      await expireAbandonedReservations(env);
      expect(db.prepare("SELECT status FROM orders WHERE id=?").get(order)).toEqual({ status: "queued" });
      db.prepare("UPDATE production_source_uploads SET expires_at=unixepoch()-1 WHERE source_id=?").run(source.id);
      await expireAbandonedReservations(env);
      expect(db.prepare("SELECT status FROM orders WHERE id=?").get(order)).toEqual({ status: "cancelled" });
      expect(db.prepare("SELECT status FROM production_source_uploads WHERE source_id=?").get(source.id)).toEqual({ status: "aborted" });
    } finally { db.close(); }
  });

  it("libera un conjunto incompleto vencido sin cancelar uno completamente cargado", async () => {
    const db = databaseForIngest();
    try {
      db.prepare("UPDATE orders SET created_at=unixepoch()-90000 WHERE id=?").run(order);
      const items = ["uno", "dos"].map(name => ({name:`${name}.mp4`,bytes:7,sha256:digest,extension:"mp4"}));
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(order,JSON.stringify(items),digest);
      const sources = db.prepare("SELECT id FROM production_sources WHERE order_id=? ORDER BY position").all(order) as {id:string}[];
      db.prepare(`INSERT INTO production_source_uploads(source_id,object_key,upload_id,status,object_etag,created_at,expires_at)
        VALUES (?,'objeto1','multipart1','completed','etag',unixepoch()-90000,unixepoch()-1)`).run(sources[0]!.id);
      db.prepare(`INSERT INTO production_source_uploads(source_id,object_key,upload_id,status,created_at,expires_at)
        VALUES (?,'objeto2','multipart2','open',unixepoch()-100,unixepoch()+100)`).run(sources[1]!.id);
      const env = {DB:sqliteAdapter(db),BILLING_MODE:"production"} as IngestEnv;
      await expireAbandonedReservations(env);
      expect(db.prepare("SELECT status FROM orders WHERE id=?").get(order)).toEqual({status:"queued"});
      db.prepare("UPDATE production_source_uploads SET expires_at=unixepoch()-1 WHERE source_id=?").run(sources[1]!.id);
      await expireAbandonedReservations(env);
      expect(db.prepare("SELECT status FROM orders WHERE id=?").get(order)).toEqual({status:"cancelled"});
      expect(db.prepare("SELECT status FROM usage_ledger WHERE order_id=?").get(order)).toEqual({status:"released"});
    } finally { db.close(); }

    const complete = databaseForIngest();
    try {
      complete.prepare("UPDATE orders SET created_at=unixepoch()-90000 WHERE id=?").run(order);
      const item = [{name:"completo.mp4",bytes:7,sha256:digest,extension:"mp4"}];
      complete.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(order,JSON.stringify(item),digest);
      const source = complete.prepare("SELECT id FROM production_sources WHERE order_id=?").get(order) as {id:string};
      complete.prepare(`INSERT INTO production_source_uploads(source_id,object_key,upload_id,status,object_etag,created_at,expires_at)
        VALUES (?,'objeto','multipart','completed','etag',unixepoch()-90000,unixepoch()-1)`).run(source.id);
      await expireAbandonedReservations({DB:sqliteAdapter(complete),BILLING_MODE:"production"} as IngestEnv);
      expect(complete.prepare("SELECT status FROM orders WHERE id=?").get(order)).toEqual({status:"queued"});
    } finally { complete.close(); }
  });

  it("rechaza y elimina una fuente cuyo contenido no coincide con el hash declarado", async () => {
    const db = databaseForIngest();
    const bytes = new TextEncoder().encode("falsaso");
    const expected = new TextEncoder().encode("fixture");
    const expectedHash = createHash("sha256").update(expected).digest("hex");
    const { bucket } = mockMedia(bytes);
    const restore = installDigestStream();
    const listing = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
      `<ListPartsResult><IsTruncated>false</IsTruncated><Part><PartNumber>1</PartNumber>
       <Size>7</Size><ETag>"${etag}"</ETag></Part></ListPartsResult>`));
    try {
      db.prepare("INSERT INTO production_source_sets(order_id,user_id,manifest,manifest_sha256) VALUES (?,'user',?,?)")
        .run(order, JSON.stringify([{ name: "uno.mp4", bytes: 7, sha256: expectedHash, extension: "mp4" }]), digest);
      const source = db.prepare("SELECT id FROM production_sources WHERE order_id=?").get(order) as { id: string };
      const env = { DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production",
        R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
        R2_SECRET_ACCESS_KEY: credentials.secretAccessKey } as IngestEnv;
      await createSourceUpload(env, "user", order, source.id);
      await expect(completeSourceUpload(request("/unused", "POST", { parts: [{ PartNumber: 1, ETag: etag }] }),
        env, "user", order, source.id)).rejects.toMatchObject({ status: 422, code: "SUBIDA_HASH_DISTINTO" });
      expect(bucket.delete).toHaveBeenCalledOnce();
      expect(db.prepare("SELECT status FROM production_source_uploads WHERE source_id=?").get(source.id)).toEqual({ status: "aborted" });
      expect(db.prepare("SELECT COUNT(*) AS n FROM job_outbox").get()).toEqual({ n: 0 });
      const correctBucket = mockMedia(expected).bucket;
      const retry = { ...env, MEDIA: correctBucket };
      expect(await (await createSourceUpload(retry, "user", order, source.id)).json()).toMatchObject({ status: "open" });
      expect(await (await completeSourceUpload(request("/unused", "POST", { parts: [{ PartNumber: 1, ETag: etag }] }),
        retry, "user", order, source.id)).json()).toMatchObject({ status: "completed", all_sources_complete: true });
    } finally { listing.mockRestore(); restore(); db.close(); }
  });

  it.each([["free", 0, 1], ["ilimitado", 3, 2]] as const)(
    "serializa dos cargas contra el límite del plan %s", async (plan, priority, accepted) => {
      const db = databaseForIngest();
      try {
        const { bucket } = mockMedia(new Uint8Array(7));
        const env = { DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production" } as IngestEnv;
        const other = "ae8ca550-12a5-4a4c-a50a-1aa11616c8f1";
        db.exec("UPDATE plan_quotas SET monthly_reels=2,storage_bytes=10,storage_limit_bytes=10 WHERE plan_id='free'");
        db.prepare(`INSERT INTO orders (id,user_id,plan_id,kind,status,idempotency_key,request_sha256,
          project_slug,queue_priority) VALUES (?,'user','free','production','queued','segunda',?,'job-segunda',0)`)
          .run(other, digest);
        db.prepare("INSERT INTO usage_ledger (id,user_id,order_id,period_start) VALUES ('segunda','user',?,strftime('%Y-%m-01','now'))").run(other);
        db.prepare("UPDATE orders SET plan_id=?,queue_priority=? WHERE id IN (?,?)").run(plan, priority, order, other);
        const start = "/api/uploads";
        const upload = (id: string, slug: string) => handleIngest(request(start, "POST", {
          order_id: id, project_slug: slug, extension: "mp4", bytes: 7, sha256: digest, content_type: "video/mp4",
        }), env, "user", start);
        const results = await Promise.allSettled([upload(order, "job-fixture"), upload(other, "job-segunda")]);
        expect(results.filter((entry) => entry.status === "fulfilled")).toHaveLength(accepted);
        expect(results.filter((entry) => entry.status === "rejected")).toEqual(
          accepted === 2 ? [] : [{ status: "rejected", reason: expect.objectContaining({ code: "ALMACENAMIENTO_AGOTADO" }) }],
        );
        expect(bucket.createMultipartUpload).toHaveBeenCalledTimes(accepted);
      } finally { db.close(); }
    },
  );

  it.each(["antes de consolidar", "después de consolidar", "con partes faltantes"] as const)(
    "recupera la confirmación interrumpida %s de R2 al repetir la apertura", async (phase) => {
      const db = databaseForIngest();
      const source = new TextEncoder().encode("fixture");
      const sha256 = createHash("sha256").update(source).digest("hex");
      const { bucket, handle } = mockMedia(source);
      const env: IngestEnv = {
        DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production",
        R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
        R2_SECRET_ACCESS_KEY: credentials.secretAccessKey,
      };
      const transient = new Error("R2 temporalmente inaccesible");
      if (phase === "después de consolidar") vi.mocked(bucket.get).mockResolvedValueOnce(null).mockRejectedValueOnce(transient);
      else handle.complete.mockRejectedValueOnce(transient);
      const restore = installDigestStream();
      const listing = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
        `<ListPartsResult><IsTruncated>false</IsTruncated><Part><PartNumber>1</PartNumber>
        <Size>${source.length}</Size><ETag>"${etag}"</ETag></Part></ListPartsResult>`,
      ));
      try {
        const start = "/api/uploads";
        const onCommitted = vi.fn();
        const api = (path: string, value: Record<string, unknown>) =>
          handleIngest(request(path, "POST", value), env, "user", path, onCommitted);
        const payload = { order_id: order, project_slug: "job-fixture", extension: "mp4",
          bytes: source.length, sha256, content_type: "video/mp4" };
        const created = await (await api(start, payload)).json() as { id: string; key: string };
        const complete = `${start}/${created.id}/complete`;
        const parts = { parts: [{ PartNumber: 1, ETag: `"${etag}"` }] };
        await expect(api(complete, parts)).rejects.toThrow("R2 temporalmente inaccesible");
        expect(db.prepare("SELECT status FROM upload_sessions WHERE id=?").get(created.id))
          .toEqual({ status: "completing" });
        await expect(api(start, payload)).rejects.toMatchObject({ code: "SUBIDA_EN_TRANSICION" });
        await expect(api(complete, parts)).rejects.toMatchObject({ code: "SUBIDA_EN_TRANSICION" });
        expect(handle.complete).toHaveBeenCalledOnce();
        expect(db.prepare("SELECT COUNT(*) AS n FROM job_outbox").get()).toEqual({ n: 0 });

        db.prepare("UPDATE upload_sessions SET lease_until=unixepoch()-1 WHERE id=?").run(created.id);
        if (phase === "con partes faltantes") {
          listing.mockResolvedValueOnce(new Response("<ListPartsResult><IsTruncated>false</IsTruncated></ListPartsResult>"));
          expect(await (await api(start, payload)).json()).toMatchObject({ id: created.id, uploadId, status: "open" });
          expect(db.prepare("SELECT status,operation_id,lease_until FROM upload_sessions WHERE id=?").get(created.id))
            .toEqual({ status: "open", operation_id: null, lease_until: null });
          expect(onCommitted).not.toHaveBeenCalled();
        }
        const resumed = await (await (phase === "con partes faltantes" ? api(complete, parts) : api(start, payload))).json();
        expect(resumed).toMatchObject({ id: created.id, key: created.key, uploadId, status: "completed", hash_verified: true });
        expect(await (await api(start, payload)).json()).toEqual(resumed);
        expect(bucket.createMultipartUpload).toHaveBeenCalledOnce();
        expect(handle.complete).toHaveBeenCalledTimes(phase === "después de consolidar" ? 1 : 2);
        expect(onCommitted).toHaveBeenCalledExactlyOnceWith(order);
        expect(db.prepare("SELECT status FROM usage_ledger WHERE order_id=?").get(order))
          .toEqual({ status: "reserved" });
        expect(db.prepare("SELECT COUNT(*) AS n FROM job_outbox WHERE order_id=?").get(order)).toEqual({ n: 1 });
      } finally {
        listing.mockRestore();
        restore();
        db.close();
      }
    },
  );

  it.each(["completed", "sha256_mismatch", "metadata_mismatch", "cancelled"] as const)("unifica apertura, confirmación y aborto con transacciones reales (%s)", async (outcome) => {
    const db = databaseForIngest();
    const source = new TextEncoder().encode("fixture");
    const sha256 = outcome === "sha256_mismatch" ? digest : createHash("sha256").update(source).digest("hex");
    const { bucket, handle } = mockMedia(source, outcome === "metadata_mismatch");
    const env: IngestEnv = {
      DB: sqliteAdapter(db), MEDIA: bucket, BILLING_MODE: "production",
      R2_ACCOUNT_ID: credentials.accountId, R2_ACCESS_KEY_ID: credentials.accessKeyId,
      R2_SECRET_ACCESS_KEY: credentials.secretAccessKey,
    };
    const restore = installDigestStream();
    const listing = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
      `<ListPartsResult><IsTruncated>false</IsTruncated><Part><PartNumber>1</PartNumber>
      <Size>${source.length}</Size><ETag>"${etag}"</ETag></Part></ListPartsResult>`,
    ));
    try {
      const start = "/api/uploads";
      const committedOrder = vi.fn(() => {
        expect(db.prepare("SELECT COUNT(*) AS n FROM job_outbox").get()).toMatchObject({ n: 1 });
      });
      const payload = {
        order_id: order, project_slug: "job-fixture", extension: "mp4",
        bytes: source.length, sha256, content_type: "video/mp4",
      };
      const api = (method: string, path: string, value?: Record<string, unknown>) =>
        handleIngest(request(path, method, value), env, "user", path, committedOrder);
      const created = await (await api("POST", start, payload)).json() as { id: string; key: string };
      expect(created).toMatchObject({ uploadId, status: "open", partCount: 1, newlyOpened: true });
      const resumed = await (await api("POST", start, payload)).json();
      expect(resumed).not.toHaveProperty("newlyOpened");
      expect({ ...resumed, newlyOpened: true }).toEqual(created);
      expect(bucket.createMultipartUpload).toHaveBeenCalledTimes(1);
      expect(db.prepare("SELECT COUNT(*) AS n FROM job_outbox").get()).toMatchObject({ n: 0 });
      await expect(api("POST", start, { ...payload, sha256: "d".repeat(64) })).rejects.toMatchObject({ status: 409 });
      const path = `${start}/${created.id}/complete`;
      const parts = { parts: [{ PartNumber: 1, ETag: `"${etag}"` }] };
      if (outcome !== "completed") {
        if (outcome === "cancelled") {
          const abortPath = `${start}/${created.id}`;
          expect((await api("DELETE", abortPath)).status).toBe(204);
          expect((await api("DELETE", abortPath)).status).toBe(204);
          expect(handle.abort).toHaveBeenCalledOnce();
          expect(bucket.delete).not.toHaveBeenCalled();
        } else {
          await expect(api("POST", path, parts)).rejects.toMatchObject({
            code: outcome === "sha256_mismatch" ? "SUBIDA_HASH_DISTINTO" : "SUBIDA_METADATOS",
          });
          expect(bucket.delete).toHaveBeenCalledWith(created.key);
        }
        expect(db.prepare("SELECT status FROM usage_ledger").get()).toMatchObject({ status: "released" });
        expect(db.prepare("SELECT status FROM orders").get()).toMatchObject({ status: "cancelled" });
        expect(db.prepare("SELECT COUNT(*) AS n FROM job_outbox").get()).toMatchObject({ n: 0 });
        expect(db.prepare("SELECT status,abort_reason FROM upload_sessions").get())
          .toEqual({ status: "aborted", abort_reason: outcome });
      } else {
        const completed = await (await api("POST", path, parts)).json();
        expect(completed).toMatchObject({ hash_verified: true, status: "completed" });
        expect(await (await api("POST", path, parts)).json()).toEqual(completed);
        expect(db.prepare("SELECT status FROM usage_ledger").get()).toMatchObject({ status: "reserved" });
        const items = db.prepare("SELECT payload FROM job_outbox").all() as Array<{ payload: string }>;
        expect(items.map((item) => JSON.parse(item.payload) as unknown)).toEqual([
          { ...job, asset_id: created.id, object_key: created.key, bytes: source.length, sha256 },
        ]);
        expect(bucket.delete).not.toHaveBeenCalled();
        expect(db.prepare("SELECT abort_reason FROM upload_sessions").get()).toEqual({ abort_reason: null });
        db.exec("UPDATE orders SET status = 'processing'; UPDATE usage_ledger SET status = 'consumed';");
        expect(await (await api("POST", start, payload)).json()).toEqual(completed);
      }
      expect(handle.complete).toHaveBeenCalledTimes(outcome === "cancelled" ? 0 : 1);
      expect(listing).toHaveBeenCalledTimes(outcome === "cancelled" ? 0 : 1);
      expect(committedOrder).toHaveBeenCalledTimes(outcome === "completed" ? 1 : 0);
      if (outcome === "completed") expect(committedOrder).toHaveBeenCalledWith(order);
    } finally {
      listing.mockRestore();
      restore();
      db.close();
    }
  });
});
