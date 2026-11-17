import {MAX_SEGMENT_SECONDS, outputDimensions, TITLE_FADE_FRAMES, WorkerError, type EditScene, type OutputFormat, type RenderRequest, type Resolution} from './contracts.ts';

export interface Loudness {integrated_lufs: number; true_peak_dbtp: number; range_lu: number; threshold_lufs: number; offset_lu: number}
export interface MediaInfo {duration_seconds: number; width: number; height: number; fps: number; video_codec: string | null; video_profile: string | null; codec_tag: string | null; audio_codec: string | null; has_audio: boolean; sample_rate: number | null; color_space: string | null; color_transfer: string | null; color_primaries: string | null; pixel_format: string | null}
const bad = (): never => {throw new WorkerError(422, 'MEDICION_INVALIDA', 'No se pudo acreditar el medio o su audio.');};
function finite(value: unknown): number {
  if ((typeof value !== 'string' && typeof value !== 'number') || value === '' || !Number.isFinite(Number(value))) return bad();
  return Number(value);
}
export function parseLoudness(stderr: string): Loudness {
  const blocks = stderr.match(/\{[^{}]*"input_i"[^{}]*\}/g);
  if (!blocks?.length) return bad();
  const value = JSON.parse(blocks.at(-1)!) as Record<string, unknown>;
  return {integrated_lufs: finite(value.input_i), true_peak_dbtp: finite(value.input_tp), range_lu: finite(value.input_lra), threshold_lufs: finite(value.input_thresh), offset_lu: finite(value.target_offset)};
}
export function audioAccepted(value: Loudness): boolean {
  return Math.abs(value.integrated_lufs + 14) <= 1 && value.true_peak_dbtp <= -1;
}
export function parseProbe(stdout: string): MediaInfo {
  const data = JSON.parse(stdout) as {format?: {duration?: unknown}; streams?: Record<string, unknown>[]};
  if (!Array.isArray(data.streams)) return bad();
  const video = data.streams.find((stream) => stream.codec_type === 'video');
  const audio = data.streams.find((stream) => stream.codec_type === 'audio');
  const duration_seconds = finite(data.format?.duration);
  if (duration_seconds <= 0) return bad();
  const fpsParts = String(video?.avg_frame_rate ?? '0/1').split('/').map(Number);
  const fps = (fpsParts[0] ?? 0) / (fpsParts[1] || 1);
  const field = (name: string): string | null => typeof video?.[name] === 'string' ? video[name] as string : null;
  return {duration_seconds, width: video ? finite(video.width) : 0, height: video ? finite(video.height) : 0, fps: Number.isFinite(fps) ? fps : 0, video_codec: field('codec_name'), video_profile: field('profile'), codec_tag: field('codec_tag_string'), audio_codec: audio && typeof audio.codec_name === 'string' ? audio.codec_name : null, has_audio: Boolean(audio), sample_rate: audio ? finite(audio.sample_rate) : null, color_space: field('color_space'), color_transfer: field('color_transfer'), color_primaries: field('color_primaries'), pixel_format: field('pix_fmt')};
}
const exactTimeToleranceSeconds = 1;

function simpleDisplayRotation(metadata: unknown[]): boolean {
  if (!metadata.length) return true;
  if (metadata.length !== 1) return false;
  const matrix = metadata[0] as {side_data_type?: string; rotation?: number; displaymatrix?: string} | null;
  if (matrix?.side_data_type !== 'Display Matrix' || typeof matrix.displaymatrix !== 'string' || typeof matrix.rotation !== 'number') return false;
  const rotations = new Map([[0, '65536,0,0,65536'], [90, '0,-65536,65536,0'], [-90, '0,65536,-65536,0'], [180, '-65536,0,0,-65536'], [-180, '-65536,0,0,-65536']]);
  const rows = [...matrix.displaymatrix.matchAll(/^\d{8}:\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*$/gm)].map(row => row.slice(1).map(Number));
  return rows.length === 3 && rows[0]![2] === 0 && rows[1]![2] === 0 && rows[2]!.join(',') === '0,0,1073741824'
    && rows.slice(0, 2).flatMap(row => row.slice(0, 2)).join(',') === rotations.get(matrix.rotation);
}

