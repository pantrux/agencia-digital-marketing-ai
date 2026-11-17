import Uppy from "@uppy/core";
import { createSHA256 } from "hash-wasm";
import { validateVideoDuration, videoDuration } from "./upload-duration";
import { uploadMissingParts, uploadPart, type SignedPart } from "./upload-part";

type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type Order = { id: string; project_slug: string };
export type OutputResolution = "1080p" | "4k";
export type OutputFormat = "mp4" | "prores4444";
export type OutputEdit = "full" | "reel";
type Session = { id: string; uploadId: string; key: string; partSize: number; partCount: number; status: string; newlyOpened?: true };
type Part = { PartNumber: number; ETag: string };
const files = { mp4: "video/mp4", mov: "video/quicktime", webm: "video/webm" };
const maximum = 1024 ** 3;

function post(data?: unknown): RequestInit {
  return { method: "POST", headers: data ? { "Content-Type": "application/json" } : {}, body: data ? JSON.stringify(data) : null };
}

async function sha256(file: File): Promise<string> {
  const hasher = await createSHA256();
  const stream = file.stream().getReader();
  try {
    while (true) {
      const next = await stream.read();
      if (next.done) return hasher.digest("hex");
      hasher.update(next.value);
    }
  } finally {
    stream.releaseLock();
  }
}

export async function uploadVideo(
  file: File, api: Api, report: (value: number) => void, resolution: OutputResolution, format: OutputFormat,
  brandId?: string, edit: OutputEdit = "full", onCreated?: (order: Order) => void,
): Promise<Order> {
  const extension = file.name.split(".").pop()?.toLowerCase();
  if (extension !== "mp4" && extension !== "mov" && extension !== "webm") throw new Error("ARCHIVO_INVALIDO");
  if (!file.size || file.size > maximum) throw new Error("ARCHIVO_INVALIDO");
  await validateVideoDuration(file, edit === "reel" ? 30 : 0);
  const digest = await sha256(file);
  const retryKey = "studio:upload:" + digest + (brandId ? ":" + brandId : "") + (edit === "reel" ? ":reel" : "");
  const known = sessionStorage.getItem(retryKey);
  const key = known ?? crypto.randomUUID();
  if (!known) sessionStorage.setItem(retryKey, key);
  const body = post({
    kind: "production",
    ...(resolution === "4k" ? { output_resolution: resolution } : {}),
    ...(format === "prores4444" ? { output_format: format } : {}),
    ...(edit === "reel" ? { output_edit: edit } : {}),
    ...(brandId ? { brand_id: brandId } : {}),
  });
  const order = await api<Order>("/orders", { ...body, headers: { ...body.headers, "Idempotency-Key": key } })
    .catch((error: unknown) => {
      if (error instanceof Error && error.message === "ORDEN_CANCELADA"
        && sessionStorage.getItem(retryKey) === key) sessionStorage.removeItem(retryKey);
      throw error;
    });
  onCreated?.(order);
  const uppy = new Uppy({ restrictions: { maxNumberOfFiles: 1, maxFileSize: maximum } });
  const id = uppy.addFile({ name: file.name, type: files[extension], data: file, source: "Estudio" });
  const uploadOne = async (selected: string[]) => {
    if (selected.length !== 1 || selected[0] !== id) throw new Error("ARCHIVO_INVALIDO");
    const transfer = uppy.getFile(id);
    const media = transfer?.data;
    if (!(media instanceof Blob)) throw new Error("ARCHIVO_INVALIDO");
    const upload = await api<Session>("/uploads", post({
      order_id: order.id, project_slug: order.project_slug, bytes: file.size, sha256: digest,
      content_type: files[extension], extension,
    }));
    if (upload.status !== "completed") {
      const path = "/uploads/" + encodeURIComponent(upload.id);
      if (upload.partSize !== 16 * 1024 ** 2 || upload.partCount !== Math.ceil(file.size / upload.partSize)) {
        throw new Error("CARGA_NO_DISPONIBLE");
      }
      const saved = upload.newlyOpened === true ? [] : (await api<{ parts: Part[] }>(path + "/parts")).parts;
      const parts = new Map(saved.map((part) => [part.PartNumber, part.ETag]));
      const observations = { http_403: false, sin_respuesta: false };
      try {
        await uploadMissingParts(upload.partCount, parts, (number) => {
          const signPath = path + "/parts/" + number + "/sign";
          const offset = (number - 1) * upload.partSize;
          return uploadPart(() => api<SignedPart>(signPath, post()), media.slice(offset, offset + upload.partSize),
            (seen) => { observations[seen] = true; });
        }, report);
      } finally {
        // La observación puede llegar después de la confirmación de la carga.
        if (observations.http_403 || observations.sin_respuesta) {
          void api(path + "/part-observations", { ...post(observations), keepalive: true }).catch(() => {});
        }
      }
      await api(path + "/complete", post({ parts: [...parts].sort(([a], [b]) => a - b).map(([PartNumber, ETag]) => ({ PartNumber, ETag })) }));
    }
    report(100);
    uppy.setFileState(id, { progress: { uploadStarted: Date.now(), uploadComplete: true, bytesUploaded: file.size, bytesTotal: file.size, percentage: 100 } });
  };
  uppy.addUploader(uploadOne);
  try {
    const result = await uppy.upload();
    if (result?.failed?.length) throw new Error("CARGA_NO_DISPONIBLE");
    sessionStorage.removeItem(retryKey);
    return order;
  } finally {
    uppy.destroy();
  }
}

