import assert from 'node:assert/strict';
import test from 'node:test';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {alphaMaskArgs, alphaMaskSummary, audioPass, cutSceneAudio, cutMultiSourceAudio, multiSourceVideoInputs, correctMp4Audio, encodeNvenc, encodeOutput, encodeProresFragment, measureAudio, overlayInputSupported, overlayPacketTimingSupported, parseLoudness, parseProbe, audioAccepted, outputAccepted, outputViolations} from '../src/media.ts';
import {parseRender} from '../src/contracts.ts';
import {graphicFrames, graphicLabel} from '../src/timeline.ts';

const measured = {integrated_lufs: -21, true_peak_dbtp: -4, range_lu: 2, threshold_lufs: -31, offset_lu: 0.01};

test('codifica una secuencia numerada desde uno en ProRes y conserva alfa real',
  {skip: spawnSync('ffmpeg', ['-version']).status !== 0}, () => {
    const dir = mkdtempSync(join(tmpdir(), 'studio-prores-alpha-'));
    try {
      const pattern = join(dir, 'frame_%06d.png'), output = join(dir, 'graphics.mov');
      const images = spawnSync('ffmpeg', ['-v','error','-f','lavfi','-i','nullsrc=s=64x64:r=30',
        '-vf','format=rgba,colorchannelmixer=aa=0,drawbox=x=0:y=0:w=32:h=64:color=red@1:t=fill:replace=1',
        '-frames:v','2','-threads','1','-start_number','1',pattern]);
      assert.equal(images.status, 0, images.stderr.toString());
      const encoded = spawnSync('ffmpeg', encodeProresFragment(pattern, output, 2, 1));
      assert.equal(encoded.status, 0, encoded.stderr.toString());
      const alpha = spawnSync('ffmpeg', ['-v','error','-i',output,'-vf','alphaextract',
        '-frames:v','2','-pix_fmt','gray','-f','rawvideo','-'], {maxBuffer:65536});
      assert.equal(alpha.status, 0, alpha.stderr.toString());
      assert.equal(alpha.stdout.length, 64 * 64 * 2);
      for (let frame=0; frame<2; frame++) {
        const pixels = alpha.stdout.subarray(frame * 4096, (frame + 1) * 4096);
        assert.ok(pixels.some(value => value < 10), 'debe conservar áreas transparentes');
        assert.ok(pixels.some(value => value > 245), 'debe conservar áreas opacas');
      }
    } finally {rmSync(dir, {recursive:true, force:true});}
  });

test('concatena audio real de dos fuentes y conserva su orden', {skip: spawnSync('ffmpeg', ['-version']).status !== 0}, () => {
  const dir = mkdtempSync(join(tmpdir(), 'studio-audio-'));
  try {
    const first = join(dir, 'primero.wav'), second = join(dir, 'segundo.wav'), output = join(dir, 'montaje.wav');
    for (const [frequency, file] of [[400, first], [800, second]] as const) {
      const generated = spawnSync('ffmpeg', ['-v','error','-f','lavfi','-i',`sine=frequency=${frequency}:sample_rate=48000:duration=2`, '-y',file]);
      assert.equal(generated.status, 0, generated.stderr.toString());
    }
    const scenes = [{source_id:'b',source_start_frame:0,source_end_frame:30}, {source_id:'a',source_start_frame:0,source_end_frame:30}];
    const rendered = spawnSync('ffmpeg', cutMultiSourceAudio(new Map([['a', first], ['b', second]]), output, scenes));
    assert.equal(rendered.status, 0, rendered.stderr.toString());
    const decoded = spawnSync('ffmpeg', ['-v','error','-i',output,'-ac','1','-f','f32le','-'], {maxBuffer: 1024 * 1024});
    assert.equal(decoded.status, 0, decoded.stderr.toString());
    assert.equal(decoded.stdout.length, 2 * 48000 * 4);
    const crossings = (offset: number) => {
      let count = 0, previous = decoded.stdout.readFloatLE(offset * 4);
      for (let i = offset + 1; i < offset + 48000; i += 1) {
        const next = decoded.stdout.readFloatLE(i * 4);
        if (previous < 0 && next >= 0) count += 1;
        previous = next;
      }
      return count;
    };
    assert.ok(crossings(0) > 750 && crossings(0) < 850);
    assert.ok(crossings(48000) > 350 && crossings(48000) < 450);
    const withSilence = join(dir, 'silencio.wav');
    const silent = spawnSync('ffmpeg', cutMultiSourceAudio(new Map([['a', first], ['b', null]]), withSilence, scenes));
    assert.equal(silent.status, 0, silent.stderr.toString());
    const samples = spawnSync('ffmpeg', ['-v','error','-i',withSilence,'-ac','1','-f','f32le','-'], {maxBuffer: 1024 * 1024});
    assert.equal(samples.status, 0, samples.stderr.toString());
    assert.ok(samples.stdout.subarray(0, 48000 * 4).every(byte => byte === 0));
    assert.ok(samples.stdout.subarray(48000 * 4).some(byte => byte !== 0));
    assert.throws(() => cutMultiSourceAudio(new Map([['a',first]]), output, scenes));
  } finally { rmSync(dir, {recursive:true, force:true}); }
});