export function overlayInputSupported(stdout: string, request: RenderRequest): false | 'exact' | 'normalized' {
  const minimumFrames = request.plan === 'Free' ? 900 : TITLE_FADE_FRAMES + 1;
  if (request.format !== 'mp4'
    || !request.props.video_path?.toLowerCase().endsWith('.mp4')
    || request.durationInFrames < minimumFrames || request.durationInFrames > MAX_SEGMENT_SECONDS * 30) return false;
  const data = JSON.parse(stdout) as {streams?: Record<string, unknown>[]};
  const videos = data.streams?.filter(stream => stream.codec_type === 'video');
  const video = videos?.[0];
  if (videos?.length !== 1 || !video) return false;
  const width = Number(video.width), height = Number(video.height);
  // La composición escala el crudo a la resolución elegida antes de superponer la gráfica.
  const supportedGeometry = Number.isInteger(width) && Number.isInteger(height) && width % 2 === 0 && height % 2 === 0
    && Math.min(width,height) >= 540 && Math.max(width,height) <= outputDimensions('4k').height
    && Math.min(width,height) * 16 === Math.max(width,height) * 9;
  const tags = video.tags as {rotate?: unknown} | undefined;
  const disposition = video.disposition as {attached_pic?: number} | undefined;
  const metadata = (video.side_data_list ?? []) as unknown[];
  const hevc = video.codec_name === 'hevc' && (video.field_order === undefined || video.field_order === 'progressive');
  // Algunos móviles omiten la relación de píxel en HEVC. Un valor explícito debe ser 1:1.
  const squarePixels = video.sample_aspect_ratio === '1:1' || (hevc && video.sample_aspect_ratio === undefined);
  const supportedColor = video.pix_fmt === 'yuv420p' && squarePixels
    && video.color_space === 'bt709' && video.color_range === 'tv'
    && (video.color_transfer === undefined || video.color_transfer === 'bt709')
    && (video.color_primaries === undefined || video.color_primaries === 'bt709');
  if (!supportedGeometry || !supportedColor || disposition?.attached_pic !== 0 || tags?.rotate !== undefined
    || Number(video.start_pts) !== 0 || !Array.isArray(metadata)) return false;
  if (video.codec_name === 'h264' && video.field_order === 'progressive' && !metadata.length
    && video.r_frame_rate === '30/1' && video.avg_frame_rate === '30/1' && Number(video.nb_frames) === request.durationInFrames
    && Number(video.duration_ts) === frameTicks(video.time_base) * request.durationInFrames
  ) return 'exact';
  // Los HEVC de móvil pueden omitir field_order. Se acota la corrección a fuentes SDR
  // de cadencia nominal 30 fps con orientación simple y duración cercana al pedido.
  const longH264 = request.durationInFrames > 1800 && video.codec_name === 'h264' && video.field_order === 'progressive';
  const [rateNumerator, rateDenominator] = String(video.avg_frame_rate ?? '').split('/').map(Number);
  const averageRate = (rateNumerator ?? 0) / (rateDenominator ?? 0);
  const sourceDuration = Number(video.duration_ts) / (frameTicks(video.time_base) * 30);
  if ((hevc || longH264) && video.r_frame_rate === '30/1' && averageRate >= 24 && averageRate <= 31
    && sourceDuration > 0 && Math.abs(sourceDuration - request.durationInFrames / 30) <= exactTimeToleranceSeconds
    && simpleDisplayRotation(metadata)) return 'normalized';
  return false;
}
export function videoFrameCount(stdout: string): number {
  const data = JSON.parse(stdout) as {streams?: Record<string, unknown>[]};
  return Number(data.streams?.find(stream => stream.codec_type === 'video')?.nb_frames);
}
function frameTicks(timeBase: unknown): number {
  const [numerator, denominator] = String(timeBase ?? '').split('/').map(Number);
  return (denominator ?? 0) / ((numerator ?? 0) * 30);
}
export function packetProbeArgs(input: string): string[] {
  return ['-v','error','-select_streams','v:0','-show_entries','stream=time_base:packet=pts,duration','-of','json',input];
}
export function overlayPacketTimingSupported(stdout: string, frameCount: number, normalize = false): boolean {
  const data = JSON.parse(stdout) as {streams?: {time_base?: string}[]; packets?: {pts?: number; duration?: number}[]};
  const ticks = frameTicks(data.streams?.[0]?.time_base);
  if (!Number.isSafeInteger(ticks) || ticks <= 0 || !data.packets?.length) return false;
  if (normalize) {
    const ticksPerSecond = ticks * 30;
    const packets = [...data.packets].sort((left, right) => Number(left.pts) - Number(right.pts));
    if (packets.length > MAX_SEGMENT_SECONDS * 31 + 31 || Number(packets[0]!.pts) !== 0) return false;
    let previous = -1, end = 0;
    for (const packet of packets) {
      const pts = Number(packet.pts), duration = Number(packet.duration);
      if (!Number.isSafeInteger(pts) || !Number.isSafeInteger(duration) || pts <= previous
        || pts - previous > 2 * ticksPerSecond || duration <= 0 || duration > 2 * ticksPerSecond) return false;
      previous = pts;
      end = Math.max(end, pts + duration);
    }
    return Math.abs(end / ticksPerSecond - frameCount / 30) <= exactTimeToleranceSeconds;
  }
  if (data.packets.length !== frameCount) return false;
  // El orden de decodificación puede diferir del orden de presentación con fotogramas B.
  const positions = new Set<number>();
  for (const packet of data.packets) {
    const position = Number(packet.pts) / ticks;
    if (Number(packet.duration) !== ticks || !Number.isInteger(position) || position < 0 || position >= frameCount) return false;
    positions.add(position);
  }
  return positions.size === frameCount;
}
export function audioPass(duration: number): string {
  if (!Number.isFinite(duration) || duration <= 0 || duration > 600) return bad();
  // Medir y limitar antes del upmix mono→estéreo desaprovecha 3 dB de pico por canal.
  // La preparación idéntica en ambos pases refleja el formato final antes de loudnorm.
  return `aformat=sample_rates=48000:channel_layouts=stereo,apad=whole_dur=${duration},atrim=duration=${duration}`;
}
export function measureAudio(input: string, duration: number): string[] {
  return ['-hide_banner','-nostdin','-nostats','-i',input,'-map','0:a:0','-vn','-af',`${audioPass(duration)},loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json`,'-f','null','-'];
}
const limitedBt709 = 'setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709';
function sdrVideoFilter(pixelFormat: 'yuv420p' | 'yuva444p10le', jpeg = false): string {
  // Chromium genera JPEG con rango completo y coeficientes BT.601.
  // Las etiquetas de salida se fijan después de convertir el fotograma.
  const sourceMatrix = jpeg ? ':in_color_matrix=bt601' : '';
  return `scale=in_range=pc:out_range=tv${sourceMatrix}:out_color_matrix=bt709,format=${pixelFormat},${limitedBt709}`;
}
function normalizeAudio(count: number, duration: number, measured: Loudness, peak: number): string {
  if (!Number.isInteger(count) || count < 1 || ![-1.5,-2,-3.5].includes(peak)
    || Object.values(measured).some(value => !Number.isFinite(value))) return bad();
  const loudnorm = `loudnorm=I=-14:TP=${peak}:LRA=11:measured_I=${measured.integrated_lufs}:measured_TP=${measured.true_peak_dbtp}:measured_LRA=${measured.range_lu}:measured_thresh=${measured.threshold_lufs}:offset=${measured.offset_lu}:linear=true:print_format=json`;
  return `${audioPass(duration)},${loudnorm}`;
}
const aacAudio = ['-c:a','aac','-b:a','256k','-ar','48000','-ac','2'];

