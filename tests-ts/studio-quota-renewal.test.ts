import { expect, it } from "vitest";
import { quotaNotice } from "../cloudflare-saas/studio-portal-web/app/quota.js";

it("muestra el reinicio de diciembre en la hora oficial de Santiago", () => {
  expect(quotaNotice({ monthly_reels: 1, used: 1, period_start: "2026-11-01" }))
    .toBe("Alcanzaste el límite de producciones del mes. Tu cupo se renueva el 30 de noviembre a las 21:00 (hora de Santiago).");
});

it("respeta el cambio de año y la hora de invierno", () => {
  expect(quotaNotice({ monthly_reels: 1, used: 1, period_start: "2026-12-01" }))
    .toContain("31 de diciembre a las 21:00");
  expect(quotaNotice({ monthly_reels: 12, used: 12, period_start: "2026-06-01" }))
    .toContain("30 de junio a las 20:00");
});

it("omite el aviso si quedan producciones o el plan es ilimitado", () => {
  expect(quotaNotice({ monthly_reels: 1, used: 0, period_start: "2026-11-01" })).toBeNull();
  expect(quotaNotice({ monthly_reels: null, used: 180, period_start: "2026-11-01" })).toBeNull();
});

it("omite la fecha si el período no está disponible o es inválido", () => {
  const plan = { monthly_reels: 1, used: 2 };
  expect(quotaNotice(plan)).toBe("Alcanzaste el límite de producciones del mes.");
  expect(quotaNotice({ ...plan, period_start: "2026-13-01" })).toBe(quotaNotice(plan));
});