test('une video real de fuentes con geometrías distintas en el orden elegido', {skip: spawnSync('ffmpeg', ['-version']).status !== 0}, () => {
  const dir = mkdtempSync(join(tmpdir(), 'studio-video-'));
  try {
    const red = join(dir, 'rojo.mkv'), blue = join(dir, 'azul.mkv');
    for (const [color, size, file] of [['red','320x180',red], ['blue','180x320',blue]] as const) {
      const result = spawnSync('ffmpeg', ['-v','error','-f','lavfi','-i',`color=c=${color}:s=${size}:r=30:d=1`, '-c:v','ffv1','-y',file]);
      assert.equal(result.status, 0, result.stderr.toString());
    }
    const scenes = [
      {source_id:'b',source_start_frame:0,source_end_frame:15,
        label:{text:'Plantas',start_frame:0,end_frame:15,position:'panel' as const}},
      {source_id:'a',source_start_frame:0,source_end_frame:15},
    ];
    const montage = multiSourceVideoInputs(new Map([['a',red], ['b',blue]]), scenes, 180, 320);
    const rendered = spawnSync('ffmpeg', ['-v','error','-filter_complex_threads','1',...montage.videoInputs,
      '-filter_complex',montage.cuts.join(';'),'-map','[montaje]','-pix_fmt','rgb24','-f','rawvideo','-'], {maxBuffer: 8 * 1024 * 1024});
    assert.equal(rendered.status, 0, rendered.stderr.toString());
    const frameBytes = 180 * 320 * 3;
    assert.equal(rendered.stdout.length, frameBytes * 30);
    const margin = (10 * 180 + 90) * 3;
    const center = (180 * 180 + 90) * 3;
    const panel = rendered.stdout.subarray(margin, margin + 3);
    const first = rendered.stdout.subarray(center, center + 3);
    const last = rendered.stdout.subarray(15 * frameBytes, 15 * frameBytes + 3);
    assert.ok(panel.every(channel => channel < 30), 'el panel debe quedar fuera de la imagen');
    assert.ok(first[2]! > 200 && first[0]! < 30);
    assert.ok(last[0]! > 200 && last[2]! < 30);
    assert.throws(() => multiSourceVideoInputs(new Map([['a',red]]), scenes, 180, 320));
  } finally { rmSync(dir, {recursive:true, force:true}); }
});

