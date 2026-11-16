type Quota = { monthly_reels: number | null; used: number; period_start?: string };

const santiagoDay = new Intl.DateTimeFormat("es-CL", {
  timeZone: "America/Santiago", day: "numeric", month: "long",
});
const santiagoTime = new Intl.DateTimeFormat("es-CL", {
  timeZone: "America/Santiago", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

export function quotaNotice({ monthly_reels, used, period_start }: Quota): string | null {
  if (monthly_reels === null || used < monthly_reels) return null;
  const exhausted = "Alcanzaste el límite de producciones del mes.";
  if (!period_start || !/^\d{4}-\d{2}-01$/.test(period_start)) return exhausted;
  const renewal = new Date(`${period_start}T00:00:00Z`);
  if (Number.isNaN(renewal.getTime())) return exhausted;
  renewal.setUTCMonth(renewal.getUTCMonth() + 1);
  return `${exhausted} Tu cupo se renueva el ${santiagoDay.format(renewal)} a las ${santiagoTime.format(renewal)} (hora de Santiago).`;
}
