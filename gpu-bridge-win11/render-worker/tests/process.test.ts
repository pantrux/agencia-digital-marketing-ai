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

test('los valores de avance repetidos no evitan la cancelación del codificador', async () => {
  const script = 'setInterval(() => process.stdout.write("frame=  1\\r\\nout_time_us=N/A\\r\\nprogress=continue\\r\\n"), 40)';
  await assert.rejects(runProcess(process.execPath, ['-e', script], {timeoutMs: 4000, videoProgressTimeoutMs: 500}),
    {status: 504, code: 'CODIFICACION_SIN_AVANCE'});
});

test('las marcas de tiempo crecientes mantienen vivo el proceso con líneas fragmentadas', async () => {
  const script = `let marca = 0;
    const reloj = setInterval(() => {
      process.stdout.write('frame=  1\\nout_time_');
      setImmediate(() => {
        process.stdout.write('us=' + ++marca + '\\nprogress=continue\\n');
        if (marca === 9) clearInterval(reloj);
      });
    }, 120);`;
  const result = await runProcess(process.execPath, ['-e', script], {timeoutMs: 5000, videoProgressTimeoutMs: 800});
  assert.match(result.stdout, /out_time_us=9/);
});
