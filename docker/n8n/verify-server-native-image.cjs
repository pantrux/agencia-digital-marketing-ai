const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { readFileSync, realpathSync } = require('node:fs');
const { createRequire } = require('node:module');
const { isAbsolute, join, relative, resolve } = require('node:path');

const root = realpathSync(resolve(process.argv[2]));
assert.equal(process.platform, 'linux');
assert.equal(process.arch, 'x64');
assert.equal(process.versions.modules, '147');

function inside(path) {
  const actual = realpathSync(path);
  const member = relative(root, actual);
  assert(member && !isAbsolute(member) && !member.startsWith('..'), 'Path outside runtime');
  return actual;
}

function assertHash(path, hash) {
  assert.equal(createHash('sha256').update(readFileSync(inside(path))).digest('hex'), hash);
}

const fromCli = createRequire(join(root, 'package.json'));
const fromNodes = createRequire(inside(fromCli.resolve('n8n-nodes-base/package.json')));
const canvasPath = inside(fromNodes.resolve('@napi-rs/canvas'));
const imagePath = inside(fromNodes.resolve('@napi-rs/image'));
const fromCanvas = createRequire(canvasPath);
const fromImage = createRequire(imagePath);
const canvasBinding = fromCanvas.resolve('@napi-rs/canvas-linux-x64-musl');
const imageBinding = fromImage.resolve('@napi-rs/image-linux-x64-musl');
assertHash(canvasBinding, 'aaffa1cf32d5c77783c39aa918c640d8e85f51a0d2e2455bc449321d7c91590e');
assertHash(imageBinding, 'e8dac9890cd707a2422c16c04dcedf49c3539c3a70f75d9e7142b193b364c00b');

const surface = fromNodes('@napi-rs/canvas').createCanvas(4, 3);
const context = surface.getContext('2d');
context.fillStyle = '#ff0000';
context.fillRect(0, 0, 4, 3);
assert.deepEqual([...context.getImageData(0, 0, 1, 1).data], [255, 0, 0, 255]);

const { Transformer, ResizeFit } = fromNodes('@napi-rs/image');
const original = new Transformer(surface.toBuffer('image/png'));
const resized = new Transformer(original.resize({ width: 2, height: 2, fit: ResizeFit.Fill }).pngSync());
const dimensions = resized.metadataSync();
assert.equal(dimensions.width, 2);
assert.equal(dimensions.height, 2);
assert(require.cache[canvasBinding]);
assert(require.cache[imageBinding]);
console.log(JSON.stringify({ success: true, canvas: '1.0.11', image: '1.15.0', width: 2, height: 2 }));
