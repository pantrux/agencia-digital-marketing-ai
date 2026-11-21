import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";
import { prepareConversation, readConversation, saveConversation } from "../cloudflare-saas/studio-api-worker/src/conversation.js";
import { handleSourceSet } from "../cloudflare-saas/studio-api-worker/src/production-sources.js";
import { handleReference } from "../cloudflare-saas/studio-api-worker/src/references.js";
import type { SqlDatabase, SqlStatement } from "../cloudflare-saas/studio-api-worker/src/platform.js";

const order = "10b4d244-3ae5-4ea8-b56e-ddd35bfbdeed";
const requestId = "2037baa2-5189-4616-b6cc-cd9819e75543";
const user = "13dde435-f2f5-4797-a98f-d0affddcd812";
const other = "a110448d-5f5e-42ec-922c-1eb4b3e7e061";

function fixture() {
  const db = new DatabaseSync(":memory:");
  db.exec(`PRAGMA foreign_keys=ON;
    CREATE TABLE orders(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT, provider_mode TEXT,
      status TEXT NOT NULL DEFAULT 'queued', UNIQUE(id,user_id)) STRICT;
    CREATE TABLE usage_ledger(order_id TEXT, user_id TEXT, status TEXT);
    CREATE TABLE upload_sessions(order_id TEXT);`);
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0022_sofia_conversation.sql", import.meta.url), "utf8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0023_production_sources.sql", import.meta.url), "utf8"));
  db.exec(readFileSync(new URL("../cloudflare-saas/migrations/0025_brand_reference.sql", import.meta.url), "utf8"));
  db.prepare("INSERT INTO orders(id,user_id,kind,provider_mode) VALUES(?,?,?,?)").run(order, user, "production", "sandbox");
  db.prepare("INSERT INTO usage_ledger VALUES(?,?,?)").run(order, user, "reserved");
  function prepare(sql: string, values: (string | number | null)[] = []): SqlStatement {
    const statement = db.prepare(sql);
    return {
      bind: (...params) => prepare(sql, params),
      first: async <T>() => (statement.get(...values) as T | undefined) ?? null,
      all: async <T>() => ({ results: statement.all(...values) as T[], meta: {} }),
      run: async () => ({ results: [], meta: { changes: Number(statement.run(...values).changes) } }),
    };
  }
  const env = { DB: { prepare, withSession: () => ({ prepare }) } as unknown as SqlDatabase, BILLING_MODE: "sandbox" as const };
  return { db, env };
}

describe("Referencia de marca", () => {
  it("aísla cada producción, guarda el contenido público y limita consultas", async () => {
    const {env} = fixture();
    let calls = 0;
    const bound = {...env, REFERENCE_SERVICE: {fetch: async () => {
      calls += 1;
      return Response.json({status:"available", title:"Vivero", description:"Plantas de interior", excerpt:"Hechas para tu hogar"});
    }}};
    const req = () => new Request("https://studio.invalid/api", {method:"POST",
      headers:{"content-type":"application/json"}, body:JSON.stringify({url:"https://vivero.cl/"})});
    await expect(handleReference(req(), bound, other, order)).rejects.toMatchObject({status:404});
    expect(calls).toBe(0);
    expect(await (await handleReference(req(), bound, user, order)).json()).toMatchObject({reference:{
      status:"available", title:"Vivero", description:"Plantas de interior", excerpt:"Hechas para tu hogar"}});
    expect(await (await handleReference(new Request("https://studio.invalid/api"), bound, user, order)).json())
      .toMatchObject({reference:{source_url:"https://vivero.cl/", status:"available"}});
    await expect(handleReference(req(), bound, user, order)).rejects.toMatchObject({status:429});
    expect(calls).toBe(1);
  });
});

