import {copyFile, link, lstat, mkdir, readFile, writeFile, stat} from 'node:fs/promises';
import {constants, createReadStream} from 'node:fs';
import {createHash, createHmac, timingSafeEqual} from 'node:crypto';
import path from 'node:path';
import {performance} from 'node:perf_hooks';
import {AD_MUSIC_ID, parseStills, TITLE_FADE_FRAMES, WorkerError, type RenderRequest, type StillsRequest, type InspectRequest} from './contracts.ts';
import {type RuntimeConfig, verifyBinary} from './config.ts';
import {type Backend, type Health} from './server.ts';
import {NasPaths} from './nas.ts';
import {ScratchSpace} from './scratch.ts';
import {runProcess} from './process.ts';
import {renderScene} from './remotion.ts';
import {alphaMaskArgs, alphaMaskSummary, audioAccepted, correctMp4Audio, cutSceneAudio, mixAdAudio, encodeOutput, encodeProresFragment, measureAudio, muxProresFragments, outputViolations, overlayInputSupported, overlayPacketTimingSupported, packetProbeArgs, parseLoudness, parseProbe, probeArgs, ebur128Args, parseEbur128, videoFrameCount, type MediaInfo} from './media.ts';
import {contactSheet, normalizedRuler} from './qa.ts';
import {graphicFrames, graphicLabel} from './timeline.ts';

interface Dependencies {run: typeof runProcess; scene: typeof renderScene; verifyTools(config: RuntimeConfig): Promise<void>; verifyMapping(config: RuntimeConfig): Promise<void>}
interface Artifact {path: string; bytes: number; sha256: string}
const receiptName = '.recibo-puente-v1.json';
const hashPattern = /^[a-f0-9]{64}$/;
type RenderStage = 'render' | 'stills';
const linksPerImage = 512;
const approvedAdMusic = {
  relative: '_biblioteca_global/musica-stock/' + AD_MUSIC_ID + '.mp3',
  sha256: '8433b770a630d9b1594fd484442c677907ece899a4d149954cd2e74fd733e311',
};

export async function stageAdMusic(nas: NasPaths, scratch: Pick<ScratchSpace, 'ensureSpace'>, project: string, work: string,
  signal: AbortSignal, asset = approvedAdMusic): Promise<string> {
  const track = path.join(work, 'soundtrack.mp3');
  try {
    const source = await nas.input(asset.relative, project);
    const bytes = (await stat(source)).size;
    if (bytes < 1024 || bytes > 10 * 1024 ** 2) throw Error('Tamaño de pista inválido');
    await scratch.ensureSpace(bytes);
    await copyFile(source, track, constants.COPYFILE_EXCL);
    if (await sha256File(track, signal) !== asset.sha256) throw Error('Huella de pista inválida');
    return track;
  } catch {
    signal.throwIfAborted();
    throw new WorkerError(503, 'MUSICA_NO_VERIFICADA', 'La pista de catálogo no está disponible o cambió.');
  }
}

export async function timelineImages(work: string, frames: number[], files: string[], count: number, signal: AbortSignal) {
  const dir = path.join(work, 'rotulos');
  await mkdir(dir);
  // Los enlaces comparten los PNG de cada estado y mantienen el reloj exacto de 30 fps.
  for (const [index, start] of frames.entries()) {
    let image = files[index]!;
    for (let frame = start; frame < (frames[index + 1] ?? count); frame += 1) {
      signal.throwIfAborted();
      // NTFS limita los enlaces por archivo. Cada bloque usa su propio PNG.
      if (frame > start && (frame - start) % linksPerImage === 0) {
        image = path.join(dir, `estado-${frame}.png`);
        await copyFile(files[index]!, image, constants.COPYFILE_EXCL);
      }
      await link(image, path.join(dir, `still-${frame}.png`));
    }
  }
  return path.join(dir, 'still-%d.png');
}