// Motivo instrumental original, cuatro notas con envolvente de cuerda pulsada.
const adScore = "0.15*(sin(2*PI*(220+27.5*mod(floor(t/0.6),4))*mod(t,0.6))+0.19*sin(2*PI*440*mod(t,0.6)))*exp(-6*mod(t,0.6))*min(1,mod(t,0.6)*80)";

export function mixAdAudio(speech: string, output: string): string[] {
  const filter = '[0:a]aformat=channel_layouts=stereo,afade=t=out:st=25.85:d=0.15,asplit=2[voice][key];'
    + '[1:a]aformat=channel_layouts=stereo,volume=0.30,afade=d=0.45,afade=t=out:st=28.9:d=1.1[music];'
    + '[music][key]sidechaincompress=threshold=0.025:ratio=6:attack=14:release=260[bed];'
    + '[voice][bed]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.94:level=0[audio]';
  return ['-hide_banner','-nostdin','-v','error','-n','-i',speech,'-f','lavfi','-i',
    'aevalsrc=' + adScore.replaceAll(',', '\\,') + ':s=48000:d=30',
    '-filter_complex',filter,'-map','[audio]','-ar','48000','-c:a','pcm_s24le',output];
}

export function cutSceneAudio(input: string, output: string, scenes: readonly EditScene[]): string[] {
  const channels = scenes.map((_, index) => `[a${index}]`).join('');
  const pieces = scenes.map((scene, index) => `[a${index}]atrim=start_sample=${scene.source_start_frame * 1600}:end_sample=${scene.source_end_frame * 1600},asetpts=PTS-STARTPTS[p${index}]`);
  const concat = scenes.map((_, index) => `[p${index}]`).join('');
  const filter = [`[0:a:0]aformat=sample_rates=48000:channel_layouts=stereo,asplit=${scenes.length}${channels}`,
    ...pieces, `${concat}concat=n=${scenes.length}:v=0:a=1[audio]`].join(';');
  return ['-hide_banner','-nostdin','-nostats','-n','-i',input,'-filter_complex',filter,'-map','[audio]','-c:a','pcm_s24le',output];
}