test('procesa más de doce escenas, incluidas tomas breves, de varias fuentes en audio y video', {skip: spawnSync('ffmpeg', ['-version']).status !== 0}, () => {
  const dir = mkdtempSync(join(tmpdir(), 'studio-escenas-largas-'));
  try {
    const video = join(dir, 'source.mkv'), audio = join(dir, 'source.wav');
    const picture = spawnSync('ffmpeg', ['-v','error','-f','lavfi','-i','color=c=red:s=32x32:r=30:d=1','-c:v','ffv1',video]);
    assert.equal(picture.status, 0, picture.stderr.toString());
    const sound = spawnSync('ffmpeg', ['-v','error','-f','lavfi','-i','sine=frequency=400:sample_rate=48000:duration=1',audio]);
    assert.equal(sound.status, 0, sound.stderr.toString());
    const scenes = Array.from({length: 13}, (_, i) => ({source_id: i % 2 ? 'a' : 'b', source_start_frame: 0, source_end_frame: i === 0 ? 9 : 15}));
    const montage = multiSourceVideoInputs(new Map([['a',video], ['b',video]]), scenes, 32, 32);
    const rendered = spawnSync('ffmpeg', ['-v','error','-filter_complex_threads','1',...montage.videoInputs,
      '-filter_complex',montage.cuts.join(';'),'-map','[montaje]','-f','null','-']);
    assert.equal(rendered.status, 0, rendered.stderr.toString());
    const mixed = spawnSync('ffmpeg', cutMultiSourceAudio(new Map([['a',audio], ['b',audio]]), join(dir, 'mixed.wav'), scenes));
    assert.equal(mixed.status, 0, mixed.stderr.toString());
    assert.throws(() => multiSourceVideoInputs(new Map([['a',video], ['b',video]]), [...scenes, ...scenes, ...scenes], 32, 32));
  } finally { rmSync(dir, {recursive:true, force:true}); }
});

test('los cortes mantienen la sincronía de audio y los rótulos desaparecen en su límite', () => {
  const scenes = [
    {source_start_frame: 30, source_end_frame: 390, label: {text: 'Inicio', start_frame: 0, end_frame: 90, position: 'bottom' as const}},
    {source_start_frame: 600, source_end_frame: 1320},
  ];
  assert.deepEqual(graphicFrames(scenes), [0, 90]);
  assert.equal(graphicLabel(scenes, 89)?.text, 'Inicio');
  assert.equal(graphicLabel(scenes, 90), undefined);
  assert.equal(graphicLabel(scenes, 360), undefined);
  const edit = encodeNvenc('rotulos%d.png', 'cortes.wav', 'reel.mp4', 1080, 36, measured, -1.5, '1080p', 'fuente.mp4', false, scenes);
  const filter = edit[edit.indexOf('-filter_complex') + 1]!;
  assert.match(filter, /trim=start_frame=30:end_frame=390/);
  assert.match(filter, /trim=start_frame=30:end_frame=750/);
  assert.match(filter, /\[plano0\]\[plano1\]concat=n=2:v=1:a=0/);
  assert.deepEqual(edit.filter(arg => arg === 'fuente.mp4').length, 2);
  const audio = cutSceneAudio('fuente.wav', 'cortes.wav', scenes);
  assert.match(audio[audio.indexOf('-filter_complex') + 1]!, /atrim=start_sample=48000:end_sample=624000/);
  assert.match(audio[audio.indexOf('-filter_complex') + 1]!, /atrim=start_sample=960000:end_sample=2112000/);
});
test('el audio alternativo conserva la duración del corte de imagen', () => {
  const args = cutSceneAudio('fuente.wav','cortes.wav',[
    {source_start_frame:200,source_end_frame:260,audio_source_start_frame:40},
    {source_start_frame:360,source_end_frame:420},
  ]);
  const filter = args[args.indexOf('-filter_complex') + 1]!;
  assert.match(filter, /atrim=start_sample=64000:end_sample=160000/);
  assert.match(filter, /atrim=start_sample=576000:end_sample=672000/);
});
test('la medición usa entrada real, no valores previstos de salida', () => {
  const result = parseLoudness('aviso\n' + JSON.stringify({input_i: '-14.20', input_tp: '-1.30', input_lra: '1.10', input_thresh: '-24.20', target_offset: '0.10', output_i: '-2.00'}));
  assert.equal(result.integrated_lufs, -14.2);
  assert.equal(audioAccepted(result), true);
  assert.equal(audioAccepted({...result, true_peak_dbtp: -0.9}), false);
  assert.throws(() => parseLoudness(JSON.stringify({input_i: '-inf'})));
});
test('NVENC, BT.709 y audio en dos pasadas son obligatorios en el comando', () => {
  const args = encodeNvenc('frames/frame-%03d.png', 'voice.wav', 'master.mp4', 90, 3, measured);
  assert.equal(args[args.indexOf('-c:v') + 1], 'h264_nvenc');
  assert.equal(args[args.indexOf('-color_trc') + 1], 'bt709');
  assert.ok(args[args.indexOf('-af') + 1]?.includes('measured_I=-21'));
  assert.ok(args[args.indexOf('-af') + 1]?.includes('TP=-1.5'));
  assert.ok(measureAudio('voz.wav', 3).includes('0:a:0'));
  assert.ok(audioPass(3).includes('apad='));
});
test('la capa de la banda conserva el cuadro completo y las dimensiones de salida', () => {
  for (const [resolution, width, height, band] of [['1080p', 1080, 1920, 264], ['4k', 2160, 3840, 528]] as const) {
    const args = encodeNvenc('frames/still-%d.png', 'voz.wav', 'kit.mp4', 1800, 60, measured, -1.5,
      resolution, 'crudo.mp4', false, undefined, undefined, false, 'band');
    const filter = args[args.indexOf('-filter_complex') + 1]!;
    assert.ok(filter.includes(`scale=${width}:${height-band}:force_original_aspect_ratio=decrease`));
    assert.ok(filter.includes(`pad=${width}:${height}:trunc((ow-iw)/4)*2:trunc((oh-ih+${band})/4)*2`));
    assert.equal(args[args.indexOf('-frames:v') + 1], '1800');
    assert.equal(args[args.indexOf('-c:v') + 1], 'h264_nvenc');
  }
});