type GroupSource = { id: string; position: number; name: string; bytes: number; sha256: string; extension: keyof typeof files };

async function mapTwo<T>(length: number, work: (index: number) => Promise<T>): Promise<T[]> {
  const result: T[] = new Array(length);
  let cursor = 0;
  let failure: { cause: unknown } | undefined;
  async function next() {
    while (cursor < length && !failure) {
      const index = cursor++;
      try { result[index] = await work(index); }
      catch (cause) { failure ??= { cause }; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(2, length) }, () => next()));
  if (failure) throw failure.cause;
  return result;
}

export async function uploadVideos(
  selected: File[], api: Api, report: (value: number) => void, resolution: OutputResolution, format: OutputFormat,
  brandId?: string, edit: OutputEdit = "reel", onCreated?: (order: Order) => void,
): Promise<Order> {
  if (selected.length < 2 || selected.length > 32) throw new Error("ARCHIVOS_INVALIDOS");
  const items = selected.map((file) => {
    const extension = file.name.split(".").pop()?.toLowerCase();
    if ((extension !== "mp4" && extension !== "mov" && extension !== "webm") || !file.size || file.size > maximum) {
      throw new Error("ARCHIVO_INVALIDO");
    }
    return { file, extension };
  });
  const durations = await mapTwo(items.length, (index) => videoDuration(items[index]!.file));
  if (edit === "reel" && durations.reduce((sum, value) => sum + value, 0) < 30) throw new Error("VIDEO_CORTO_PARA_REEL");
  const digests = await mapTwo(items.length, (index) => sha256(items[index]!.file));
  const manifest = items.map(({ file, extension }, index) => ({ name: file.name, bytes: file.size, sha256: digests[index]!, extension }));
  const signature = new TextEncoder().encode(JSON.stringify({ manifest, brandId, resolution, format, edit }));
  const groupHash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", signature)), (byte) => byte.toString(16).padStart(2, "0")).join("");
  const retryKey = "studio:grupo:" + groupHash;
  const key = sessionStorage.getItem(retryKey) ?? crypto.randomUUID();
  sessionStorage.setItem(retryKey, key);
  const options = post({ kind: "production", ...(edit === "reel" ? {output_edit: "reel"} : {}), ...(resolution === "4k" ? { output_resolution: resolution } : {}),
    ...(format === "prores4444" ? { output_format: format } : {}), ...(brandId ? { brand_id: brandId } : {}) });
  const order = await api<Order>("/orders", { ...options, headers: { ...options.headers, "Idempotency-Key": key } }).catch((error: unknown) => {
    if (error instanceof Error && error.message === "ORDEN_CANCELADA" && sessionStorage.getItem(retryKey) === key) sessionStorage.removeItem(retryKey);
    throw error;
  });
  onCreated?.(order);
  const root = `/orders/${encodeURIComponent(order.id)}/sources`;
  const response = await api<{ assets: GroupSource[] }>(root, post({ sources: manifest }));
  if (!Array.isArray(response.assets) || response.assets.length !== items.length || new Set(response.assets.map((source) => source.id)).size !== items.length || response.assets.some((source, index) =>
    source.position !== index || source.name !== manifest[index]!.name || source.bytes !== manifest[index]!.bytes || source.sha256 !== manifest[index]!.sha256 ||
    source.extension !== manifest[index]!.extension || !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/u.test(source.id))) {
    throw new Error("CARGA_NO_DISPONIBLE");
  }
  const progress = items.map(() => 0);
  const totalBytes = items.reduce((sum, item) => sum + item.file.size, 0);
  await mapTwo(items.length, async (index) => {
    const { file } = items[index]!;
    const path = `${root}/${encodeURIComponent(response.assets[index]!.id)}`;
    const upload = await api<Session>(`${path}/upload`, post());
    if (upload.status !== "completed") {
      if (upload.status !== "open" || upload.partSize !== 16 * 1024 ** 2 || upload.partCount !== Math.ceil(file.size / upload.partSize)) {
        throw new Error("CARGA_NO_DISPONIBLE");
      }
      const saved = upload.newlyOpened === true ? [] : (await api<{ parts: Part[] }>(`${path}/parts`)).parts;
      const parts = new Map(saved.map((part) => [part.PartNumber, part.ETag]));
      await uploadMissingParts(upload.partCount, parts, (number) => {
        const offset = (number - 1) * upload.partSize;
        return uploadPart(() => api<SignedPart>(`${path}/parts/${number}/sign`, post()), file.slice(offset, offset + upload.partSize));
      }, (percentage) => {
        progress[index] = percentage;
        report(Math.floor(progress.reduce((sum, value, i) => sum + value * items[i]!.file.size, 0) / totalBytes));
      });
      await api(`${path}/complete`, post({ parts: [...parts].sort(([a], [b]) => a - b).map(([PartNumber, ETag]) => ({ PartNumber, ETag })) }));
    }
    progress[index] = 100;
    report(Math.floor(progress.reduce((sum, value, i) => sum + value * items[i]!.file.size, 0) / totalBytes));
  });
  sessionStorage.removeItem(retryKey);
  return order;
}
