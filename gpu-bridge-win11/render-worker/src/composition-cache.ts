import {constants} from 'node:fs';
import {copyFile, lstat, mkdir, mkdtemp, readFile, readdir, realpath, rmdir, statfs, unlink, writeFile} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {WorkerError} from './contracts.ts';

interface CompileOptions {entryPoint: string; outDir: string; publicDir: string; enableCaching: false; gitSource: null; onSymlinkDetected: () => never}
export type CompositionBuilder = (options: CompileOptions) => Promise<string>;
interface AssetLease {videoFile: string | null; release(): Promise<void>}
const owner = 'código compilado propio del renderizador v1\n';
const scratchOwner = 'temporal exclusivo del renderizador v1\n';
const caches = new Map<string, Promise<CompositionCache>>();
const denied = (): never => {throw new WorkerError(500, 'CACHE_COMPOSICION_RECHAZADA', 'La caché no acredita propiedad o contiene archivos ajenos.');};

async function plainDirectory(directory: string): Promise<void> {
  if (!path.isAbsolute(directory)) denied();
  const info = await lstat(directory);
  if (!info.isDirectory() || info.isSymbolicLink() || path.relative(directory, await realpath(directory)) !== '') denied();
}
async function regular(file: string): Promise<void> {
  const info = await lstat(file);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || path.relative(file, await realpath(file)) !== '') denied();
}
async function marker(file: string, expected: string): Promise<void> {
  await regular(file); if (await readFile(file, 'utf8') !== expected) denied();
}
async function ownedDirectory(directory: string, filename: string): Promise<void> {
  let created = false;
  try {await mkdir(directory); created = true;} catch (error) {if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;}
  await plainDirectory(directory);
  if (created) await writeFile(path.join(directory, filename), owner, {flag: 'wx'});
  await marker(path.join(directory, filename), owner);
}

export class CompositionCache {
  readonly root: string;
  readonly builder: CompositionBuilder;
  #building: Promise<string> | undefined;
  #assets: string | undefined;
  private constructor(root: string, builder: CompositionBuilder) {this.root = root; this.builder = builder;}
  static async create(parent: string, builder: CompositionBuilder): Promise<CompositionCache> {
    await plainDirectory(parent);
    if (path.basename(parent) !== 'render-worker') denied();
    await marker(path.join(parent, '.render-worker-owned'), scratchOwner);
    const root = path.join(parent, 'compiled-composition');
    await ownedDirectory(root, '.composition-owned');
    return new CompositionCache(root, builder);
  }
  ready(): Promise<string> {this.#building ??= this.#compile(); return this.#building;}
  async #compile(): Promise<string> {
    // Un proceso nuevo recompila en un directorio exclusivo: nunca adopta código viejo.
    const work = await mkdtemp(path.join(this.root, 'process-'));
    await writeFile(path.join(work, '.composition-owned'), owner, {flag: 'wx'});
    const input = path.join(work, 'empty-public'), output = path.join(work, 'bundle');
    await mkdir(input);
    const serve = await this.builder({entryPoint: fileURLToPath(new URL('../composition/index.tsx', import.meta.url)), outDir: output, publicDir: input, enableCaching: false, gitSource: null, onSymlinkDetected: denied});
    if (path.relative(output, serve) !== '') denied();
    await plainDirectory(output);
    const publicRoot = path.join(output, 'public');
    try {await mkdir(publicRoot);} catch (error) {if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;}
    await plainDirectory(publicRoot);
    this.#assets = path.join(publicRoot, 'media');
    await ownedDirectory(this.#assets, '.assets-owned');
    return serve;
  }
  async stageVideo(source: string | null): Promise<AssetLease> {
    await this.ready();
    if (source === null) return {videoFile: null, release: async () => {}};
    const extension = path.extname(source).toLowerCase();
    if (!['.mp4', '.mov', '.mkv', '.webm', '.m4v'].includes(extension)) denied();
    await regular(source);
    const assets = this.#assets!;
    await plainDirectory(assets); await marker(path.join(assets, '.assets-owned'), owner);
    const info = await statfs(assets);
    if ((await lstat(source)).size + 512 * 1024 ** 2 > info.bavail * info.bsize) throw new WorkerError(507, 'CACHE_INSUFICIENTE', 'No hay espacio para copiar el vídeo del trabajo.');
    const id = randomUUID(), directory = path.join(assets, id), filename = 'source' + extension;
    await mkdir(directory);
    const content = JSON.stringify({owner, id});
    await writeFile(path.join(directory, '.asset-owned'), content, {flag: 'wx'});
    let released = false;
    const release = async () => {
      if (released) return;
      await plainDirectory(assets); await plainDirectory(directory);
      if (path.dirname(directory) !== assets || !/^[a-f0-9-]{36}$/.test(path.basename(directory))) denied();
      await marker(path.join(directory, '.asset-owned'), content);
      const files = await readdir(directory);
      if (files.some((file) => file !== '.asset-owned' && file !== filename)) denied();
      if (files.includes(filename)) {await regular(path.join(directory, filename)); await unlink(path.join(directory, filename));}
      await unlink(path.join(directory, '.asset-owned')); await rmdir(directory);
      released = true;
    };
    try {await copyFile(source, path.join(directory, filename), constants.COPYFILE_EXCL);}
    catch (error) {await release(); throw error;}
    return {videoFile: path.posix.join('media', id, filename), release};
  }
}

export function compositionCache(parent: string, builder: CompositionBuilder): Promise<CompositionCache> {
  const key = process.platform === 'win32' ? path.resolve(parent).toLowerCase() : path.resolve(parent);
  let pending = caches.get(key);
  if (!pending) {pending = CompositionCache.create(parent, builder); caches.set(key, pending);}
  return pending;
}
