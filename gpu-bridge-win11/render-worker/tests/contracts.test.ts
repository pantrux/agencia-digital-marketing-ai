import assert from 'node:assert/strict';
import test from 'node:test';
import {outputDimensions, parseInspect, parseRender, parseStills, WorkerError} from '../src/contracts.ts';
import {compositionInput} from '../src/remotion.ts';

const request = () => ({
  project_id: 'sansevieras', job_id: 'trabajo-01', composition: 'AgencyVertical',
  plan: 'Free', format: 'mp4',
  props: {title: 'Prueba local', duration_seconds: 5, audio_path: 'proyectos/sansevieras/03_assets/voz.wav'},
});

test('contrato vertical local con marca de agua obligatoria en Free', () => {
  const value = parseRender(request());
  assert.equal(value.watermark, true);
  assert.equal(value.durationInFrames, 150);
  assert.equal(value.width, 1080);
  assert.equal(value.height, 1920);
});

test('un reel conserva cortes y rótulos con extremos exclusivos en fotogramas', () => {
  const props = {...request().props, duration_seconds: 36, video_path: 'proyectos/sansevieras/01_raw/crudo.mp4'};
  const scenes = [
    {source_start_frame: 30, source_end_frame: 390, label: {text: 'Catálogo', start_frame: 0, end_frame: 90, position: 'bottom'}},
    {source_start_frame: 1200, source_end_frame: 1920},
  ];
  const reel = parseRender({...request(), props: {...props, scenes}});
  assert.deepEqual(reel.props.scenes, scenes);
  assert.deepEqual(compositionInput(reel, null).scenes, scenes);
  assert.throws(() => parseRender({...request(), props: {...props, duration_seconds: 35, scenes}}), WorkerError);
  assert.throws(() => parseRender({...request(), props: {...props, scenes: [
    scenes[0], {source_start_frame: 389, source_end_frame: 1109},
  ]}}), WorkerError);
  assert.throws(() => parseRender({...request(), props: {...props, scenes: [
    {...scenes[0], label: {...scenes[0]!.label, end_frame: 361}}, scenes[1],
  ]}}), WorkerError);
});

test('rechaza composición remota, propiedades desconocidas y duración fuera del plan', () => {
  for (const value of [
    {...request(), composition: 'https://ejemplo.invalid/composicion'},
    {...request(), props: {...request().props, code: 'no ejecutar'}},
    {...request(), props: {...request().props, duration_seconds: 601}},
    {...request(), watermark: false},
  ]) assert.throws(() => parseRender(value), WorkerError);
});

test('rutas de otro proyecto, dispositivos, ADS y escape no son medios admitidos', () => {
  for (const input_path of [
    '../datos.mp4', 'C:/datos.mp4', '//servidor/recurso/datos.mp4',
    'proyectos/otra/01_raw/datos.mp4', 'proyectos/sansevieras/01_raw/CON.mp4',
    'proyectos/sansevieras/01_raw/video.mp4:secreto',
  ]) assert.throws(() => parseInspect({project_id: 'sansevieras', job_id: 'qa-01', input_path}), WorkerError);
});

test('stills usa segundo dos y valida fotogramas dentro de la composición', () => {
  assert.deepEqual(parseStills(request()).frames, [60]);
  assert.throws(() => parseStills({...request(), frames: [150]}), WorkerError);
});

test('render requiere audio y los planes de pago no incorporan marca Free', () => {
  const value = request();
  assert.equal(parseRender({...value, plan: 'Pro'}).watermark, false);
  assert.throws(() => parseRender({...value, props: {title: 'Silencio', duration_seconds: 5}}), WorkerError);
});

test('el techo operativo es común y no aplica cuotas comerciales por plan', () => {
  for (const plan of ['Free', 'Pro', 'Ultra', 'Ilimitado']) {
    assert.equal(parseRender({...request(), plan, props: {...request().props, duration_seconds: 45}}).durationInFrames, 1350);
  }
});

test('MP4/1080p siguen por defecto y el cliente no puede imponer dimensiones ni transparencia', () => {
  const value = parseRender({...request(), format: undefined});
  assert.equal(value.format, 'mp4'); assert.equal(value.resolution, '1080p');
  assert.equal(value.transparent, false);
  for (const extra of [{width: 2160}, {height: 3840}, {transparent: true}]) {
    assert.throws(() => parseRender({...request(), ...extra}), WorkerError);
  }
});

test('4K vertical requiere Ultra o Ilimitado y usa metadatos exactos', () => {
  for (const plan of ['Ultra', 'Ilimitado']) {
    const value = parseRender({...request(), plan, resolution: '4k'});
    assert.equal(value.width, 2160); assert.equal(value.height, 3840);
    assert.equal(value.fps, 30); assert.equal(value.format, 'mp4');
    const props = compositionInput(value, null);
    assert.equal(props.resolution, '4k'); assert.equal(props.transparent, false);
    assert.deepEqual(outputDimensions(value.resolution), {width: 2160, height: 3840});
  }
  for (const plan of ['Free', 'Pro']) assert.throws(() => parseRender({...request(), plan, resolution: '4k'}), WorkerError);
  for (const resolution of ['4K', '2160p', 'https://ajeno.invalid', 2160, null]) {
    assert.throws(() => parseRender({...request(), plan: 'Ultra', resolution}), WorkerError);
  }
});

test('ProRes4444 alfa es exclusivo de Ilimitado y conserva 1080p o 4K y capturas PNG transparentes', () => {
  for (const resolution of ['1080p', '4k']) {
    const value = parseRender({...request(), plan: 'Ilimitado', resolution, format: 'prores4444'});
    assert.equal(value.format, 'prores4444'); assert.equal(value.transparent, true);
    const props = compositionInput(value, 'alpha.mov');
    assert.equal(props.transparent, true); assert.equal(props.resolution, resolution);
    const still = parseStills({...request(), plan: 'Ilimitado', resolution, format: 'prores4444', frames: [0]});
    assert.equal(still.transparent, true); assert.deepEqual(still.frames, [0]);
  }
  for (const plan of ['Free', 'Pro', 'Ultra']) assert.throws(() => parseRender({...request(), plan, format: 'prores4444'}), WorkerError);
  for (const format of ['mov', 'prores', 'h264', null]) assert.throws(() => parseRender({...request(), plan: 'Ilimitado', format}), WorkerError);
});
