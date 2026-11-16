import { createHmac } from 'node:crypto';
import { describe, expect, it, vi } from 'vitest';
import { publishD1Request } from '../cloudflare-saas/studio-d1-backup/src/index.js';

const key = '7b'.repeat(32);

describe('Cron del respaldo D1', () => {
  it.each([
    ['2026-11-15T06:00:00Z', '2026-11-15', '2026-11-15T06:00:00Z'],
    ['2026-07-15T07:00:00Z', '2026-07-15', '2026-07-15T07:00:00Z'],
    ['2026-11-15T06:02:00Z', '2026-11-15', '2026-11-15T06:00:00Z'],
    ['2026-07-15T07:05:00Z', '2026-07-15', '2026-07-15T07:00:00Z'],
  ])('firma la misma orden de las 03:00 para %s', async (instant, day, scheduled) => {
    const put = vi.fn(async () => ({}));
    const millis = Date.parse(scheduled);
    await expect(publishD1Request(Date.parse(instant), { REQUESTS: { put }, TRIGGER_KEY_HEX: key })).resolves.toBe('publicada');
    const signature = createHmac('sha256', Buffer.from(key, 'hex'))
      .update(`studio-d1-clock-v1\n${day}\n${millis}\n`).digest('hex');
    expect(put).toHaveBeenCalledWith(`d1-clock/v1/${day}.json`, JSON.stringify({ day, scheduled_ms: millis, signature }), {
      onlyIf: new Headers({ 'If-None-Match': '*' }),
      httpMetadata: { contentType: 'application/json' },
    });
  });

  it.each(['2026-11-15T07:00:00Z', '2026-07-15T06:00:00Z', '2026-11-15T06:00:01Z',
    '2026-11-15T06:06:00Z', '2026-11-15T06:04:00Z'])('omite otros instantes %s', async (instant) => {
    const put = vi.fn();
    await expect(publishD1Request(Date.parse(instant), { REQUESTS: { put }, TRIGGER_KEY_HEX: key })).resolves.toBe('fuera-de-horario');
    expect(put).not.toHaveBeenCalled();
  });

  it('preserva una orden ya publicada', async () => {
    const put = vi.fn(async () => null);
    await expect(publishD1Request(Date.parse('2026-11-15T06:00:00Z'), { REQUESTS: { put }, TRIGGER_KEY_HEX: key })).resolves.toBe('existente');
    expect(put).toHaveBeenCalledTimes(1);
  });

  it('rechaza una clave inválida antes de publicar', async () => {
    const put = vi.fn();
    await expect(publishD1Request(Date.parse('2026-11-15T06:00:00Z'), { REQUESTS: { put }, TRIGGER_KEY_HEX: 'CHANGE_ME' })).rejects.toThrow('CLAVE_DE_PROGRAMACION');
    expect(put).not.toHaveBeenCalled();
  });
});
