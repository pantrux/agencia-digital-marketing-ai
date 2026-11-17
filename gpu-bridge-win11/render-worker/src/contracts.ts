export class WorkerError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message); this.status = status; this.code = code;
  }
}

export type Plan = 'Free' | 'Pro' | 'Ultra' | 'Ilimitado';
export type Resolution = '1080p' | '4k';
export type OutputFormat = 'mp4' | 'prores4444';
export interface SceneLabel {text: string; start_frame: number; end_frame: number; position: 'top' | 'bottom'; size?: 'large'}
export interface AdCaption {start_frame: number; end_frame: number; text: string; position?: 'top' | 'left' | 'right'}
export const AD_MUSIC_ID = 'carefree-usuan1400037' as const;
export type SocialChannel = 'facebook' | 'instagram' | 'whatsapp';
export interface AdDestination {channel: SocialChannel; url: string}
export interface ScriptedAd {engine?: 'hyperframes'; destination?: AdDestination; cta: string; brand: string; captions: AdCaption[]; soundtrack?: typeof AD_MUSIC_ID}
export const AD_ENDCARD_FRAME = 780;
export const SOCIAL_ENDCARD_FRAME = 750;
export const SOCIAL_FADE_FRAME = 864;
export const AD_FRAMES = 900;
export const LABEL_ENTRANCE_FRAMES = 8;
export const SOCIAL_ENTRANCE_FRAMES = 12;
export interface EditScene {source_start_frame: number; source_end_frame: number; audio_source_start_frame?: number; label?: SceneLabel}
export interface CompositionProps {
  title: string; subtitle: string; duration_seconds: number;
  subtitle_size?: 'large';
  background: string; accent: string; video_path?: string; audio_path?: string;
  scenes?: EditScene[];
  ad?: ScriptedAd;
}
export interface RenderRequest {
  project_id: string; job_id: string; composition: 'AgencyVertical'; plan: Plan;
  format: OutputFormat; resolution: Resolution; transparent: boolean; props: CompositionProps; watermark: boolean;
  width: 1080 | 2160; height: 1920 | 3840; fps: 30; durationInFrames: number;
}
export interface StillsRequest extends RenderRequest {frames: number[]}
export interface InspectRequest {project_id: string; job_id: string; input_path: string; samples: number}

// Ilimitado se procesa en piezas acotadas; este techo operativo no es una cuota comercial.
export const MAX_SEGMENT_SECONDS = 600;
const durations: Record<Plan, number> = {Free: MAX_SEGMENT_SECONDS, Pro: MAX_SEGMENT_SECONDS, Ultra: MAX_SEGMENT_SECONDS, Ilimitado: MAX_SEGMENT_SECONDS};
const extensions = new Set(['mp4', 'mov', 'mkv', 'webm', 'm4v', 'mp3', 'wav', 'flac', 'm4a', 'aac', 'ogg', 'png', 'jpg', 'jpeg']);

