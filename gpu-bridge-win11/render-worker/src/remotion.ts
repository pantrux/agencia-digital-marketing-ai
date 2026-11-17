import {bundle} from '@remotion/bundler';
import {makeCancelSignal, openBrowser, renderFrames, renderStill, selectComposition} from '@remotion/renderer';
import {mkdir, lstat} from 'node:fs/promises';
import path from 'node:path';
import {WorkerError, type RenderRequest} from './contracts.ts';
import {compositionCache} from './composition-cache.ts';

export function compositionInput(request: RenderRequest, videoFile: string | null): Record<string, unknown> {
  return {title: request.props.title, subtitle: request.props.subtitle, duration_seconds: request.durationInFrames / 30, background: request.props.background, accent: request.props.accent, watermark: request.watermark, video_file: videoFile, resolution: request.resolution, transparent: request.transparent,
    ...(request.props.subtitle_size && {subtitle_size: request.props.subtitle_size}),
    ...(request.props.scenes && {scenes: request.props.scenes}),
    ...(request.props.ad && {ad: request.props.ad})};
}
export interface SceneOptions {request: RenderRequest; scratch: string; cacheRoot: string; browser: string; videoSource: string | null; signal: AbortSignal; overlay?: true}
const defaultSdk = {bundle, makeCancelSignal, openBrowser, renderFrames, renderStill, selectComposition};
export type SceneDependencies = typeof defaultSdk;
export function createSceneRenderer(sdk: SceneDependencies = defaultSdk) {
return async function scene(options: SceneOptions, frames: number[] | null): Promise<{files: string[]; frameCount: number}> {
  const browserInfo = await lstat(options.browser);
  if (!browserInfo.isFile() || browserInfo.isSymbolicLink()) throw new WorkerError(503, 'NAVEGADOR_NO_VERIFICADO', 'Falta el navegador fijado.');
  options.signal.throwIfAborted();
  const cache = await compositionCache(options.cacheRoot, sdk.bundle);
  const serveUrl = await cache.ready();
  options.signal.throwIfAborted();
  const lease = await cache.stageVideo(options.videoSource);
  let browser: Awaited<ReturnType<typeof openBrowser>> | undefined;
  let abort: (() => void) | undefined;
  try {
    browser = await sdk.openBrowser('chrome', {browserExecutable: options.browser, logLevel: 'error', chromiumOptions: {enableMultiProcessOnLinux: false}});
    const {cancel, cancelSignal} = sdk.makeCancelSignal();
    abort = (): void => cancel(); options.signal.addEventListener('abort', abort, {once: true});
    options.signal.throwIfAborted();
    const inputProps = compositionInput(options.request, lease.videoFile);
    if (options.overlay) {
      inputProps.transparent = true;
      inputProps.overlay_only = true;
    }
    const composition = await sdk.selectComposition({serveUrl, id: 'AgencyVertical', inputProps, puppeteerInstance: browser, browserExecutable: options.browser, logLevel: 'error'});
    if (composition.width !== options.request.width || composition.height !== options.request.height || composition.fps !== options.request.fps || composition.durationInFrames !== options.request.durationInFrames) throw new WorkerError(502, 'COMPOSICION_FUERA_DE_CONTRATO', 'La composición no conserva las dimensiones o duración autorizadas.');
    const outputDir = path.join(options.scratch, 'frames'); await mkdir(outputDir);
    if (frames) {
      const files: string[] = [];
      for (const frame of frames) {
        const output = path.join(outputDir, `still-${frame}.png`);
        await sdk.renderStill({serveUrl, composition, inputProps, output, frame, imageFormat: 'png', overwrite: false, puppeteerInstance: browser, browserExecutable: options.browser, cancelSignal, logLevel: 'error', isProduction: true}); files.push(output);
      }
      return {files, frameCount: files.length};
    }
    const useParallelFrames = options.request.resolution === '1080p'
      && options.videoSource !== null && composition.durationInFrames >= 30 * composition.fps;
    const imageFormat = useParallelFrames && options.request.format === 'mp4' ? 'jpeg' : 'png';
    const result = await sdk.renderFrames({
      serveUrl, composition, inputProps, outputDir, imageFormat,
      ...(imageFormat === 'jpeg' ? {jpegQuality: 95} : {}),
      imageSequencePattern: 'frame-[frame].[ext]', onStart: () => {}, onFrameUpdate: () => {},
      muted: true, concurrency: useParallelFrames ? 4 : 1, puppeteerInstance: browser,
      browserExecutable: options.browser, cancelSignal, logLevel: 'error', timeoutInMilliseconds: 60000,
    });
    return {files: [path.join(outputDir, `frame-%0${String(result.frameCount - 1).length}d.${imageFormat}`)], frameCount: result.frameCount};
  } finally {
    if (abort) options.signal.removeEventListener('abort', abort);
    try {if (browser) await browser.close({silent: true});}
    finally {await lease.release();}
  }
};
}
export const renderScene = createSceneRenderer();
