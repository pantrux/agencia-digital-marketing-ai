import {WorkerError} from './contracts.ts';

export function normalizedRuler(source: {width: number; height: number}) {
  if (![source.width, source.height].every((size) => Number.isSafeInteger(size) && size > 0)) throw new WorkerError(422, 'QA_INVALIDO', 'Las dimensiones originales no son válidas.');
  const scale = Math.min(1080 / source.width, 1920 / source.height);
  return {y_min: 0, y_max: 1920, canvas_width: 1080, canvas_height: 1920, original_width: source.width, original_height: source.height, source_scale: scale, source_x: (1080 - source.width * scale) / 2, source_y: (1920 - source.height * scale) / 2};
}
export function contactSheet(frames: {png: Buffer; seconds: number}[], source: {width: number; height: number} = {width: 1080, height: 1920}): string {
  if (frames.length < 1 || frames.length > 12 || frames.some((frame) => !Number.isFinite(frame.seconds) || frame.seconds < 0 || frame.png.length > 4 * 1024 * 1024)) throw new WorkerError(422, 'QA_INVALIDO', 'La tira QA supera sus límites.');
  const columns = Math.min(3, frames.length), cellWidth = 444, cellHeight = 700;
  const ruler = normalizedRuler(source);
  const cells = frames.map((frame, index) => {
    const ticks = Array.from({length: 9}, (_, tick) => `<line x1="360" y1="${tick * 80}" x2="370" y2="${tick * 80}" stroke="#fff"/><text x="374" y="${Math.max(16, tick * 80 + 5)}">Y=${tick * ruler.y_max / 8}</text>`).join('');
    return `<g transform="translate(${index % columns * cellWidth},${Math.floor(index / columns) * cellHeight + 30})"><image width="360" height="640" href="data:image/png;base64,${frame.png.toString('base64')}"/>${ticks}<text x="8" y="666">${frame.seconds.toFixed(3)} s · QA 1080×1920 · origen ${source.width}×${source.height}</text></g>`;
  }).join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${columns * cellWidth}" height="${Math.ceil(frames.length / columns) * cellHeight}" viewBox="0 0 ${columns * cellWidth} ${Math.ceil(frames.length / columns) * cellHeight}"><rect width="100%" height="100%" fill="#18232e"/><g fill="#fff" font-family="sans-serif" font-size="14">${cells}</g></svg>`;
}
