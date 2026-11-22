import {after, mock, test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtemp, mkdir, writeFile, rm, access, readFile, stat} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {RenderBackend, stageAdMusic, timelineImages} from '../src/backend.ts';
import {outputDimensions, parseRender, parseStills, TITLE_FADE_FRAMES, type OutputFormat, type Resolution} from '../src/contracts.ts';
import type {RuntimeConfig} from '../src/config.ts';
import {ScratchSpace} from '../src/scratch.ts';
import {NasPaths} from '../src/nas.ts';

// Los medios de este archivo son sintéticos y ocupan sólo unos bytes.
mock.method(ScratchSpace.prototype, 'ensureSpace', async (bytes: number) => {
  assert.ok(Number.isFinite(bytes) && bytes >= 0);
});
after(() => mock.restoreAll());

test('el reel de 45 segundos mantiene los fotogramas transparentes sin superar el límite NTFS', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'rotulos-secundarios-'));
  try {
    const title = path.join(root, 'titulo.png'), transparent = path.join(root, 'transparente.png');
    await writeFile(title, 'título');
    await writeFile(transparent, 'alfa');
    const pattern = await timelineImages(root, [0, 90], [title, transparent], 1350, new AbortController().signal);
    const frame = (index: number) => pattern.replace('%d', String(index));
    assert.equal(await readFile(frame(89), 'utf8'), 'título');
    assert.equal(await readFile(frame(90), 'utf8'), 'alfa');
    assert.equal(await readFile(frame(1349), 'utf8'), 'alfa');
    for (const index of [90, 602, 1114, 1349]) assert.ok((await stat(frame(index))).nlink <= 513);
  } finally {
    await rm(root, {recursive: true, force: true});
  }
});

test('copia la pista aprobada por huella y rechaza una pista ausente o cambiada', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'musica-catalogo-'));
  try {
    const media = path.join(root,'nas'), work = path.join(root,'work');
    const library = path.join(media,'_biblioteca_global','musica-stock');
    await mkdir(library,{recursive:true}); await mkdir(work);
    const nas = await NasPaths.create(media);
    const source = path.join(library,'pista.mp3');
    const fixture = Buffer.alloc(2048, 75);
    const asset = {relative:'_biblioteca_global/musica-stock/pista.mp3',
      sha256:createHash('sha256').update(fixture).digest('hex')};
    const reserve = mock.fn(async (bytes: number) => {
      assert.equal(bytes, fixture.length);
    });
    const scratch = {ensureSpace:reserve};
    const signal = new AbortController().signal;
    await assert.rejects(stageAdMusic(nas,scratch,'sansevieras',work,signal,asset), {code:'MUSICA_NO_VERIFICADA'});
    await writeFile(source,fixture);
    const stamped = await stageAdMusic(nas,scratch,'sansevieras',work,signal,asset);
    assert.deepEqual(await readFile(stamped),fixture);
    assert.equal(reserve.mock.callCount(), 1);
    const secondWork = path.join(root,'segundo-trabajo');
    await mkdir(secondWork);
    await writeFile(source,Buffer.alloc(fixture.length,88));
    await assert.rejects(stageAdMusic(nas,scratch,'sansevieras',secondWork,signal,asset), {code:'MUSICA_NO_VERIFICADA'});
  } finally {await rm(root,{recursive:true,force:true});}
});

