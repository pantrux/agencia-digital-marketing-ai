import assert from 'node:assert/strict';
import {test} from 'node:test';
import {access, mkdir, mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {compositionCache} from '../src/composition-cache.ts';
import {ScratchSpace} from '../src/scratch.ts';

async function fixture() {
  const root = await mkdtemp(path.join(tmpdir(), 'compiled-composition-'));
  const scratch = await ScratchSpace.create(root);
  return {root, scratch};
}

test('la compilación concurrente es única por proceso y sobrevive a limpiar trabajos', async () => {
  const {root, scratch} = await fixture();
  let builds = 0;
  const builder = async (options: {outDir: string; gitSource: null}) => {
    builds += 1; assert.equal(options.gitSource, null);
    await mkdir(path.join(options.outDir, 'public'), {recursive: true});
    await writeFile(path.join(options.outDir, 'index.html'), 'código estático');
    return options.outDir;
  };
  try {
    const [one, two] = await Promise.all([compositionCache(scratch.root, builder), compositionCache(scratch.root, builder)]);
    assert.equal(one, two);
    const [a, b] = await Promise.all([one.ready(), two.ready()]);
    assert.equal(a, b); assert.equal(builds, 1);
    const job = await scratch.job(); await scratch.remove(job);
    assert.equal(await readFile(path.join(a, 'index.html'), 'utf8'), 'código estático');
  } finally {await rm(root, {recursive: true, force: true});}
});

test('dos vídeos tienen UUID aislados y retirar uno preserva el otro y el código', async () => {
  const {root, scratch} = await fixture();
  try {
    const cache = await compositionCache(scratch.root, async (options) => {await mkdir(path.join(options.outDir, 'public'), {recursive: true}); return options.outDir;});
    const first = path.join(root, 'one.mp4'), second = path.join(root, 'two.mov');
    await writeFile(first, 'video uno'); await writeFile(second, 'video dos');
    const [a, b] = await Promise.all([cache.stageVideo(first), cache.stageVideo(second)]);
    assert.notEqual(a.videoFile, b.videoFile);
    const serve = await cache.ready();
    assert.equal(await readFile(path.join(serve, 'public', a.videoFile!), 'utf8'), 'video uno');
    await a.release();
    await assert.rejects(access(path.join(serve, 'public', a.videoFile!)));
    assert.equal(await readFile(path.join(serve, 'public', b.videoFile!), 'utf8'), 'video dos');
    await b.release(); await b.release();
    await access(serve); await access(first); await access(second);
  } finally {await rm(root, {recursive: true, force: true});}
});

test('la limpieza no adopta archivos ajenos y un error de compilación permanece visible', async () => {
  const {root, scratch} = await fixture();
  try {
    const cache = await compositionCache(scratch.root, async (options) => {await mkdir(path.join(options.outDir, 'public'), {recursive: true}); return options.outDir;});
    const source = path.join(root, 'video.mp4'); await writeFile(source, 'video');
    const lease = await cache.stageVideo(source), serve = await cache.ready();
    const extra = path.join(serve, 'public', path.dirname(lease.videoFile!), 'ajeno.txt');
    await writeFile(extra, 'conservar');
    await assert.rejects(lease.release(), /propiedad|ajenos/);
    assert.equal(await readFile(extra, 'utf8'), 'conservar');
    const other = await fixture(); let calls = 0;
    try {
      const broken = await compositionCache(other.scratch.root, async () => {calls += 1; throw new Error('fallo de compilación comprobado');});
      await assert.rejects(broken.ready(), /comprobado/); await assert.rejects(broken.ready(), /comprobado/);
      assert.equal(calls, 1);
    } finally {await rm(other.root, {recursive: true, force: true});}
  } finally {await rm(root, {recursive: true, force: true});}
});

test('una carpeta sin marcador o un job temporal no pueden alojar código compartido', async () => {
  const {root, scratch} = await fixture();
  try {
    await assert.rejects(compositionCache(root, async () => root));
    await assert.rejects(compositionCache(await scratch.job(), async () => root));
    const foreign = path.join(scratch.root, 'compiled-composition'); await mkdir(foreign);
    await writeFile(path.join(foreign, 'ajeno.txt'), 'preservar');
    await assert.rejects(compositionCache(scratch.root, async () => root));
    assert.equal(await readFile(path.join(foreign, 'ajeno.txt'), 'utf8'), 'preservar');
  } finally {await rm(root, {recursive: true, force: true});}
});
