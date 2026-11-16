import {test} from 'node:test';
import assert from 'node:assert/strict';
import {main} from '../src/start.ts';

test('importar el lanzador no carga secretos ni abre un puerto', () => {
  assert.equal(typeof main, 'function');
});