const request = parseRender({project_id: 'sansevieras', job_id: 'prueba', composition: 'AgencyVertical', plan: 'Free', props: {title: 'Prueba sintética', duration_seconds: 3, audio_path: 'proyectos/sansevieras/03_assets/voz.wav'}});
async function setup(badPeak: boolean, options: {format?: OutputFormat; resolution?: Resolution; badAlpha?: boolean; failProresProbe?: boolean; missingColor?: boolean; duration?: number; badTiming?: boolean; recoverPeak?: boolean; normalizedVideo?: boolean} = {}) {
  const root = await mkdtemp(path.join(tmpdir(), 'render-backend-'));
  const nas = path.join(root, 'nas'), cache = path.join(root, 'cache');
  await mkdir(path.join(nas, 'proyectos', 'sansevieras', '03_assets'), {recursive: true}); await mkdir(cache);
  await writeFile(path.join(nas, 'proyectos', 'sansevieras', '03_assets', 'voz.wav'), 'audio sintético controlado');
  if (options.normalizedVideo) {
    const raw = path.join(nas, 'proyectos', 'sansevieras', '01_raw');
    await mkdir(raw);
    await writeFile(path.join(raw, 'movil.mp4'), 'vídeo de entrada sintético');
  }
  const config: RuntimeConfig = {token: 'a'.repeat(40), ffmpeg: 'ffmpeg-controlado', ffprobe: 'ffprobe-controlado', browser: 'browser-controlado', cacheRoot: cache, nasRoot: nas};
  const dimensions = outputDimensions(options.resolution ?? '1080p');
  const prores = options.format === 'prores4444';
  const calls: string[][] = [];
  const packetTimeouts: number[] = [];
  const captures: (number[] | null)[] = [];
  const conformed: boolean[] = [];
  const duration = options.duration ?? 3;
  const count = duration * 30;
  const sourceCount = Math.floor(duration * 30000 / 1001);
  const backend = await RenderBackend.create(config, {
    verifyTools: async () => {}, verifyMapping: async () => {},
    scene: async (options, frames) => {
      captures.push(frames);
      conformed.push(options.preconformed === true);
      const files = (frames ?? [0]).map(frame => path.join(options.scratch, frames ? `still-${frame}.png` : 'frame-0.png'));
      for (const file of files) await writeFile(file, 'imagen');
      return {files, frameCount: frames?.length ?? count};
    },
    run: async (executable, args, processOptions) => {
      calls.push(args);
      if (executable === config.ffprobe) {
        const filename = path.basename(args.at(-1)!);
        if (options.normalizedVideo && filename === 'movil.mp4') {
          return {stdout: JSON.stringify({format:{duration},streams:[{codec_type:'video',codec_name:'hevc',
            width:1920,height:1080,pix_fmt:'yuv420p',color_space:'bt709',color_range:'tv',avg_frame_rate:'30000/1001',r_frame_rate:'30/1',
            start_pts:0,duration_ts:sourceCount*3003,time_base:'1/90000',disposition:{attached_pic:0},
            side_data_list:[{side_data_type:'Display Matrix',rotation:90,displaymatrix:'00000000: 0 -65536 0\n00000001: 65536 0 0\n00000002: 0 0 1073741824'}]}]}), stderr:''};
        }
        if (args.includes('-show_entries')) {
          packetTimeouts.push(processOptions.timeoutMs);
          if (options.normalizedVideo && filename === 'crudo.mp4') {
            const packets = Array.from({length:sourceCount}, (_, index) => ({pts:index * 3003, duration:3003}));
            return {stdout:JSON.stringify({streams:[{time_base:'1/90000'}],packets}), stderr:''};
          }
          const packets = Array.from({length: count}, (_, i) => ({pts: options.badTiming && i === 1 ? 0 : i * 512, duration: 512}));
          return {stdout: JSON.stringify({streams: [{time_base:'1/15360'}], packets}), stderr: ''};
        }
        return {stdout: JSON.stringify({format: {duration}, streams: [{codec_type:'video',codec_name:prores ? 'prores' : 'h264',profile:prores ? '4444' : 'High',codec_tag_string:prores ? 'ap4h' : 'avc1',...dimensions,nb_frames:count,avg_frame_rate:'30/1',color_space:'bt709',color_transfer:options.missingColor ? undefined : 'bt709',color_primaries:options.missingColor ? undefined : 'bt709',pix_fmt:prores ? 'yuva444p12le' : 'yuv420p'},{codec_type:'audio',codec_name:prores ? 'pcm_s24le' : 'aac',sample_rate:'48000'}]}), stderr: ''};
      }
      const output = args.at(-1)!;
      if (options.failProresProbe && args.includes('prores_ks')) throw new Error('Codificador ProRes no disponible.');
      if (output.endsWith('.mp4') || output.endsWith('.mov')) await writeFile(output, 'vídeo controlado');
      if (output.endsWith('.wav') && args.includes('-filter_complex')) await writeFile(output, 'audio por escenas');
      if (output.endsWith('.raw')) {const mask = Buffer.alloc(4096, 255); if (!options.badAlpha) mask.fill(0, 0, 2048); await writeFile(output, mask);}
      if (args.includes('-af')) {
        const firstMasterCheck = options.recoverPeak && output === '-'
          && args.some(value => path.basename(value) === 'master.mp4');
        return {stdout: '', stderr: JSON.stringify({input_i:'-14',input_tp:badPeak || firstMasterCheck ? '-0.5' : '-1.4',input_lra:'1',input_thresh:'-24',target_offset:'0'})};
      }
      return {stdout: '', stderr: ''};
    },
  });
  return {root, nas, backend, calls, captures, conformed, packetTimeouts};
}
test('salud no disponible antes de pruebas y publicación solo después de medición final aceptada', async () => {
  const fixture = await setup(false);
  try {
    assert.equal(fixture.backend.health().ready, false); await fixture.backend.initialize(); assert.equal(fixture.backend.health().nvenc_available, true);
    const result = await fixture.backend.render(request, new AbortController().signal);
    assert.equal(result.audio_verified, true); assert.equal(result.codec, 'h264_nvenc');
    assert.deepEqual(Object.keys(result.stage_ms), ['preparacion_audio', 'fotogramas', 'codificacion_y_control', 'publicacion_nas']);
    assert.ok(Object.values(result.stage_ms).every((ms) => Number.isInteger(ms) && ms >= 0));
    assert.ok(Object.values(result.stage_ms).reduce((sum, ms) => sum + ms, 0) <= result.elapsed_ms + 2);
    await access(path.join(fixture.nas, result.files[0]!.path));
    assert.equal(result.audio.true_peak_dbtp, -1.4);
  } finally {await rm(fixture.root, {recursive: true, force: true});}
});