test('la secuencia JPEG convierte su matriz BT.601 a BT.709 antes de NVENC', () => {
  const args = encodeNvenc('frames/frame-%04d.jpeg', 'voice.wav', 'master.mp4', 1800, 60, measured);
  assert.match(args[args.indexOf('-vf') + 1]!, /in_range=pc:out_range=tv:in_color_matrix=bt601:out_color_matrix=bt709/);
  assert.equal(args[args.indexOf('-c:v') + 1], 'h264_nvenc');
});
test('la composición por capas admite H.264 progresivo de 30 fps con geometría y color verificados', () => {
  const request = parseRender({project_id:'sansevieras',job_id:'capas',composition:'AgencyVertical',plan:'Free',props:{title:'Capas',duration_seconds:60,video_path:'proyectos/sansevieras/01_raw/video.mp4',audio_path:'proyectos/sansevieras/03_assets/voz.wav'}});
  const video = {
    codec_type:'video',codec_name:'h264',pix_fmt:'yuv420p',field_order:'progressive',width:720,height:1280,
    sample_aspect_ratio:'1:1',color_space:'bt709',color_range:'tv',r_frame_rate:'30/1',avg_frame_rate:'30/1',
    start_pts:0,nb_frames:'1800',duration_ts:921600,time_base:'1/15360',disposition:{attached_pic:0},
  };
  const probe = (changes = {}) => JSON.stringify({streams:[{...video,...changes}]});
  assert.ok(overlayInputSupported(probe(),request));
  assert.ok(overlayInputSupported(probe({width:960,height:540}),request));
  assert.ok(overlayInputSupported(probe({width:1280,height:720}),request));
  for (const changes of [
    {sample_aspect_ratio:'4:3'}, {field_order:'tt'}, {avg_frame_rate:'30000/1001'}, {duration_ts:921088},
    {nb_frames:'1799'}, {start_pts:512}, {color_transfer:'smpte2084'}, {width:1000}, {width:721},
    {side_data_list:[{side_data_type:'Display Matrix',rotation:90}]}, {tags:{rotate:'90'}}, {disposition:{attached_pic:1}},
  ]) assert.equal(overlayInputSupported(probe(changes),request),false);
  assert.ok(overlayInputSupported(probe(), {...request, plan:'Pro', watermark:false}));
  const ultra = parseRender({project_id:'sansevieras',job_id:'capas-ultra',composition:'AgencyVertical',
    plan:'Ultra',format:'mp4',resolution:'4k',props:request.props});
  assert.ok(overlayInputSupported(probe(), ultra));
  assert.ok(overlayInputSupported(probe({width:2160,height:3840}), ultra));
  assert.ok(overlayInputSupported(probe({width:2160,height:3840}), {...ultra,plan:'Ilimitado'}));
  assert.ok(overlayInputSupported(probe({width:2160,height:3840}),request));
  assert.ok(overlayInputSupported(probe({width:3840,height:2160}), {...request, plan:'Pro', watermark:false}));
  assert.equal(overlayInputSupported(probe({width:4320,height:7680}),request),false);
  assert.equal(overlayInputSupported(probe(),{...ultra,format:'prores4444',transparent:true}),false);
  assert.equal(overlayInputSupported(probe({color_primaries:'bt2020'}),ultra),false);
  assert.equal(overlayInputSupported(probe({nb_frames:'6000',duration_ts:3072000}),{...ultra,durationInFrames:6000}),'exact');
  assert.equal(overlayInputSupported(probe({nb_frames:'18001',duration_ts:9216512}),{...ultra,durationInFrames:18001}),false);
  assert.ok(overlayInputSupported(probe({nb_frames:'120',duration_ts:61440}),{...ultra,durationInFrames:120}));
  assert.equal(overlayInputSupported(probe({nb_frames:'12',duration_ts:6144}),{...ultra,durationInFrames:12}),false);
  assert.equal(overlayInputSupported(probe(),{...request,durationInFrames:1799}),false);
});
test('la codificación por capas usa el vídeo, prolonga el último PNG y conserva la pista normalizada', () => {
  const args = encodeOutput('mp4','1080p','still-%d.png','voz.wav','master.mp4',1800,60,measured,-1.5,'crudo.mp4');
  const inputs = args.flatMap((value,index) => value === '-i' ? [args[index+1]] : []);
  assert.deepEqual(inputs,['crudo.mp4','still-%d.png','voz.wav']);
  const filter = args[args.indexOf('-filter_complex')+1]!;
  assert.match(filter, /in_color_matrix=bt709:out_color_matrix=bt709:in_range=tv:out_range=tv,crop=1080:1920,setsar=1,format=yuv420p\[video\]/);
  assert.match(filter, /\[1:v\]scale=in_range=pc:out_range=tv:out_color_matrix=bt709,format=yuva420p\[capa\]/);
  assert.match(filter, /overlay=format=yuv420:alpha=straight:eof_action=repeat:repeatlast=1,setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709/);
  assert.ok(args.includes('2:a:0'));
  assert.equal(args[args.indexOf('-frames:v')+1],'1800');
  assert.equal(args[args.indexOf('-c:v')+1],'h264_nvenc');
});
test('la composición 4K conserva el vídeo en YUV BT.709 y convierte la capa transparente', () => {
  const args = encodeOutput('mp4','4k','still-%d.png','voz.wav','master.mp4',1800,60,measured,-1.5,'crudo.mp4');
  const filter = args[args.indexOf('-filter_complex')+1]!;
  assert.match(filter, /in_color_matrix=bt709:out_color_matrix=bt709:in_range=tv:out_range=tv,crop=2160:3840,setsar=1,format=yuv420p\[video\]/);
  assert.match(filter, /\[1:v\]scale=in_range=pc:out_range=tv:out_color_matrix=bt709,format=yuva420p\[capa\]/);
  assert.match(filter, /overlay=format=yuv420:alpha=straight:eof_action=repeat:repeatlast=1,setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709/);
  assert.equal(args[args.indexOf('-c:v')+1],'h264_nvenc');
});
test('la composición por capas verifica el tiempo de presentación de todos los fotogramas', () => {
  const probe = (positions: number[]) => JSON.stringify({
    streams:[{time_base:'1/15360'}],
    packets:positions.map(pts => ({pts,duration:512})),
  });
  assert.equal(overlayPacketTimingSupported(probe([0,1024,512]),3),true);
  assert.equal(overlayPacketTimingSupported(probe([0,1024,480]),3),false);
  assert.equal(overlayPacketTimingSupported(probe([0,1024,1024]),3),false);
  assert.equal(overlayPacketTimingSupported(probe([0,512]),3),false);
});
test('un MP4 HEVC vertical de celular y seis minutos usa capas con cadencia normalizada', () => {
  const request = parseRender({project_id:'sansevieras',job_id:'capas-largas',composition:'AgencyVertical',plan:'Ilimitado',
    resolution:'4k',props:{title:'Capas',duration_seconds:367.80225,video_path:'proyectos/sansevieras/01_raw/crudo.mp4',audio_path:'proyectos/sansevieras/03_assets/voz.wav'}});
  const video = {codec_type:'video',codec_name:'hevc',pix_fmt:'yuv420p',width:1920,height:1080,
    color_range:'tv',color_space:'bt709',color_primaries:'bt709',color_transfer:'bt709',
    r_frame_rate:'30/1',avg_frame_rate:'987660000/33088421',start_pts:0,duration_ts:33085494,time_base:'1/90000',
    nb_frames:'10974',disposition:{attached_pic:0},side_data_list:[{side_data_type:'Display Matrix',rotation:90,
      displaymatrix:'\n00000000: 0 -65536 0\n00000001: 65536 0 0\n00000002: 0 0 1073741824\n'}]};
  const probe = (changes: Record<string, unknown> = {}) => JSON.stringify({streams:[{...video,...changes}]});
  assert.equal(overlayInputSupported(probe(), request), 'normalized');
  assert.equal(overlayInputSupported(probe({sample_aspect_ratio:'4:3'}), request), false);
  assert.equal(overlayInputSupported(probe({duration_ts:25200000}), request), false);
  assert.equal(overlayInputSupported(probe({start_pts:1}), request), false);
  assert.equal(overlayInputSupported(probe({color_transfer:'smpte2084'}), request), false);
  assert.equal(overlayInputSupported(probe({side_data_list:[{...video.side_data_list[0],rotation:17}]}), request), false);
  assert.equal(overlayInputSupported(probe({side_data_list:[{...video.side_data_list[0],displaymatrix:'00000000: 0 65536 0'}]}), request), false);
});