export function encodeNvenc(frames: string, audio: string, output: string, count: number, duration: number, measured: Loudness, peak = -1.5, resolution: Resolution = '1080p', sourceVideo?: string, normalize = false, scenes?: readonly EditScene[]): string[] {
  if (scenes && !sourceVideo) return bad();
  const audioFilter = normalizeAudio(count, duration, measured, peak);
  const {width,height} = outputDimensions(resolution);
  const high = resolution === '4k';
  const images = ['-framerate','30','-start_number','0','-i',frames];
  const tail = normalize ? `tpad=stop_mode=clone:stop_duration=${exactTimeToleranceSeconds},` : '';
  const videoInputs: string[] = [];
  const cuts = scenes?.map((scene, index) => {
    // Buscar en segundos enteros deja un margen de fotogramas antes de cada corte.
    const seek = Math.max(0, Math.floor(scene.source_start_frame / 30) - 1);
    videoInputs.push('-threads','2','-ss',String(seek),'-i',sourceVideo!);
    return `[${index}:v]fps=30:start_time=0,trim=start_frame=${scene.source_start_frame - seek * 30}:end_frame=${scene.source_end_frame - seek * 30},setpts=PTS-STARTPTS[plano${index}]`;
  }) ?? [];
  if (scenes) {
    const links = scenes.map((_, index) => `[plano${index}]`).join('');
    cuts.push(`${links}concat=n=${scenes.length}:v=1:a=0,settb=1/30,setpts=N[montaje]`);
  } else if (sourceVideo) videoInputs.push('-i',sourceVideo);
  const videoFilter = scenes ? '[montaje]' : `[0:v]fps=fps=30:start_time=0,${tail}`;
  const scale = `${videoFilter}scale=${width}:${height}:force_original_aspect_ratio=increase:force_divisible_by=2:flags=bilinear`;
  const crop = `crop=${width}:${height},setsar=1`;
  const graphicsInput = scenes?.length ?? 1;
  const overlay = 'alpha=straight:eof_action=repeat:repeatlast=1';
  // El crudo admitido ya está en YUV BT.709. Solo se convierte la capa RGBA.
  const filter = [
    ...cuts,
    `${scale}:${scenes ? 'out_color_matrix=bt709:out_range=tv' : 'in_color_matrix=bt709:out_color_matrix=bt709:in_range=tv:out_range=tv'},${crop},format=yuv420p[video]`,
    `[${graphicsInput}:v]scale=in_range=pc:out_range=tv:out_color_matrix=bt709,format=yuva420p[capa]`,
    `[video][capa]overlay=format=yuv420:${overlay},${limitedBt709}[comp]`,
  ].join(';');
  const inputs = sourceVideo
    ? ['-xerror','-filter_complex_threads','4',...videoInputs,...images,'-i',audio,'-filter_complex',filter,'-map','[comp]','-map',`${graphicsInput + 1}:a:0`]
    : [...images,'-i',audio,'-map','0:v:0','-map','1:a:0','-vf',sdrVideoFilter('yuv420p', frames.endsWith('.jpeg'))];
  return ['-hide_banner','-nostdin','-nostats','-n',...inputs,'-frames:v',String(count),'-t',String(duration),'-c:v','h264_nvenc','-preset','p5','-profile:v','high','-level:v',high ? '5.1' : '4.2','-b:v',high ? '24M' : '6M','-maxrate',high ? '32M' : '8M','-bufsize',high ? '48M' : '12M','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709','-color_range','tv','-af',audioFilter,...aacAudio,'-movflags','+faststart',output];
}