test('renderiza una escaleta con dos fuentes verificadas sin volver a cortar el montaje', async () => {
  const fixture = await setup(false, {duration:30});
  const first = '11111111-1111-4111-8111-111111111111', second = '22222222-2222-4222-8222-222222222222';
  const raw = path.join(fixture.nas, 'proyectos', 'sansevieras', '01_raw');
  const assets = path.join(fixture.nas, 'proyectos', 'sansevieras', '03_assets');
  try {
    await mkdir(raw, {recursive:true});
    await writeFile(path.join(raw, `${first}.mp4`), 'primer video');
    await writeFile(path.join(raw, `${second}.mp4`), 'segundo video');
    await writeFile(path.join(assets, 'intake-source.wav'), 'primer audio');
    await writeFile(path.join(assets, `intake-source-${second}.wav`), 'segundo audio');
    const sources = [
      {id:first,video_path:`proyectos/sansevieras/01_raw/${first}.mp4`,audio_path:'proyectos/sansevieras/03_assets/intake-source.wav'},
      {id:second,video_path:`proyectos/sansevieras/01_raw/${second}.mp4`,audio_path:`proyectos/sansevieras/03_assets/intake-source-${second}.wav`},
    ];
    const payload = {project_id:'sansevieras',job_id:'multiple',composition:'AgencyVertical',plan:'Free',props:{
      title:'Montaje',duration_seconds:30,sources,scenes:[
        {source_id:second,source_start_frame:0,source_end_frame:300,label:{text:'Inicio',start_frame:0,end_frame:90,position:'top'}},
        {source_id:first,source_start_frame:0,source_end_frame:600},
      ],
    }};
    const edit = parseRender(payload);
    await fixture.backend.initialize();
    const result = await fixture.backend.render(edit, new AbortController().signal);
    assert.equal(result.audio_verified, true);
    const assembled = fixture.calls.find(args => path.basename(args.at(-1)!) === 'montaje-multiple.mp4');
    assert.ok(assembled?.some(arg => arg.endsWith(`${first}.mp4`)));
    assert.ok(assembled?.some(arg => arg.endsWith(`${second}.mp4`)));
    const audio = fixture.calls.find(args => args.at(-1)?.endsWith('audio.wav') && args.includes('-filter_complex'));
    assert.ok(audio?.some(arg => arg.endsWith(`intake-source-${second}.wav`)));
    const final = fixture.calls.find(args => path.basename(args.at(-1)!) === 'master.mp4');
    assert.ok(final?.some(arg => path.basename(arg) === 'montaje-multiple.mp4'));
    assert.ok(!final?.some(arg => arg.includes('trim=start_frame')));
    const preview = await fixture.backend.stills(parseStills({...payload,frames:[0,300]}), new AbortController().signal);
    assert.deepEqual(preview.frames, [0,300]);
    assert.equal(fixture.conformed.at(-1), true);
    assert.ok(fixture.calls.some(args => path.basename(args.at(-1)!) === 'vista-previa-multiple.mp4'));
  } finally { await rm(fixture.root, {recursive:true, force:true}); }
});