test('en capas normalizadas acepta la cadencia variable con final cercano y rechaza discontinuidades extensas', () => {
  const packets = Array.from({length: 115}, (_, index) => ({pts:index * 3060,duration:3060}));
  const probe = (values = packets) => JSON.stringify({streams:[{time_base:'1/90000'}],packets:values.reverse()});
  assert.equal(overlayPacketTimingSupported(probe([...packets]),120,true),true);
  assert.equal(overlayPacketTimingSupported(probe([...packets]),120),false);
  assert.equal(overlayPacketTimingSupported(probe([packets[0]!, ...packets.slice(72)]),120,true),false);
  assert.equal(overlayPacketTimingSupported(probe([...packets,packets[0]!]),120,true),false);
  assert.equal(overlayPacketTimingSupported(probe([...packets]),180,true),false);
});

test('el render por capas normalizado completa el final con un margen de un segundo', () => {
  const args = encodeOutput('mp4','4k','still-%d.png','voz.wav','master.mp4',11034,367.8,measured,-1.5,'crudo.mp4',true);
  assert.match(args[args.indexOf('-filter_complex')+1]!, /fps=fps=30:start_time=0,tpad=stop_mode=clone:stop_duration=1,scale=2160:3840/);
  assert.equal(args[args.indexOf('-frames:v')+1], '11034');
});
test('ffprobe devuelve únicamente metadatos técnicos y rechaza duración desconocida', () => {
  const info = parseProbe(JSON.stringify({format: {duration: '3.0', filename: 'no-publicar'}, streams: [{codec_type:'video', codec_name:'h264', width:1080,height:1920,avg_frame_rate:'30/1'},{codec_type:'audio',codec_name:'aac',sample_rate:'48000'}]}));
  assert.equal(info.duration_seconds, 3);
  assert.equal(info.width, 1080);
  assert.equal(info.has_audio, true);
  assert.equal('filename' in info, false);
  assert.throws(() => parseProbe('{"format":{"duration":"N/A"},"streams":[]}'));
});

