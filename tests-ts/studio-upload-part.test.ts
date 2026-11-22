import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { partSigners, uploadMissingParts, uploadPart } from "../cloudflare-saas/studio-portal-web/app/upload-part.js";

const origin = "https://" + "a".repeat(32) + ".r2.cloudflarestorage.com";
const segment = new Blob(["segmento"]);

function signer() {
  return vi.fn().mockResolvedValueOnce({ url: origin + "/parte?a=1", headers: {} })
    .mockResolvedValueOnce({ url: origin + "/parte?a=2", headers: {} })
    .mockResolvedValueOnce({ url: origin + "/parte?a=3", headers: {} });
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function signed(number: number) {
  return { partNumber: number, url: `${origin}/parte?numero=${number}`, headers: {} };
}

it("reduce de 35 a 12 las solicitudes de firma de 38 partes con las tres primeras firmadas", async () => {
  const signOne = vi.fn(async (number: number) => signed(number));
  const signBatch = vi.fn(async (numbers: number[]) => ({ parts: numbers.map(signed), expiresIn: 60 }));
  const sign = partSigners(38, new Map(), [1, 2, 3].map(signed), signOne, signBatch);
  const parts = new Map<number, string>();
  await uploadMissingParts(38, parts, async (number) => {
    expect(await sign(number)()).toEqual(signed(number));
    return `"parte-${number}"`;
  }, vi.fn());
  expect(parts.size).toBe(38);
  expect(signOne).not.toHaveBeenCalled();
  expect(signBatch).toHaveBeenCalledTimes(12);
  expect(signBatch).toHaveBeenNthCalledWith(1, [4, 5, 6]);
  expect(signBatch).toHaveBeenLastCalledWith([37, 38]);
});

it("evita firmar partes ya recibidas y pide una firma individual después de un rechazo de R2", async () => {
  const existing = new Map([[2, '"guardada"'], [4, '"guardada"']]);
  const signOne = vi.fn(async (number: number) => ({ url: signed(number).url + "&nueva=1", headers: {} }));
  const signBatch = vi.fn(async (numbers: number[]) => ({ parts: numbers.map(signed), expiresIn: 60 }));
  const sign = partSigners(6, existing, [], signOne, signBatch);
  const transfer = vi.fn().mockResolvedValueOnce(new Response(null, { status: 403 }))
    .mockResolvedValueOnce(new Response(null, { headers: { ETag: '"confirmada"' } }));
  vi.stubGlobal("fetch", transfer);

  const done = uploadPart(sign(3), segment);
  await vi.runAllTimersAsync();
  await expect(done).resolves.toBe('"confirmada"');
  expect(signBatch.mock.calls).toEqual([[[1, 3, 5]]]);
  expect(signOne.mock.calls).toEqual([[3]]);
  expect(transfer.mock.calls.map(([url]) => String(url))).toEqual([signed(3).url, signed(3).url + "&nueva=1"]);
});

it("pide una firma nueva antes de enviar una parte que esperó a que su lote venciera", async () => {
  const signOne = vi.fn(async (number: number) => ({ url: signed(number).url + "&nueva=1", headers: {} }));
  const signBatch = vi.fn(async (numbers: number[]) => ({ parts: numbers.map(signed), expiresIn: 60 }));
  const sign = partSigners(3, new Map(), [], signOne, signBatch);
  expect(await sign(1)()).toEqual(signed(1));
  const transfer = vi.fn().mockResolvedValue(new Response(null, { headers: { ETag: '"confirmada"' } }));
  vi.stubGlobal("fetch", transfer);

  await vi.advanceTimersByTimeAsync(60_000);
  await expect(uploadPart(sign(2), segment)).resolves.toBe('"confirmada"');
  expect(signBatch.mock.calls).toEqual([[[1, 2, 3]]]);
  expect(signOne.mock.calls).toEqual([[2]]);
  expect(transfer.mock.calls.map(([url]) => String(url))).toEqual([signed(2).url + "&nueva=1"]);
});

it("rechaza una firma de lote que no corresponde a la parte solicitada", async () => {
  const signOne = vi.fn(async (number: number) => signed(number));
  const sign = partSigners(2, new Map(), [], signOne, async () => ({ parts: [signed(2), signed(1)], expiresIn: 60 }));
  await expect(sign(1)()).rejects.toThrow("CARGA_NO_DISPONIBLE");
  expect(signOne).not.toHaveBeenCalled();
});

it("vuelve a firmar la misma parte tras un rechazo de R2 y un corte de red", async () => {
  const sign = signer();
  const transfer = vi.fn().mockResolvedValueOnce(new Response(null, { status: 403 }))
    .mockRejectedValueOnce(new TypeError("conexión interrumpida"))
    .mockResolvedValueOnce(new Response(null, { headers: { ETag: '"parte-confirmada"' } }));
  vi.stubGlobal("fetch", transfer);
  const observed = vi.fn();
  const done = uploadPart(sign, segment, observed);

  await vi.advanceTimersByTimeAsync(249);
  expect(sign).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(sign).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(1000);
  await expect(done).resolves.toBe('"parte-confirmada"');
  expect(sign).toHaveBeenCalledTimes(3);
  expect(observed.mock.calls).toEqual([["http_403"], ["sin_respuesta"]]);
  for (let attempt = 1; attempt <= 3; attempt++) {
    expect(transfer).toHaveBeenNthCalledWith(attempt, new URL(origin + "/parte?a=" + attempt), {
      method: "PUT", headers: {}, body: segment, credentials: "omit", redirect: "error",
    });
  }
});

it("detiene los reintentos de un fallo persistente después de tres solicitudes", async () => {
  const sign = signer();
  const transfer = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));
  vi.stubGlobal("fetch", transfer);

  const done = expect(uploadPart(sign, segment)).rejects.toThrow("CARGA_NO_DISPONIBLE");
  await vi.runAllTimersAsync();
  await done;
  expect(sign).toHaveBeenCalledTimes(3);
  expect(transfer).toHaveBeenCalledTimes(3);
});

