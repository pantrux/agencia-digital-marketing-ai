import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runProcess, childEnvironment} from '../src/process.ts';

test('el proceso no recibe secretos y conserva salida acotada', async () => {
  const env = childEnvironment({PATH: process.env.PATH, GPU_BRIDGE_BEARER_TOKEN: 'privado', SystemRoot: process.env.SystemRoot});
  assert.equal(env.GPU_BRIDGE_BEARER_TOKEN, undefined);
  const result = await runProcess(process.execPath, ['-e', 'process.stdout.write("ok");process.stderr.write("medida")'], {timeoutMs: 3000});
  assert.equal(result.stdout, 'ok'); assert.equal(result.stderr, 'medida');
});
test('timeout y salida excesiva terminan exclusivamente el proceso propio', async () => {
  await assert.rejects(runProcess(process.execPath, ['-e', 'setInterval(()=>{},1000)'], {timeoutMs: 100}), /tiempo/);
  await assert.rejects(runProcess(process.execPath, ['-e', 'process.stdout.write("x".repeat(30000))'], {timeoutMs: 3000, maxOutputBytes: 1000}), /salida/);
});
