import { beforeEach, describe, expect, it, vi } from "vitest";
import { uploadVideos } from "../cloudflare-saas/studio-portal-web/app/upload.js";

vi.mock("../cloudflare-saas/studio-portal-web/app/upload-duration", () => ({
  videoDuration: vi.fn(async () => 20), validateVideoDuration: vi.fn(async () => undefined),
}));
vi.mock("../cloudflare-saas/studio-portal-web/app/upload-part", () => ({
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
  it("reserva una orden y completa dos fuentes conservando sus identidades", async () => {
    const paths: string[] = [], progress: number[] = [];
    const api = async <T>(path: string, init?: RequestInit): Promise<T> => {
      paths.push(path);
      let result: unknown;
      if (path === "/orders") result = { id: order, project_slug: "job-fixture" };
      else if (path.endsWith("/sources")) {
        const body = JSON.parse(String(init?.body)) as { sources: Entry[] };
        result = { assets: body.sources.map((entry, position) => ({ ...entry, id: ids[position], position })) };
      } else if (path.endsWith("/upload")) result = { id: path.split("/").at(-2), status: "open", newlyOpened: true, partSize: 16 * 1024 ** 2, partCount: 1 };
      else if (path.endsWith("/sign")) result = { url: "https://fixture.invalid", headers: {} };
      else if (path.endsWith("/complete")) result = { status: "completed" };
      else throw new Error(path);
      return result as T;
    };
    const created = vi.fn();
    const result = await uploadVideos([new File(["uno"], "uno.mp4"), new File(["dos"], "dos.mov")], api,
      (value) => progress.push(value), "1080p", "mp4", undefined, "reel", created);
    expect(result.id).toBe(order);
    expect(created).toHaveBeenCalledOnce();
    expect(paths.filter((path) => path === "/orders")).toHaveLength(1);
    expect(paths.filter((path) => path.endsWith("/complete"))).toHaveLength(2);
    expect(paths.some((path) => path.includes(ids[0]!))).toBe(true);
    expect(paths.some((path) => path.includes(ids[1]!))).toBe(true);
    expect(progress.at(-1)).toBe(100);
  });
});