test('el ProRes por escenas codifica una vez cada estado del rótulo y sincroniza el MOV de 45 segundos', async () => {
  const fixture = await setup(false, {duration: 45, format: 'prores4444', resolution: '4k'});
  const edit = parseRender({project_id:request.project_id, job_id:'reel-alfa', composition:'AgencyVertical',
    plan:'Ilimitado', format:'prores4444', resolution:'4k', props:{...request.props, duration_seconds:45, scenes:[
      {source_start_frame:30, source_end_frame:390, label:{text:'Catálogo', start_frame:30, end_frame:105, position:'top'}},
      {source_start_frame:900, source_end_frame:1890},
    ]}});
  try {
    await fixture.backend.initialize();
    const result = await fixture.backend.render(edit, new AbortController().signal);
    assert.equal(result.alpha_verified, true);
    assert.deepEqual(fixture.captures.at(-1), [0, 30, 105]);
    const fragments = fixture.calls.filter(args => args.includes('prores_ks') && path.basename(args.at(-1)!).startsWith('prores-escena-'));
    assert.equal(fragments.length, 3);
    assert.ok(fragments.every(args => args[args.indexOf('-frames:v') + 1] === '1'));
    const master = fixture.calls.find(args => path.basename(args.at(-1)!) === 'master.mov');
    assert.equal(master?.[master.indexOf('-c:v') + 1], 'copy');
  } finally {await rm(fixture.root, {recursive:true, force:true});}
});
test('pico final fuera de límite registra la fase sin publicar ni exponer la ruta', async (t) => {
  const fixture = await setup(true);
  try {
    const warn = t.mock.method(console, 'warn', () => {});
    await fixture.backend.initialize(); await assert.rejects(fixture.backend.render(request, new AbortController().signal), /audio/);
    await assert.rejects(access(path.join(fixture.nas, 'proyectos', 'sansevieras', '06_renders', 'prueba')));
    assert.equal(warn.mock.callCount(), 1);
    const message = String(warn.mock.calls[0]?.arguments[0]);
    const entry = JSON.parse(message);
    assert.equal(entry.operation, 'render'); assert.equal(entry.phase, 'control');
    assert.equal(entry.error, 'AUDIO_FUERA_DE_OBJETIVO');
    assert.equal(entry.job, createHash('sha256').update('sansevieras/prueba').digest('hex').slice(0, 16));
    assert.ok(Number.isFinite(entry.elapsed_ms) && Number.isFinite(Date.parse(entry.utc)));
    assert.doesNotMatch(message, /sansevieras|voz\.wav|prueba/);
  } finally {await rm(fixture.root, {recursive: true, force: true});}
});

