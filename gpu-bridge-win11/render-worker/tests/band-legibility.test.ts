import {execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {mkdtemp, realpath, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {parseRender, TITLE_BAND_HEIGHT} from '../src/contracts.ts';
import {createSceneRenderer} from '../src/remotion.ts';
import {ScratchSpace} from '../src/scratch.ts';

const browser = process.env.GPU_REMOTION_BROWSER_PATH;

function lineBounds(file: string, scale: number) {
  const width = 1080 * scale;
  const height = TITLE_BAND_HEIGHT * scale;
  const pixels = execFileSync('ffmpeg', [
    '-v', 'error', '-nostdin', '-i', file, '-vf', `crop=${width}:${height + 2}:0:0`,
    '-frames:v', '1', '-pix_fmt', 'rgba', '-f', 'rawvideo', 'pipe:1',
  ], {timeout: 15_000, maxBuffer: width * (height + 2) * 4 + 1024});
  const stride = width * 4;
  assert.equal(pixels.length, stride * (height + 2));
  const lines: {top: number; bottom: number}[] = [];
  for (let row = 0; row < height; row++) {
    let hasText = false;
    // El borde de color queda a la izquierda de esta columna.
    for (let i = row * stride + 100 * scale * 4; i < (row + 1) * stride; i += 4) {
      if (pixels[i]! > 175 && pixels[i + 1]! > 185 && pixels[i + 2]! > 175) {
        hasText = true;
        break;
      }
    }
    if (!hasText) continue;
    const prev = lines.at(-1);
    if (prev?.bottom === row - 1) prev.bottom = row;
    else lines.push({top: row, bottom: row});
  }
  assert.equal(pixels[height * stride + 4 * 100 + 3], 0, 'El área del vídeo debe conservar el alfa');
  assert.ok(lines.length > 1, 'El título y el subtítulo tienen que quedar visibles');
  assert.ok(lines[0]!.top >= 12 * scale && lines.at(-1)!.bottom < height - 12 * scale);
  return lines;
}

test('Remotion amplía la banda sin cortar el texto ni pintar sobre el vídeo', {
  skip: browser ? false : 'GPU_REMOTION_BROWSER_PATH permite ejecutar la captura real',
  timeout: 95_000,
}, async () => {
  assert.ok(browser);
  const temporary = await realpath(tmpdir());
  const root = await mkdtemp(path.join(temporary, 'studio-tipografia-banda-'));
  try {
    const cache = await ScratchSpace.create(root);
    const render = createSceneRenderer();
    const signal = AbortSignal.timeout(85_000);
    const common = {title: 'El Rincón de las Sansevieras', subtitle: 'Conoce sus plantas y maceteros con la voz original.',
      title_layout: 'band', duration_seconds: 2, audio_path: 'proyectos/banda/03_assets/voz.wav'};
    const examples = [
      {resolution: '1080p', props: common, scale: 1},
      {resolution: '1080p', props: {...common, subtitle_size: 'large'}, scale: 1},
      {resolution: '4k', props: {...common, title: 'W'.repeat(42), subtitle: 'W'.repeat(64), subtitle_size: 'large'}, scale: 2},
    ];
    const heights = [];
    for (const [index, {resolution, props, scale}] of examples.entries()) {
      const request = parseRender({project_id: 'banda', job_id: `texto-${index}`, composition: 'AgencyVertical',
        plan: 'Ilimitado', format: 'prores4444', resolution, props});
      const output = await render({request, scratch: await cache.job(), cacheRoot: cache.root, browser, videoSource: null, signal}, [12]);
      const last = lineBounds(output.files[0]!, scale).at(-1)!;
      heights.push(last.bottom - last.top + 1);
    }
    assert.ok(heights[1]! > heights[0]!, 'La opción ampliada debe aumentar la altura visible del subtítulo');
  } finally {
    assert.equal(path.dirname(await realpath(root)), temporary);
    await rm(root, {recursive: true, force: true});
  }
});