async function sha256File(filename: string, signal?: AbortSignal): Promise<string> {
  const digest = createHash('sha256');
  for await (const chunk of createReadStream(filename, {signal})) digest.update(chunk as Buffer);
  return digest.digest('hex');
}
async function verifyTools(config: RuntimeConfig): Promise<void> {
  const manifest = JSON.parse(await readFile(new URL('../versions.json', import.meta.url), 'utf8')) as {ffmpeg: {executable_sha256: string; ffprobe_sha256: string}};
  await verifyBinary(config.ffmpeg, manifest.ffmpeg.executable_sha256); await verifyBinary(config.ffprobe, manifest.ffmpeg.ffprobe_sha256);
}
async function verifyMapping(config: RuntimeConfig): Promise<void> {
  const shell = path.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  const script = `$ErrorActionPreference='Stop'; Get-SmbMapping -LocalPath '${config.nasRoot.slice(0, 2)}' | Select-Object RemotePath,Status,RequirePrivacy | ConvertTo-Json -Compress`;
  const {stdout} = await runProcess(shell, ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script], {timeoutMs: 15000});
  const mapping = JSON.parse(stdout) as {RemotePath?: string; Status?: number | string; RequirePrivacy?: boolean};
  if (mapping.RemotePath?.toLowerCase() !== '\\\\192.168.0.8\\producciones' || ![0, 'OK'].includes(mapping.Status ?? '') || mapping.RequirePrivacy !== true) throw new WorkerError(503, 'NAS_NO_VERIFICADO', 'La unidad SMB no coincide con el recurso cifrado autorizado.');
}
export class RenderBackend implements Backend {
  readonly config: RuntimeConfig; readonly nas: NasPaths; readonly scratch: ScratchSpace; readonly deps: Dependencies;
  #ready = false; #nvenc = false; #prores = false; #checkedAt: string | null = null;
  #inProgress = new Set<string>();
  private constructor(config: RuntimeConfig, nas: NasPaths, scratch: ScratchSpace, deps: Dependencies) {this.config = config; this.nas = nas; this.scratch = scratch; this.deps = deps;}
  static async create(config: RuntimeConfig, dependencies: Partial<Dependencies> = {}): Promise<RenderBackend> {
    return new RenderBackend(config, await NasPaths.create(config.nasRoot), await ScratchSpace.create(config.cacheRoot), {run: runProcess, scene: renderScene, verifyTools, verifyMapping, ...dependencies});
  }
  health(): Health {return {service: 'remotion', status: this.#ready ? 'ok' : 'degraded', ready: this.#ready, version: '4.0.541', nvenc_available: this.#nvenc, prores_available: this.#prores, checked_at: this.#checkedAt, max_concurrent_jobs: 2, output_formats: ['mp4', 'prores4444'], output_resolutions: ['1080p', '4k']};}
  async initialize(): Promise<void> {
    const startup = AbortSignal.timeout(120000);
    this.#ready = false; this.#nvenc = false; this.#prores = false;
    await this.deps.verifyTools(this.config); await this.deps.verifyMapping(this.config);
    // Ejecutar el codificador acredita más que enumerar -encoders; no hay alternativa CPU.
    await this.deps.run(this.config.ffmpeg, ['-hide_banner','-nostdin','-nostats','-f','lavfi','-i','color=c=black:s=320x240:r=30','-frames:v','3','-an','-c:v','h264_nvenc','-pix_fmt','yuv420p','-f','null','-'], {timeoutMs: 20000, signal: startup});
    // ProRes es codificación CPU explícita; no se atribuye disponibilidad NVENC a este formato.
    await this.deps.run(this.config.ffmpeg, ['-hide_banner','-nostdin','-nostats','-f','lavfi','-i','color=c=black@0:s=320x240:r=30,format=rgba','-frames:v','3','-an','-c:v','prores_ks','-profile:v','4444','-pix_fmt','yuva444p10le','-alpha_bits','16','-threads','2','-f','null','-'], {timeoutMs: 20000, signal: startup});
    const work = await this.scratch.job();
    try {
      const request = parseStills({project_id: 'sansevieras', job_id: 'arranque', composition: 'AgencyVertical', plan: 'Free', props: {title: 'Prueba de arranque', duration_seconds: 0.1}});
      await this.deps.scene({request, scratch: work, cacheRoot: this.scratch.root, browser: this.config.browser, videoSource: null, signal: startup}, [0]);
    } finally {await this.scratch.remove(work);}
    startup.throwIfAborted();
    this.#nvenc = true; this.#prores = true; this.#ready = true; this.#checkedAt = new Date().toISOString();
  }
  #requireReady(): void {if (!this.#ready) throw new WorkerError(503, 'BACKEND_NO_DISPONIBLE', 'El backend aún no acredita GPU, navegador y NAS.');}
  async #job<T>(signal: AbortSignal, fn: (work: string, signal: AbortSignal) => Promise<T>): Promise<T> {
    this.#requireReady();
    const work = await this.scratch.job();
    try {return await fn(work, AbortSignal.any([signal, AbortSignal.timeout(1200000)]));}
    finally {await this.scratch.remove(work);}
  }
  async #readProbe(file: string, signal: AbortSignal): Promise<string> {
    return (await this.deps.run(this.config.ffprobe, probeArgs(file), {timeoutMs: 30000, signal})).stdout;
  }
  async #probe(file: string, signal: AbortSignal): Promise<MediaInfo> {return parseProbe(await this.#readProbe(file, signal));}
  async #alpha(file: string, work: string, id: string, seconds: number, signal: AbortSignal) {
    const mask = path.join(work, `alpha-${id}.raw`);
    await this.deps.run(this.config.ffmpeg, alphaMaskArgs(file, mask, seconds), {timeoutMs: 60000, signal});
    const result = alphaMaskSummary(await readFile(mask));
    if (!result.has_transparency) throw new WorkerError(502, 'ALFA_NO_ACREDITADO', 'El archivo no conserva píxeles alfa transparentes.');
    return result;
  }
  async #videoInput(request: RenderRequest, signal: AbortSignal) {
    if (!request.props.video_path) return null;
    const source = await this.nas.input(request.props.video_path, request.project_id);
    const probe = await this.#readProbe(source, signal);
    const info = parseProbe(probe);
    if (!info.video_codec || info.width < 1 || info.height < 1) throw new WorkerError(422, 'VIDEO_INVALIDO', 'La composición requiere un vídeo válido.');
    return {source, probe};
  }
  async #scene(request: RenderRequest, work: string, frames: number[] | null, signal: AbortSignal, videoSource: string | null, overlay?: true) {
    signal.throwIfAborted();
    return this.deps.scene({request, scratch: work, cacheRoot: this.scratch.root, browser: this.config.browser, videoSource, signal, ...(overlay && {overlay})}, frames);
  }
  async #copyArtifacts(output: {absolute: string; relative: string}, files: string[]): Promise<Artifact[]> {
    const results: Artifact[] = [];
    for (const file of files) {
      const name = path.basename(file), target = path.join(output.absolute, name);
      await copyFile(file, target, constants.COPYFILE_EXCL);
      results.push({path: `${output.relative}/${name}`, bytes: (await stat(target)).size, sha256: await sha256File(target)});
    }
    return results;
  }

  #receiptSignature(body: string): Buffer {
    return createHmac('sha256', this.config.token).update('agencia-puente-gpu:recibo:v1\n').update(body).digest();
  }

  async #replay<T extends {files: Artifact[]}>(folder: {absolute: string; relative: string}, identity: string, signal: AbortSignal): Promise<T> {
    const file = path.join(folder.absolute, receiptName);
    const rejected = () => new WorkerError(409, 'TRABAJO_EXISTENTE', 'La salida existente no coincide con el recibo del puente.');
    let row: {body: string; signature: string};
    try {
      const info = await lstat(file);
      if (!info.isFile() || info.isSymbolicLink() || info.size > 65536) throw rejected();
      row = JSON.parse(await readFile(file, 'utf8')) as typeof row;
    } catch (error) {
      if (error instanceof SyntaxError || (error as NodeJS.ErrnoException).code === 'ENOENT') throw rejected();
      throw error;
    }
    if (!row || typeof row.body !== 'string' || typeof row.signature !== 'string' || !hashPattern.test(row.signature) ||
        !timingSafeEqual(this.#receiptSignature(row.body), Buffer.from(row.signature, 'hex'))) throw rejected();
    const {request_sha256, result} = JSON.parse(row.body) as {request_sha256: string; result: T};
    if (request_sha256 !== identity) throw new WorkerError(409, 'TRABAJO_EXISTENTE', 'El recibo corresponde a una solicitud distinta.');
    for (const item of result.files) {
      const name = path.posix.basename(item.path);
      if (!/^[a-zA-Z0-9_.-]+$/.test(name) || name === '.' || name === '..' || item.path !== `${folder.relative}/${name}`) throw rejected();
      const output = path.join(folder.absolute, name);
      const stat = await lstat(output).catch(error => {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') throw rejected();
        throw error;
      });
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== item.bytes || await sha256File(output, signal) !== item.sha256) throw rejected();
    }
    return result;
  }

  async #withReceipt<T extends {files: Artifact[]}>(request: RenderRequest, kind: RenderStage, signal: AbortSignal,
    execute: (publish: (files: string[]) => Promise<Artifact[]>) => Promise<T>): Promise<T> {
    this.#requireReady();
    const jobKey = `${request.project_id}/${request.job_id}/${kind}`;
    if (this.#inProgress.has(jobKey)) throw new WorkerError(409, 'TRABAJO_EN_CURSO', 'El puente ya procesa este trabajo.');
    this.#inProgress.add(jobKey);
    let folder: Awaited<ReturnType<NasPaths['stageOutput']>> | undefined;
    try {
      const inputs = [request.props.video_path, request.props.audio_path];
      const sources: (string | null)[] = [];
      for (const relative of inputs) {
        sources.push(relative ? await sha256File(await this.nas.input(relative, request.project_id), signal) : null);
      }
      const identity = createHash('sha256').update(JSON.stringify([kind, request, sources])).digest('hex');
      const existing = await this.nas.existingOutput(request.project_id, request.job_id, kind);
      if (existing) return await this.#replay<T>(existing, identity, signal);
      const result = await execute(async files => {
        for (const [index, relative] of inputs.entries()) {
          if (relative && await sha256File(await this.nas.input(relative, request.project_id), signal) !== sources[index]) {
            throw new WorkerError(409, 'INSUMO_CAMBIADO', 'Un insumo cambió durante el render.');
          }
        }
        signal.throwIfAborted();
        folder = await this.nas.stageOutput(request.project_id, request.job_id, kind);
        return this.#copyArtifacts(folder, files);
      });
      if (!folder) throw new WorkerError(500, 'RECIBO_NO_DISPONIBLE', 'No se pudo preparar el recibo del puente.');
      const body = JSON.stringify({request_sha256: identity, result});
      await writeFile(path.join(folder.absolute, receiptName), JSON.stringify({body, signature: this.#receiptSignature(body).toString('hex')}), {flag: 'wx', flush: true});
      signal.throwIfAborted();
      await folder.commit();
      folder = undefined;
      return result;
    } finally {
      // Un temporal ajeno al destino no bloquea el próximo intento si su limpieza falla.
      await folder?.discard().catch(() => {
        console.warn(JSON.stringify({service: 'remotion', error: 'LIMPIEZA_PUBLICACION_FALLIDA', stage: kind}));
      });
      this.#inProgress.delete(jobKey);
    }
  }

  async #prepareStaticProres(work: string, framePattern: string, lastFrame: string, count: number, signal: AbortSignal): Promise<string> {
    const fade = path.join(work, 'prores-fundido.mov');
    const tail = path.join(work, 'prores-estatico.mov');
    const run = (args: string[]) => this.deps.run(this.config.ffmpeg, args, {timeoutMs: 1200000, signal});
    await run(encodeProresFragment(framePattern, fade, TITLE_FADE_FRAMES));
    await run(encodeProresFragment(lastFrame, tail, 1));
    return this.#writeProresList(work, [
      {file: fade, repeat: 1}, {file: tail, repeat: count - TITLE_FADE_FRAMES},
    ], count, signal);
  }

  async #prepareSceneProres(work: string, frames: number[], files: string[], count: number, signal: AbortSignal) {
    const fragments = [];
    for (const [index, start] of frames.entries()) {
      const file = path.join(work, `prores-escena-${index}.mov`);
      await this.deps.run(this.config.ffmpeg, encodeProresFragment(files[index]!, file, 1), {timeoutMs: 120000, signal});
      fragments.push({file, repeat: (frames[index + 1] ?? count) - start});
    }
    return this.#writeProresList(work, fragments, count, signal);
  }

  async #writeProresList(work: string, fragments: {file: string; repeat: number}[], count: number, signal: AbortSignal) {
    const sizes = await Promise.all(fragments.map(async ({file, repeat}) => (await stat(file)).size * repeat));
    // El PCM estéreo de 24 bits ocupa 288 000 bytes/s. Reservar ambos resultados del ajuste de pico.
    const estimated = sizes.reduce((total, size) => total + size, 0) + Math.ceil(count / 30) * 288000 + 16 * 1024 ** 2;
    await this.scratch.ensureSpace(2 * estimated);
    const list = path.join(work, 'prores-lista.txt');
    const entries = fragments.map(({file, repeat}) => `file ${path.basename(file)}\n`.repeat(repeat)).join('');
    await writeFile(list, entries, {flag: 'wx', signal});
    return list;
  }

  async render(request: RenderRequest, signal: AbortSignal) {
    const started = performance.now();
    return this.#withReceipt(request, 'render', signal, publish => this.#job(signal, async (work, bounded) => {
      const audioStarted = performance.now();
      const duration = request.durationInFrames / 30;
      const pixels = request.width * request.height;
      const frameBytes = pixels * 4 + 1024 ** 2;
      const outputs = request.format === 'prores4444' ? request.durationInFrames * pixels * 16 : 4 * 1024 ** 3;
      const scenes = request.props.scenes;
      const sceneProres = Boolean(scenes && request.transparent);
      if (!request.props.audio_path) throw new WorkerError(422, 'AUDIO_AUSENTE', 'El render final requiere una pista de audio.');
      const sourceAudio = await this.nas.input(request.props.audio_path, request.project_id);
      const ad = request.props.ad;
      const speech = path.join(work, 'speech.wav');
      const audio = path.join(work, 'audio' + (scenes ? '.wav' : path.extname(sourceAudio)));
      await this.scratch.ensureSpace(scenes ? Math.ceil(duration) * 288000 * (ad ? 2 : 1) + 1024 ** 2 : (await stat(sourceAudio)).size);
      if (scenes) await this.deps.run(this.config.ffmpeg, cutSceneAudio(sourceAudio, ad ? speech : audio, scenes), {timeoutMs: 120000, signal: bounded});
      else await copyFile(sourceAudio, audio, constants.COPYFILE_EXCL);
      const audioInfo = await this.#probe(ad ? speech : audio, bounded);
      if (!audioInfo.has_audio) throw new WorkerError(422, 'AUDIO_AUSENTE', 'La pista no contiene audio.');
      if (scenes && Math.abs(audioInfo.duration_seconds - duration) > 0.02) {
        throw new WorkerError(422, 'ESCENAS_FUERA_DEL_AUDIO', 'Los cortes deben conservar audio en todos los intervalos.');
      }
      if (ad) {
        const track = await stageAdMusic(this.nas, this.scratch, request.project_id, work, bounded);
        await this.deps.run(this.config.ffmpeg, mixAdAudio(speech, track, audio, Boolean(ad.destination)), {timeoutMs: 120000, signal: bounded});
      }
      const measured = parseLoudness((await this.deps.run(this.config.ffmpeg, measureAudio(audio, duration), {timeoutMs: 1200000, signal: bounded})).stderr);
      const framesStarted = performance.now();
      const video = await this.#videoInput(request, bounded);
      const staticProres = !scenes && request.format === 'prores4444' && !video && request.durationInFrames > TITLE_FADE_FRAMES + 1;
      const videoBytes = video ? (await stat(video.source)).size : 0;
      let sourceVideo: string | undefined;
      let normalizeVideo = false;
      const overlayMode = video && overlayInputSupported(video.probe, request);
      if (video && scenes) {
        const info = parseProbe(video.probe);
        if (info.duration_seconds + 0.001 < Math.max(...scenes.map(cut => cut.source_end_frame)) / 30
          || info.color_transfer === 'smpte2084' || info.color_transfer === 'arib-std-b67') {
          throw new WorkerError(422, 'FUENTE_REEL_INVALIDA', 'Los cortes requieren vídeo SDR y deben quedar dentro de la fuente.');
        }
        sourceVideo = video.source;
      } else if (video && overlayMode) {
        await this.scratch.ensureSpace(videoBytes + (TITLE_FADE_FRAMES + 1) * frameBytes + outputs);
        const localVideo = path.join(work, 'crudo.mp4');
        await copyFile(video.source, localVideo, constants.COPYFILE_EXCL);
        const {stdout} = await this.deps.run(this.config.ffprobe, packetProbeArgs(localVideo), {timeoutMs: 30000, signal: bounded});
        if (overlayPacketTimingSupported(stdout, request.durationInFrames, overlayMode === 'normalized')) {
          sourceVideo = localVideo;
          normalizeVideo = overlayMode === 'normalized';
        }
      }
      const layerFrames = scenes ? graphicFrames(scenes, ad)
        : sourceVideo || staticProres ? Array.from({length: TITLE_FADE_FRAMES + 1}, (_, index) => index) : null;
      // La gráfica sin crudo solo renderiza el fundido. El margen del MOV se calcula tras codificar sus fragmentos.
      if (sceneProres) await this.scratch.ensureSpace((layerFrames?.length ?? 0) * frameBytes * 2);
      else if (scenes) {
        const clones = Math.floor(request.durationInFrames / linksPerImage);
        await this.scratch.ensureSpace(((layerFrames?.length ?? 0) + clones) * frameBytes + outputs);
      }
      else if (staticProres) await this.scratch.ensureSpace((TITLE_FADE_FRAMES + 1) * frameBytes * 2);
      else if (!sourceVideo) await this.scratch.ensureSpace(videoBytes + request.durationInFrames * frameBytes + outputs);
      const scene = await this.#scene(request, work, layerFrames, bounded, sourceVideo ? null : video?.source ?? null, sourceVideo ? true : undefined);
      const encodeStarted = performance.now();
      if (scene.frameCount !== (layerFrames?.length ?? request.durationInFrames)
        || scene.files.length !== (layerFrames?.length ?? 1)) throw new WorkerError(502, 'FOTOGRAMAS_INCOMPLETOS', 'La composición no produjo todos los fotogramas.');
      const framePattern = scenes && layerFrames && !sceneProres
        ? await timelineImages(work, layerFrames, scene.files, request.durationInFrames, bounded)
        : layerFrames ? path.join(work, 'frames', 'still-%d.png') : scene.files[0]!;
      const proresList = sceneProres && layerFrames
        ? await this.#prepareSceneProres(work, layerFrames, scene.files, request.durationInFrames, bounded)
        : staticProres
        ? await this.#prepareStaticProres(work, framePattern, scene.files.at(-1)!, request.durationInFrames, bounded)
        : null;
      let output = '', info: MediaInfo | undefined, finalAudio = measured;
      const retryPeak = request.durationInFrames > 1800 && (sourceVideo || proresList) ? -3.5 : -2;
      for (const peak of [-1.5, retryPeak]) {
        const extension = request.format === 'prores4444' ? 'mov' : 'mp4';
        const previousOutput = output;
        const correctingMp4 = peak === retryPeak && request.format === 'mp4';
        output = path.join(work, `${peak === -1.5 ? 'master' : 'master-corrected'}.${extension}`);
        const encode = correctingMp4
          ? correctMp4Audio(previousOutput, audio, output, request.durationInFrames, duration, measured, peak)
          : proresList
          ? muxProresFragments(proresList, audio, output, request.durationInFrames, duration, measured, peak)
          : encodeOutput(request.format, request.resolution, framePattern, audio, output, request.durationInFrames, duration, measured, peak, sourceVideo, normalizeVideo, scenes, ad ? AD_MUSIC_ID : undefined, Boolean(ad?.destination));
        await this.deps.run(this.config.ffmpeg, encode, {timeoutMs: 1200000, signal: bounded});
        const outputProbe = await this.#readProbe(output, bounded);
        if ((sourceVideo || proresList || correctingMp4 || scenes) && videoFrameCount(outputProbe) !== request.durationInFrames) {
          throw new WorkerError(502, 'FOTOGRAMAS_INCOMPLETOS', 'El máster no conserva todos los fotogramas esperados.');
        }
        if (proresList || normalizeVideo || scenes) {
          const {stdout} = await this.deps.run(this.config.ffprobe, packetProbeArgs(output), {timeoutMs: 30000, signal: bounded});
          if (!overlayPacketTimingSupported(stdout, request.durationInFrames)) {
            throw new WorkerError(502, 'FOTOGRAMAS_INCOMPLETOS', 'El máster no conserva las marcas de tiempo esperadas.');
          }
        }
        info = parseProbe(outputProbe);
        finalAudio = parseLoudness((await this.deps.run(this.config.ffmpeg, measureAudio(output, info.duration_seconds), {timeoutMs: 1200000, signal: bounded})).stderr);
        if (audioAccepted(finalAudio)) break;
      }
      if (!audioAccepted(finalAudio)) throw new WorkerError(422, 'AUDIO_FUERA_DE_OBJETIVO', 'El audio final no cumple sonoridad y pico verdadero.');
      const violations = info ? outputViolations(info,request) : ['metadata'];
      if (!info || violations.length) throw new WorkerError(502, 'VIDEO_FUERA_DE_CONTRATO', 'El archivo final no cumple el contrato técnico: '+violations.join(', ')+'.');
      const labelFrame = scenes && layerFrames?.find(frame => graphicLabel(scenes, frame));
      const alphaAt = labelFrame === undefined ? duration / 2 : labelFrame / 30;
      const alpha = request.transparent ? {...await this.#alpha(output, work, 'render', alphaAt, bounded), sample_seconds: alphaAt} : null;
      bounded.throwIfAborted();
      const publishStarted = performance.now();
      const files = await publish([output]);
      const finished = performance.now();
      const stage_ms = {
        preparacion_audio: Math.round(framesStarted - audioStarted),
        fotogramas: Math.round(encodeStarted - framesStarted),
        codificacion_y_control: Math.round(publishStarted - encodeStarted),
        publicacion_nas: Math.round(finished - publishStarted),
      };
      const render_pipeline = scenes ? 'capas_por_escena' : sourceVideo ? 'capas_remotion_nvenc' : staticProres ? 'prores_grafica_estatica' : 'fotogramas_remotion';
      return {files, codec: request.format === 'mp4' ? 'h264_nvenc' : 'prores_ks', hardware_acceleration: request.format === 'mp4', resolution: request.resolution, watermark: request.watermark, audio_verified: true, audio: finalAudio, alpha_verified: request.transparent && alpha?.has_transparency === true, alpha, media: info, elapsed_ms: Math.round(finished - started), stage_ms, render_pipeline};
    }));
  }
  async stills(request: StillsRequest, signal: AbortSignal) {
    const started = performance.now();
    return this.#withReceipt(request, 'stills', signal, publish => this.#job(signal, async (work, bounded) => {
      await this.scratch.ensureSpace(4 * 1024 ** 3);
      const video = await this.#videoInput(request, bounded);
      const scene = await this.#scene(request, work, request.frames, bounded, video?.source ?? null);
      if (scene.frameCount !== request.frames.length || scene.files.length !== request.frames.length) throw new WorkerError(502, 'CAPTURAS_INCOMPLETAS', 'La composición no produjo todas las capturas solicitadas.');
      const alpha = [];
      if (request.transparent) for (const [index, file] of scene.files.entries()) alpha.push(await this.#alpha(file, work, `still-${index}`, 0, bounded));
      return {files: await publish(scene.files), frames: request.frames, width: request.width, height: request.height, resolution: request.resolution, transparent: request.transparent, alpha_verified: request.transparent && alpha.length === scene.files.length, alpha, duration_seconds: request.durationInFrames / 30, watermark: request.watermark, elapsed_ms: Math.round(performance.now() - started)};
    }));
  }
  async inspect(request: InspectRequest, signal: AbortSignal) {
    const started = performance.now();
    return this.#job(signal, async (work, bounded) => {
      await this.scratch.ensureSpace(64 * 1024 ** 2);
      const input = await this.nas.input(request.input_path, request.project_id), media = await this.#probe(input, bounded);
      if (!media.video_codec || media.width < 1 || media.height < 1 || media.duration_seconds > 600) throw new WorkerError(422, 'INSPECCION_NO_PERMITIDA', 'Se requiere vídeo de hasta 600 segundos.');
      const audio = media.has_audio ? parseEbur128((await this.deps.run(this.config.ffmpeg, ebur128Args(input), {timeoutMs: 1200000, signal: bounded})).stderr) : null;
      const files: string[] = [], frames: {png: Buffer; seconds: number}[] = [];
      for (let index = 0; index < request.samples; index += 1) {
        const seconds = media.duration_seconds * (index + 0.5) / request.samples, output = path.join(work, `inspect-${index}.png`);
        await this.deps.run(this.config.ffmpeg, ['-hide_banner','-nostdin','-nostats','-n','-ss',String(seconds),'-i',input,'-frames:v','1','-an','-vf','scale=360:640:force_original_aspect_ratio=decrease,pad=360:640:(ow-iw)/2:(oh-ih)/2',output], {timeoutMs: 60000, signal: bounded});
        files.push(output); frames.push({seconds, png: await readFile(output)});
      }
      const sheet = path.join(work, 'contact-sheet.svg'); await writeFile(sheet, contactSheet(frames, media), {flag: 'wx'}); files.push(sheet);
      return {files: await this.#copyArtifacts(await this.nas.output(request.project_id, request.job_id, 'inspect'), files), media, audio, ruler: normalizedRuler(media), samples_seconds: frames.map((frame) => frame.seconds), elapsed_ms: Math.round(performance.now() - started)};
    });
  }
}
