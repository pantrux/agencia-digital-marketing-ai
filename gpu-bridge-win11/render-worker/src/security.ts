import {createHash, timingSafeEqual} from 'node:crypto';
import {WorkerError} from './contracts.ts';

const allowed = new Set(['192.168.0.85', '192.168.0.7', '192.168.0.8', '127.0.0.1', '::1']);
export function authorize(ip: string | undefined, header: string | string[] | undefined, expected: string): void {
  const normalized = ip?.startsWith('::ffff:') ? ip.slice(7) : ip;
  if (!normalized || !allowed.has(normalized)) throw new WorkerError(403, 'CLIENTE_NO_PERMITIDO', 'El cliente no está permitido.');
  if (expected.length < 32 || /[\r\n]/.test(expected) || /CHANGE_ME|PENDIENTE|COMPLETAR/.test(expected)) throw new WorkerError(503, 'CONFIGURACION_INCOMPLETA', 'El servicio no está configurado.');
  const validHeader = typeof header === 'string' && header.startsWith('Bearer ') && header.length <= 4096;
  const supplied = validHeader ? header.slice(7) : '';
  // Ambos digest tienen longitud fija; no se publica ni se devuelve el secreto.
  const match = timingSafeEqual(createHash('sha256').update(supplied).digest(), createHash('sha256').update(expected).digest());
  if (!validHeader || !match) throw new WorkerError(401, 'NO_AUTORIZADO', 'Se requiere autenticación válida.');
}

export class SlotPool {
  readonly maximum: number;
  #active = 0;
  constructor(maximum = 2) {
    if (!Number.isInteger(maximum) || maximum < 1 || maximum > 2) throw new Error('El máximo propio debe ser uno o dos.');
    this.maximum = maximum;
  }
  get active(): number {return this.#active;}
  acquire(): () => void {
    if (this.#active >= this.maximum) throw new WorkerError(429, 'RENDER_OCUPADO', 'Los dos puestos de procesamiento están ocupados.');
    this.#active += 1;
    let released = false;
    return () => {if (!released) {released = true; this.#active -= 1;}};
  }
}
