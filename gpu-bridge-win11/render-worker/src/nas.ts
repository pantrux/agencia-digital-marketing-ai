import {lstat, realpath, mkdir, mkdtemp, readdir, rename, rmdir, unlink} from 'node:fs/promises';
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
  async #outputParent(parts: string[]): Promise<string> {
    // El proyecto debe existir previamente; no se crean proyectos desde esta API.
    const projectRoot = await this.#check(parts.slice(0, 2), false);
    if (!(await lstat(projectRoot)).isDirectory()) throw new WorkerError(422, 'PROYECTO_INVALIDO', 'El proyecto no existe.');
    return this.#check(parts.slice(0, -1), true);
  }
  async stageOutput(project: string, job: string, kind: 'render' | 'stills') {
    const parts = this.#outputParts(project, job, kind);
    const parent = await this.#outputParent(parts);
    const absolute = await mkdtemp(path.join(parent, `.${kind}-`));
    const destination = path.join(parent, kind);
    const stagingParts = [...parts.slice(0, -1), path.basename(absolute)];
    const sameDirectory = async (): Promise<void> => {
      if (await this.#check(stagingParts, false) !== absolute || !inside(this.root, absolute)
        || await realpath(absolute) !== absolute || !(await lstat(absolute)).isDirectory()) {
        throw new WorkerError(500, 'TEMPORAL_INVALIDO', 'No se pudo verificar el directorio temporal de publicación.');
      }
    };
    const collision = () => new WorkerError(409, 'TRABAJO_EXISTENTE', 'El área de salida de este trabajo ya existe.');
    await sameDirectory();

    return {
      absolute, relative: parts.join('/'),
      commit: async () => {
        await sameDirectory();
        if (await this.existingOutput(project, job, kind)) throw collision();
        try {await rename(absolute, destination);}
        catch (error) {
          if (await this.existingOutput(project, job, kind)) throw collision();
          throw error;
        }
      },
      discard: async () => {
        try {await sameDirectory();}
        catch (error) {if ((error as NodeJS.ErrnoException).code === 'ENOENT') return; throw error;}
        for (const name of await readdir(absolute)) await unlink(path.join(absolute, name));
        await rmdir(absolute);
      },
    };
  }
  async output(project: string, job: string, kind: 'render' | 'stills' | 'inspect'): Promise<{absolute: string; relative: string}> {
    const parts = this.#outputParts(project, job, kind);
    const parent = await this.#outputParent(parts);
    const absolute = path.join(parent, kind);
    try {await mkdir(absolute);} catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'EEXIST') throw new WorkerError(409, 'TRABAJO_EXISTENTE', 'El área de salida de este trabajo ya existe.');
      throw error;
    }
    await this.#check(parts, false);
    return {absolute, relative: parts.join('/')};
  }
}
