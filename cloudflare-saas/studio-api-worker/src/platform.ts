export interface SqlRows<T> {
  results: T[];
  meta: { changes?: number };
}

export interface SqlStatement {
  bind(...values: (string | number | null)[]): SqlStatement;
  first<T>(): Promise<T | null>;
  all<T>(): Promise<SqlRows<T>>;
  run(): Promise<SqlRows<never>>;
}

export interface SqlSession {
  prepare(sql: string): SqlStatement;
}

export interface SqlDatabase extends SqlSession {
  batch<T>(commands: SqlStatement[]): Promise<SqlRows<T>[]>;
  withSession(constraint: "first-primary"): SqlSession;
}

export interface SqlOwner {
  ownerQuery: string;
  ownerParams: readonly (string | number)[];
}

export const ownerId = "(SELECT user_id FROM request_owner)";

export function prepareOwnedStatement(db: SqlSession, owner: SqlOwner | string, sql: string,
  ...params: (string | number)[]): SqlStatement {
  const scoped = typeof owner === "string" ? { ownerQuery: "SELECT ? AS user_id", ownerParams: [owner] } : owner;
  return db.prepare(`WITH request_owner AS (${scoped.ownerQuery}) ${sql}`).bind(...scoped.ownerParams, ...params);
}

export interface SessionCache {
  get(key: string): Promise<string | null>;
  put(key: string, value: string, options: { expirationTtl: number }): Promise<void>;
  delete(key: string): Promise<void>;
}

export interface ExecutionTasks {
  waitUntil(task: Promise<unknown>): void;
}

export class HttpError extends Error {
  constructor(readonly status: number, readonly code: string) {
    super(code);
  }
}

export function response(data: unknown, status = 200, headers?: HeadersInit): Response {
  const result = new Headers(headers);
  result.set("Cache-Control", "no-store");
  result.set("X-Content-Type-Options", "nosniff");
  return Response.json(data, { status, headers: result });
}

export async function readJson(request: Request, maximum = 16384): Promise<Record<string, unknown>> {
  const type = request.headers.get("content-type")?.split(";", 1)[0];
  if (type !== "application/json" || !request.body) {
    throw new HttpError(415, "JSON_REQUERIDO");
  }
  const stream = request.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let text = "";
  let bytes = 0;
  try {
    while (true) {
      const next = await stream.read();
      if (next.done) break;
      bytes += next.value.byteLength;
      if (bytes > maximum) {
        await stream.cancel();
        throw new HttpError(413, "SOLICITUD_DEMASIADO_GRANDE");
      }
      text += decoder.decode(next.value, { stream: true });
    }
    const value: unknown = JSON.parse(text + decoder.decode());
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new SyntaxError();
    return value as Record<string, unknown>;
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(400, "JSON_INVALIDO");
  } finally {
    stream.releaseLock();
  }
}