test('4K MP4 usa nivel y tasa adecuados; ProRes usa 4444 alfa y PCM sin atribuirse NVENC', () => {
  const mp4 = encodeOutput('mp4', '4k', 'frames.png', 'audio.wav', 'master.mp4', 90, 3, measured);
  assert.equal(mp4[mp4.indexOf('-c:v') + 1], 'h264_nvenc');
  assert.equal(mp4[mp4.indexOf('-level:v') + 1], '5.1');
  assert.equal(mp4[mp4.indexOf('-b:v') + 1], '24M');
  const prores = encodeOutput('prores4444', '4k', 'frames.png', 'audio.wav', 'master.mov', 90, 3, measured);
  assert.equal(prores[prores.indexOf('-c:v') + 1], 'prores_ks');
  assert.equal(prores[prores.indexOf('-profile:v') + 1], '4444');
  assert.equal(prores[prores.indexOf('-alpha_bits') + 1], '16');
  assert.equal(prores[prores.indexOf('-c:a') + 1], 'pcm_s24le');
  assert.ok(prores[prores.indexOf('-vf') + 1]?.includes('yuva444p10le'));
  assert.ok(prores[prores.indexOf('-af') + 1]?.includes('measured_I=-21'));
  assert.ok(!prores.includes('h264_nvenc'));
});

