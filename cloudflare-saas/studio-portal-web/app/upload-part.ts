export type SignedPart = { url: string; headers: Record<string, string> };
export type NumberedSignedPart = SignedPart & { partNumber: number };
export type SignedPartBatch = { parts: NumberedSignedPart[]; expiresIn: number };
type PutObservation = "http_403" | "sin_respuesta";

const retryDelayMs = [250, 1000];
const concurrentParts = 3;

export function partSigners(partCount: number, stored: ReadonlyMap<number, string>, preSigned: readonly NumberedSignedPart[],
  signOne: (number: number) => Promise<SignedPart>, signBatch?: (numbers: number[]) => Promise<SignedPartBatch>,
): (number: number) => () => Promise<SignedPart> {
  const initial = new Map(preSigned.map((part) => [part.partNumber, part]));
  const groups = new Map<number, number[]>();
  const pending = new Map<number[], Promise<{ parts: Map<number, SignedPart>; expiresAt: number }>>();
  if (signBatch) {
    const missing = Array.from({ length: partCount }, (_, index) => index + 1)
      .filter((number) => !stored.has(number) && !initial.has(number));
    for (let offset = 0; offset < missing.length; offset += concurrentParts) {
      const group = missing.slice(offset, offset + concurrentParts);
      if (group.length > 1) for (const number of group) groups.set(number, group);
    }
  }

  return (number) => {
    let first = true;
    return async () => {
      if (!first) return signOne(number);
      first = false;
      const part = initial.get(number);
      if (part) return part;
      const group = groups.get(number);
      if (!signBatch || !group) return signOne(number);
      let request = pending.get(group);
      if (!request) {
        const requestedAt = performance.now();
        request = signBatch(group).then(({ parts, expiresIn }) => {
          if (!Array.isArray(parts) || parts.length !== group.length
            || parts.some((item, index) => item?.partNumber !== group[index])
            || !Number.isSafeInteger(expiresIn) || expiresIn < 1 || expiresIn > 60) throw new Error("CARGA_NO_DISPONIBLE");
          // El tiempo de la solicitud ya consume parte de la vigencia.
          return { parts: new Map(parts.map((item) => [item.partNumber, item])), expiresAt: requestedAt + expiresIn * 1000 - 2000 };
        });
        pending.set(group, request);
      }
      const received = await request;
      return performance.now() < received.expiresAt ? received.parts.get(number)! : signOne(number);
    };
  };
}

function retryable(status: number): boolean {
  return status === 403 || status === 408 || status === 429 || (status >= 500 && status <= 599);
}

export async function uploadPart(
  sign: () => Promise<SignedPart>, segment: Blob, observe?: (observation: PutObservation) => void,
): Promise<string> {
  for (let attempt = 0; ; attempt++) {
    const { url, headers } = await sign();
    const target = new URL(url);
    if (target.protocol !== "https:" || target.port || target.username || target.password
      || !/^[a-f0-9]{32}\.r2\.cloudflarestorage\.com$/.test(target.hostname)) {
      throw new Error("CARGA_NO_DISPONIBLE");
    }
    const response = await fetch(target, {
      method: "PUT", headers, body: segment, credentials: "omit", redirect: "error",
    }).catch((error: unknown) => {
      if (error instanceof TypeError) return null;
      throw error;
    });
    if (response?.ok) {
      const tag = response.headers.get("ETag");
      if (!tag) throw new Error("CARGA_NO_DISPONIBLE");
      return tag;
    }
    if (!response) observe?.("sin_respuesta");
    else if (response.status === 403) observe?.("http_403");
    const delay = retryDelayMs[attempt];
    if (delay === undefined || (response && !retryable(response.status))) {
      throw new Error("CARGA_NO_DISPONIBLE");
    }
    // La parte conserva su número y bytes, aunque la URL anterior haya vencido.
    await new Promise<void>((resolve) => setTimeout(resolve, delay));
  }
}

export async function uploadMissingParts(
  partCount: number, parts: Map<number, string>, send: (number: number) => Promise<string>,
  report: (progress: number) => void,
): Promise<void> {
  const missing = Array.from({ length: partCount }, (_, index) => index + 1).filter((number) => !parts.has(number));
  const queue = missing.values();
  const state: { failure?: { cause: unknown } } = {};
  let completed = partCount - missing.length;
  report(Math.floor(completed * 100 / partCount));

  async function transfer() {
    while (!state.failure) {
      const next = queue.next();
      if (next.done) return;
      try {
        parts.set(next.value, await send(next.value));
        completed++;
        report(Math.floor(completed * 100 / partCount));
      } catch (cause) {
        state.failure ??= { cause };
      }
    }
  }

  // Antes de reanudar, deja que R2 termine las partes que ya recibió.
  await Promise.all(Array.from({ length: Math.min(concurrentParts, missing.length) }, transfer));
  if (state.failure) throw state.failure.cause;
}
