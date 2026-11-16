import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, writeFile, rm, access} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {ScratchSpace} from '../src/scratch.ts';

test('limpieza solo elimina el temporal propio con marcador intacto', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'render-scratch-'));
  try {
    const space = await ScratchSpace.create(root), job = await space.job();
    await writeFile(path.join(job, 'prueba'), 'dato'); await space.remove(job);
    await assert.rejects(access(job));
    const second = await space.job(); await rm(path.join(second, '.render-worker-owned'));
    await assert.rejects(space.remove(second)); await access(second);
    await assert.rejects(space.remove(root)); await access(root);
  } finally {await rm(root, {recursive: true, force: true});}
});
