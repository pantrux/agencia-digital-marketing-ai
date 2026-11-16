import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { completeOrderWithKit, deliveryKitKey, handleKits, publishDeliveryKit, type KitsEnv } from "../cloudflare-saas/studio-api-worker/src/kits.js";
import { handleKitProducer, kitProducerPath } from "../cloudflare-saas/studio-api-worker/src/kit-producer.js";
import { createHmac } from "node:crypto";
import type { SqlDatabase, SqlRows, SqlStatement } from "../cloudflare-saas/studio-api-worker/src/platform.js";

const ORDER = "00000000-0000-4000-8000-000000000001";
const KIT = "00000000-0000-4000-8000-000000000002";
const OTHER = "00000000-0000-4000-8000-000000000003";
const ZIP = new Uint8Array([0x50, 0x4b, 0x03, 0x04]);
const SHA = "a".repeat(64);
const key = "kits/sandbox/" + ORDER + "/" + KIT + ".zip";
const base = "/api/kits";
const path = base + "/" + KIT + "/download";
const input = { id: KIT, orderId: ORDER, userId: "owner", title: "Campaña noviembre", bytes: ZIP.length, sha256: SHA };
const signedMetadata = { key, size: ZIP.length, etag: "d".repeat(32), version: "version-original",
  customMetadata: { sha256: SHA },
  httpMetadata: { contentType: "text/html", cacheControl: "public", contentDisposition: "inline" } };
let db: DatabaseSync;
let env: KitsEnv;

class Statement implements SqlStatement {
  constructor(readonly sql: string, readonly args: (string | number | null)[] = []) {}
  bind(...values: (string | number | null)[]): SqlStatement { return new Statement(this.sql, values); }
  async first<T>(): Promise<T | null> { return db.prepare(this.sql).get(...this.args) as T | undefined ?? null; }
  async all<T>(): Promise<SqlRows<T>> { return { results: db.prepare(this.sql).all(...this.args) as T[], meta: {} }; }
  async run(): Promise<SqlRows<never>> {
    return { results: [], meta: { changes: Number(db.prepare(this.sql).run(...this.args).changes) } };
  }
}