it.each([400, 404, 200])("requiere un PUT válido con ETag antes de confirmar la parte (HTTP %s)", async (status) => {
  const sign = signer();
  const transfer = vi.fn().mockResolvedValue(new Response(null, { status }));
  vi.stubGlobal("fetch", transfer);

  await expect(uploadPart(sign, segment)).rejects.toThrow("CARGA_NO_DISPONIBLE");
  expect(sign).toHaveBeenCalledTimes(1);
});

it("rechaza destinos con puerto alternativo incluso para un nombre R2 válido", async () => {
  const transfer = vi.fn();
  vi.stubGlobal("fetch", transfer);

  await expect(uploadPart(async () => ({ url: origin + ":444/parte", headers: {} }), segment))
    .rejects.toThrow("CARGA_NO_DISPONIBLE");
  expect(transfer).not.toHaveBeenCalled();
});

it("reanuda las partes pendientes con un máximo de tres transferencias y progreso ascendente", async () => {
  const saved = new Map([[2, '"parte-2"'], [5, '"parte-5"']]);
  const pending = new Map<number, (tag: string) => void>();
  const send = vi.fn((number: number) => new Promise<string>((resolve) => pending.set(number, resolve)));
  const report = vi.fn();
  const done = uploadMissingParts(7, saved, send, report);

  expect(send.mock.calls.flat()).toEqual([1, 3, 4]);
  pending.get(4)?.('"parte-4"');
  await vi.advanceTimersByTimeAsync(0);
  expect(send.mock.calls.flat()).toEqual([1, 3, 4, 6]);
  pending.get(1)?.('"parte-1"');
  await vi.advanceTimersByTimeAsync(0);
  expect(send.mock.calls.flat()).toEqual([1, 3, 4, 6, 7]);
  for (const part of [7, 3, 6]) pending.get(part)?.(`"parte-${part}"`);

  await done;
  expect([...saved].sort(([left], [right]) => left - right)).toEqual(
    Array.from({ length: 7 }, (_, index) => [index + 1, `"parte-${index + 1}"`]),
  );
  expect(report.mock.calls.flat()).toEqual([28, 42, 57, 71, 85, 100]);
});

it("detiene la cola al fallar una parte y espera las transferencias activas para poder reanudar", async () => {
  const saved = new Map<number, string>();
  const pending = new Map<number, PromiseWithResolvers<string>>();
  const send = vi.fn((number: number) => {
    const result = Promise.withResolvers<string>();
    pending.set(number, result);
    return result.promise;
  });
  const reason = new Error("fallo de la parte dos");
  const outcome = vi.fn();
  const done = uploadMissingParts(6, saved, send, vi.fn()).catch(outcome);

  pending.get(2)?.reject(reason);
  await vi.advanceTimersByTimeAsync(0);
  expect(outcome).not.toHaveBeenCalled();
  pending.get(1)?.resolve('"parte-1"');
  await vi.advanceTimersByTimeAsync(0);
  expect(outcome).not.toHaveBeenCalled();
  pending.get(3)?.resolve('"parte-3"');

  await done;
  expect(send.mock.calls.flat()).toEqual([1, 2, 3]);
  expect(outcome).toHaveBeenCalledExactlyOnceWith(reason);
  expect([...saved]).toEqual([[1, '"parte-1"'], [3, '"parte-3"']]);
});
