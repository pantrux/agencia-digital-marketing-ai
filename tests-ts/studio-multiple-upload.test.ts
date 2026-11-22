import { beforeEach, describe, expect, it, vi } from "vitest";
import { uploadVideos } from "../cloudflare-saas/studio-portal-web/app/upload.js";

vi.mock("../cloudflare-saas/studio-portal-web/app/upload-duration", () => ({
  videoDuration: vi.fn(async () => 20), validateVideoDuration: vi.fn(async () => undefined),
}));
vi.mock("../cloudflare-saas/studio-portal-web/app/upload-part", async (actual) => ({
  ...await actual<typeof import("../cloudflare-saas/studio-portal-web/app/upload-part.js")>(),
  uploadPart: vi.fn(async (sign: () => Promise<unknown>) => { await sign(); return '"etag"'; }),
  uploadMissingParts: vi.fn(async (count: number, parts: Map<number, string>, send: (n: number) => Promise<string>, report: (p: number) => void) => {
    for (let n = 1; n <= count; n += 1) if (!parts.has(n)) parts.set(n, await send(n));
    report(100);
  }),
}));

const order = "10b4d244-3ae5-4ea8-b56e-ddd35bfbdeed";
const ids = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"];
type Entry = { name: string; bytes: number; sha256: string; extension: string };

describe("Carga de varios vídeos", () => {
  beforeEach(() => {
    const memory = new Map<string, string>();
    vi.stubGlobal("sessionStorage", { getItem: (key: string) => memory.get(key) ?? null,
      setItem: (key: string, value: string) => memory.set(key, value), removeItem: (key: string) => memory.delete(key) });
  });
  it.each(["individual", "anticipada", "lote"] as const)("reserva una orden y completa dos fuentes con firma %s", async (mode) => {
    const paths: string[] = [], progress: number[] = [];
    const grouped = mode === "lote";
    const initial = mode !== "individual";
    const partSize = 16 * 1024 ** 2;
    const partCount = grouped ? 6 : 1;
    const preSigned = (partNumber: number) => ({ partNumber, url: `https://fixture.invalid/parte/${partNumber}`, headers: {} });
    const api = async <T>(path: string, init?: RequestInit): Promise<T> => {
      paths.push(path);
      let result: unknown;
      if (path === "/orders") result = { id: order, project_slug: "job-fixture" };
      else if (path.endsWith("/sources")) {
        const body = JSON.parse(String(init?.body)) as { sources: Entry[] };
        result = { assets: body.sources.map((entry, position) => ({ ...entry, id: ids[position], position })) };
      } else if (path.endsWith("/upload")) {
        expect(new Headers(init?.headers).get("X-Studio-Prefirmar-Partes")).toBe("1");
        result = { id: path.split("/").at(-2), status: "open", newlyOpened: true, partSize, partCount,
          ...(initial ? { initialParts: (grouped ? [1, 2, 3] : [1]).map(preSigned) } : {}),
          ...(grouped ? { partSignBatchSize: 3 } : {}) };
      } else if (path.endsWith("/parts/sign")) {
        const { partNumbers } = JSON.parse(String(init?.body)) as { partNumbers: number[] };
        expect(partNumbers).toEqual([4, 5, 6]);
        result = { parts: partNumbers.map(preSigned), expiresIn: 60 };
      } else if (path.endsWith("/sign")) result = preSigned(1);
      else if (path.endsWith("/complete")) result = { status: "completed" };
      else throw new Error(path);
      return result as T;
    };
    const created = vi.fn();
    const files = [new File(["uno"], "uno.mp4"), new File(["dos"], "dos.mov")];
    // El PUT está simulado. La longitud declarada permite ejercitar seis partes sin asignar dos vídeos grandes.
    if (grouped) for (const file of files) Object.defineProperty(file, "size", { value: partSize * 5 + 7 });
    const result = await uploadVideos(files, api,
      (value) => progress.push(value), "1080p", "mp4", undefined, "reel", created);
    expect(result.id).toBe(order);
    expect(created).toHaveBeenCalledOnce();
    expect(paths.filter((path) => path === "/orders")).toHaveLength(1);
    expect(paths.filter((path) => path.endsWith("/complete"))).toHaveLength(2);
    expect(paths.filter((path) => path.endsWith("/parts/sign"))).toHaveLength(grouped ? 2 : 0);
    expect(paths.filter((path) => path.endsWith("/1/sign"))).toHaveLength(initial ? 0 : 2);
    expect(paths.some((path) => path.includes(ids[0]!))).toBe(true);
    expect(paths.some((path) => path.includes(ids[1]!))).toBe(true);
    expect(progress.at(-1)).toBe(100);
  });
});