test('el contrato final valida dimensiones 4K, perfil 4444, alfa y audio del MOV', () => {
  const request = parseRender({project_id: 'sansevieras', job_id: 'alfa', composition: 'AgencyVertical', plan: 'Ilimitado', format: 'prores4444', resolution: '4k', props: {title: 'Alfa', duration_seconds: 3, audio_path: 'proyectos/sansevieras/03_assets/voz.wav'}});
  const info = parseProbe(JSON.stringify({format: {duration: '3'}, streams: [{codec_type:'video',codec_name:'prores',profile:'4444',codec_tag_string:'ap4h',width:2160,height:3840,avg_frame_rate:'30/1',pix_fmt:'yuva444p12le',color_space:'bt709',color_transfer:'bt709',color_primaries:'bt709'},{codec_type:'audio',codec_name:'pcm_s24le',sample_rate:'48000'}]}));
  assert.equal(outputAccepted(info, request), true);
  for (const change of [{width: 1080}, {pixel_format: 'yuv444p12le'}, {video_profile: 'HQ'}, {codec_tag: 'apch'}, {audio_codec: 'aac'}, {sample_rate: 44100}, {fps: 25}, {duration_seconds: 4}]) {
    assert.equal(outputAccepted({...info, ...change}, request), false);
  }
});

