const maxVideoSeconds = 600;
const metadataTimeoutMs = 10_000;

export async function videoDuration(file: Blob, minimum = 0): Promise<number> {
  const video = document.createElement("video");
  video.preload = "metadata";
  const url = URL.createObjectURL(file);
  const events = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const duration = await new Promise<number>((resolve, reject) => {
      const unreadable = () => reject(new Error("VIDEO_NO_VERIFICABLE"));
      video.addEventListener("loadedmetadata", () => resolve(video.duration), { signal: events.signal });
      video.addEventListener("error", unreadable, { signal: events.signal });
      timer = setTimeout(unreadable, metadataTimeoutMs);
      video.src = url;
    });
    if (!Number.isFinite(duration) || duration <= 0 || video.videoWidth === 0 || video.videoHeight === 0) {
      throw new Error("VIDEO_NO_VERIFICABLE");
    }
    if (duration > maxVideoSeconds) throw new Error("VIDEO_SUPERA_DURACION");
    if (duration < minimum) throw new Error("VIDEO_CORTO_PARA_REEL");
    return duration;
  } finally {
    clearTimeout(timer);
    events.abort();
    video.removeAttribute("src");
    video.load();
    URL.revokeObjectURL(url);
  }
}

export async function validateVideoDuration(file: Blob, minimum = 0): Promise<void> {
  await videoDuration(file, minimum);
}