test('el límite global se distingue de un rechazo de herramienta', async (t) => {
  const fixture = await setup(false);
  try {
    await fixture.backend.initialize();
    t.mock.method(AbortSignal, 'timeout', (ms: number) => {
      assert.equal(ms, 1_200_000);
      return AbortSignal.abort(new DOMException('Venció el plazo', 'TimeoutError'));
    });
    t.mock.method(console, 'warn', () => {});
    await assert.rejects(fixture.backend.render(request, new AbortController().signal),
      {code: 'PLAZO_TRABAJO_AGOTADO', status: 504});
    await assert.rejects(access(path.join(fixture.nas, 'proyectos', 'sansevieras', '06_renders', 'prueba')));
  } finally {await rm(fixture.root, {recursive: true, force: true});}
});

test('corrige el audio del MP4 conservando el flujo NVENC y vuelve a medir antes de publicar', async () => {
  const fixture = await setup(false, {recoverPeak: true});
  try {
    await fixture.backend.initialize();
    const result = await fixture.backend.render(request, new AbortController().signal);
    const masters = fixture.calls.filter(args => args.includes('-c:v')
      && /^master(?:-corrected)?\.mp4$/.test(path.basename(args.at(-1)!)));
    assert.equal(masters.length, 2);
    assert.ok(masters[0]?.includes('h264_nvenc'));
    const correction = masters[1]!;
    assert.equal(correction[correction.indexOf('-c:v') + 1], 'copy');
    assert.ok(correction.some(value => path.basename(value) === 'master.mp4'));
    assert.ok(correction.some(value => value.includes('TP=-2')));
    assert.equal(result.audio.true_peak_dbtp, -1.4);
    assert.equal(path.basename(result.files[0]!.path), 'master-corrected.mp4');
  } finally {await rm(fixture.root, {recursive: true, force: true});}
});

test('el puente entrega HEVC 4K largo por capas y corrige el audio sin volver a codificar el vídeo', async () => {
  const duration_seconds = 130;
  const fixture = await setup(false,{duration:duration_seconds,resolution:'4k',normalizedVideo:true,recoverPeak:true});
  const long = parseRender({project_id:request.project_id, job_id:'hevc-largo', composition:'AgencyVertical', plan:'Ilimitado', resolution:'4k', props:{...request.props,
    duration_seconds, video_path:'proyectos/sansevieras/01_raw/movil.mp4'}});
  try {
    await fixture.backend.initialize();
    const result = await fixture.backend.render(long, new AbortController().signal);
    assert.equal(result.render_pipeline, 'capas_remotion_nvenc');
    assert.equal(result.media.duration_seconds, duration_seconds);
    assert.deepEqual(fixture.captures.at(-1), Array.from({length:13}, (_, index) => index));
    const masters = fixture.calls.filter(args => args.includes('-c:v') && path.basename(args.at(-1)!).startsWith('master'));
    assert.deepEqual(masters.map(args => args[args.indexOf('-c:v') + 1]), ['h264_nvenc','copy']);
    assert.ok(masters[1]?.some(value => value.includes('TP=-3.5')));
    assert.deepEqual(fixture.calls.filter(args => args.includes('-show_entries')).map(args => path.basename(args.at(-1)!)),
      ['crudo.mp4','master.mp4','master-corrected.mp4']);
    await access(path.join(fixture.nas, result.files[0]!.path));
  } finally {await rm(fixture.root, {recursive:true, force:true});}
});