export function correctMp4Audio(video: string, audio: string, output: string, count: number, duration: number, measured: Loudness, peak = -2): string[] {
  const filter = normalizeAudio(count, duration, measured, peak);
  return ['-hide_banner','-nostdin','-nostats','-n','-i',video,'-i',audio,'-map','0:v:0','-map','1:a:0',
    '-c:v','copy','-af',filter,...aacAudio,'-movflags','+faststart',output];
}

const proresVideo = ['-vf',sdrVideoFilter('yuva444p10le'),'-c:v','prores_ks','-profile:v','4444','-alpha_bits','16',
  '-tag:v','ap4h','-threads','2','-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709','-color_range','tv'];
const pcmAudio = ['-c:a','pcm_s24le','-ar','48000','-ac','2'];
const proresTimescale = 15360;

export function encodeProresFragment(frames: string, output: string, count: number): string[] {
  if (!Number.isInteger(count) || count < 1) return bad();
  return ['-hide_banner','-nostdin','-nostats','-n','-f','image2','-framerate','30','-start_number','0','-i',frames,
    '-map','0:v:0','-an','-frames:v',String(count),...proresVideo,'-video_track_timescale',String(proresTimescale),output];
}

export function muxProresFragments(list: string, audio: string, output: string, count: number, duration: number, measured: Loudness, peak: number): string[] {
  const audioFilter = normalizeAudio(count, duration, measured, peak);
  const ticks = proresTimescale / 30;
  // Las duraciones que el demuxer concat redondea a microsegundos no fijan el reloj del MOV.
  const timestamps = `setts=pts=N*${ticks}:dts=N*${ticks}:duration=${ticks}:time_base=1/${proresTimescale}`;
  return ['-hide_banner','-nostdin','-nostats','-n','-f','concat','-safe','1','-i',list,'-i',audio,
    '-map','0:v:0','-map','1:a:0','-c:v','copy','-tag:v','ap4h','-bsf:v',timestamps,
    '-video_track_timescale',String(proresTimescale),'-af',audioFilter,...pcmAudio,'-f','mov',output];
}

