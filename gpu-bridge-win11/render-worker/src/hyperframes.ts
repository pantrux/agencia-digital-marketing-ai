import {mkdir, lstat, rename, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {WorkerError, type RenderRequest} from './contracts.ts';
import {hyperframesHtml} from './hyperframes-html.ts';
import {stageHyperframesPicture} from './media.ts';
import type {runProcess} from './process.ts';

interface HyperframesOptions {
  request: RenderRequest;
  workspace: string;
  videoSource: string | null;
  browser: string;
  ffmpeg: string;
  signal: AbortSignal;
  run: typeof runProcess;
}

export function hyperframesCapture(frames: readonly number[], overlay: boolean, output: string): {args: string[]; files: string[]} {
  if (overlay) {
    return {
      args: ['render', '.', '--format', 'png-sequence', '--fps', '30', '--workers', '1',
        '--no-browser-gpu', '--frames-cache-dir', 'off', '--output', output],
      files: frames.map(frame => path.join(output, `frame_${String(frame + 1).padStart(6,'0')}.png`)),
    };
  }
  const times = frames.map(frame => (frame / 30).toFixed(3));
  return {
    args: ['snapshot', '.', '--at', times.join(','), '--keep', '--no-end', '--output', output],
    files: times.map(time => path.join(output, `at-${Number(time)}s.png`)),
  };
}

export async function renderHyperframes(options: HyperframesOptions, frames: number[], overlay: boolean) {
  const {request, workspace, videoSource, browser, ffmpeg, signal, run} = options;
  if (!overlay && (!videoSource || !request.props.scenes)) {
    throw new WorkerError(422,'HYPERFRAMES_FUENTE_AUSENTE','La vista previa requiere el vídeo del anuncio.');
  }
  const project = path.join(workspace,'hyperframes');
  await mkdir(project);
  await writeFile(path.join(project,'index.html'),hyperframesHtml(request,!overlay),{flag:'wx',signal});
  if (!overlay && videoSource && request.props.scenes) {
    await run(ffmpeg,stageHyperframesPicture(videoSource,request.props.scenes,request.resolution,path.join(project,'picture.mp4')),
      {timeoutMs:120000,signal});
  }
  const cli = path.resolve(import.meta.dirname,'../node_modules/hyperframes/bin/hyperframes.mjs');
  const {args,files} = hyperframesCapture(frames,overlay,path.join(workspace,'frames-hyperframes'));
  await run(process.execPath,[cli,...args],{
    timeoutMs:overlay ? 1100000 : 180000,signal,hyperframes:{browser,ffmpegDir:path.dirname(ffmpeg),project},
  });
  for (const file of files) {
    const info = await lstat(file).catch(() => null);
    if (!info?.isFile() || info.isSymbolicLink() || info.size < 64) {
      throw new WorkerError(502,'HYPERFRAMES_FOTOGRAMA_AUSENTE','Falta un fotograma de la composición HyperFrames.');
    }
  }
  if (overlay) return {files,frameCount:files.length};
  const normalized = frames.map(frame => path.join(workspace, `still-${frame}.png`));
  await Promise.all(files.map((file,index) => rename(file,normalized[index]!)));
  return {files:normalized,frameCount:normalized.length};
}
