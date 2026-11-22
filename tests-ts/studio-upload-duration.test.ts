import { afterEach, expect, it, vi } from "vitest";
import { validateVideoDuration } from "../cloudflare-saas/studio-portal-web/app/upload-duration.js";

function prepare(result: number | "error" | "timeout", width = 360) {
  const video = new EventTarget() as HTMLVideoElement;
  video.removeAttribute = vi.fn();
  video.load = vi.fn();
  const revoke = vi.fn();
  Object.defineProperty(video, "videoWidth", { value: width });
  Object.defineProperty(video, "videoHeight", { value: 640 });
  Object.defineProperty(video, "src", { set() {
    if (result === "timeout") return;
    Object.defineProperty(video, "duration", { value: result });
    video.dispatchEvent(new Event(result === "error" ? "error" : "loadedmetadata"));
  } });
  vi.stubGlobal("document", { createElement: vi.fn(() => video) });
  vi.stubGlobal("URL", { createObjectURL: () => "blob:prueba", revokeObjectURL: revoke });
  return revoke;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("rechaza un archivo con audio sin pista de vídeo", async () => {
  prepare(35, 0);
  await expect(validateVideoDuration(new Blob())).rejects.toThrow("VIDEO_NO_VERIFICABLE");
});

it("admite un vídeo de exactamente diez minutos y libera la URL local", async () => {
  const revoke = prepare(600);
  await expect(validateVideoDuration(new Blob())).resolves.toBeUndefined();
  expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:prueba");
});

it("valida los metadatos de un clip de 22 segundos", async () => {
  const revoke = prepare(22);
  await expect(validateVideoDuration(new Blob())).resolves.toBeUndefined();
  expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:prueba");
});

it.each([601, Infinity, NaN, 0, "error"] as const)("rechaza metadatos fuera del contrato (%s)", async (result) => {
  const revoke = prepare(result);
  await expect(validateVideoDuration(new Blob())).rejects.toThrow(
    result === 601 ? "VIDEO_SUPERA_DURACION" : "VIDEO_NO_VERIFICABLE",
  );
  expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:prueba");
});

it("limita la espera si el navegador no devuelve metadatos", async () => {
  vi.useFakeTimers();
  const revoke = prepare("timeout");
  const error = expect(validateVideoDuration(new Blob())).rejects.toThrow("VIDEO_NO_VERIFICABLE");
  await vi.runAllTimersAsync();
  await error;
  expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:prueba");
});