test('alfa se mide en píxeles decodificados y rechaza un plano opaco o truncado', () => {
  const mask = Buffer.alloc(4096, 255); mask.fill(0, 0, 2048);
  const result = alphaMaskSummary(mask);
  assert.equal(result.has_transparency, true); assert.equal(result.alpha_min, 0); assert.equal(result.alpha_max, 255);
  assert.equal(result.transparent_pixels, 2048);
  assert.equal(alphaMaskSummary(Buffer.alloc(4096, 255)).has_transparency, false);
  assert.equal(alphaMaskSummary(Buffer.alloc(4096, 254)).has_transparency, false);
  assert.throws(() => alphaMaskSummary(Buffer.alloc(4095)));
  const args = alphaMaskArgs('master.mov', 'alpha.raw', 1.5);
  assert.ok(args.includes('alphaextract,scale=64:64:flags=area,format=gray'));
  assert.equal(args[args.indexOf('-ss') + 1], '1.5');
});

test('PNG sin etiquetas requiere señalización BT709 en cada frame y no se aceptan primarias/transfer ausentes', () => {
  const request = parseRender({project_id:'sansevieras',job_id:'bt709',composition:'AgencyVertical',plan:'Free',props:{title:'SDR',duration_seconds:3,audio_path:'proyectos/sansevieras/03_assets/tone.wav'}});
  // Forma real observada con FFmpeg9/NVENC: la matriz existe y las otras dos etiquetas faltan.
  const info = parseProbe(JSON.stringify({format:{duration:'3'},streams:[{codec_type:'video',codec_name:'h264',width:1080,height:1920,avg_frame_rate:'30/1',pix_fmt:'yuv420p',color_space:'bt709'},{codec_type:'audio',codec_name:'aac',sample_rate:'48000'}]}));
  assert.equal(outputAccepted(info,request),false);
  assert.deepEqual(outputViolations(info,request),['color_transfer','color_primaries']);
  for (const format of ['mp4','prores4444'] as const) {
    const args = encodeOutput(format,'1080p','frames.png','audio.wav',format==='mp4'?'video.mp4':'video.mov',90,3,measured);
    const filters = args[args.indexOf('-vf')+1]!;
    assert.ok(filters.includes('setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709'));
  }
  assert.equal(outputAccepted({...info,color_transfer:'bt709',color_primaries:'bt709'},request),true);
});

test('los dos pases miden y normalizan estéreo48k antes del limitador, en MP4 y ProRes', () => {
  const preparation = 'aformat=sample_rates=48000:channel_layouts=stereo,';
  for (const duration of [1,3]) {
    const measurement = measureAudio('mono-22050.wav',duration);
    const firstFilter = measurement[measurement.indexOf('-af')+1]!;
    assert.ok(firstFilter.startsWith(preparation));
    assert.ok(firstFilter.indexOf('apad=') < firstFilter.indexOf('loudnorm='));
    assert.ok(firstFilter.indexOf('atrim=') < firstFilter.indexOf('loudnorm='));
    for (const format of ['mp4','prores4444'] as const) {
      const encoded = encodeOutput(format,'1080p','frames.png','mono-22050.wav','output',duration*30,duration,measured);
      const secondFilter = encoded[encoded.indexOf('-af')+1]!;
      assert.ok(secondFilter.startsWith(preparation));
      assert.equal(secondFilter.slice(0,secondFilter.indexOf('loudnorm=')),firstFilter.slice(0,firstFilter.indexOf('loudnorm=')));
      assert.equal(encoded[encoded.indexOf('-ar')+1],'48000');
      assert.equal(encoded[encoded.indexOf('-ac')+1],'2');
    }
  }
});

test('el anuncio añade el crédito de catálogo y la corrección de picos hereda sus metadatos', () => {
  const id = 'carefree-usuan1400037';
  const master = encodeNvenc('frames.png','mix.wav','master.mp4',900,30,measured,-1.5,'1080p',undefined,false,undefined,id);
  const metadata = master.indexOf('-metadata');
  assert.notEqual(metadata,-1);
  assert.equal(master[metadata + 1],'comment=' + id);
  const secondPass = correctMp4Audio('master.mp4','mix.wav','master-corrected.mp4',900,30,measured);
  const source = secondPass.indexOf('-map_metadata');
  assert.notEqual(source,-1);
  assert.equal(secondPass[source + 1],'0');
});
