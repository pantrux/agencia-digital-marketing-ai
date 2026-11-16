import {lstat, realpath, mkdir, mkdtemp, readFile, writeFile, rm, statfs} from 'node:fs/promises';
import path from 'node:path';
import {WorkerError} from './contracts.ts';

const marker = 'temporal exclusivo del renderizador v1\n';
export class ScratchSpace {
  readonly root: string;
  private constructor(root: string) {this.root = root;}
  static async create(cacheRoot: string): Promise<ScratchSpace> {
    if (!path.isAbsolute(cacheRoot) || !(await lstat(cacheRoot)).isDirectory() || (await lstat(cacheRoot)).isSymbolicLink()) throw new WorkerError(503, 'CACHE_INVALIDA', 'La caché no es una carpeta verificada.');
    const parent = await realpath(cacheRoot), root = path.join(parent, 'render-worker');
    let created = false;
    try {await mkdir(root); created = true;} catch (error) {if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;}
    if ((await lstat(root)).isSymbolicLink() || await realpath(root) !== root) throw new WorkerError(503, 'CACHE_INVALIDA', 'La caché contiene un enlace no permitido.');
    if (created) await writeFile(path.join(root, '.render-worker-owned'), marker, {flag: 'wx'});
    if (await readFile(path.join(root, '.render-worker-owned'), 'utf8') !== marker) throw new WorkerError(503, 'CACHE_AJENA', 'La carpeta de caché no pertenece al renderizador.');
    return new ScratchSpace(root);
  }
  async job(): Promise<string> {
    const job = await mkdtemp(path.join(this.root, 'job-'));
    await writeFile(path.join(job, '.render-worker-owned'), marker, {flag: 'wx'}); return job;
  }
  async ensureSpace(bytes: number): Promise<void> {
    const info = await statfs(this.root);
    if (!Number.isFinite(bytes) || bytes < 0 || info.bavail * info.bsize < bytes + 512 * 1024 ** 2) throw new WorkerError(507, 'CACHE_INSUFICIENTE', 'No hay espacio temporal suficiente para este trabajo.');
  }
  async remove(candidate: string): Promise<void> {
    // Verificar el destino absoluto completo antes del único borrado recursivo.
    if (!path.isAbsolute(candidate) || path.dirname(candidate) !== this.root || !path.basename(candidate).startsWith('job-') || (await lstat(candidate)).isSymbolicLink() || await realpath(candidate) !== candidate || await readFile(path.join(candidate, '.render-worker-owned'), 'utf8') !== marker) throw new WorkerError(500, 'LIMPIEZA_RECHAZADA', 'La carpeta temporal no supera la comprobación de propiedad.');
    await rm(candidate, {recursive: true, force: false});
  }
}