describe("Historial de Sofía", () => {
  it("recupera turnos vinculados a la cuenta y reintenta sin duplicar", async () => {
    const { env } = fixture();
    expect(await prepareConversation(env, user, order, requestId, "Quiero un reel")).toBe("Quiero un reel");
    await saveConversation(env, user, order, requestId, "Quiero un reel", "¿Para quién es?");
    await saveConversation(env, user, order, requestId, "Quiero un reel", "¿Para quién es?");
    const result = await readConversation(env, user, order);
    expect(await result.json()).toMatchObject({ turns: [{ request_id: requestId, message: "Quiero un reel", reply: "¿Para quién es?" }] });
    await expect(saveConversation(env, user, order, requestId, "Otro mensaje", "¿Para quién es?"))
      .rejects.toMatchObject({ status: 409 });
    await expect(readConversation(env, other, order)).rejects.toMatchObject({ status: 404 });
    await expect(saveConversation(env, other, order, requestId, "Quiero un reel", "¿Para quién es?"))
      .rejects.toMatchObject({ status: 409 });
  });
  it("incluye el diálogo anterior y conserva el contexto original en los reintentos", async () => {
    const { env } = fixture();
    const next = "33a8a5da-cff2-4d02-99e8-0e8ec20d0049";
    await prepareConversation(env, user, order, requestId, "Quiero un reel");
    await saveConversation(env, user, order, requestId, "Quiero un reel", "¿Para quién es?");
    const wire = await prepareConversation(env, user, order, next, "Para amantes de las plantas");
    expect(JSON.parse(wire)).toMatchObject({
      historial: [{ usuario: "Quiero un reel", sofia: "¿Para quién es?" }],
      mensaje_actual: "Para amantes de las plantas",
    });
    expect(await prepareConversation(env, user, order, next, "Para amantes de las plantas")).toBe(wire);
    await expect(prepareConversation(env, user, order, next, "Otro texto")).rejects.toMatchObject({ status: 409 });
  });
  it("aporta datos reales del proyecto y conserva parte de respuestas largas sin exceder el límite", async () => {
    const { env } = fixture();
    const next = "33a8a5da-cff2-4d02-99e8-0e8ec20d0049";
    await prepareConversation(env, user, order, requestId, "Busco un reel de plantas");
    await saveConversation(env, user, order, requestId, "Busco un reel de plantas", "🌿".repeat(2000));
    const wire = await prepareConversation(env, user, order, next, "¿Qué sigue?", {
      marca: "El Rincón", audiencia_confirmada: "Amantes de las plantas", campo_ausente: null,
    });
    expect(new TextEncoder().encode(wire).length).toBeLessThanOrEqual(2048);
    const context = JSON.parse(wire);
    expect(context.contexto_proyecto).toEqual({ marca: "El Rincón", audiencia_confirmada: "Amantes de las plantas" });
    expect(context.historial).toHaveLength(1);
    expect(context.historial[0].usuario).toBe("Busco un reel de plantas");
    expect(context.historial[0].sofia.length).toBeGreaterThan(0);
    expect(context.mensaje_actual).toBe("¿Qué sigue?");
    expect(await prepareConversation(env, user, order, next, "¿Qué sigue?", { marca: "Cambió" })).toBe(wire);
  });
  it("reserva espacio para el diálogo reciente ante una referencia y un brief extensos", async () => {
    const {env} = fixture();
    const next = "33a8a5da-cff2-4d02-99e8-0e8ec20d0049";
    await prepareConversation(env,user,order,requestId,"Prefiero una edición cálida y cercana");
    await saveConversation(env,user,order,requestId,"Prefiero una edición cálida y cercana","Usaré música suave y rótulos discretos.");
    const wire = await prepareConversation(env,user,order,next,"¿Y el cierre?",{
      referencia_estado:"available",referencia_titulo_publico:"Vivero".repeat(30),
      referencia_descripcion_publica:"Plantas".repeat(50),referencia_texto_publico:"Estilo cercano ".repeat(100),
      referencia_url:"https://vivero.cl/",marca:"Mi marca",objetivo_confirmado:"Mostrar plantas".repeat(30),
      audiencia_confirmada:"Amantes de las plantas".repeat(20),tono_confirmado:"Cercano".repeat(40),
    });
    const context = JSON.parse(wire);
    expect(new TextEncoder().encode(wire).length).toBeLessThanOrEqual(2048);
    expect(context.historial).toEqual([{usuario:"Prefiero una edición cálida y cercana",sofia:"Usaré música suave y rótulos discretos."}]);
    expect(context.mensaje_actual).toBe("¿Y el cierre?");
  });
  it("mantiene parte del último turno junto a un mensaje largo sin exceder el límite en bytes", async () => {
    const { env } = fixture();
    const next = "33a8a5da-cff2-4d02-99e8-0e8ec20d0049";
    const previous = "🌿".repeat(50);
    const reply = "🎬".repeat(50);
    const message = "a".repeat(1830);
    await prepareConversation(env, user, order, requestId, previous);
    await saveConversation(env, user, order, requestId, previous, reply);
    const wire = await prepareConversation(env, user, order, next, message, {
      objetivo_confirmado: "Mostrar varias plantas", referencia_texto_publico: "Vivero".repeat(100),
    });
    expect(new TextEncoder().encode(wire).length).toBeLessThanOrEqual(2048);
    const context = JSON.parse(wire);
    expect(context.mensaje_actual).toBe(message);
    expect(context.historial).toHaveLength(1);
    expect(context.historial[0].usuario).toMatch(/^(?:🌿)+$/u);
    expect(context.historial[0].sofia).toMatch(/^(?:🎬)+$/u);
  });
});

