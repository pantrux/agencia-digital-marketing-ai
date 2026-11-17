/** Aislado: no conectar secretos, VPC, bases de datos ni rutas públicas a este Worker. */
const MAX_BYTES = 262_144;
const MAX_REDIRECTS = 3;
const DEADLINE_MS = 8_000;
const sensitive = /^(?:access[_-]?token|token|api[_-]?key|secret|password|passwd|code|auth|authorization|session|signature|sig)$/iu;

export function publicUrl(input: string): URL | null {
  if (input.length > 2048) return null;
  let url: URL;
  try { url = new URL(input); } catch { return null; }
  const host = url.hostname.toLowerCase();
  if (url.protocol !== 'https:' || url.username || url.password || url.port ||
      !host.includes('.') || host.endsWith('.') || host.startsWith('[') ||
      /^[0-9.]+$/u.test(host) || /(?:^|\.)(?:localhost|local|internal|invalid|test|example)$/u.test(host) ||
      !/^[a-z0-9.-]+$/u.test(host) || [...url.searchParams.keys()].some(key => sensitive.test(key))) return null;
  url.hash = '';
  return url;
}

function clean(value: string, limit = 500): string {
  return value.replace(/<[^>]*>/gu, ' ').replace(/&(?:amp|#38);/giu, '&')
    .replace(/&(?:quot|#34);/giu, '"').replace(/&(?:apos|#39);/giu, "'")
    .replace(/&(?:lt|#60);/giu, '<').replace(/&(?:gt|#62);/giu, '>')
    .replace(/[\u0000-\u001f\u007f]+/gu, ' ').replace(/\s+/gu, ' ').trim().slice(0, limit);
}

/** Fragmento público de texto; no ejecuta scripts ni extrae contenido oculto por scripts. */
export function extractPageText(html: string): string {
  const body = /<body\b[^>]*>([\s\S]*?)<\/body\s*>/iu.exec(html)?.[1] ?? '';
  return clean(body.replace(/<!--[\s\S]*?-->/gu, ' ')
    .replace(/<(script|style|noscript|template|svg|form|nav|footer)\b[^>]*>[\s\S]*?<\/\1\s*>/giu, ' '), 1000);
}

export function extractMetadata(html: string): { title: string; description: string } {
  const title = clean(/<title\b[^>]*>([\s\S]*?)<\/title\s*>/iu.exec(html)?.[1] ?? '');
  let description = '';
  for (const match of html.matchAll(/<meta\b[^>]{0,4096}>/giu)) {
    const attributes = new Map<string, string>();
    for (const attribute of match[0].matchAll(/([a-z_:][-a-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/giu)) {
      attributes.set(attribute[1]!.toLowerCase(), attribute[2] ?? attribute[3] ?? attribute[4] ?? '');
    }
    if (['description', 'og:description'].includes((attributes.get('name') ?? attributes.get('property') ?? '').toLowerCase())) {
      description = clean(attributes.get('content') ?? '');
      if (description) break;
    }
  }
  return {title, description};
}

async function boundedText(body: ReadableStream<Uint8Array>): Promise<string> {
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > MAX_BYTES) { await reader.cancel(); throw new Error('demasiado grande'); }
      chunks.push(part.value);
    }
  } finally { reader.releaseLock(); }
  const buffer = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) { buffer.set(chunk, offset); offset += chunk.length; }
  return new TextDecoder().decode(buffer);
}

export async function extractReference(input: string, send: typeof fetch = fetch): Promise<{ status: 'available' | 'unavailable'; title?: string; description?: string; excerpt?: string }> {
  let current = publicUrl(input);
  if (!current) return {status:'unavailable'};
  const signal = AbortSignal.timeout(DEADLINE_MS);
  try {
    for (let redirects = 0; redirects <= MAX_REDIRECTS; redirects += 1) {
      const reply = await send(current.toString(), {method:'GET', redirect:'manual', signal,
        headers:{accept:'text/html', 'user-agent':'EscaleStudioReference/1.0'}});
      if ([301,302,303,307,308].includes(reply.status)) {
        const location = reply.headers.get('location');
        await reply.body?.cancel();
        if (!location || redirects === MAX_REDIRECTS) return {status:'unavailable'};
        current = publicUrl(new URL(location, current).toString());
        if (!current) return {status:'unavailable'};
        continue;
      }
      if (!reply.ok || !reply.body || !/^text\/html(?:\s*;|$)/iu.test(reply.headers.get('content-type') ?? '')) {
        await reply.body?.cancel();
        return {status:'unavailable'};
      }
      const html = await boundedText(reply.body);
      const metadata = extractMetadata(html), excerpt = extractPageText(html);
      return metadata.title || metadata.description || excerpt ? {status:'available', ...metadata, excerpt} : {status:'unavailable'};
    }
  } catch { return {status:'unavailable'}; }
  return {status:'unavailable'};
}

export default {
  async fetch(request: Request): Promise<Response> {
    if (request.method !== 'POST' || new URL(request.url).pathname !== '/extract') return new Response(null, {status:404});
    if (Number(request.headers.get('content-length') ?? 0) > 4096) return new Response(null, {status:413});
    try {
      const raw = await request.text();
      if (new TextEncoder().encode(raw).length > 4096) return new Response(null, {status:413});
      const body: unknown = JSON.parse(raw);
      if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).length !== 1 ||
          typeof (body as {url?: unknown}).url !== 'string') return new Response(null, {status:400});
      return Response.json(await extractReference((body as {url: string}).url), {headers:{'cache-control':'no-store'}});
    } catch { return new Response(null, {status:400}); }
  },
};
