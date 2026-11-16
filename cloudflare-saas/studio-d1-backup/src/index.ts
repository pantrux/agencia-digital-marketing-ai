interface D1ClockEnv {
  REQUESTS: {
    put(key: string, value: string, options: {
      onlyIf: Headers;
      httpMetadata: { contentType: string };
    }): Promise<object | null>;
  };
  TRIGGER_KEY_HEX: string;
}

const santiago = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/Santiago',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const utf8 = new TextEncoder();

export async function publishD1Request(scheduledTime: number, env: D1ClockEnv) {
  if (!Number.isSafeInteger(scheduledTime) || scheduledTime % 60_000 !== 0) return 'fuera-de-horario';
  const parts = Object.fromEntries(santiago.formatToParts(scheduledTime).map(({ type, value }) => [type, value]));
  const minute = Number(parts.minute);
  if (parts.hour !== '03' || ![0, 2, 5].includes(minute)) return 'fuera-de-horario';
  const day = `${parts.year}-${parts.month}-${parts.day}`;
  const scheduled = scheduledTime - minute * 60_000;
  if (!/^[a-f0-9]{64}$/.test(env.TRIGGER_KEY_HEX)) throw new Error('CLAVE_DE_PROGRAMACION');

  const keyBytes = Uint8Array.from(env.TRIGGER_KEY_HEX.match(/../g)!, (hex) => parseInt(hex, 16));
  const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const body = utf8.encode(`studio-d1-clock-v1\n${day}\n${scheduled}\n`);
  const signed = await crypto.subtle.sign('HMAC', key, body);
  const signature = Array.from(new Uint8Array(signed), (byte) => byte.toString(16).padStart(2, '0')).join('');
  const result = await env.REQUESTS.put(`d1-clock/v1/${day}.json`, JSON.stringify({ day, scheduled_ms: scheduled, signature }), {
    onlyIf: new Headers({ 'If-None-Match': '*' }),
    httpMetadata: { contentType: 'application/json' },
  });
  return result ? 'publicada' : 'existente';
}

export default {
  async scheduled(event: { scheduledTime: number }, env: D1ClockEnv): Promise<void> {
    const result = await publishD1Request(event.scheduledTime, env);
    console.info({ evento: 'orden-d1', fecha_programada_ms: event.scheduledTime, resultado: result });
  },
};
