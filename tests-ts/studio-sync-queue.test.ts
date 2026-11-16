import { expect, it, vi } from "vitest";
import { syncQueue } from "../cloudflare-saas/studio-api-worker/src/jobs.js";
import type { SqlStatement } from "../cloudflare-saas/studio-api-worker/src/platform.js";

const jobId = "22222222-2222-4222-8222-222222222222";
const assetId = "11111111-1111-4111-8111-111111111111";
const job = {
  job_id: jobId, asset_id: assetId, project_slug: "proyecto_01", extension: "mp4", bytes: 3,
  sha256: "a".repeat(64), object_key: `ingest/${jobId}/${assetId}.mp4`,
};

function fixture() {
  const statement: SqlStatement = {
    bind: () => statement,
    first: async <T>() => ({ status: "queued", usage_status: "reserved", payload: JSON.stringify(job) }) as T,
    all: async () => ({ results: [], meta: {} }),
    run: async () => ({ results: [], meta: {} }),
  };
  const batch = vi.fn(async () => []);
  return {
    batch,
    env: {
      HOMELAB_SYNC_ORIGIN: "https://api-studio.e-scale.cl",
      STUDIO_EDGE_TO_HOMELAB_HMAC_SECRET: "f".repeat(32),
      DB: { prepare: () => statement, withSession: () => ({ prepare: () => statement }), batch },
    },
    message: { body: job, attempts: 1, ack: vi.fn(), retry: vi.fn() },
  };
}

it("espera una copia lenta dentro del mismo intento y consume el cupo tras recibir el recibo", async () => {
  const { batch, env, message } = fixture();
  const send = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(Response.json({ status: "processing" }, { status: 202 }))
    .mockResolvedValueOnce(Response.json({ error: "BUSY" }, { status: 503 }))
    .mockResolvedValueOnce(Response.json({ ...job, status: "completed" }));
  const setTimer = globalThis.setTimeout;
  const timers = vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => setTimer(callback, 0));
  try {
    await syncQueue([message], env, send);
  } finally {
    timers.mockRestore();
  }
  expect(send).toHaveBeenCalledTimes(3);
  expect(batch).toHaveBeenCalledTimes(1);
  expect(message.ack).toHaveBeenCalledOnce();
  expect(message.retry).not.toHaveBeenCalled();
});

it("conserva la reserva si un origen devuelve un recibo sin confirmar", async () => {
  const { batch, env, message } = fixture();
  const send = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ...job, status: "processing" }));
  await syncQueue([message], env, send);
  expect(batch).not.toHaveBeenCalled();
  expect(message.ack).not.toHaveBeenCalled();
  expect(message.retry).toHaveBeenCalledOnce();
});

it("reintenta si el consumidor sigue ocupado al vencer el plazo de sondeo", async () => {
  const { batch, env, message } = fixture();
  const clock = vi.spyOn(Date, "now");
  const send = vi.fn<typeof fetch>(async () => {
    clock.mockReturnValue(Date.now() + 331_000);
    return Response.json({ status: "processing" }, { status: 202 });
  });
  try {
    await syncQueue([message], env, send);
  } finally {
    clock.mockRestore();
  }
  expect(send).toHaveBeenCalledOnce();
  expect(message.retry).toHaveBeenCalledOnce();
  expect(message.ack).not.toHaveBeenCalled();
  expect(batch).not.toHaveBeenCalled();
});
