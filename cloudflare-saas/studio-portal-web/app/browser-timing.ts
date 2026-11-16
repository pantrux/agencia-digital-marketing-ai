const routes: Readonly<Record<string, string>> = {
  "/api/auth/me": "auth_me",
  "/api/dashboard": "panel",
  "/api/uploads": "multipart_inicio",
};
const partPath = /^\/api\/uploads\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\/parts\/[1-9][0-9]*\/sign$/u;
const batchSize = 12;
type Sample = [route: string, status: number, milliseconds: number, reused: boolean];

export function browserSample(entry: PerformanceResourceTiming, origin: string): Sample | null {
  const { requestStart, responseStart, responseStatus, transferSize, initiatorType } = entry;
  if (initiatorType !== "fetch" || !Number.isInteger(responseStatus) || responseStatus < 200 || responseStatus > 299
    || transferSize <= 0 || requestStart <= 0 || responseStart < requestStart) return null;
  const url = new URL(entry.name);
  if (url.origin !== origin) return null;
  const route = routes[url.pathname] ?? (partPath.test(url.pathname) ? "multipart_firma" : undefined);
  if (!route) return null;
  const milliseconds = Math.min(120_000, Math.max(1, Math.round(responseStart - requestStart)));
  return [route, responseStatus, milliseconds, entry.connectEnd === entry.connectStart];
}

export function observeBrowserTimings(): (() => void) | undefined {
  if (typeof PerformanceObserver === "undefined" || !PerformanceObserver.supportedEntryTypes?.includes("resource")) return;
  const samples: Sample[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  function flush() {
    clearTimeout(timer);
    timer = undefined;
    if (!samples.length) return;
    void fetch("/api/metrics/ttfb", {
      method: "POST", credentials: "same-origin", keepalive: true,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ samples: samples.splice(0) }),
    }).catch(() => undefined);
  }
  function collect(entries: PerformanceEntry[]) {
    for (const entry of entries) {
      const sample = browserSample(entry as PerformanceResourceTiming, location.origin);
      if (!sample) continue;
      samples.push(sample);
      if (samples.length === batchSize) flush();
    }
    if (samples.length && timer === undefined) timer = setTimeout(flush, 3000);
  }
  const observer = new PerformanceObserver((list) => collect(list.getEntries()));
  observer.observe({ type: "resource" });
  function pageHide() {
    collect(observer.takeRecords());
    flush();
  }
  window.addEventListener("pagehide", pageHide);
  return () => {
    observer.disconnect();
    clearTimeout(timer);
    window.removeEventListener("pagehide", pageHide);
  };
}