describe("Materiales de la producción", () => {
  it("registra un conjunto por orden, acepta reintentos idénticos y aísla al titular", async () => {
    const { db, env } = fixture();
    const entries = [
      { name: "principal.mp4", bytes: 10, sha256: "a".repeat(64), extension: "mp4" },
      { name: "detalle.mov", bytes: 20, sha256: "b".repeat(64), extension: "mov" },
    ];
    const req = (value: unknown) => new Request("https://studio.invalid/api", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sources: value }),
    });
    const first = await (await handleSourceSet(req(entries), env, user, order)).json();
    expect(first).toMatchObject({ sources: entries, assets: [
      { position: 0, upload_status: "pending", ...entries[0] }, { position: 1, upload_status: "pending", ...entries[1] },
    ] });
    const saved = db.prepare("SELECT id,position,name,bytes,sha256,extension FROM production_sources WHERE order_id=? ORDER BY position").all(order);
    expect(saved).toHaveLength(2);
    expect(saved.map(({ position, name, bytes, sha256, extension }) => ({ position, name, bytes, sha256, extension })))
      .toEqual(entries.map((entry, position) => ({ position, ...entry })));
    expect(saved[0]?.id).toMatch(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/u);
    expect(await (await handleSourceSet(req(entries), env, user, order)).json()).toEqual(first);
    expect(await (await handleSourceSet(new Request("https://studio.invalid/api"), env, user, order)).json()).toEqual(first);
    expect(db.prepare("SELECT id FROM production_sources WHERE order_id=? ORDER BY position").all(order))
      .toEqual(saved.map(({ id }) => ({ id })));
    const firstId = String(saved[0]?.id);
    db.prepare(`INSERT INTO production_source_uploads(source_id,object_key,upload_id,status,created_at,expires_at)
      VALUES (?,?,'multipart','open',1,2)`).run(firstId, "fixture/first");
    const partial = await (await handleSourceSet(new Request("https://studio.invalid/api"), env, user, order)).json();
    expect(partial.assets.map((asset: { upload_status: string }) => asset.upload_status)).toEqual(["open", "pending"]);
    expect(partial.assets.map((asset: { synced_at: number | null }) => asset.synced_at)).toEqual([null, null]);
    db.prepare("INSERT INTO production_source_outbox(source_id,payload,synced_at) VALUES (?,'{}',123)").run(firstId);
    const received = await (await handleSourceSet(new Request("https://studio.invalid/api"), env, user, order)).json();
    expect(received.assets.map((asset: { synced_at: number | null }) => asset.synced_at)).toEqual([123, null]);
    expect(() => db.prepare(`INSERT INTO production_source_uploads(source_id,object_key,status,created_at,expires_at)
      VALUES (?,?,'completed',1,2)`).run(String(saved[1]?.id), "fixture/second")).toThrow();
    await expect(handleSourceSet(req(entries.slice(0, 1)), env, user, order)).rejects.toMatchObject({ status: 409 });
    await expect(handleSourceSet(new Request("https://studio.invalid/api"), env, other, order))
      .rejects.toMatchObject({ status: 404 });
    db.prepare("UPDATE production_sources SET name='distinto.mp4' WHERE order_id=? AND position=0").run(order);
    await expect(handleSourceSet(new Request("https://studio.invalid/api"), env, user, order))
      .rejects.toMatchObject({ status: 503, code: "MATERIALES_INCONSISTENTES" });
  });
  it("rechaza registrar materiales después de que empieza la subida", async () => {
    const { db, env } = fixture();
    db.prepare("INSERT INTO upload_sessions VALUES(?)").run(order);
    const request = new Request("https://studio.invalid/api", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ sources: [{ name: "video.mp4", bytes: 1, sha256: "a".repeat(64), extension: "mp4" }] }),
    });
    await expect(handleSourceSet(request, env, user, order)).rejects.toMatchObject({ status: 409 });
  });
});