test('repite el recibo de render sin recodificar y rechaza una solicitud cambiada', async () => {
  const fixture = await setup(false);
  try {
    await fixture.backend.initialize();
    const signal = new AbortController().signal;
    const first = await fixture.backend.render(request, signal);
    const calls = fixture.calls.length;
    assert.deepEqual(await fixture.backend.render(request, signal), first);
    assert.equal(fixture.calls.length, calls);
    await assert.rejects(fixture.backend.render({...request, props: {...request.props, title: 'Otro'}}, signal), /solicitud/);
    const source = path.join(fixture.nas, request.props.audio_path!);
    await writeFile(source, 'audio diferente');
    await assert.rejects(fixture.backend.render(request, signal), /solicitud/);
    await writeFile(source, 'audio sintético controlado');
    assert.equal(fixture.calls.length, calls);
    await writeFile(path.join(fixture.nas, first.files[0]!.path), 'archivo reemplazado');
    await assert.rejects(fixture.backend.render(request, signal), /recibo/);
    assert.equal(fixture.calls.length, calls);
  } finally {await rm(fixture.root, {recursive: true, force: true});}
});

test('un fallo de limpieza antes de confirmar el render permite reintentar la misma revisión', async (t) => {
  const fixture = await setup(false);
  try {
    await fixture.backend.initialize();
    const scratch = fixture.backend.scratch;
    const remove = scratch.remove.bind(scratch);
    let firstCleanup = true;
    t.mock.method(scratch, 'remove', async (job: string) => {
      await remove(job);
      if (firstCleanup) {
        firstCleanup = false;
        throw new Error('Fallo temporal de limpieza');
      }
    });

    const signal = new AbortController().signal;
    await assert.rejects(fixture.backend.render(request, signal), /Fallo temporal de limpieza/);
    const result = await fixture.backend.render(request, signal);
    await access(path.join(fixture.nas, result.files[0]!.path));
    assert.deepEqual(await fixture.backend.render(request, signal), result);
    await assert.rejects(fixture.backend.render({...request, props: {...request.props, title: 'Otro'}}, signal), /solicitud/);
  } finally {await rm(fixture.root, {recursive: true, force: true});}
});

const extendedInput = (format: OutputFormat, resolution: Resolution) => ({project_id: 'sansevieras', job_id: 'extendido', composition: 'AgencyVertical', plan: 'Ilimitado', format, resolution, props: {title: 'Prueba de formatos', duration_seconds: 3, audio_path: 'proyectos/sansevieras/03_assets/voz.wav'}});
const extendedRequest = (format: OutputFormat, resolution: Resolution) => parseRender(extendedInput(format, resolution));

test('4K se verifica en el archivo final y ProRes publica MOV con alfa medido sin atribuir NVENC', async () => {
  for (const format of ['mp4', 'prores4444'] as const) {
    const fixture = await setup(false, {format, resolution: '4k'});
    try {
      await fixture.backend.initialize();
      const result = await fixture.backend.render(extendedRequest(format, '4k'), new AbortController().signal);
      assert.equal(result.media.width, 2160); assert.equal(result.media.height, 3840);
      assert.equal(result.hardware_acceleration, format === 'mp4');
      assert.equal(result.codec, format === 'mp4' ? 'h264_nvenc' : 'prores_ks');
      assert.equal(result.audio_verified, true);
      assert.ok(result.files[0]!.path.endsWith(format === 'mp4' ? '.mp4' : '.mov'));
      if (format === 'prores4444') {assert.equal(result.alpha_verified, true); assert.equal(result.alpha?.alpha_min, 0);}
      await access(path.join(fixture.nas, result.files[0]!.path));
    } finally {await rm(fixture.root, {recursive: true, force: true});}
  }
});