function context(): KitsEnv {
  const database: SqlDatabase = {
    prepare: (sql) => new Statement(sql),
    withSession: () => database,
    async batch<T>(statements: SqlStatement[]) {
      db.exec("BEGIN");
      try {
        const result = [];
        for (const statement of statements as Statement[]) {
          result.push(statement.sql.startsWith("SELECT") ? await statement.all<T>() : await statement.run());
        }
        db.exec("COMMIT");
        return result;
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
    },
  };
  return { DB: database, BILLING_MODE: "sandbox",
    MEDIA: {
      head: vi.fn(async () => signedMetadata),
      get: vi.fn(async () => ({ ...signedMetadata, body: new ReadableStream<Uint8Array>({
        start(controller) { controller.enqueue(ZIP); controller.close(); },
      }) })),
    } };
}

beforeEach(() => {
  db = new DatabaseSync(":memory:");
  db.exec("PRAGMA foreign_keys=ON");
  for (const name of ["0001_schema.sql", "0003_delivery_kits.sql", "0007_delivery_duration.sql",
    "0010_production_manual_review.sql", "0011_production_output_format.sql", "0012_delivery_prores.sql"]) {
    db.exec(readFileSync(new URL("../cloudflare-saas/migrations/" + name, import.meta.url), "utf8"));
  }
  const person = db.prepare("INSERT INTO users (id,google_sub,email) VALUES (?,?,?)");
  person.run("owner", "sub1", "fixture@example.test");
  person.run("other", "sub2", "other@example.test");
  const order = db.prepare("INSERT INTO orders (id,user_id,plan_id,kind,status,idempotency_key,request_sha256," +
    "project_slug,queue_priority,provider_mode) VALUES (?,?, 'free', 'production', ?, ?, ?, ?, 0, 'sandbox')");
  order.run(ORDER, "owner", "completed", "fixture", SHA, "job-fixture");
  order.run(OTHER, "other", "completed", "fixture", SHA, "job-other");
  env = context();
});

afterEach(() => db.close());

function send(method: string, target: string, userId = "owner", headers?: HeadersInit) {
  return handleKits(new Request("https://studio.e-scale.cl" + target, { method, headers: headers ?? {} }),
    env, userId, target);
}

it("registra metadatos de un pedido propio, devuelve enlace protegido y reproduce el alta idéntica", async () => {
  expect(deliveryKitKey("sandbox", ORDER, KIT)).toBe(key);
  const first = await publishDeliveryKit(env, input);
  expect(first).toMatchObject({ id: KIT, order_id: ORDER, title: input.title,
    bytes: ZIP.length, url: path });
  expect(await publishDeliveryKit(env, input)).toEqual(first);
  expect(await (await send("GET", base)).json()).toEqual({ kits: [first] });
  expect(await (await send("GET", base, "other")).json()).toEqual({ kits: [] });
  expect(env.MEDIA.head).toHaveBeenCalledWith(key);
  expect(db.prepare("SELECT count(*) AS n FROM delivery_kits").get()?.n).toBe(1);
});

it("cierra alcance ajeno, estado no completado, entorno cruzado y IDs manipulados antes de R2", async () => {
  await expect(publishDeliveryKit(env, { ...input, userId: "other" })).rejects.toMatchObject({ status: 404 });
  await expect(publishDeliveryKit(env, { ...input, id: "../raw/video.mp4" })).rejects.toMatchObject({ status: 422 });
  await expect(publishDeliveryKit({ ...env, BILLING_MODE: "production" }, input)).rejects.toMatchObject({ status: 404 });
  db.prepare("UPDATE orders SET status='processing' WHERE id=?").run(ORDER);
  await expect(publishDeliveryKit(env, input)).rejects.toMatchObject({ status: 404 });
  expect(env.MEDIA.head).not.toHaveBeenCalled();
  expect(env.MEDIA.get).not.toHaveBeenCalled();
});

it("descarga por streaming sólo como propietario y emite headers de adjunto de confianza", async () => {
  await publishDeliveryKit(env, input);
  await expect(send("GET", path, "other")).rejects.toMatchObject({ status: 404 });
  await expect(send("GET", base + "/%2e%2e/download")).rejects.toMatchObject({ status: 404 });
  expect(env.MEDIA.get).not.toHaveBeenCalled();
  const head = await send("HEAD", path);
  expect(head.status).toBe(200);
  expect(head.body).toBeNull();
  expect(env.MEDIA.get).not.toHaveBeenCalled();
  const download = await send("GET", path);
  expect(download.status).toBe(200);
  expect(download.headers.get("content-type")).toBe("application/zip");
  expect(download.headers.get("content-disposition")).toBe('attachment; filename="kit-' + KIT + '.zip"');
  expect(download.headers.get("cache-control")).toBe("private, no-store");
  expect(download.headers.get("x-content-type-options")).toBe("nosniff");
  expect(download.headers.get("content-length")).toBe(String(ZIP.length));
  expect(new Uint8Array(await download.arrayBuffer())).toEqual(ZIP);
  expect(env.MEDIA.get).toHaveBeenCalledWith(key);
});

it.each([
  ["free", 7], ["pro", 30], ["ultra", 90], ["ilimitado", null],
] as const)("aplica la retención del plan %s en la lista y ambos métodos de descarga", async (plan, days) => {
  db.prepare("UPDATE orders SET plan_id=? WHERE id=?").run(plan, ORDER);
  const kit = await publishDeliveryKit(env, input);
  const cutoff = kit.created_at + (days ?? 3650) * 86400;
  const clock = vi.spyOn(Date, "now");
  try {
    clock.mockReturnValue((cutoff - 1) * 1000);
    expect(await (await send("GET", base)).json()).toEqual({ kits: [kit] });
    expect((await send("HEAD", path)).status).toBe(200);
    clock.mockReturnValue(cutoff * 1000);
    if (days === null) {
      expect(await (await send("GET", base)).json()).toEqual({ kits: [kit] });
      expect((await send("GET", path)).status).toBe(200);
    } else {
      expect(await (await send("GET", base)).json()).toEqual({ kits: [] });
      const before = vi.mocked(env.MEDIA.head).mock.calls.length;
      await expect(send("HEAD", path)).rejects.toMatchObject({ status: 404 });
      await expect(send("GET", path)).rejects.toMatchObject({ status: 404 });
      expect(env.MEDIA.head).toHaveBeenCalledTimes(before);
      expect(env.MEDIA.get).not.toHaveBeenCalled();
      expect(await publishDeliveryKit(env, input)).toEqual(kit);
    }
  } finally {
    clock.mockRestore();
  }
});

it("cancela el stream si cambia la versión física, aunque coincidan contenido declarado y ETag", async () => {
  await publishDeliveryKit(env, input);
  let cancelled = false;
  env.MEDIA.get = vi.fn(async () => ({ ...signedMetadata, version: "other-version",
    body: new ReadableStream({ cancel() { cancelled = true; } }) }));
  await expect(send("GET", path)).rejects.toMatchObject({ status: 409, code: "KIT_OBJETO_CAMBIO" });
  expect(cancelled).toBe(true);
});

it("valida el tamaño, hash declarado y título, y reserva el ID para el manifiesto publicado", async () => {
  await expect(publishDeliveryKit(env, { ...input, sha256: "b".repeat(64) })).rejects.toMatchObject({ status: 409 });
  await expect(publishDeliveryKit(env, { ...input, bytes: 15 })).rejects.toMatchObject({ status: 409 });
  await expect(publishDeliveryKit(env, { ...input, title: "Título\r\nX-Any: b" })).rejects.toMatchObject({ status: 422 });
  await publishDeliveryKit(env, input);
  await expect(publishDeliveryKit(env, { ...input, title: "Título distinto" })).rejects.toMatchObject({ status: 409 });
  db.prepare("UPDATE orders SET status='failed' WHERE id=?").run(ORDER);
  await expect(send("GET", path)).rejects.toMatchObject({ status: 404 });
  expect(await (await send("GET", base)).json()).toEqual({ kits: [] });
});

function processingOrder(format: "mp4" | "prores4444" = "mp4") {
  const userId = "00000000-0000-4000-8000-000000000004";
  const orderId = "00000000-0000-4000-8000-000000000005";
  const uploadId = "00000000-0000-4000-8000-000000000006";
  db.prepare("INSERT INTO users (id,google_sub,email) VALUES (?,?,?)").run(userId, "sub3", "pilot@example.test");
  const plan = format === "prores4444" ? "ilimitado" : "free";
  if (plan === "ilimitado") {
    db.prepare(`INSERT INTO subscriptions (id,user_id,plan_id,status,billing_status,provider_mode,external_reference,
      provider_subscription_id,current_period_start,current_period_end)
      VALUES ('ilimitado-test',?,'ilimitado','active','paid','sandbox','fixture','test',unixepoch()-60,unixepoch()+600)`).run(userId);
  }
  db.prepare("INSERT INTO orders (id,user_id,plan_id,kind,status,idempotency_key,request_sha256," +
    "project_slug,queue_priority,provider_mode,output_format) VALUES (?,?,?,'production','queued','pilot',?,'job-pilot',?,'sandbox',?)")
    .run(orderId, userId, plan, SHA, plan === "ilimitado" ? 3 : 0, format);
  db.prepare("INSERT INTO usage_ledger(id,user_id,order_id,period_start) VALUES ('usage',?,?,strftime('%Y-%m-01','now'))")
    .run(userId, orderId);
  db.prepare("INSERT INTO upload_sessions(id,user_id,order_id,object_key,sha256,bytes,mime_type,extension,upload_id,status,object_etag,created_at,expires_at) " +
    "VALUES (?,?,?,'uploads/pilot',?,4,'video/mp4','mp4','multipart','completed','etag',unixepoch(),unixepoch()+60)")
    .run(uploadId, userId, orderId, SHA);
  db.prepare("UPDATE orders SET status='processing' WHERE id=?").run(orderId);
  env.MEDIA.head = vi.fn(async (key: string) => ({ ...signedMetadata, key }));
  return { ...input, userId, orderId, uploadId, sourceSha256: SHA };
}

it("cierra Ilimitado solo con ZIP y MOV, cuenta ambos archivos y protege la descarga", async () => {
  const approved = processingOrder("prores4444");
  db.exec("UPDATE usage_ledger SET status='consumed'; UPDATE plan_quotas SET storage_bytes=11,storage_limit_bytes=11 WHERE plan_id='ilimitado'");
  const graphic = new Uint8Array([0x6d, 0x6f, 0x6f, 0x76]);
  const graphicSha = "b".repeat(64);
  const movieKey = `kits/sandbox/${approved.orderId}/${approved.id}.mov`;
  const movie = { key: movieKey, size: graphic.length, etag: "movie-etag", version: "movie-version",
    customMetadata: { sha256: graphicSha } };
  const value = { ...approved, durationMs: 60000, prores: { bytes: graphic.length, sha256: graphicSha } };
  const route = `${base}/${approved.id}/prores`;
  env.MEDIA.head = vi.fn(async (objectKey) => objectKey === movieKey ? null : { ...signedMetadata, key: objectKey });
  await expect(completeOrderWithKit(env, approved)).rejects.toMatchObject({ status: 404 });
  expect(env.MEDIA.head).not.toHaveBeenCalled();
  await expect(completeOrderWithKit(env, value)).rejects.toMatchObject({ status: 409, code: "KIT_OBJETO_CAMBIO" });
  env.MEDIA.head = vi.fn(async (objectKey) => objectKey === movieKey ? movie : { ...signedMetadata, key: objectKey });
  await expect(completeOrderWithKit(env, value)).rejects.toMatchObject({ status: 409, code: "ALMACENAMIENTO_AGOTADO" });
  expect(db.prepare("SELECT status FROM orders WHERE id=?").get(approved.orderId)?.status).toBe("processing");
  db.exec("UPDATE plan_quotas SET storage_limit_bytes=12 WHERE plan_id='ilimitado'");
  const kit = await completeOrderWithKit(env, value);
  expect(kit.prores).toEqual({ bytes: graphic.length, url: route });
  expect(await completeOrderWithKit(env, value)).toEqual(kit);
  expect(await (await send("GET", base, approved.userId)).json()).toEqual({ kits: [kit] });
  await expect(send("GET", route, "other")).rejects.toMatchObject({ status: 404 });
  expect(env.MEDIA.get).not.toHaveBeenCalled();
  const info = await send("HEAD", route, approved.userId);
  expect(info.body).toBeNull();
  expect(info.headers.get("content-type")).toBe("video/quicktime");
  expect(info.headers.get("content-disposition")).toBe(`attachment; filename="grafica-${KIT}.mov"`);
  env.MEDIA.get = vi.fn(async () => ({ ...movie, body: new Response(graphic).body! }));
  const download = await send("GET", route, approved.userId);
  expect(download.headers.get("cache-control")).toBe("private, no-store");
  expect(new Uint8Array(await download.arrayBuffer())).toEqual(graphic);
  expect(env.MEDIA.get).toHaveBeenCalledWith(movieKey);
  env.MEDIA.get = vi.fn(async () => ({ ...movie, version: "changed", body: new Response(graphic).body! }));
  await expect(send("GET", route, approved.userId)).rejects.toMatchObject({ status: 409 });
});

it("la firma v3 vincula el tamaño y la huella del MOV con la entrega", async () => {
  const approved = processingOrder("prores4444");
  db.exec("UPDATE usage_ledger SET status='consumed'");
  const secret = "a".repeat(64);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const proresBytes = 8, proresSha256 = "b".repeat(64), durationMs = 60000;
  const values = [approved.id, approved.orderId, approved.userId, approved.title, approved.bytes,
    approved.sha256, approved.uploadId, approved.sourceSha256, durationMs, proresBytes, proresSha256];
  const wire = JSON.stringify(["studio-kit-v3", "POST", kitProducerPath, "sandbox", timestamp, values]);
  const signature = "v3=" + createHmac("sha256", secret).update(wire).digest("hex");
  const invoke = (digest: string) => handleKitProducer(new Request("https://studio.e-scale.cl" + kitProducerPath, {
    method: "POST", headers: { "content-type": "application/json", timestamp, signature },
    body: JSON.stringify({ ...approved, durationMs, proresBytes, proresSha256: digest }),
  }), { ...env, STUDIO_KIT_PRODUCER_HMAC_SECRET: secret });
  env.MEDIA.head = vi.fn(async (objectKey) => objectKey.endsWith(".mov")
    ? { ...signedMetadata, key: objectKey, size: proresBytes, customMetadata: { sha256: proresSha256 } }
    : { ...signedMetadata, key: objectKey });
  await expect(invoke("c".repeat(64))).rejects.toMatchObject({ status: 401 });
  expect(env.MEDIA.head).not.toHaveBeenCalled();
  expect((await invoke(proresSha256)).status).toBe(200);
});

it("cierra el pedido con cuota consumida y crudo verificado; el reintento conserva la entrega", async () => {
  const approved = processingOrder();
  await expect(completeOrderWithKit(env, approved)).rejects.toMatchObject({ status: 404 });
  db.exec("UPDATE usage_ledger SET status='consumed'");
  await expect(completeOrderWithKit(env, { ...approved, sourceSha256: "b".repeat(64) })).rejects.toMatchObject({ status: 404 });
  expect(env.MEDIA.head).not.toHaveBeenCalled();
  const saved = await completeOrderWithKit(env, approved);
  expect(await completeOrderWithKit(env, approved)).toEqual(saved);
  await expect(completeOrderWithKit(env, { ...approved, id: OTHER })).rejects.toMatchObject({ status: 409 });
  expect(db.prepare("SELECT status FROM orders WHERE id=?").get(approved.orderId)?.status).toBe("completed");
  expect(await (await send("GET", base, approved.userId)).json()).toEqual({ kits: [saved] });
});

it("retiene la entrega automática mientras el montaje requiere revisión manual", async () => {
  const approved = processingOrder();
  db.exec("UPDATE usage_ledger SET status='consumed'");
  db.prepare("INSERT INTO production_manual_reviews VALUES (?,1,?,1)").run(approved.orderId, approved.sha256);
  await expect(completeOrderWithKit(env, approved)).rejects.toMatchObject({ status: 404 });
  expect(() => db.prepare(`INSERT INTO delivery_kits(id,order_id,user_id,provider_mode,title,bytes,sha256,r2_etag,r2_version,created_at)
    VALUES (?,?,?,'sandbox',?,?,?,'etag','version',1)`).run(approved.id, approved.orderId, approved.userId,
    approved.title, approved.bytes, approved.sha256)).toThrow("PRODUCTION_MANUAL_REVIEW");
  expect(db.prepare("SELECT count(*) AS n FROM delivery_kits").get()?.n).toBe(0);
  db.prepare("DELETE FROM production_manual_reviews WHERE order_id=?").run(approved.orderId);
  expect(await completeOrderWithKit(env, approved)).toMatchObject({ id: approved.id });
});

it("aplica al cierre el espacio ocupado por el crudo y permite reintentar al recuperar espacio", async () => {
  const approved = processingOrder();
  db.exec("UPDATE usage_ledger SET status='consumed'; UPDATE plan_quotas SET storage_bytes=7,storage_limit_bytes=7 WHERE plan_id='free'");
  await expect(completeOrderWithKit(env, approved)).rejects.toMatchObject({
    status: 409, code: "ALMACENAMIENTO_AGOTADO",
  });
  expect(db.prepare("SELECT count(*) AS n FROM delivery_kits").get()?.n).toBe(0);
  expect(db.prepare("SELECT status FROM orders WHERE id=?").get(approved.orderId)?.status).toBe("processing");
  db.exec("UPDATE plan_quotas SET storage_limit_bytes=8 WHERE plan_id='free'");
  const saved = await completeOrderWithKit(env, approved);
  expect(await completeOrderWithKit(env, approved)).toEqual(saved);
});

it("aplica el tope a las revisiones nuevas y conserva la entrega registrada", async () => {
  db.exec("UPDATE plan_quotas SET storage_bytes=4,storage_limit_bytes=4 WHERE plan_id='free'");
  env.MEDIA.head = vi.fn(async (objectKey: string) => ({ ...signedMetadata, key: objectKey }));
  const saved = await publishDeliveryKit(env, input);
  await expect(publishDeliveryKit(env, { ...input, id: OTHER })).rejects.toMatchObject({
    status: 409, code: "ALMACENAMIENTO_AGOTADO",
  });
  expect(await publishDeliveryKit(env, input)).toEqual(saved);
  db.prepare("UPDATE delivery_kits SET created_at=unixepoch()-7*86400 WHERE id=?").run(KIT);
  expect(await publishDeliveryKit(env, { ...input, id: OTHER })).toMatchObject({ id: OTHER });
});

it("una escritura fallida o una firma inválida no publica el kit ni completa el pedido", async () => {
  const approved = processingOrder();
  db.exec("UPDATE usage_ledger SET status='consumed'");
  const secret = "a".repeat(64);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const message = JSON.stringify(["studio-kit-v1", "POST", kitProducerPath, "sandbox", timestamp,
    [approved.id, approved.orderId, approved.userId, approved.title, approved.bytes, approved.sha256, approved.uploadId, approved.sourceSha256]]);
  const signature = "v1=" + createHmac("sha256", secret).update(message).digest("hex");
  const invoke = (title: string, when = timestamp) => handleKitProducer(new Request("https://studio.e-scale.cl" + kitProducerPath, {
    method: "POST", headers: { "content-type": "application/json", timestamp: when, signature }, body: JSON.stringify({ ...approved, title }),
  }), { ...env, STUDIO_KIT_PRODUCER_HMAC_SECRET: secret });
  await expect(invoke("otro título")).rejects.toMatchObject({ status: 401 });
  await expect(invoke(approved.title, String(Number(timestamp) - 121))).rejects.toMatchObject({ status: 401 });
  db.exec("CREATE TRIGGER force_error BEFORE UPDATE ON orders BEGIN SELECT RAISE(ABORT, 'fallo'); END");
  await expect(invoke(approved.title)).rejects.toThrow("fallo");
  expect(db.prepare("SELECT count(*) AS n FROM delivery_kits").get()?.n).toBe(0);
  expect(db.prepare("SELECT status FROM orders WHERE id=?").get(approved.orderId)?.status).toBe("processing");
  db.exec("DROP TRIGGER force_error");
  expect((await invoke(approved.title)).status).toBe(200);
});

it.each([false, true])("la firma v2 vincula la duración del máster a un único manifiesto (v1 previo: %s)", async (previous) => {
  const approved = processingOrder();
  db.exec("UPDATE usage_ledger SET status='consumed'");
  if (previous) await completeOrderWithKit(env, approved);
  const secret = "a".repeat(64);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const values = [approved.id, approved.orderId, approved.userId, approved.title, approved.bytes,
    approved.sha256, approved.uploadId, approved.sourceSha256];
  const invoke = (durationMs: number, signedDuration: number) => {
    const wire = JSON.stringify(["studio-kit-v2", "POST", kitProducerPath, "sandbox", timestamp, [...values, signedDuration]]);
    const signature = "v2=" + createHmac("sha256", secret).update(wire).digest("hex");
    return handleKitProducer(new Request("https://studio.e-scale.cl" + kitProducerPath, {
      method: "POST", headers: { "content-type": "application/json", timestamp, signature },
      body: JSON.stringify({ ...approved, durationMs }),
    }), { ...env, STUDIO_KIT_PRODUCER_HMAC_SECRET: secret });
  };
  await expect(invoke(60001, 60000)).rejects.toMatchObject({ status: 401 });
  expect((await invoke(60000, 60000)).status).toBe(200);
  expect((await invoke(60000, 60000)).status).toBe(200);
  await expect(invoke(60001, 60001)).rejects.toMatchObject({ status: 409 });
  expect(db.prepare("SELECT duration_ms FROM delivery_kits").all()).toEqual([{ duration_ms: 60000 }]);
});
