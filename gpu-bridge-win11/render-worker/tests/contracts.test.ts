import assert from 'node:assert/strict';
import {mkdir, mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {AD_MUSIC_ID, outputDimensions, parseInspect, parseRender, parseStills, WorkerError} from '../src/contracts.ts';
import {renderHyperframes} from '../src/hyperframes.ts';
import {compositionInput} from '../src/remotion.ts';
import {graphicFrames, socialFadeProgress} from '../src/timeline.ts';

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
    {source_start_frame: 30, source_end_frame: 390, label: {text: 'Catálogo', start_frame: 0, end_frame: 90, position: 'bottom', size: 'large'}},
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

test('un montaje múltiple acredita fuente y ruta de cada escena', () => {
  const first = '11111111-1111-4111-8111-111111111111', second = '22222222-2222-4222-8222-222222222222';
  const sources = [
    {id:first, video_path:`proyectos/sansevieras/01_raw/${first}.mp4`, audio_path:'proyectos/sansevieras/03_assets/intake-source.wav'},
    {id:second, video_path:`proyectos/sansevieras/01_raw/${second}.mov`, audio_path:`proyectos/sansevieras/03_assets/intake-source-${second}.wav`},
  ];
  const scenes = [
    {source_id:second, source_start_frame:0, source_end_frame:300, label:{text:'Gancho',start_frame:0,end_frame:90,position:'top'}},
    {source_id:first, source_start_frame:0, source_end_frame:600},
  ];
  const props = {title:'Montaje', duration_seconds:30, sources, scenes};
  const parsed = parseRender({...request(), props});
  assert.deepEqual(parsed.props.sources, sources);
  assert.deepEqual(parsed.props.scenes, scenes);
  const longScenes = [
    {source_id:first,source_start_frame:0,source_end_frame:1800,label:{text:'Inicio',start_frame:0,end_frame:90,position:'top'}},
    {source_id:second,source_start_frame:0,source_end_frame:1800},
  ];
  assert.equal(parseRender({...request(),props:{...props,duration_seconds:120,scenes:longScenes}}).durationInFrames,3600);
  assert.throws(() => parseRender({...request(),props:{...props,duration_seconds:121,scenes:longScenes}}),WorkerError);
  assert.throws(() => parseRender({...request(), props:{...props, sources:[sources[0], {...sources[1],video_path:sources[0]!.video_path}]}}), WorkerError);
  assert.throws(() => parseRender({...request(), props:{...props, scenes:[{...scenes[0],source_id:first}, {...scenes[1],source_id:first}]}}), WorkerError);
  assert.throws(() => parseRender({...request(), props:{...props, sources:[sources[0], {...sources[1],audio_path:'proyectos/otra/03_assets/voz.wav'}]}}), WorkerError);
  assert.throws(() => parseRender({...request(), props:{...props, video_path:sources[0]!.video_path}}), WorkerError);
});

test('el anuncio reordena la fuente y programa los cambios de subtítulo y CTA', () => {
  const scenes = [
    {source_start_frame: 2000, source_end_frame: 2300, label: {text:'Gancho', start_frame: 0, end_frame: 90, position:'top'}},
    {source_start_frame: 400, source_end_frame: 1000},
  ];
  const ad = {cta:'Escríbenos',brand:'Vivero',captions:[{start_frame:20,end_frame:60,text:'Nuestra colección'}]};
  const props = {...request().props,duration_seconds:30,video_path:'proyectos/sansevieras/01_raw/crudo.mp4',scenes,ad};
  const adRequest = parseRender({...request(),props});
  assert.deepEqual(adRequest.props.scenes, scenes);
  assert.deepEqual(compositionInput(adRequest,null).ad,ad);
  const composed = parseRender({...request(), props:{...props,ad:{...ad,soundtrack:AD_MUSIC_ID}}});
  assert.equal(composed.props.ad?.soundtrack,AD_MUSIC_ID);
  assert.throws(() => parseRender({...request(), props:{...props,ad:{...ad,soundtrack:'external-file'}}}),WorkerError);
  assert.deepEqual(graphicFrames(adRequest.props.scenes!,adRequest.props.ad),[0,20,60,90,780]);
  assert.throws(() => parseRender({...request(),props:{...props,ad:undefined}}),WorkerError);
  assert.throws(() => parseRender({...request(),props:{...props,ad:{...ad,captions:[{...ad.captions[0],end_frame:781}]}}}),WorkerError);
  assert.throws(() => parseRender({...request(),props:{...props,scenes:[scenes[0],{source_start_frame:2299,source_end_frame:2899}]}}),WorkerError);
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

test('el CTA social conserva sólo el destino elegido y agenda cada fotograma animado', () => {
  const scenes = [
    {source_start_frame: 2000, source_end_frame: 2300, label: {text:'Gancho', start_frame:0, end_frame:90, position:'top'}},
    {source_start_frame: 400, source_end_frame:1000},
  ];
  const destination = {channel:'facebook',url:'https://www.facebook.com/rinconsansevieras'};
  const ad = {cta:'Escríbenos',brand:'Vivero',captions:[{start_frame:20,end_frame:60,text:'Nuestra colección'}],destination};
  const props = {...request().props,duration_seconds:30,video_path:'proyectos/sansevieras/01_raw/crudo.mp4',scenes,ad};
  const value = parseRender({...request(),props});
  assert.deepEqual(value.props.ad?.destination,destination);
  const graphic = parseRender({...request(),plan:'Ilimitado',format:'prores4444',
    props:{...props,video_path:undefined,ad:{...ad,engine:'hyperframes'}}});
  assert.equal(graphic.transparent,true);
  assert.deepEqual(graphic.props.ad?.destination,destination);
  assert.throws(() => parseRender({...request(),plan:'Pro',format:'prores4444',props:{...props,video_path:undefined}}),WorkerError);
  assert.equal(parseRender({...request(),props:{...props,ad:{...ad,engine:'hyperframes'}}}).props.ad?.engine,'hyperframes');
  assert.throws(() => parseRender({...request(),props:{...props,ad:{...ad,engine:'remote'}}}),WorkerError);
  const alternateAudio = [{...scenes[0],audio_source_start_frame:120},scenes[1]];
  assert.equal(parseRender({...request(),props:{...props,scenes:alternateAudio}}).props.scenes?.[0]?.audio_source_start_frame,120);
  assert.throws(() => parseRender({...request(),props:{...props,scenes:alternateAudio,ad:undefined}}),
    /audio alternativo/u);
  assert.throws(() => parseRender({...request(),props:{...props,
    scenes:[{...scenes[0],audio_source_start_frame:17999},scenes[1]]}}),WorkerError);
  const frames = graphicFrames(value.props.scenes!,value.props.ad);
  assert.ok(frames.includes(1) && frames.includes(751) && frames.includes(899));
  assert.equal(socialFadeProgress(863),0);
  assert.ok(socialFadeProgress(882) > 0 && socialFadeProgress(882) < 1);
  assert.equal(socialFadeProgress(899),1);
  for (const bad of ['https://instagram.com/rinconsansevieras','https://www.facebook.com.ejemplo.invalid/perfil','https://www.facebook.com/perfil?track=1']) {
    assert.throws(() => parseRender({...request(),props:{...props,ad:{...ad,destination:{...destination,url:bad}}}}),WorkerError);
  }
});

test('publica las capturas de HyperFrames con los nombres que verifica Lucas', async () => {
  const workspace = await mkdtemp(path.join(os.tmpdir(), 'studio-hyperframes-'));
  try {
    const props = {...request().props, duration_seconds:30, video_path:'proyectos/sansevieras/01_raw/crudo.mp4',
      scenes:[{source_start_frame:0,source_end_frame:300,label:{text:'Gancho',start_frame:0,end_frame:90,position:'top'}},
        {source_start_frame:600,source_end_frame:1200}],
      ad:{engine:'hyperframes',cta:'Escríbenos',brand:'Vivero',captions:[{start_frame:1,end_frame:30,text:'Una hoja'}]}};
    const result = await renderHyperframes({
      request:parseRender({...request(),props}),workspace,videoSource:'local.mp4',browser:'chrome',ffmpeg:'ffmpeg',
      signal:AbortSignal.timeout(5000),
      run:async (_tool,args,options) => {
        if (options.hyperframes) {
          const output = args.at(-1)!;
          await mkdir(output);
          await Promise.all(['at-0s.png','at-15s.png','at-29.967s.png'].map(name => writeFile(path.join(output,name),Buffer.alloc(64))));
        }
        return {stdout:'',stderr:''};
      },
    },[0,450,899],false);
    assert.deepEqual(result.files.map(file => path.basename(file)),['still-0.png','still-450.png','still-899.png']);
  } finally {await rm(workspace,{recursive:true,force:true});}
});

test('HyperFrames produce la secuencia sin imagen de fondo para la gráfica ProRes', async () => {
  const workspace = await mkdtemp(path.join(os.tmpdir(), 'studio-hyperframes-alfa-'));
  try {
    const props = {...request().props, duration_seconds:30,
      scenes:[{source_start_frame:0,source_end_frame:450,label:{text:'Gancho',start_frame:0,end_frame:90,position:'top'}},
        {source_start_frame:450,source_end_frame:900}],
      ad:{engine:'hyperframes',cta:'Escríbenos',brand:'Vivero',captions:[{start_frame:1,end_frame:30,text:'Plantas'}]}};
    const graphic = parseRender({...request(),plan:'Ilimitado',format:'prores4444',props});
    const result = await renderHyperframes({request:graphic,workspace,videoSource:null,browser:'chrome',ffmpeg:'ffmpeg',
      signal:AbortSignal.timeout(15000),run:async (_tool,args,options) => {
        assert.ok(options.hyperframes);
        const html = await readFile(path.join(options.hyperframes.project,'index.html'),'utf8');
        assert.ok(!html.includes('<video'));
        assert.ok(args.includes('png-sequence'));
        const output = args.at(-1)!;
        await mkdir(output);
        for (let frame=1; frame<=900; frame++) await writeFile(path.join(output,`frame_${String(frame).padStart(6,'0')}.png`),Buffer.alloc(64));
        return {stdout:'',stderr:''};
      }},Array.from({length:900},(_,index)=>index),true);
    assert.equal(result.frameCount,900);
    assert.equal(path.basename(result.files[0]!), 'frame_000001.png');
    assert.equal(path.basename(result.files[899]!), 'frame_000900.png');
  } finally {await rm(workspace,{recursive:true,force:true});}
});
