import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtemp, mkdir, writeFile, symlink, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {NasPaths} from '../src/nas.ts';

test('resuelve medios propios y nunca sobrescribe el área de salida', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'render-nas-'));
  try {
    await mkdir(path.join(root, 'proyectos/sansevieras/03_assets'), {recursive: true});
    await writeFile(path.join(root, 'proyectos/sansevieras/03_assets/voz.wav'), 'fixture');
    const nas = await NasPaths.create(root);
    assert.equal(await nas.input('proyectos/sansevieras/03_assets/voz.wav', 'sansevieras'), path.join(root, 'proyectos/sansevieras/03_assets/voz.wav'));
    const output = await nas.output('sansevieras', 'prueba-01', 'render');
    assert.equal(output.relative, 'proyectos/sansevieras/06_renders/prueba-01/render');
    await assert.rejects(() => nas.output('sansevieras', 'prueba-01', 'render'));
  } finally {await rm(root, {recursive: true, force: true});}
});
test('rechaza junction o enlace incluso si parece un medio del proyecto', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'render-root-'));
  const other = await mkdtemp(path.join(tmpdir(), 'render-outside-'));
  try {
    await mkdir(path.join(root, 'proyectos/sansevieras'), {recursive: true});
    await writeFile(path.join(other, 'privado.wav'), 'no leer');
    await symlink(other, path.join(root, 'proyectos/sansevieras/03_assets'), 'junction');
    const nas = await NasPaths.create(root);
    await assert.rejects(() => nas.input('proyectos/sansevieras/03_assets/privado.wav', 'sansevieras'));
  } finally {await rm(root, {recursive: true, force: true}); await rm(other, {recursive: true, force: true});}
});
