import { createHash, createHmac } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { handleSofiaChat, type ChatEnv } from "../cloudflare-saas/studio-api-worker/src/chat.js";

const order = "10b4d244-3ae5-4ea8-b56e-ddd35bfbdeed";
const id = "2037baa2-5189-4616-b6cc-cd9819e75543";
const user = "13dde435-f2f5-4797-a98f-d0affddcd812";
const secret = "fixture".repeat(7);
const origin = "https://api-studio.e-scale.cl";
const answer = "Primero, definamos el objetivo.";
const body = { order_id: order, message: " Necesito definir el objetivo. " };
const hash = (text: string) => createHash("sha256").update(text).digest("hex");
const mac = (value: string) => createHmac("sha256", secret).update(value).digest("hex");

function fixture(owned = true) {
  const first = vi.fn().mockResolvedValue(owned ? { id: order, plan_id: "free" } : null);
  const bind = vi.fn<(...values: string[]) => { first: typeof first }>(() => ({ first }));
  const prepare = vi.fn<(sql: string) => { bind: typeof bind }>(() => ({ bind }));
  const env = {
    DB: { withSession: () => ({ prepare }) },
    BILLING_MODE: "sandbox", SOFIA_OWNER_USER_ID: user,
    SOFIA_BROKER_ORIGIN: origin, STUDIO_SOFIA_HMAC_SECRET: secret,
  } as unknown as ChatEnv;
  return { env, first, bind, prepare };
}

function request(value: Record<string, unknown> = body, requestId = id): Request {
  return new Request("https://studio.e-scale.cl/api/chat", {
    method: "POST", headers: { "content-type": "application/json", "Idempotency-Key": requestId },
    body: JSON.stringify(value),
  });
}

function brokerReply(timestamp: string, data: object, signature?: string): Response {
  const raw = JSON.stringify(data);
  return new Response(raw, { headers: {
    "content-type": "application/json",
    timestamp,
    signature: signature ?? `v1=${mac(`studio-sofia-v1\nresponse\n${timestamp}\n200\n/v1/sofia/chat\n${hash(raw)}`)}`,
  } });
}

describe("Chat del portal con Sofía", () => {
  it("vincula usuario y orden, firma una solicitud exacta y proyecta únicamente la respuesta verificada", async () => {
    const { env, bind, prepare } = fixture();
    const send = vi.fn<typeof fetch>(async (url, init) => {
      expect(url).toBe(origin + "/v1/sofia/chat");
      expect(init).toMatchObject({ method: "POST", redirect: "manual", cache: "no-store" });
      const raw = init?.body as string;
      const hdr = new Headers(init?.headers);
      const timestamp = hdr.get("timestamp") ?? "";
      const actor = mac(`studio-sofia-v1\nactor\n${user}`);
      expect(JSON.parse(raw)).toEqual({
        agent_id: "agency-account", actor_id: actor, order_id: order,
        billing_mode: "sandbox", plan_id: "free", request_id: id, message: "Necesito definir el objetivo.",
      });
      expect(hdr.get("signature")).toBe(`v1=${mac(`studio-sofia-v1\nrequest\n${timestamp}\nPOST\n/v1/sofia/chat\n${hash(raw)}`)}`);
      expect(hdr.has("authorization")).toBe(false);
      expect(raw).not.toContain(user);
      return brokerReply(timestamp, { actor_id: actor, order_id: order, request_id: id, text: answer });
    });
    const result = await handleSofiaChat(request(), env, user, send);
    expect(result.headers.get("Cache-Control")).toBe("no-store");
    expect(await result.json()).toEqual({ text: answer, request_id: id });
    expect(bind).toHaveBeenCalledWith(order, user, "sandbox");
    expect(prepare.mock.calls.at(0)?.[0]).toContain("s.status = 'completed'");
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("no consulta el broker cuando el titular de la orden no coincide", async () => {
    const { env } = fixture(false);
    const send = vi.fn<typeof fetch>();
    await expect(handleSofiaChat(request(), env, user, send)).rejects.toMatchObject({ status: 404 });
    expect(send).not.toHaveBeenCalled();
  });

  it("exige sandbox y titular fijado antes de consultar D1; excluye órdenes de pago", async () => {
    const { env, first } = fixture();
    const send = vi.fn<typeof fetch>();
    const withoutOwner = { ...env };
    delete withoutOwner.SOFIA_OWNER_USER_ID;
    for (const settings of [{ ...env, BILLING_MODE: "production" as const }, withoutOwner]) {
      await expect(handleSofiaChat(request(), settings, user, send)).rejects.toMatchObject({ status: 503 });
    }
    await expect(handleSofiaChat(request(), env, id, send)).rejects.toMatchObject({ status: 403 });
    expect(first).not.toHaveBeenCalled();
    first.mockResolvedValueOnce({ id: order, plan_id: "pro" });
    await expect(handleSofiaChat(request(), env, user, send)).rejects.toMatchObject({ status: 403 });
    expect(send).not.toHaveBeenCalled();
  });

  it.each([
    { order_id: order, message: "", agent_id: "otro" },
    { order_id: order, message: "texto", system: "otro" },
    { order_id: "externa", message: "texto" },
    { order_id: order, message: "💜".repeat(3000) },
  ])("rechaza mensajes y campos fuera del contrato", async (value) => {
    const { env, first } = fixture();
    const send = vi.fn<typeof fetch>();
    await expect(handleSofiaChat(request(value), env, user, send)).rejects.toMatchObject({ status: 422 });
    expect(first).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });

  it("exige clave de idempotencia válida y destino fijado", async () => {
    const { env } = fixture();
    const send = vi.fn<typeof fetch>();
    await expect(handleSofiaChat(request(body, "otra clave"), env, user, send)).rejects.toMatchObject({ status: 400 });
    await expect(handleSofiaChat(request(), { ...env, SOFIA_BROKER_ORIGIN: "https://example.invalid" }, user, send))
      .rejects.toMatchObject({ status: 503 });
    expect(send).not.toHaveBeenCalled();
  });

  it.each(["firma", "actor", "orden", "request_id", "extra", "expiry"])("rechaza respuestas alteradas, cruzadas o vencidas (%s)", async (which) => {
    const { env } = fixture();
    const actor = mac(`studio-sofia-v1\nactor\n${user}`);
    const send = vi.fn<typeof fetch>(async (_url, init) => {
      const time = new Headers(init?.headers).get("timestamp") ?? "";
      const data = { actor_id: actor, order_id: order, request_id: id, text: answer };
      if (which === "actor") data.actor_id = "a".repeat(64);
      if (which === "orden") data.order_id = id;
      if (which === "request_id") data.request_id = order;
      if (which === "extra") Object.assign(data, { internal_token: "no aparece" });
      return brokerReply(which === "expiry" ? String(Number(time) - 301) : time, data,
        which === "firma" ? "v1=" + "0".repeat(64) : undefined);
    });
    await expect(handleSofiaChat(request(), env, user, send)).rejects.toMatchObject({ code: "CHAT_RESPUESTA_INVALIDA" });
  });

  it("no reintenta cuando el broker está ocupado", async () => {
    const { env } = fixture();
    const send = vi.fn<typeof fetch>(async () => new Response("detalle privado", { status: 429 }));
    await expect(handleSofiaChat(request(), env, user, send)).rejects.toMatchObject({ status: 429, code: "CHAT_OCUPADO" });
    expect(send).toHaveBeenCalledTimes(1);
  });
});
