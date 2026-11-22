import {test} from 'node:test';
import assert from 'node:assert/strict';
import {parseRender} from '../src/contracts.ts';
import {compositionInput, createSceneRenderer, type SceneDependencies} from '../src/remotion.ts';
import {access, mkdir, mkdtemp, readdir, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {outputDimensions, type Resolution} from '../src/contracts.ts';
import {ScratchSpace} from '../src/scratch.ts';

test('solo las propiedades locales necesarias llegan al navegador; Free conserva marca', () => {
  const request = parseRender({project_id: 'sansevieras', job_id: 'sintetico', composition: 'AgencyVertical', plan: 'Free', props: {title: '<Prueba>', duration_seconds: 3, audio_path: 'proyectos/sansevieras/03_assets/voz.wav'}});
  const props = compositionInput(request, 'source.mp4');
  assert.equal(props.watermark, true); assert.equal(props.video_file, 'source.mp4'); assert.equal(props.title, '<Prueba>');
  assert.equal(props.audio_path, undefined); assert.equal(props.project_id, undefined); assert.equal(props.token, undefined);
});

test('el tamaño ampliado llega a la composición y el tamaño estándar conserva su contrato', () => {
  const options = {project_id: 'sansevieras', job_id: 'texto', composition: 'AgencyVertical', plan: 'Ultra', resolution: '4k',
    props: {title: 'Titular breve', subtitle: 'Texto legible', duration_seconds: 60, audio_path: 'proyectos/sansevieras/03_assets/voz.wav'}};
  const expanded = parseRender({...options, props: {...options.props, subtitle_size: 'large'}});
  assert.equal(compositionInput(expanded, 'source.mp4').subtitle_size, 'large');
  assert.equal(Object.hasOwn(compositionInput(parseRender(options), null), 'subtitle_size'), false);
  assert.throws(() => parseRender({...options, props: {...options.props, subtitle_size: 240}}), /tamaño del subtítulo/);
});

test('la banda llega al MP4 y a su gráfica ProRes, con un contrato acotado', () => {
  const props = {title: 'Rincón de las Sansevieras', subtitle: 'Plantas de interior', duration_seconds: 60,
    title_layout: 'band', audio_path: 'proyectos/sansevieras/03_assets/voz.wav'};
  const order = {project_id: 'sansevieras', job_id: 'banda-60s', composition: 'AgencyVertical', plan: 'Ilimitado', props};
  const picture = {...props, video_path: 'proyectos/sansevieras/01_raw/video.mp4'};
  assert.equal(compositionInput(parseRender({...order, props: picture}), 'source.mp4').title_layout, 'band');
  assert.equal(compositionInput(parseRender({...order, format: 'prores4444'}), null).title_layout, 'band');
  assert.throws(() => parseRender(order), /montaje continuo/);
  assert.throws(() => parseRender({...order, props: {...picture, title: 'a'.repeat(43)}}), /texto breve/);
  assert.throws(() => parseRender({...order, props: {...picture, subtitle_size: 'large'}}), /texto breve/);
  assert.throws(() => parseRender({...order, props: {...picture, scenes: []}}), /montaje continuo/);
});

test('dos trabajos reutilizan bundle conservando props, dimensiones y assets aislados', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'render-cached-scene-'));
  try {
    const space = await ScratchSpace.create(root), browser = path.join(root, 'browser.exe');
    await writeFile(browser, 'navegador controlado');
    const source = path.join(root, 'video.mp4'); await writeFile(source, 'vídeo controlado');
    let builds = 0, closes = 0, compiled = '';
    const props: Record<string, unknown>[] = [];
    const sdk = {
      bundle: async (options: {outDir: string; gitSource: null}) => {builds += 1; assert.equal(options.gitSource, null); compiled = options.outDir; await mkdir(path.join(compiled, 'public'), {recursive: true}); return compiled;},
      openBrowser: async () => ({close: async () => {closes += 1;}}),
      makeCancelSignal: () => ({cancel: () => {}, cancelSignal: {}}),
      selectComposition: async (options: {inputProps: Record<string, unknown>}) => ({...outputDimensions(options.inputProps.resolution as Resolution), fps: 30, durationInFrames: 90}),
      renderStill: async (options: {inputProps: Record<string, unknown>; output: string}) => {props.push(options.inputProps); await writeFile(options.output, 'PNG controlado');},
    } as unknown as SceneDependencies;
    const scene = createSceneRenderer(sdk);
    const requests = (['1080p', '4k'] as const).map((resolution) => parseRender({project_id: 'sansevieras',job_id: resolution,composition:'AgencyVertical',plan:'Ilimitado',resolution,props:{title:resolution,duration_seconds:3,audio_path:'proyectos/sansevieras/03_assets/voz.wav'}}));
    await Promise.all(requests.map(async (request) => scene({request,scratch:await space.job(),cacheRoot:space.root,browser,videoSource:source,signal:new AbortController().signal},[0])));
    assert.equal(builds, 1); assert.equal(closes, 2);
    assert.deepEqual(props.map((value) => value.resolution).sort(), ['1080p', '4k']);
    assert.notEqual(props[0]?.video_file, props[1]?.video_file);
    assert.deepEqual(await readdir(path.join(compiled, 'public/media')), ['.assets-owned']);
    await access(compiled); await access(source);
  } finally {await rm(root, {recursive:true,force:true});}
});

