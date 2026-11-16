import { createHmac } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import worker from "../cloudflare-saas/studio-api-worker/src/index.js";
import { browserSample } from "../cloudflare-saas/studio-portal-web/app/browser-timing.js";

const origin = "https://studio.e-scale.cl";
const context = { waitUntil: vi.fn() };

function environment(writeDataPoint: ReturnType<typeof vi.fn>) {
  return {
    PUBLIC_ORIGIN: origin,
    GOOGLE_CLIENT_ID: "ensayo.apps.googleusercontent.com",
    GOOGLE_CLIENT_SECRET: "secreto-de-ensayo",
    STUDIO_JWT_SESSION_SECRET: "ensayo-".repeat(8),
    STUDIO_HTTP_METRICS: { writeDataPoint },
  } as unknown as Parameters<typeof worker.fetch>[1];
}

describe("Medición HTTP de Studio", () => {
  it("registra la ruta normalizada sin el identificador de carga ni parámetros", async () => {
    const write = vi.fn();
    const request = new Request(
      origin + "/api/uploads/00000000-0000-4000-8000-000000000001/parts/1/sign?token=oculto",
      { method: "POST", headers: { Origin: origin } },
    );
    const response = await worker.fetch(request, environment(write), context);
    expect(response.status).toBe(401);
    expect(write.mock.calls).toEqual([[
      { indexes: ["multipart_firma"], blobs: ["produccion"], doubles: [expect.any(Number), 401] },
    ]]);
  });

  it("conserva la redirección y la cookie de OAuth si falla Analytics", async () => {
    const write = vi.fn(() => { throw new Error("ANALYTICS_NO_DISPONIBLE"); });
    const response = await worker.fetch(
      new Request(origin + "/api/auth/google"), environment(write), context,
    );
    expect(response.status).toBe(302);
    expect(response.headers.get("Set-Cookie")).toContain("HttpOnly");
    expect(write).toHaveBeenCalledOnce();
  });

  it("omite métodos, rutas y hosts ajenos a las mediciones", async () => {
    const write = vi.fn();
    const env = environment(write);
    await worker.fetch(new Request(origin + "/api/auth/google", { method: "HEAD" }), env, context);
    await worker.fetch(new Request(origin + "/api/orders"), env, context);
    await worker.fetch(new Request("https://example.invalid/api/auth/me"), env, context);
    expect(write).not.toHaveBeenCalled();
  });
});

describe("Medición del primer byte desde el portal", () => {
  const submission = "/api/metrics/ttfb";

  function browserFixture() {
    const metrics = vi.fn();
    const internal = vi.fn();
    const base = environment(internal);
    const first = vi.fn().mockResolvedValue({ id: "fixture", email: "medicion@example.org", display_name: null });
    const bound = { bind: () => ({ first }) };
    const env = {
      ...base, STUDIO_BROWSER_METRICS: { writeDataPoint: metrics },
      DB: { withSession: () => ({ prepare: () => bound }) } as unknown as typeof base.DB,
    };
    const claim = Buffer.from(JSON.stringify({ kind: "session", version: 2, nonce: "nonce-fixture",
      exp: Math.floor(Date.now() / 1000) + 30 })).toString("base64url");
    const signed = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." + claim;
    const mac = createHmac("sha256", base.STUDIO_JWT_SESSION_SECRET).update(signed).digest("base64url");
    const cookie = "__Host-studio_session=" + signed + "." + mac;
    const send = (body: object, credentials = cookie, caller = origin) => worker.fetch(new Request(origin + submission, {
      method: "POST", headers: { Origin: caller, Cookie: credentials, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }), env, context);
    return { send, cookie, metrics, internal, first };
  }

  it("guarda sólo ruta, estado, latencia y tipo de conexión en la serie del navegador", async () => {
    const { send, metrics, internal } = browserFixture();
    const reply = await send({ samples: [["panel", 200, 183, false], ["multipart_firma", 200, 201, true]] });
    expect(reply.status).toBe(204);
    expect(metrics.mock.calls).toEqual([
      [{ indexes: ["panel"], blobs: ["produccion", "nueva"], doubles: [183, 200] }],
      [{ indexes: ["multipart_firma"], blobs: ["produccion", "reutilizada"], doubles: [201, 200] }],
    ]);
    expect(internal).not.toHaveBeenCalled();
  });

  it("rechaza la tanda completa cuando una fila contiene identificadores o valores fuera del contrato", async () => {
    const { send, metrics } = browserFixture();
    expect((await send({ samples: [["panel", 200, 183, false], ["/api/uploads/identificador", 200, 300, true]] })).status).toBe(422);
    expect((await send({ samples: [["panel", 200, 120001, false]] })).status).toBe(422);
    expect((await send({ samples: [["panel", 401, 10, false]] })).status).toBe(422);
    expect(metrics).not.toHaveBeenCalled();
  });

  it("sólo admite el propio origen y una sesión vigente", async () => {
    const { send, cookie, metrics, first } = browserFixture();
    const body = { samples: [["panel", 200, 183, true]] };
    expect((await send(body, "")).status).toBe(401);
    expect((await send(body, cookie, "https://other.example.invalid")).status).toBe(403);
    first.mockResolvedValue(null);
    expect((await send(body)).status).toBe(401);
    expect(metrics).not.toHaveBeenCalled();
  });

  it("excluye las cuentas sintéticas de la serie de producción", async () => {
    const { send, metrics, first } = browserFixture();
    first.mockResolvedValue({ id: "fixture", email: "medicion@example.invalid", display_name: null });
    expect((await send({ samples: [["panel", 200, 183, false]] })).status).toBe(204);
    expect(metrics).toHaveBeenCalledWith({ indexes: ["panel"], blobs: ["ensayo", "nueva"], doubles: [183, 200] });
  });
});

it("mide desde requestStart y descarta el identificador y los parámetros antes de enviar", () => {
  const entry = {
    name: origin + "/api/uploads/00000000-0000-4000-8000-000000000001/parts/3/sign?state=valor",
    initiatorType: "fetch", requestStart: 100, responseStart: 282.8, responseStatus: 200, transferSize: 1024,
    connectStart: 98, connectEnd: 98,
  } as PerformanceResourceTiming;
  expect(browserSample(entry, origin)).toEqual(["multipart_firma", 200, 183, true]);
  expect(browserSample({ ...entry, transferSize: 0 }, origin)).toBeNull();
  expect(browserSample({ ...entry, responseStatus: 401 }, origin)).toBeNull();
  expect(browserSample({ ...entry, name: entry.name.replace(origin, "https://r2.example.invalid") }, origin)).toBeNull();
});