function invalid(message: string): never {
  throw new WorkerError(422, 'SOLICITUD_INVALIDA', message);
}
export function outputDimensions(resolution: Resolution): {width: 1080 | 2160; height: 1920 | 3840} {
  if (resolution === '1080p') return {width: 1080, height: 1920};
  if (resolution === '4k') return {width: 2160, height: 3840};
  return invalid('La resolución no está permitida.');
}
function object(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalid('Se requiere un objeto JSON.');
  const result = value as Record<string, unknown>;
  if (Object.keys(result).some((key) => !keys.includes(key))) invalid('Hay propiedades no admitidas.');
  return result;
}
function identifier(value: unknown, project = false): string {
  if (typeof value !== 'string' || !(project ? /^[a-z][a-z0-9_-]{0,47}$/ : /^[a-z0-9][a-z0-9_-]{0,63}$/).test(value)) {
    invalid('El identificador no tiene el formato permitido.');
  }
  return value;
}
function text(value: unknown, fallback: string, maximum: number): string {
  if (value === undefined) return fallback;
  if (typeof value !== 'string' || value.length > maximum || hasControl(value)) invalid('El texto no es válido.');
  return value;
}
function hasControl(value: string): boolean {return [...value].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127);}
function numeric(value: unknown, min: number, max: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) invalid('El valor numérico supera los límites.');
  return value;
}
function frameNumber(value: unknown, minimum: number, maximum: number): number {
  const frame = numeric(value, minimum, maximum);
  if (!Number.isInteger(frame)) invalid('Los límites de escena deben ser fotogramas enteros.');
  return frame;
}
function editScenes(value: unknown, count: number, ad: boolean): EditScene[] {
  if (!Array.isArray(value) || value.length < 2 || value.length > (ad ? 12 : 6) || count < 900 || count > 1350) {
    invalid('La cantidad de escenas o la duración no corresponde al montaje.');
  }
  let last = 0, total = 0, labels = 0;
  const scenes = value.map((entry: unknown): EditScene => {
    const item = object(entry, ['source_start_frame', 'source_end_frame', 'audio_source_start_frame', 'label']);
    const start = frameNumber(item.source_start_frame, ad ? 0 : last, MAX_SEGMENT_SECONDS * 30 - 15);
    const end = frameNumber(item.source_end_frame, start + 15, MAX_SEGMENT_SECONDS * 30);
    last = end;
    total += end - start;
    if (item.audio_source_start_frame !== undefined && !ad) invalid('El audio alternativo requiere un anuncio.');
    const audioStart = item.audio_source_start_frame === undefined ? undefined
      : frameNumber(item.audio_source_start_frame, 0, MAX_SEGMENT_SECONDS * 30 - (end - start));
    const scene = {source_start_frame: start, source_end_frame: end,
      ...(audioStart === undefined ? {} : {audio_source_start_frame: audioStart})};
    if (item.label === undefined) return scene;
    const caption = object(item.label, ['text', 'start_frame', 'end_frame', 'position', 'size']);
    const label = text(caption.text, '', 80);
    const labelStart = frameNumber(caption.start_frame, 0, end - start - 15);
    const labelEnd = frameNumber(caption.end_frame, labelStart + 15, Math.min(end - start, labelStart + 180));
    if (!label.trim() || (caption.position !== 'top' && caption.position !== 'bottom')) {
      invalid('El rótulo requiere texto y posición.');
    }
    if (caption.size !== undefined && caption.size !== 'large') invalid('El tamaño del rótulo no está permitido.');
    labels += 1;
    return {...scene, label: {text: label, start_frame: labelStart, end_frame: labelEnd, position: caption.position,
      ...(caption.size === 'large' ? {size: caption.size} : {})}};
  });
  if (ad) {
    const sorted = [...scenes].sort((a,b) => a.source_start_frame - b.source_start_frame);
    if (sorted.some((scene,i) => i > 0 && scene.source_start_frame < sorted[i-1]!.source_end_frame)
      || !scenes[0]?.label || scenes[0].label.start_frame > 15 || scenes[0].label.end_frame < 60) {
      invalid('El anuncio requiere un gancho al inicio y tramos de origen sin solapamiento.');
    }
  }
  if (!labels || total !== count) invalid('Los cortes deben sumar la duración del reel y contener al menos un rótulo.');
  return scenes;
}
const socialUrls = {
  facebook: /^https:\/\/www\.facebook\.com\/(?:[a-zA-Z0-9.]{3,60}|profile\.php\?id=[1-9][0-9]{3,24})$/u,
  instagram: /^https:\/\/www\.instagram\.com\/[a-zA-Z0-9._]{1,30}$/u,
  whatsapp: /^https:\/\/wa\.me\/[1-9][0-9]{6,14}$/u,
};

function socialDestination(value: unknown): AdDestination {
  const item = object(value, ['channel', 'url']);
  const {channel, url} = item;
  if ((channel !== 'facebook' && channel !== 'instagram' && channel !== 'whatsapp')
    || typeof url !== 'string' || !socialUrls[channel].test(url) || /\s/u.test(url)) {
    return invalid('El destino del CTA no es válido.');
  }
  return {channel, url};
}

function scriptedAd(value: unknown): ScriptedAd {
  const input = object(value, ['cta', 'brand', 'captions', 'soundtrack', 'destination', 'engine']);
  if (input.engine !== undefined && input.engine !== 'hyperframes') invalid('El motor de animación no está disponible.');
  if (input.soundtrack !== undefined && input.soundtrack !== AD_MUSIC_ID) invalid('La pista no pertenece al catálogo del anuncio.');
  const cta = text(input.cta, '', 70), brand = text(input.brand, '', 80);
  if (!cta.trim() || !brand.trim() || !Array.isArray(input.captions)
    || !input.captions.length || input.captions.length > 60) invalid('Falta CTA, marca o subtítulos.');
  let previousEnd = 0;
  const captions = input.captions.map((entry): AdCaption => {
    const cue = object(entry, ['start_frame', 'end_frame', 'text', 'position']);
    const start_frame = frameNumber(cue.start_frame, previousEnd, AD_ENDCARD_FRAME - 9);
    const end_frame = frameNumber(cue.end_frame, start_frame + 9, AD_ENDCARD_FRAME);
    const caption = text(cue.text, '', 68);
    if (!caption.trim()) invalid('Falta el texto del subtítulo.');
    previousEnd = end_frame;
    const {position} = cue;
    if (position !== undefined && position !== 'top' && position !== 'left' && position !== 'right') {
      return invalid('La posición del subtítulo no es válida.');
    }
    return {start_frame,end_frame,text:caption,...(position === undefined ? {} : {position})};
  });
  return {cta,brand,captions,...(input.engine === 'hyperframes' ? {engine: input.engine} : {}),...(input.destination === undefined ? {} : {destination: socialDestination(input.destination)}),...(input.soundtrack === AD_MUSIC_ID && {soundtrack:AD_MUSIC_ID})};
}