test('un fallo de captura cierra navegador y retira sólo el asset UUID del trabajo', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'render-cached-failure-'));
  try {
    const space = await ScratchSpace.create(root), browser = path.join(root, 'browser.exe'), source = path.join(root,'video.mov');
    await writeFile(browser,'navegador'); await writeFile(source,'video');
    let compiled = '', closed = false;
    const sdk = {
      bundle:async(options:{outDir:string}) => {compiled=options.outDir;await mkdir(path.join(compiled,'public'),{recursive:true});return compiled;},
      openBrowser:async() => ({close:async() => {closed=true;}}), makeCancelSignal:() => ({cancel:() => {},cancelSignal:{}}),
      selectComposition:async() => ({width:1080,height:1920,fps:30,durationInFrames:90}),
      renderStill:async() => {throw new Error('captura fallida visible');},
    } as unknown as SceneDependencies;
    const request = parseRender({project_id:'sansevieras',job_id:'fallido',composition:'AgencyVertical',plan:'Free',props:{title:'Prueba',duration_seconds:3,audio_path:'proyectos/sansevieras/03_assets/voz.wav'}});
    await assert.rejects(createSceneRenderer(sdk)({request,scratch:await space.job(),cacheRoot:space.root,browser,videoSource:source,signal:new AbortController().signal},[0]),/captura fallida/);
    assert.equal(closed,true); assert.deepEqual(await readdir(path.join(compiled,'public/media')),['.assets-owned']);
    await access(source); await access(compiled);
  } finally {await rm(root,{recursive:true,force:true});}
});

test('la secuencia cacheada limita 4K a un navegador y conserva fotogramas y metadatos', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'render-cached-frames-'));
  try {
    const space = await ScratchSpace.create(root), browser = path.join(root,'browser.exe'); await writeFile(browser,'navegador');
    let wrongSize = false, closed = 0;
    const sdk = {
      bundle:async(options:{outDir:string}) => {await mkdir(path.join(options.outDir,'public'),{recursive:true});return options.outDir;},
      openBrowser:async() => ({close:async() => {closed+=1;}}),makeCancelSignal:() => ({cancel:() => {},cancelSignal:{}}),
      selectComposition:async() => ({width:wrongSize ? 1080 : 2160,height:3840,fps:30,durationInFrames:90}),
      renderFrames:async(options:{composition:{width:number;height:number;durationInFrames:number};inputProps:Record<string,unknown>;concurrency:number}) => {assert.equal(options.composition.width,2160);assert.equal(options.composition.height,3840);assert.equal(options.inputProps.resolution,'4k');assert.equal(options.concurrency,1);return {frameCount:options.composition.durationInFrames};},
    } as unknown as SceneDependencies;
    const scene = createSceneRenderer(sdk), request = parseRender({project_id:'sansevieras',job_id:'secuencia',composition:'AgencyVertical',plan:'Ultra',resolution:'4k',props:{title:'4K',duration_seconds:3,audio_path:'proyectos/sansevieras/03_assets/voz.wav'}});
    const options = {request,cacheRoot:space.root,browser,videoSource:null,signal:new AbortController().signal};
    const result = await scene({...options,scratch:await space.job()},null);
    assert.equal(result.frameCount,90); assert.match(result.files[0]!,/frame-%02d\.png$/);
    wrongSize = true;
    await assert.rejects(scene({...options,scratch:await space.job()},null),/dimensiones/);
    assert.equal(closed,2);
  } finally {await rm(root,{recursive:true,force:true});}
});

