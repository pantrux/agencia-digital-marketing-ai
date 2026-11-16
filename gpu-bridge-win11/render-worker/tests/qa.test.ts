import {test} from 'node:test';
import assert from 'node:assert/strict';
import {contactSheet, normalizedRuler} from '../src/qa.ts';

test('la tira QA incorpora cada imagen y regla visible Y=0..1920', () => {
  const svg = contactSheet([{png: Buffer.from('imagen'), seconds: 2}]);
  assert.match(svg, /Y=0/); assert.match(svg, /Y=1920/); assert.match(svg, /data:image\/png;base64,aW1hZ2Vu/);
  assert.match(svg, /2\.000 s/); assert.doesNotMatch(svg, /href="https?:/);
  assert.throws(() => contactSheet([]));
});

test('4K conserva regla normalizada 0..1920 y devuelve escala y dimensiones originales', () => {
  const source = {width:2160,height:3840};
  const svg = contactSheet([{png: Buffer.from('imagen 4k'), seconds: 1.5}], source);
  assert.match(svg,/Y=1920/); assert.doesNotMatch(svg,/Y=3840/); assert.match(svg,/2160×3840/);
  assert.match(svg,/Y=240/); assert.match(svg,/1080×1920/);
  const ruler = normalizedRuler(source);
  assert.equal(ruler.y_max,1920); assert.equal(ruler.source_scale,0.5);
  assert.equal(ruler.original_height,3840); assert.equal(ruler.original_width,2160);
  assert.equal(ruler.source_x,0); assert.equal(ruler.source_y,0);
  const landscape = normalizedRuler({width:1920,height:1080});
  assert.equal(landscape.source_scale,1080/1920); assert.ok(landscape.source_y>0);
  assert.throws(() => normalizedRuler({width:0,height:3840}));
});