test('la gráfica ProRes de 60 s publica solo después de comprobar 1800 marcas exactas', async () => {
  const input = extendedInput('prores4444', '4k');
  const request = parseRender({...input, props: {...input.props, duration_seconds: 60}});
  for (const badTiming of [false, true]) {
    const fixture = await setup(false, {format: 'prores4444', resolution: '4k', duration: 60, badTiming});
    try {
      await fixture.backend.initialize();
      const render = fixture.backend.render(request, new AbortController().signal);
      if (badTiming) {
        await assert.rejects(render, /marcas de tiempo/);
        await assert.rejects(access(path.join(fixture.nas, 'proyectos/sansevieras/06_renders/extendido')));
      } else {
        const result = await render;
        assert.equal(result.render_pipeline, 'prores_grafica_estatica');
        assert.equal(result.media.duration_seconds, 60);
        assert.equal(result.alpha_verified, true);
        assert.equal(result.audio_verified, true);
        assert.deepEqual(fixture.captures.at(-1), Array.from({length: TITLE_FADE_FRAMES + 1}, (_, frame) => frame));
        const mux = fixture.calls.find(call => call.includes('-bsf:v'));
        assert.equal(mux?.[mux.indexOf('-c:v') + 1], 'copy');
        const tail = fixture.calls.find(call => call.some(arg => arg.endsWith('prores-estatico.mov')));
        assert.equal(tail?.[tail.indexOf('-f') + 1], 'image2');
        await access(path.join(fixture.nas,result.files[0]!.path));
      }
      assert.deepEqual(fixture.packetTimeouts, [180_000]);
    } finally {await rm(fixture.root, {recursive: true, force: true});}
  }
});

test('un MOV con metadatos alfa pero píxeles opacos no se publica', async () => {
  const fixture = await setup(false, {format: 'prores4444', badAlpha: true});
  try {
    await fixture.backend.initialize();
    await assert.rejects(fixture.backend.render(extendedRequest('prores4444', '1080p'), new AbortController().signal), /alfa/);
    await assert.rejects(access(path.join(fixture.nas, 'proyectos/sansevieras/06_renders/extendido')));
  } finally {await rm(fixture.root, {recursive: true, force: true});}
});

test('capturas 4K alfa acreditan píxeles transparentes y dimensiones del pedido', async () => {
  const fixture = await setup(false, {format: 'prores4444', resolution: '4k'});
  try {
    await fixture.backend.initialize();
    const request = parseStills(extendedInput('prores4444', '4k'));
    const result = await fixture.backend.stills(request, new AbortController().signal);
    assert.equal(result.width, 2160); assert.equal(result.height, 3840); assert.equal(result.alpha_verified, true);
  } finally {await rm(fixture.root, {recursive: true, force: true});}
});

test('sonoridad fuera de objetivo también bloquea ProRes y el codificador debe probarse al iniciar', async () => {
  const fixture = await setup(true, {format: 'prores4444'});
  try {
    await fixture.backend.initialize();
    await assert.rejects(fixture.backend.render(extendedRequest('prores4444', '1080p'), new AbortController().signal), /audio/);
    await assert.rejects(access(path.join(fixture.nas, 'proyectos/sansevieras/06_renders/extendido')));
  } finally {await rm(fixture.root, {recursive: true, force: true});}
  const missing = await setup(false, {failProresProbe: true});
  try {await assert.rejects(missing.backend.initialize(), /ProRes/); assert.equal(missing.backend.health().ready, false);}
  finally {await rm(missing.root, {recursive: true, force: true});}
});

test('la ausencia de color no se relaja y el rechazo identifica campos sin publicar datos privados', async () => {
  const fixture = await setup(false,{missingColor:true});
  try {
    await fixture.backend.initialize();
    await assert.rejects(fixture.backend.render(request,new AbortController().signal),/color_transfer, color_primaries/);
    await assert.rejects(access(path.join(fixture.nas,'proyectos/sansevieras/06_renders/prueba')));
  } finally {await rm(fixture.root,{recursive:true,force:true});}
});
