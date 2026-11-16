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
export interface CompositionProps {
  title: string; subtitle: string; duration_seconds: number;
  subtitle_size?: 'large';
  background: string; accent: string; video_path?: string; audio_path?: string;
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
  const input = object(data.props, ['title', 'subtitle', 'subtitle_size', 'duration_seconds', 'background', 'accent', 'video_path', 'audio_path']);
  const duration_seconds = numeric(input.duration_seconds, 0.1, durations[plan]);
  const props: CompositionProps = {
    title: text(input.title, '', 120), subtitle: text(input.subtitle, '', 240), duration_seconds,
    background: text(input.background, '#102032', 7), accent: text(input.accent, '#8fe3c5', 7),
  };
  if (!props.title.trim() || !/^#[a-f0-9]{6}$/i.test(props.background) || !/^#[a-f0-9]{6}$/i.test(props.accent)) invalid('Se requiere título y colores hexadecimales.');
  if (input.subtitle_size === 'large') props.subtitle_size = input.subtitle_size;
  else if (input.subtitle_size !== undefined) invalid('El tamaño del subtítulo no está permitido.');
  if (input.video_path !== undefined) props.video_path = mediaPath(input.video_path, project_id);
  if (input.audio_path !== undefined) props.audio_path = mediaPath(input.audio_path, project_id);
  if (!stills && !props.audio_path) invalid('El render final requiere una pista de audio verificable.');
  return {project_id, job_id, composition: 'AgencyVertical', plan, format, resolution, transparent: format === 'prores4444', props, watermark: plan === 'Free', ...outputDimensions(resolution), fps: 30, durationInFrames: Math.max(1, Math.round(duration_seconds * 30))};
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
