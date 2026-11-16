// Ejecutar con Node en la imagen fijada de Immich, sin red y con datos sintéticos.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const sha = (bytes) => createHash('sha256').update(bytes).digest('hex');
const SOURCE_PIN = 'eb5209345dcdad607b5beb58d0883ef999ab5b7c75430c173910af334fcda07c';
const PREVIEW_PIN = 'ecf51274231ce3eba81990d64b04a6741fd0831c6759b861723378c287652a25';
const options = { colorspace: 'srgb', size: 1440, processInvalidImages: false };
const [mode = 'q100', ...extra] = process.argv.slice(2);
assert.ok(extra.length === 0 && ['q100', 'webp-lossless'].includes(mode), 'Modo esperado: q100 o webp-lossless');

async function manifest(filename, digest) {
  const bytes = await readFile(filename);
  assert.equal(sha(bytes), digest);
  return JSON.parse(bytes).fixtures;
}

async function encode(repo, bitmap, quality, destination) {
  await repo.generateThumbnail(bitmap, {
    ...options, format: 'jpeg', quality, progressive: false, edits: [],
  }, destination);
  return readFile(destination);
}

function verifyLosslessWebp(bytes) {
  assert.ok(bytes.length >= 20);
  assert.equal(bytes.toString('ascii', 0, 4), 'RIFF');
  assert.equal(bytes.toString('ascii', 8, 12), 'WEBP');
  assert.equal(bytes.readUInt32LE(4) + 8, bytes.length);
  let lossless = 0;
  for (let offset = 12; offset < bytes.length;) {
    assert.ok(offset + 8 <= bytes.length);
    const type = bytes.toString('ascii', offset, offset + 4);
    const size = bytes.readUInt32LE(offset + 4);
    const next = offset + 8 + size + (size % 2);
    assert.ok(next <= bytes.length);
    assert.ok(!['VP8 ', 'ANIM', 'ANMF'].includes(type));
    if (type === 'VP8L') {
      assert.ok(size >= 5 && bytes[offset + 8] === 0x2f);
      lossless += 1;
    }
    offset = next;
  }
  assert.equal(lossless, 1);
}

async function encodeLossless(repo, sharp, bitmap, destination) {
  // Conserva el remuestreo y el perfil exactos de generateThumbnail antes de codificar.
  const transformed = await repo.transform(bitmap, { ...options, edits: [] });
  const tagged = repo.tag(transformed, options.colorspace);
  const reference = await tagged.clone().png().toBuffer();
  const candidate = await tagged.webp({ lossless: true }).toBuffer();
  verifyLosslessWebp(candidate);
  const expected = await sharp(reference).raw().toBuffer({ resolveWithObject: true });
  const actual = await sharp(candidate).raw().toBuffer({ resolveWithObject: true });
  assert.deepEqual(actual.info, expected.info);
  assert.ok(actual.data.equals(expected.data), 'Píxeles del WebP distintos de la referencia PNG');
  await writeFile(destination, candidate, { flag: 'wx' });
  return candidate;
}

const fixtures = (await manifest('/source/fixtures.json', SOURCE_PIN))
  .filter((fixture) => fixture.split === 'calibration');
const exact = new Map((await manifest('/exact/previews.json', PREVIEW_PIN))
  .map((fixture) => [fixture.id, fixture]));
assert.equal(fixtures.length, 36);
assert.equal(exact.size, 36);

const { MediaRepository } = await import(pathToFileURL(
  '/usr/src/app/server/dist/repositories/media.repository.js',
).href);
const repository = new MediaRepository({ setContext() {} });
await mkdir('/out/q80');
const checked = [];
for (const { id, path, prepared_sha256: digest } of fixtures) {
  assert.match(id, /^cf-[0-9]{2}-o[345]$/);
  assert.equal(path, `images/${id}.png`);
  const previous = exact.get(id);
  assert.equal(previous.path, `previews/${id}.jpg`);
  const source = await readFile(`/source/${path}`);
  assert.equal(sha(source), digest);
  assert.equal(sha(await readFile(`/exact/${previous.path}`)), previous.sha256);
  const bitmap = await repository.decodeImage(source, options);
  assert.equal(bitmap.info.width, 512);
  assert.equal(bitmap.info.height, 512);
  const baseline = await encode(repository, bitmap, 80, `/out/q80/${id}.jpg`);
  assert.equal(sha(baseline), previous.sha256, `Vista de calidad 80 distinta para ${id}`);
  checked.push({ id, previous, bitmap });
}

await mkdir('/out/previews');
const lossless = mode === 'webp-lossless';
const sharp = lossless ? createRequire('/usr/src/app/server/package.json')('sharp') : undefined;
const candidates = [];
for (const { id, previous, bitmap } of checked) {
  const path = `previews/${id}.${lossless ? 'webp' : 'jpg'}`;
  const bytes = lossless
    ? await encodeLossless(repository, sharp, bitmap, `/out/${path}`)
    : await encode(repository, bitmap, 100, `/out/${path}`);
  candidates.push({ id, asset_id: previous.asset_id, path, sha256: sha(bytes), bytes: bytes.length });
}
const output = JSON.stringify({ schema: 1, fixtures: candidates });
await writeFile('/out/previews.json', output, { flag: 'wx' });
console.log(JSON.stringify({
  success: true, matched_q80: checked.length, manifest_sha256: sha(output),
  ...(lossless && { vp8l_rgb_verified: candidates.length }),
}));
