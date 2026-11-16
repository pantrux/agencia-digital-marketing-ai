import {lstat, realpath, mkdir} from 'node:fs/promises';
import path from 'node:path';
import {mediaPath, WorkerError} from './contracts.ts';

function inside(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === '' || (!path.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..' + path.sep));
}
export class NasPaths {
  readonly root: string;
  private constructor(root: string) {this.root = root;}
  static async create(root: string): Promise<NasPaths> {
    const info = await lstat(root);
    if (!path.isAbsolute(root) || !info.isDirectory() || info.isSymbolicLink()) throw new WorkerError(503, 'NAS_NO_VERIFICADO', 'La raíz NAS no está disponible.');
    return new NasPaths(await realpath(root));
  }
  async #check(parts: string[], create: boolean): Promise<string> {
    let candidate = this.root;
    for (const part of parts) {
      if (!part || part === '.' || part === '..' || /[\\/:]/.test(part)) throw new WorkerError(422, 'RUTA_INVALIDA', 'La ruta no está permitida.');
      candidate = path.join(candidate, part);
      if (!inside(this.root, candidate)) throw new WorkerError(422, 'RUTA_INVALIDA', 'La ruta sale del NAS.');
      if (create) await mkdir(candidate).catch((error: NodeJS.ErrnoException) => {if (error.code !== 'EEXIST') throw error;});
      const info = await lstat(candidate);
      if (info.isSymbolicLink() || !inside(this.root, await realpath(candidate))) throw new WorkerError(422, 'REANALISIS_NO_PERMITIDO', 'Se rechazó un enlace o punto de reanálisis.');
    }
    return candidate;
  }
  async input(relative: string, project: string): Promise<string> {
    const candidate = await this.#check(mediaPath(relative, project).split('/'), false);
    const info = await lstat(candidate);
    if (!info.isFile() || info.size > 4 * 1024 ** 3) throw new WorkerError(422, 'MEDIO_INVALIDO', 'El medio no es un archivo permitido.');
    return candidate;
  }
  #outputParts(project: string, job: string, kind: 'render' | 'stills' | 'inspect'): string[] {
    if (!/^[a-z][a-z0-9_-]{0,47}$/.test(project) || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(job)) throw new WorkerError(422, 'RUTA_INVALIDA', 'El identificador no es válido.');
    return ['proyectos', project, kind === 'render' ? '06_renders' : '05_revision', job, kind];
  }
  async existingOutput(project: string, job: string, kind: 'render' | 'stills') {
    const parts = this.#outputParts(project, job, kind);
    try {
      const absolute = await this.#check(parts, false);
      if (!(await lstat(absolute)).isDirectory()) throw new WorkerError(409, 'TRABAJO_EXISTENTE', 'El área del recibo no es un directorio.');
      return {absolute, relative: parts.join('/')};
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
      throw error;
    }
  }
  async output(project: string, job: string, kind: 'render' | 'stills' | 'inspect'): Promise<{absolute: string; relative: string}> {
    const parts = this.#outputParts(project, job, kind);
    // El proyecto debe existir previamente; no se crean proyectos desde esta API.
    const projectRoot = await this.#check(['proyectos', project], false);
    if (!(await lstat(projectRoot)).isDirectory()) throw new WorkerError(422, 'PROYECTO_INVALIDO', 'El proyecto no existe.');
    const parent = await this.#check(parts.slice(0, -1), true);
    const absolute = path.join(parent, kind);
    try {await mkdir(absolute);} catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'EEXIST') throw new WorkerError(409, 'TRABAJO_EXISTENTE', 'El área de salida de este trabajo ya existe.');
      throw error;
    }
    await this.#check(parts, false);
    return {absolute, relative: parts.join('/')};
  }
}
