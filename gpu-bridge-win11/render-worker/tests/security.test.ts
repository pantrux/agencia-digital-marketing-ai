import assert from 'node:assert/strict';
import test from 'node:test';
import {authorize, SlotPool} from '../src/security.ts';

const token = 'valor-local-de-prueba-con-longitud-suficiente-01';
test('autoriza exclusivamente IP del socket y Bearer correcto', () => {
  for (const ip of ['192.168.0.85', '192.168.0.7', '192.168.0.8', '127.0.0.1', '::1', '::ffff:192.168.0.85']) {
    assert.doesNotThrow(() => authorize(ip, 'Bearer ' + token, token));
  }
  for (const [ip, header] of [['192.168.0.9', 'Bearer ' + token], ['127.0.0.1', 'Bearer incorrecto'], ['127.0.0.1', undefined]]) {
    assert.throws(() => authorize(ip, header, token));
  }
});
test('dos slots son propios y el tercero se rechaza sin ampliar la cola', async () => {
  const pool = new SlotPool(2);
  const release = [pool.acquire(), pool.acquire()];
  assert.equal(pool.active, 2);
  assert.throws(() => pool.acquire());
  release[0]?.(); release[0]?.();
  assert.equal(pool.active, 1);
  const next = pool.acquire(); next(); release[1]?.();
  assert.equal(pool.active, 0);
});