export function mediaPath(value: unknown, project: string): string {
  if (typeof value !== 'string' || value.length > 500 || /[\\<>:"|?*]/.test(value) || hasControl(value)) invalid('La ruta de medios no es válida.');
  const parts = value.split('/');
  if (parts.some((part) => !part || part === '.' || part === '..' || /[. ]$/.test(part) || /^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/i.test(part))) invalid('La ruta contiene un segmento no permitido.');
  const own = parts[0] === 'proyectos' && parts[1] === project && ['01_raw', '03_assets', '04_edicion', '06_renders'].includes(parts[2] ?? '') && parts.length >= 4;
  const library = parts[0] === '_biblioteca_global' && parts.length >= 2;
  const extension = parts.at(-1)?.split('.').at(-1)?.toLowerCase();
  if ((!own && !library) || !extension || !extensions.has(extension)) invalid('El medio no pertenece al área permitida del proyecto.');
  return value;
}
function base(value: unknown, stills: boolean): RenderRequest {
  const data = object(value, ['project_id', 'job_id', 'composition', 'plan', 'format', 'resolution', 'props', ...(stills ? ['frames'] : [])]);
  const project_id = identifier(data.project_id, true), job_id = identifier(data.job_id);
  if (data.composition !== 'AgencyVertical' || !Object.hasOwn(durations, String(data.plan))) invalid('La composición o plan no está permitido.');
  const plan = data.plan as Plan;
  const format = data.format === undefined ? 'mp4' : data.format;
  const resolution = data.resolution === undefined ? '1080p' : data.resolution;
  if (format !== 'mp4' && format !== 'prores4444') invalid('El formato no está permitido.');
  if (resolution !== '1080p' && resolution !== '4k') invalid('La resolución no está permitida.');
  if (resolution === '4k' && plan !== 'Ultra' && plan !== 'Ilimitado') invalid('4K requiere Ultra o Ilimitado.');
  if (format === 'prores4444' && plan !== 'Ilimitado') invalid('ProRes4444 alfa requiere Ilimitado.');
  const input = object(data.props, ['title', 'subtitle', 'subtitle_size', 'duration_seconds', 'background', 'accent', 'video_path', 'audio_path', 'scenes', 'ad']);
  const duration_seconds = numeric(input.duration_seconds, 0.1, durations[plan]);
  const durationInFrames = Math.max(1, Math.round(duration_seconds * 30));
  const props: CompositionProps = {
    title: text(input.title, '', 120), subtitle: text(input.subtitle, '', 240), duration_seconds,
    background: text(input.background, '#102032', 7), accent: text(input.accent, '#8fe3c5', 7),
  };
  if (!props.title.trim() || !/^#[a-f0-9]{6}$/i.test(props.background) || !/^#[a-f0-9]{6}$/i.test(props.accent)) invalid('Se requiere título y colores hexadecimales.');
  if (input.subtitle_size === 'large') props.subtitle_size = input.subtitle_size;
  else if (input.subtitle_size !== undefined) invalid('El tamaño del subtítulo no está permitido.');
  if (input.video_path !== undefined) props.video_path = mediaPath(input.video_path, project_id);
  if (input.audio_path !== undefined) props.audio_path = mediaPath(input.audio_path, project_id);
  if (input.ad !== undefined) {
    if (format !== 'mp4' || durationInFrames !== 900 || !input.scenes) invalid('El anuncio requiere 30 segundos, escenas y salida MP4.');
    props.ad = scriptedAd(input.ad);
  }
  if (input.scenes !== undefined) {
    if (Boolean(props.video_path) !== (format === 'mp4')) invalid('El MP4 por escenas requiere vídeo y el ProRes solo contiene la gráfica.');
    props.scenes = editScenes(input.scenes, durationInFrames, Boolean(props.ad));
  }
  if (!stills && !props.audio_path) invalid('El render final requiere una pista de audio verificable.');
  return {project_id, job_id, composition: 'AgencyVertical', plan, format, resolution, transparent: format === 'prores4444', props, watermark: plan === 'Free', ...outputDimensions(resolution), fps: 30, durationInFrames};
}
export function parseRender(value: unknown): RenderRequest {return base(value, false);}
export function parseStills(value: unknown): StillsRequest {
  const result = base(value, true);
  const input = (value as Record<string, unknown>).frames;
  const frames = input === undefined ? [Math.min(60, result.durationInFrames - 1)] : input;
  if (!Array.isArray(frames) || frames.length < 1 || frames.length > 8 || frames.some((frame) => !Number.isInteger(frame) || frame < 0 || frame >= result.durationInFrames) || new Set(frames).size !== frames.length) invalid('Los fotogramas QA no son válidos.');
  return {...result, frames: frames as number[]};
}
export function parseInspect(value: unknown): InspectRequest {
  const data = object(value, ['project_id', 'job_id', 'input_path', 'samples']);
  const project_id = identifier(data.project_id, true), job_id = identifier(data.job_id);
  const samples = numeric(data.samples ?? 6, 1, 12);
  if (!Number.isInteger(samples)) invalid('El número de muestras debe ser entero.');
  return {project_id, job_id, input_path: mediaPath(data.input_path, project_id), samples};
}
export const TITLE_FADE_FRAMES = 12;
