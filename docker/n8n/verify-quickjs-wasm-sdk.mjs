import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, realpath } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, join, relative, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const variants = {
  'debug-asyncify': 'DEBUG_ASYNC',
  'debug-sync': 'DEBUG_SYNC',
  'release-asyncify': 'RELEASE_ASYNC',
  'release-sync': 'RELEASE_SYNC',
};
const loaders = [
  'emscripten-module.mjs',
  'emscripten-module.cjs',
  'emscripten-module.browser.mjs',
  'emscripten-module.cloudflare.cjs',
];
const source = '066d2e04ea3c5da743853e27a047febb932cc89d77ea3f5c08c798b151187c7d';
const expectedValue = '["1,7,3","1,2,3","9,3",true]';
const expression = `(() => {
  const original = Uint8Array.of(1, 2, 3);
  const updated = original.with(1, 7);
  const bigint = BigInt64Array.of(2n, 3n).with(0, 9n);
  let outOfBounds = false;
  try { original.with(3, 4); } catch (error) { outOfBounds = error instanceof RangeError; }
  return JSON.stringify([updated.join(','), original.join(','), bigint.join(','), outOfBounds]);
})()`;

function sha(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

async function verifyFile(root, path, expected) {
  const actualPath = await realpath(path);
  const location = relative(root, actualPath);
  assert(location && !location.startsWith('..') && actualPath === path, 'Unexpected output path');
  const bytes = await readFile(actualPath);
  assert.equal(bytes.length, expected.bytes);
  assert.equal(sha(bytes), expected.sha256);
  return bytes;
}

async function main(workspace, buildReceipt, resolution) {
  const root = await realpath(workspace);
  const { source_sha256: sourceDigest, outputs } = JSON.parse(await readFile(buildReceipt, 'utf8'));
  assert.equal(sourceDigest, source);
  assert.deepEqual(Object.keys(outputs).sort(), Object.keys(variants).sort());

  const requireFromWorkspace = createRequire(join(root, 'package.json'));
  const requireFromExpression = resolution === 'direct'
    ? requireFromWorkspace
    : createRequire(createRequire(requireFromWorkspace.resolve('n8n-workflow')).resolve('@n8n/expression-runtime'));
  const quickjsManifest = await realpath(requireFromExpression.resolve('quickjs-emscripten/package.json'));
  assert(!relative(root, quickjsManifest).startsWith('..'), 'Package resolved outside runtime');
  const wrapper = JSON.parse(await readFile(quickjsManifest, 'utf8'));
  assert.equal(wrapper.version, '0.32.0');
  const requireFromWrapper = createRequire(quickjsManifest);
  const quickjs = await import(pathToFileURL(join(dirname(quickjsManifest), wrapper.exports['.'].import)));
  const result = {};
  for (const [variant, exportedVariant] of Object.entries(variants)) {
    const name = `@jitl/quickjs-wasmfile-${variant}`;
    const manifestPath = await realpath(requireFromWrapper.resolve(`${name}/package.json`));
    const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
    assert.equal(manifest.name, name);
    assert.equal(manifest.version, '0.32.0');
    const dist = join(dirname(manifestPath), 'dist');
    const files = [...loaders, 'emscripten-module.wasm'];
    if (variant.startsWith('debug-')) files.push('emscripten-module.wasm.map');
    assert.deepEqual(Object.keys(outputs[variant]).sort(), files.sort());
    for (const leaf of files) await verifyFile(root, join(dist, leaf), outputs[variant][leaf]);

    const wasm = await readFile(join(dist, 'emscripten-module.wasm'));
    const module = await WebAssembly.compile(wasm);
    const cjs = requireFromWrapper(`${name}/emscripten-module`);
    const cjsModule = await cjs({ wasmBinary: wasm });
    assert.equal(typeof cjsModule.cwrap, 'function');

    const async = variant.endsWith('asyncify');
    const vm = async
      ? await quickjs.newQuickJSAsyncWASMModule(quickjs[exportedVariant])
      : await quickjs.newQuickJSWASMModule(quickjs[exportedVariant]);
    const context = vm.newContext();
    try {
      const evaluated = async ? await context.evalCodeAsync(expression) : context.evalCode(expression);
      const value = context.unwrapResult(evaluated);
      try {
        assert.equal(context.getString(value), expectedValue);
      } finally {
        value.dispose();
      }
    } finally {
      context.dispose();
    }
    result[variant] = {
      files: files.length,
      imports: WebAssembly.Module.imports(module).length,
      exports: WebAssembly.Module.exports(module).length,
      node_loaders_instantiated: 2,
    };
  }
  return { success: true, node: process.version, variants: result };
}

try {
  assert(process.argv.length === 4 || (process.argv.length === 5 && process.argv[4] === '--direct'));
  const resolution = process.argv[4] === '--direct' ? 'direct' : 'n8n';
  console.log(JSON.stringify(await main(resolve(process.argv[2]), resolve(process.argv[3]), resolution)));
} catch (error) {
  console.error(JSON.stringify({ success: false, error: error.name, message: error.message }));
  process.exitCode = 1;
}
