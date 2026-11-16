const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { readFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { join, dirname } = require('node:path');

const root = process.argv[2];
const fromRunner = createRequire(join(root, 'package.json'));
const fromCore = createRequire(fromRunner.resolve('n8n-core'));
const fromWorkflow = createRequire(fromRunner.resolve('n8n-workflow'));
const fromExpression = createRequire(fromWorkflow.resolve('@n8n/expression-runtime'));
const backend = fromCore.resolve('@n8n/backend-common');
const utils = join(dirname(backend), 'utils');

async function verify() {
  assert.equal(process.versions.node, '26.12.1');
  assert.equal(process.versions.modules, '147');
  assert.equal(fromRunner('./package.json').version, '2.46.5');
  const { Isolate } = fromExpression('isolated-vm');
  const isolate = new Isolate({ memoryLimit: 16 });
  try {
    assert.equal(isolate.createContextSync().evalSync('6 * 7'), 42);
  } finally {
    isolate.dispose();
  }

  const flatted = readFileSync(join(utils, 'flatted-async.js'));
  assert.equal(createHash('sha256').update(flatted).digest('hex'),
    'a67a0fb6f31f98356540a7c7bd8f0a77109202399c9f9ff09a3486597bc01888');
  const { parseFlatted, SIZE_THRESHOLD } = fromRunner(join(utils, 'parse-flatted.js'));
  const fromBackend = createRequire(backend);
  const value = { text: 'agency-runner-'.repeat(81_000) };
  value.self = value;
  const serialized = fromBackend('flatted').stringify(value);
  assert(serialized.length > SIZE_THRESHOLD);
  const parsed = await parseFlatted(serialized);
  assert.equal(parsed.text, value.text);
  assert.equal(parsed.self, parsed);
  console.log(JSON.stringify({ isolated_vm: true, flatted_stream: true }));
}

verify().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
