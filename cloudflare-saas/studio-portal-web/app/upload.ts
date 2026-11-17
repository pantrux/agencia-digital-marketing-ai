import Uppy from "@uppy/core";
import { createSHA256 } from "hash-wasm";
import { validateVideoDuration } from "./upload-duration";
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
  brandId?: string, edit: OutputEdit = "full",
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