export function encodeOutput(format: OutputFormat, resolution: Resolution, frames: string, audio: string, output: string, count: number, duration: number, measured: Loudness, peak = -1.5, sourceVideo?: string, normalize = false, scenes?: readonly EditScene[]): string[] {
  if (format === 'mp4') return encodeNvenc(frames, audio, output, count, duration, measured, peak, resolution, sourceVideo, normalize, scenes);
  outputDimensions(resolution);
  if (format !== 'prores4444') return bad();
  const audioFilter = normalizeAudio(count, duration, measured, peak);
  return ['-hide_banner','-nostdin','-nostats','-n','-framerate','30','-start_number','0','-i',frames,'-i',audio,
    '-map','0:v:0','-map','1:a:0','-frames:v',String(count),'-t',String(duration),...proresVideo,
    '-af',audioFilter,...pcmAudio,'-f','mov',output];
}
export function outputViolations(info: MediaInfo, request: RenderRequest): string[] {
  const errors: string[] = [];
  const exact: [string,unknown,unknown][] = [['width',info.width,request.width],['height',info.height,request.height],['has_audio',info.has_audio,true],['sample_rate',info.sample_rate,48000],['color_space',info.color_space,'bt709'],['color_transfer',info.color_transfer,'bt709'],['color_primaries',info.color_primaries,'bt709']];
  for (const [name,actual,expected] of exact) if (actual !== expected) errors.push(name);
  if (!Number.isFinite(info.fps) || Math.abs(info.fps-request.fps)>0.01) errors.push('fps');
  if (!Number.isFinite(info.duration_seconds) || Math.abs(info.duration_seconds-request.durationInFrames/request.fps)>0.1) errors.push('duration_seconds');
  if (request.format==='mp4') {
    if (info.video_codec!=='h264') errors.push('video_codec');
    if (info.audio_codec!=='aac') errors.push('audio_codec');
    if (info.pixel_format!=='yuv420p') errors.push('pixel_format');
  } else {
    if (info.video_codec!=='prores') errors.push('video_codec');
    if (info.video_profile!=='4444') errors.push('video_profile');
    if (info.codec_tag!=='ap4h') errors.push('codec_tag');
    if (info.audio_codec!=='pcm_s24le') errors.push('audio_codec');
    if (!['yuva444p10le','yuva444p12le'].includes(info.pixel_format??'')) errors.push('pixel_format');
  }
  return errors;
}
export function outputAccepted(info: MediaInfo, request: RenderRequest): boolean {return outputViolations(info,request).length===0;}
export function alphaMaskArgs(input: string, output: string, seconds = 0): string[] {
  if (!Number.isFinite(seconds) || seconds < 0 || seconds > 600) return bad();
  return ['-hide_banner','-nostdin','-nostats','-n','-ss',String(seconds),'-i',input,'-map','0:v:0','-frames:v','1','-an','-vf','alphaextract,scale=64:64:flags=area,format=gray','-pix_fmt','gray','-f','rawvideo',output];
}
export function alphaMaskSummary(mask: Buffer) {
  if (mask.length !== 64 * 64) return bad();
  let minimum = 255, maximum = 0, transparent = 0;
  for (const value of mask) {minimum = Math.min(minimum, value); maximum = Math.max(maximum, value); if (value < 255) transparent += 1;}
  // Se exige al menos un píxel totalmente transparente; 254 opaco por redondeo no basta.
  return {has_transparency: minimum === 0, alpha_min: minimum, alpha_max: maximum, transparent_pixels: transparent, sample_width: 64, sample_height: 64};
}
export function probeArgs(input: string): string[] {return ['-v','error','-show_streams','-show_format','-of','json',input];}
export function ebur128Args(input: string): string[] {return ['-hide_banner','-nostdin','-nostats','-i',input,'-map','0:a:0','-vn','-af','ebur128=peak=true:framelog=verbose','-f','null','-'];}
export function parseEbur128(stderr: string): {integrated_lufs: number; true_peak_dbtp: number} {
  const summary = stderr.slice(stderr.lastIndexOf('Summary:'));
  const integrated = summary.match(/\bI:\s*(-?\d+(?:\.\d+)?)\s+LUFS/);
  const peak = summary.match(/\bPeak:\s*(-?\d+(?:\.\d+)?)\s+dBFS/);
  if (!integrated || !peak) return bad();
  return {integrated_lufs: finite(integrated[1]), true_peak_dbtp: finite(peak[1])};
}