test('el vigilante mide fotogramas nuevos y cancela solo una secuencia detenida', async (t) => {
  const root = await mkdtemp(path.join(tmpdir(), 'render-frame-progress-'));
  try {
    const space = await ScratchSpace.create(root), browser = path.join(root, 'browser.exe');
    await writeFile(browser, 'navegador controlado');
    t.mock.timers.enable({apis: ['setTimeout']});
    let cancelled = 0, closed = 0, hasProgress = true;
    const sdk = {
      bundle: async (options: {outDir: string}) => {
        await mkdir(path.join(options.outDir, 'public'), {recursive: true});
        return options.outDir;
      },
      openBrowser: async () => ({close: async () => {closed += 1;}}),
      makeCancelSignal: () => ({cancel: () => {cancelled += 1;}, cancelSignal: {}}),
      selectComposition: async () => ({width: 1080, height: 1920, fps: 30, durationInFrames: 90}),
      renderFrames: async ({onFrameUpdate}: {onFrameUpdate: (count: number) => void}) => {
        t.mock.timers.tick(89_000);
        onFrameUpdate(1);
        t.mock.timers.tick(89_000);
        onFrameUpdate(hasProgress ? 2 : 1);
        assert.equal(cancelled, 0);
        t.mock.timers.tick(2_000);
        if (cancelled) throw Error('renderFrames() got cancelled');
        return {frameCount: 90};
      },
    } as unknown as SceneDependencies;
    const scene = createSceneRenderer(sdk);
    const request = parseRender({project_id: 'sansevieras', job_id: 'avance', composition: 'AgencyVertical',
      plan: 'Ilimitado', props: {title: 'Avance de prueba', duration_seconds: 3, audio_path: 'proyectos/sansevieras/03_assets/voz.wav'}});
    const options = {request, cacheRoot: space.root, browser, videoSource: null, signal: new AbortController().signal};
    assert.equal((await scene({...options, scratch: await space.job()}, null)).frameCount, 90);
    t.mock.timers.tick(90_000);
    assert.equal(cancelled, 0);
    hasProgress = false;
    await assert.rejects(scene({...options, scratch: await space.job()}, null), {code: 'FOTOGRAMAS_SIN_AVANCE', status: 504});
    t.mock.timers.tick(90_000);
    assert.equal(cancelled, 1); assert.equal(closed, 2);
  } finally {await rm(root, {recursive: true, force: true});}
});

test('sólo el MP4 1080p con vídeo crudo de al menos 30 s usa JPEG', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'render-bounded-concurrency-'));
  try {
    const space = await ScratchSpace.create(root), browser = path.join(root, 'browser.exe');
    const source = path.join(root, 'insumo.mp4');
    await writeFile(browser, 'navegador controlado');
    await writeFile(source, 'vídeo controlado');
    const observed: [number, string, number | undefined][] = [];
    const sdk = {
      bundle: async (options: {outDir: string}) => {
        await mkdir(path.join(options.outDir, 'public'), {recursive: true});
        return options.outDir;
      },
      openBrowser: async () => ({close: async () => {}}),
      makeCancelSignal: () => ({cancel: () => {}, cancelSignal: {}}),
      selectComposition: async (options: {inputProps: {duration_seconds: number}}) => ({
        width: 1080, height: 1920, fps: 30, durationInFrames: options.inputProps.duration_seconds * 30,
      }),
      renderFrames: async (options: {concurrency: number; imageFormat: string; jpegQuality?: number; composition: {durationInFrames: number}}) => {
        observed.push([options.concurrency, options.imageFormat, options.jpegQuality]);
        return {frameCount: options.composition.durationInFrames};
      },
    } as unknown as SceneDependencies;
    const scene = createSceneRenderer(sdk);
    for (const [duration, videoSource, format] of [[29, source, 'mp4'], [60, null, 'mp4'], [60, source, 'mp4'], [60, source, 'prores4444']] as const) {
      const request = parseRender({
        project_id: 'sansevieras', job_id: 'sintetico', composition: 'AgencyVertical', plan: 'Ilimitado', format,
        props: {title: 'Ensayo', duration_seconds: duration, audio_path: 'proyectos/sansevieras/03_assets/voz.wav'},
      });
      const result = await scene({request, videoSource, scratch: await space.job(), cacheRoot: space.root, browser, signal: new AbortController().signal}, null);
      assert.match(result.files[0]!, duration === 60 && videoSource && format === 'mp4' ? /\.jpeg$/ : /\.png$/);
    }
    assert.deepEqual(observed, [[1, 'png', undefined], [1, 'png', undefined], [4, 'jpeg', 95], [4, 'png', undefined]]);
  } finally {await rm(root, {recursive: true, force: true});}
});
