"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { observeBrowserTimings } from "./browser-timing";
import { quotaNotice } from "./quota";
import { uploadVideo, type OutputFormat, type OutputResolution } from "./upload";

type Person = { email: string; display_name: string | null };
type Plan = { plan_id: string; monthly_reels: number | null; used: number; max_resolution: OutputResolution;
  allow_prores4444: 0 | 1; production_available: boolean; period_start?: string };
type BrandFields = { display_name: string; audience: string; tone: string };
type Brand = BrandFields & { id: string };
type Order = { id: string; project_slug: string; status: string; created_at: number;
  brand?: Pick<Brand, "id" | "display_name">;
  received_at?: number | null; brief_confirmed_at?: number | null; production_issue?: "qa_rechazado" | "requiere_revision" | null };
type BriefFields = { objective: string; audience: string; tone: string };
type BriefStatus = { brief: (BriefFields & { confirmed_at: number }) | null; ready: boolean; brand?: Brand };
const emptyBrand: BrandFields = { display_name: "", audience: "", tone: "" };
type Kit = { id: string; title: string; url: string; prores?: { bytes: number; url: string } };
type Dashboard = { person: Person; plan: Plan; orders: Order[]; kits: Kit[] };
type Checkout = { checkout_id: string; checkout_enabled: boolean; checkout_url: string | null; billing_status: string; status: string };
type NoCheckout = { checkout: null; checkout_enabled: boolean };
type Section = "produccion" | "entregas" | "plan";
const statusRefreshMs = 5000;
const plans = [
  { id: "free", name: "Free", reels: "1 reel al mes", summary: "1080p · 7 días de retención" },
  { id: "pro", name: "Pro", reels: "12 reels al mes", summary: "1080p · 30 días de retención" },
  { id: "ultra", name: "Ultra", reels: "40 reels al mes", summary: "Hasta 4K · 90 días de retención" },
  { id: "ilimitado", name: "Ilimitado", reels: "Reels sin tope mensual", summary: "Hasta 4K · ProRes 4444" },
];
const states: Record<string, string> = { queued: "En preparación", processing: "En producción", completed: "Entregado", failed: "Requiere revisión", cancelled: "Cancelado" };
const awaitingDelivery = (order: Order) => (order.status === "queued" || order.status === "processing")
  && order.production_issue !== "requiere_revision";
const deliveryDelaySeconds = 6 * 60;

function productionStatus(order: Order, now: number): string {
  if (order.status === "failed" && order.production_issue === "qa_rechazado") return "Revisión de calidad rechazada";
  if (order.production_issue === "requiere_revision") return "Requiere revisión manual";
  if (order.status === "processing") {
    if (order.brief_confirmed_at != null) {
      return now - order.brief_confirmed_at >= deliveryDelaySeconds ? "En producción · con demora" : "En producción";
    }
    if (order.received_at != null) return "Pendiente de brief";
    if (order.received_at === null) return "Verificando recepción del vídeo";
  }
  return states[order.status] ?? order.status;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const result = await fetch("/api" + path, { credentials: "same-origin", ...init });
  const body = await result.json().catch(() => ({}));
  if (!result.ok) throw new Error((body as { error?: string }).error || "SERVICIO_NO_DISPONIBLE");
  return body as T;
}

function message(error: unknown): string {
  const code = error instanceof Error ? error.message : "";
  const messages: Record<string, string> = {
    CUOTA_AGOTADA: "Alcanzaste el límite de producciones del mes.",
    PRODUCCION_NO_HABILITADA: "La creación de producciones todavía no está habilitada para esta cuenta.",
    ORDEN_CANCELADA: "La reserva anterior se cerró. Pulsa «Subir e iniciar» para volver a empezar.",
    RESOLUCION_FUERA_DEL_PLAN: "Tu plan admite salidas de hasta 1080p. Selecciona 1080p para continuar.",
    FORMATO_FUERA_DEL_PLAN: "El formato ProRes 4444 está disponible con el plan Ilimitado.",
    MARCAS_FUERA_DEL_PLAN: "La gestión de marcas está disponible con el plan Ilimitado.",
    MARCA_NO_ENCONTRADA: "Esta marca ya no está disponible para tu cuenta.",
    MARCA_INVALIDA: "Revisa el nombre, la audiencia y el tono de la marca.",
    NOMBRE_DE_MARCA_EN_USO: "Ya existe una marca con ese nombre.",
    AJUSTE_INVALIDO: "Escribe las instrucciones del ajuste en hasta 800 caracteres.",
    AJUSTE_EN_CURSO: "Ya hay un ajuste en revisión.",
    REVISION_NO_PENDIENTE: "La producción ya cambió de estado. Actualiza la revisión.",
    REVISION_CAMBIO_REINTENTAR: "La revisión cambió. Comprueba su estado antes de reintentar.",
    AJUSTES_AGOTADOS: "Ya usaste los ajustes disponibles para esta producción.",
    AJUSTES_FUERA_DEL_PLAN: "Este plan no incluye ajustes humanos.",
    IDEMPOTENCIA_EN_CONFLICTO: "Ya existe una reserva para este archivo. Usa la misma resolución, formato y marca para retomarla.",
    ARCHIVO_INVALIDO: "Selecciona un vídeo MP4, MOV o WebM de hasta 1 GiB.",
    VIDEO_SUPERA_DURACION: "El vídeo supera los 10 minutos. Recórtalo antes de subirlo.",
    VIDEO_NO_VERIFICABLE: "Tu navegador no pudo leer el vídeo. Prueba con un MP4 en H.264.",
    SUBIDA_NO_DISPONIBLE: "La carga de vídeos aún está en preparación.",
    PAGO_NO_DISPONIBLE: "La activación de planes pagados todavía está en preparación.",
    CHECKOUT_DE_PRUEBA_PAUSADO: "Los pagos de prueba están suspendidos.",
    CHECKOUT_EN_CURSO: "Ya tienes una suscripción de prueba en curso. Usa «Retomar suscripción de prueba».",
    CHAT_NO_DISPONIBLE: "El chat aún no está disponible para esta producción.",
    BRIEF_ESPERA_CRUDO: "La copia del vídeo todavía está en preparación. Vuelve a comprobar el estado.",
    BRIEF_YA_CONFIRMADO: "Esta producción ya tiene un brief confirmado. Vuelve a comprobar el estado.",
    BRIEF_INVALIDO: "Completa un objetivo y una audiencia dentro de los límites del formulario.",
  };
  return messages[code] ?? "La operación quedó pendiente. Vuelve a intentarlo.";
}

function paymentLink(value: string): string {
  const url = new URL(value);
  if (url.origin !== "https://www.mercadopago.cl" || url.username || url.password
    || url.pathname !== "/subscriptions/checkout" || url.hash) throw new Error("PAGO_NO_DISPONIBLE");
  return url.href;
}

function ConfirmBrief({ order }: { order: string }) {
  const path = `/orders/${encodeURIComponent(order)}/brief`;
  const [draft, setDraft] = useState<Partial<BriefFields>>({});
  const [current, setCurrent] = useState<BriefStatus | null>(null);
  const [pending, setPending] = useState(false);
  const [fault, setFault] = useState("");
  const [checkAttempt, setCheckAttempt] = useState(0);
  const waitingForVideo = !current?.ready && !current?.brief;
  const fields = {
    objective: draft.objective ?? "",
    audience: draft.audience ?? current?.brand?.audience ?? "",
    tone: draft.tone ?? current?.brand?.tone ?? "",
  };

  useEffect(() => {
    if (!waitingForVideo) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function check() {
      try {
        const result = await api<BriefStatus>(path, { signal: controller.signal, cache: "no-store" });
        if (controller.signal.aborted) return;
        setCurrent(result);
        setFault("");
        if (result.ready || result.brief) return;
      } catch (error) {
        if (controller.signal.aborted) return;
        setFault(message(error));
        if (error instanceof Error && ["AUTENTICACION_REQUERIDA", "ORDEN_NO_ENCONTRADA"].includes(error.message)) return;
      }
      timer = setTimeout(() => void check(), statusRefreshMs);
    }
    void check();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, checkAttempt, waitingForVideo]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending || current?.brief || !fields.objective.trim() || !fields.audience.trim()) return;
    setPending(true);
    setFault("");
    try {
      setCurrent(await api<BriefStatus>(path, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fields),
      }));
    } catch (error) { setFault(message(error)); }
    finally { setPending(false); }
  }

  const confirmed = current?.brief;
  return <div className="production-brief">
    {current?.brand && <p>Marca: {current.brand.display_name}</p>}
    {fault && <p role="alert">{fault}</p>}
    {confirmed ? <div aria-live="polite"><p><strong>Brief confirmado</strong></p>
      <dl><dt>Objetivo</dt><dd>{confirmed.objective}</dd><dt>Audiencia</dt><dd>{confirmed.audience}</dd>
        {confirmed.tone && <><dt>Tono</dt><dd>{confirmed.tone}</dd></>}</dl></div>
      : <form onSubmit={submit}>
        <label htmlFor="brief-objective">Objetivo</label>
        <textarea id="brief-objective" rows={2} required maxLength={800} value={fields.objective} disabled={pending}
          onChange={(event) => setDraft({ ...draft, objective: event.currentTarget.value })} />
        <label htmlFor="brief-audience">Audiencia</label>
        <textarea id="brief-audience" rows={2} required maxLength={480} value={fields.audience} disabled={pending}
          onChange={(event) => setDraft({ ...draft, audience: event.currentTarget.value })} />
        <label htmlFor="brief-tone">Tono (opcional)</label>
        <textarea id="brief-tone" rows={1} maxLength={240} value={fields.tone} disabled={pending}
          onChange={(event) => setDraft({ ...draft, tone: event.currentTarget.value })} />
        {current?.ready ? <button className="button" type="submit" disabled={pending || !fields.objective.trim() || !fields.audience.trim()}>Confirmar brief</button>
          : <button className="button secondary" type="button" disabled={pending} onClick={() => setCheckAttempt((attempt) => attempt + 1)}>Comprobar recepción del vídeo</button>}
        {current && !current.ready && <p role="status">Podrás confirmar el brief cuando se verifique la copia del vídeo.</p>}
      </form>}
  </div>;
}

type ManualState = {
  paused: boolean; available: boolean; included: boolean; remaining: number | null;
  adjustment: { status: "pending" | "ready" | "needs_review" } | null;
};

function ManualAdjustment({ order, onResumed }: { order: string; onResumed: () => Promise<void> }) {
  const path = `/orders/${encodeURIComponent(order)}/manual-adjustment`;
  const [state, setState] = useState<ManualState | null>(null);
  const [instructions, setInstructions] = useState("");
  const [sending, setSending] = useState(false);
  const [fault, setFault] = useState("");
  const [check, setCheck] = useState(0);
  const attempt = useRef<{ instructions: string; key: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      try {
        const result = await api<ManualState>(path, { signal: controller.signal, cache: "no-store" });
        if (controller.signal.aborted) return;
        setState(result);
        if (!result.paused) {
          await onResumed();
          return;
        }
        if (result.adjustment?.status !== "pending" && result.adjustment?.status !== "ready") return;
      } catch (error) {
        if (controller.signal.aborted) return;
        setFault(message(error));
        if (error instanceof Error && ["AUTENTICACION_REQUERIDA", "ORDEN_NO_ENCONTRADA"].includes(error.message)) return;
      }
      timer = setTimeout(() => void read(), statusRefreshMs);
    }
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, check, onResumed]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const text = instructions.trim();
    if (sending || !text || !state?.available) return;
    if (attempt.current?.instructions !== text) attempt.current = { instructions: text, key: crypto.randomUUID() };
    setSending(true);
    setFault("");
    try {
      const { adjustment } = await api<{ adjustment: ManualState["adjustment"] }>(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": attempt.current.key },
        body: JSON.stringify({ instructions: text }),
      });
      setState({ ...state, adjustment, available: false });
      setInstructions("");
      attempt.current = null;
    } catch (error) {
      setFault(message(error));
    } finally {
      setSending(false);
      setCheck((value) => value + 1);
    }
  }

  const status = state?.adjustment?.status;
  return <div className="production-brief">
    <h3>Revisión manual</h3>
    {fault && <p role="alert">{fault}</p>}
    {status === "pending" && <p role="status">Valeria está revisando tu ajuste.</p>}
    {status === "needs_review" && <p role="status">El ajuste se revisó. La producción todavía requiere intervención.</p>}
    {state?.paused && !state.included && <p>Este plan no incluye ajustes humanos.</p>}
    {state?.paused && state.included && state.remaining === 0 && status !== "pending" && <p>Ya usaste los ajustes disponibles.</p>}
    {state?.available && <form onSubmit={submit}>
      <label htmlFor="manual-instructions">Instrucciones del ajuste</label>
      <textarea id="manual-instructions" rows={3} required maxLength={800} disabled={sending} value={instructions}
        onChange={(event) => setInstructions(event.currentTarget.value)} />
      {state.remaining !== null && <p>Ajustes disponibles: {state.remaining}</p>}
      <button className="button secondary" type="submit" disabled={sending || !instructions.trim()}>Enviar ajuste</button>
    </form>}
  </div>;
}

function BrandEditor({ brands, onSave }: { brands: Brand[]; onSave: (brand: Brand) => void }) {
  const [editId, setEditId] = useState("");
  const [fields, setFields] = useState(emptyBrand);
  const [busy, setBusy] = useState(false);
  const [fault, setFault] = useState("");
  const [saved, setSaved] = useState(false);

  function choose(id: string) {
    const brand = brands.find((candidate) => candidate.id === id);
    setEditId(brand?.id ?? "");
    setFields(brand ? { display_name: brand.display_name, audience: brand.audience, tone: brand.tone } : emptyBrand);
    setFault("");
    setSaved(false);
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy || !fields.display_name.trim()) return;
    setBusy(true);
    setFault("");
    setSaved(false);
    try {
      const path = editId ? "/brands/" + encodeURIComponent(editId) : "/brands";
      const { brand } = await api<{ brand: Brand }>(path, {
        method: editId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(fields),
      });
      const { id, ...data } = brand;
      setEditId(id);
      setFields(data);
      onSave(brand);
      setSaved(true);
    } catch (error) { setFault(message(error)); }
    finally { setBusy(false); }
  }

  function change(field: keyof BrandFields, value: string) {
    setFields((current) => ({ ...current, [field]: value }));
    setSaved(false);
  }

  return <section className="panel recent">
    <h2>Marcas</h2>
    {brands.length > 0 && <div className="production-choice">
      <label htmlFor="brand-edit">Perfil de marca</label>
      <select id="brand-edit" value={editId} disabled={busy} onChange={(event) => choose(event.currentTarget.value)}>
        <option value="">Crear una marca</option>
        {brands.map((brand) => <option key={brand.id} value={brand.id}>{brand.display_name}</option>)}
      </select>
    </div>}
    <form onSubmit={save}>
      <label htmlFor="brand-name">Nombre de la marca</label>
      <input id="brand-name" type="text" required maxLength={80} value={fields.display_name}
        disabled={busy} onChange={(event) => change("display_name", event.currentTarget.value)} />
      <label htmlFor="brand-audience">Audiencia habitual</label>
      <textarea id="brand-audience" rows={2} maxLength={480} value={fields.audience}
        disabled={busy} onChange={(event) => change("audience", event.currentTarget.value)} />
      <label htmlFor="brand-tone">Tono habitual</label>
      <textarea id="brand-tone" rows={1} maxLength={240} value={fields.tone}
        disabled={busy} onChange={(event) => change("tone", event.currentTarget.value)} />
      <button className="button secondary" type="submit" disabled={busy || !fields.display_name.trim()}>
        {editId ? "Guardar cambios" : "Crear marca"}
      </button>
      {fault && <p role="alert">{fault}</p>}
      {saved && <p role="status">Perfil guardado.</p>}
    </form>
  </section>;
}

export default function Home() {
  const [person, setPerson] = useState<Person | null | undefined>(undefined);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [kits, setKits] = useState<Kit[]>([]);
  const [brands, setBrands] = useState<Brand[] | null>(null);
  const [brandFault, setBrandFault] = useState("");
  const [brandRetry, setBrandRetry] = useState(0);
  const [selectedBrand, setSelectedBrand] = useState("");
  const [section, setSection] = useState<Section>("produccion");
  const [file, setFile] = useState<File | null>(null);
  const [resolution, setResolution] = useState<OutputResolution>("1080p");
  const [format, setFormat] = useState<OutputFormat>("mp4");
  const [progress, setProgress] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [chatPending, setChatPending] = useState(false);
  const chatAttempt = useRef<{ order: string; message: string; key: string } | null>(null);
  const [selected, setSelected] = useState("");
  const [checkoutId, setCheckoutId] = useState<string | null>(null);
  const [billingStatus, setBillingStatus] = useState("");
  const [billingBusy, setBillingBusy] = useState(false);
  const [checkoutAvailable, setCheckoutAvailable] = useState<boolean | null>(null);
  const [cloudError, setCloudError] = useState(false);
  const [ordersCheckedAt, setOrdersCheckedAt] = useState(0);
  const hasActiveProduction = orders.some(awaitingDelivery);
  const renewalNotice = plan?.production_available ? quotaNotice(plan) : null;
  const unlimited = plan?.plan_id === "ilimitado";
  const refreshAfterAdjustment = useCallback(async () => {
    const { orders: current } = await api<{ orders: Order[] }>("/orders", { cache: "no-store" });
    setOrders(current);
    setOrdersCheckedAt(Math.floor(Date.now() / 1000));
  }, []);

  useEffect(() => {
    if (!person || !unlimited) return;
    const controller = new AbortController();
    void api<{ brands: Brand[] }>("/brands", { signal: controller.signal, cache: "no-store" })
      .then((result) => {
        if (controller.signal.aborted) return;
        setBrands(result.brands);
        setBrandFault("");
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) setBrandFault(message(error));
      });
    return () => controller.abort();
  }, [person, unlimited, brandRetry]);

  async function refresh() {
    const [quota, productions, deliveries] = await Promise.all([
      api<Plan>("/plan"), api<{ orders: Order[] }>("/orders"), api<{ kits: Kit[] }>("/kits"),
    ]);
    setPlan(quota);
    setOrders(productions.orders);
    setOrdersCheckedAt(Math.floor(Date.now() / 1000));
    setKits(deliveries.kits);
    return productions.orders[0]?.id;
  }

  useEffect(() => {
    if (!person || !hasActiveProduction || busy) return;
    const controller = new AbortController();
    const options = { signal: controller.signal, cache: "no-store" } as const;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function checkOrders() {
      try {
        const { orders: current } = await api<{ orders: Order[] }>("/orders", options);
        if (controller.signal.aborted) return;
        const active = current.some(awaitingDelivery);
        if (section === "entregas" || !active) {
          const { kits: delivered } = await api<{ kits: Kit[] }>("/kits", options);
          if (controller.signal.aborted) return;
          setKits(delivered);
        }
        setOrders(current);
        setOrdersCheckedAt(Math.floor(Date.now() / 1000));
        if (!active) return;
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof Error && error.message === "AUTENTICACION_REQUERIDA") {
          setPerson(null);
          return;
        }
      }
      timer = setTimeout(() => void checkOrders(), statusRefreshMs);
    }
    timer = setTimeout(() => void checkOrders(), statusRefreshMs);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [person, hasActiveProduction, busy, section]);

  useEffect(observeBrowserTimings, []);

  useEffect(() => {
    async function loadDashboard() {
      try {
        const initial = await api<Dashboard>("/dashboard");
        setPlan(initial.plan);
        setOrders(initial.orders);
        setOrdersCheckedAt(Math.floor(Date.now() / 1000));
        setKits(initial.kits);
        setSelected(initial.orders[0]?.id ?? "");
        setPerson(initial.person);
      } catch (fault) {
        // Permite cargar con la versión anterior del Worker durante una reversión.
        if (!(fault instanceof Error) || fault.message !== "RUTA_NO_DISPONIBLE") throw fault;
        setPerson(await api<Person>("/auth/me"));
        const id = await refresh();
        if (id) setSelected(id);
      }
    }
    void loadDashboard().catch((fault: unknown) => {
      if (fault instanceof Error && fault.message === "AUTENTICACION_REQUERIDA") setPerson(null);
      else setCloudError(true);
    });
  }, []);

  useEffect(() => {
    if (section !== "plan") return;
    const controller = new AbortController();
    void api<Checkout | NoCheckout>("/billing/checkout", { signal: controller.signal, cache: "no-store" })
      .then((current) => {
        if (controller.signal.aborted) return;
        setCheckoutAvailable(current.checkout_enabled === true);
        setCheckoutId("checkout" in current ? null : current.checkout_id);
      }).catch((fault: unknown) => {
        if (!controller.signal.aborted) setBillingStatus(message(fault));
      });
    return () => controller.abort();
  }, [section]);

  async function begin(event: FormEvent) {
    event.preventDefault();
    if (!file || busy || !plan?.production_available) return;
    setError("");
    setBusy(true);
    setStatus("Verificando el archivo…");
    setProgress(0);
    try {
      const order = await uploadVideo(file, api, (part) => {
        setProgress(part);
        setStatus(part < 100 ? "Enviando vídeo…" : "Verificando la carga…");
      }, plan.max_resolution === "4k" ? resolution : "1080p", plan.allow_prores4444 === 1 ? format : "mp4",
      unlimited && brands?.some((brand) => brand.id === selectedBrand) ? selectedBrand : undefined);
      setSelected(order.id);
      setStatus("Vídeo recibido. Tu producción está en preparación.");
      await refresh();
    } catch (fault) {
      setError(message(fault));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  async function ask(event: FormEvent) {
    event.preventDefault();
    const brief = question.trim();
    if (chatPending || !brief || !selected) return;
    let attempt = chatAttempt.current;
    if (attempt?.order !== selected || attempt.message !== brief) {
      attempt = { order: selected, message: brief, key: crypto.randomUUID() };
      chatAttempt.current = attempt;
    }
    setChatPending(true);
    setError("");
    setAnswer("Sofía está revisando tu consulta…");
    try {
      const { text } = await api<{ text: string }>("/chat", {
        method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": attempt.key },
        body: JSON.stringify({ order_id: attempt.order, message: attempt.message }),
      });
      setAnswer(text);
      setQuestion("");
      chatAttempt.current = null;
    } catch (fault) {
      setAnswer("");
      setError(message(fault));
    } finally {
      setChatPending(false);
    }
  }

  async function checkout(planId: string) {
    if (billingBusy || checkoutAvailable !== true) return;
    setBillingBusy(true);
    setError("");
    try {
      const entry = "studio:checkout:" + planId;
      const key = sessionStorage.getItem(entry) ?? crypto.randomUUID();
      sessionStorage.setItem(entry, key);
      const result = await api<Checkout>("/billing/checkout", {
        method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key },
        body: JSON.stringify({ plan_id: planId }),
      });
      setCheckoutId(result.checkout_id);
      if (!result.checkout_url) {
        setBillingStatus("La suscripción de prueba está pendiente de confirmación.");
        return;
      }
      location.assign(paymentLink(result.checkout_url));
    } catch (fault) { setError(message(fault)); }
    finally { setBillingBusy(false); }
  }

  async function resumeCheckout() {
    if (billingBusy || checkoutAvailable !== true) return;
    setBillingBusy(true);
    setError("");
    setBillingStatus("");
    try {
      const current = await api<Checkout | NoCheckout>("/billing/checkout", { cache: "no-store" });
      if (current.checkout_enabled !== true) {
        setCheckoutAvailable(false);
        return;
      }
      if ("checkout" in current) {
        setBillingStatus("Todavía no tienes una suscripción de prueba. Puedes elegir un plan.");
        return;
      }
      setCheckoutId(current.checkout_id);
      if (!current.checkout_url) {
        setBillingStatus("Esta suscripción de prueba no tiene un enlace pendiente. Puedes consultar su estado.");
        return;
      }
      location.assign(paymentLink(current.checkout_url));
    } catch (fault) { setError(message(fault)); }
    finally { setBillingBusy(false); }
  }

  async function reconcileCheckout() {
    if (billingBusy) return;
    setBillingBusy(true);
    setError("");
    try {
      const current = checkoutId ? { checkout_id: checkoutId } : await api<Checkout | NoCheckout>("/billing/checkout", { cache: "no-store" });
      if ("checkout" in current) {
        setBillingStatus("Todavía no tienes una suscripción de prueba.");
        return;
      }
      const result = await api<Checkout>("/billing/reconcile", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ checkout_id: current.checkout_id }),
      });
      setCheckoutId(result.checkout_id);
      if (result.status === "cancelled") {
        setBillingStatus("La suscripción está cancelada.");
      } else {
        setBillingStatus(result.status === "active" && result.billing_status === "paid"
          ? "El pago de prueba está confirmado." : "La suscripción de prueba aún está pendiente.");
      }
      await refresh();
    } catch (fault) { setError(message(fault)); }
    finally { setBillingBusy(false); }
  }

  async function signOut() {
    const result = await fetch("/api/auth/logout", { method: "POST" });
    if (result.ok) location.replace("/");
    else setError("No se pudo cerrar la sesión. Vuelve a intentarlo.");
  }

  return <main>
    <header className="top"><a className="brand" href="/" aria-label="Inicio E-scale Studio"><span aria-hidden="true" className="brand-mark" />E-scale <b>Studio</b></a>
      {person && <div className="account"><span>{person.display_name || person.email}</span><button onClick={() => void signOut()}>Salir</button></div>}
    </header>
    {person === undefined && !cloudError ? <p className="loading" aria-live="polite">Cargando estudio…</p> : null}
    {cloudError && !person ? <section className="entry"><h1>Estudio audiovisual</h1><p>El portal no pudo cargar tu sesión.</p><a className="button" href="/">Volver a intentar</a></section> : null}
    {person === null && <section className="entry">
      <div><p className="eyebrow">Tu espacio de producción</p><h1>Del material<br />a la entrega.</h1>
      <p>Sube tu material, conversa con Sofía y recibe los archivos de campaña en un solo lugar.</p>
      <a className="button" href="/api/auth/google">Continuar con Google <span aria-hidden="true">↗</span></a></div>
      <div className="film" aria-hidden="true"><div className="film-inner"><span /><span /><span /></div></div>
    </section>}
    {person && <div className="workspace">
      <nav aria-label="Secciones del estudio">{(["produccion", "entregas", "plan"] as const).map((tab) =>
        <button key={tab} aria-current={section === tab ? "page" : undefined} onClick={() => { setSection(tab); setError(""); }}>
          {{ produccion: "Producción", entregas: "Entregas", plan: "Mi plan" }[tab]}
        </button>)}</nav>
      {error && <p role="alert" className="alert">{error}</p>}
      {section === "produccion" && <>
        <section className="heading"><div><p className="eyebrow">Estudio audiovisual</p><h1>Producción</h1></div>
          {plan && <p className="quota">{plan.monthly_reels === null ? `${plan.used} producciones este mes` : `${plan.used} de ${plan.monthly_reels} producciones este mes`}</p>}</section>
        <div className="columns">
          <section className="panel"><h2>Nuevo vídeo</h2>
            {renewalNotice && <p role="status">{renewalNotice}</p>}
            {plan?.production_available ? <><p>MP4, MOV o WebM · Hasta 1 GiB y 10 minutos</p>
            <form onSubmit={begin}>
              <label htmlFor="video" className="drop"><b>{file?.name || "Seleccionar vídeo"}</b>
                <span>{file ? `${(file.size / 1024 ** 2).toFixed(1)} MiB` : "Elige el archivo que quieres producir."}</span></label>
              <input id="video" className="file" type="file" accept=".mp4,.mov,.webm" disabled={busy} onChange={(event) => {
                setFile(event.currentTarget.files?.[0] ?? null); setStatus(""); setProgress(null); setError("");
              }} />
              {unlimited && brands && brands.length > 0 && <div className="production-choice">
                <label htmlFor="production-brand">Marca de la producción</label>
                <select id="production-brand" value={selectedBrand} disabled={busy}
                  onChange={(event) => setSelectedBrand(event.currentTarget.value)}>
                  <option value="">Sin marca</option>
                  {brands.map((brand) => <option key={brand.id} value={brand.id}>{brand.display_name}</option>)}
                </select>
              </div>}
              {plan.max_resolution === "4k" && <div className="production-choice">
                <label htmlFor="output-resolution">Resolución de entrega</label>
                <select id="output-resolution" disabled={busy} value={resolution} onChange={(event) => {
                  setResolution(event.currentTarget.value === "4k" ? "4k" : "1080p");
                }}>
                  <option value="1080p">1080p · 1080 × 1920</option>
                  <option value="4k">4K · 2160 × 3840</option>
                </select>
              </div>}
              {plan.allow_prores4444 === 1 && <div className="production-choice">
                <label htmlFor="output-format">Archivos de entrega</label>
                <select id="output-format" disabled={busy} value={format} onChange={(event) => {
                  setFormat(event.currentTarget.value === "prores4444" ? "prores4444" : "mp4");
                }}>
                  <option value="mp4">Vídeo MP4</option>
                  <option value="prores4444">Vídeo MP4 + gráfica transparente ProRes 4444</option>
                </select>
              </div>}
              {progress !== null && <progress value={progress} max={100} aria-label="Progreso de subida" />}
              {status && <p aria-live="polite">{status}</p>}
              <button className="button" type="submit" disabled={!file || busy}>{busy ? "En proceso…" : "Subir e iniciar"}</button>
            </form></> : <p>{plan ? message(new Error("PRODUCCION_NO_HABILITADA")) : "Consultando disponibilidad…"}</p>}
          </section>
          <section className="panel"><h2>Brief con Sofía</h2>
            <div className="production-choice">
              <label htmlFor="order">Producción</label>
              <select id="order" value={selected} disabled={chatPending} onChange={(event) => { setSelected(event.currentTarget.value); setAnswer(""); }}>
                <option value="">Selecciona una producción</option>
                {orders.map((order, index) => <option value={order.id} key={order.id}>
                  Producción {orders.length - index}{order.brand && ` · ${order.brand.display_name}`} · {productionStatus(order, ordersCheckedAt)}
                </option>)}
              </select>
            </div>
            {selected && orders.some((order) => order.id === selected && (awaitingDelivery(order)
              || order.production_issue === "requiere_revision")) && <ConfirmBrief key={selected} order={selected} />}
            {selected && orders.some((order) => order.id === selected && order.production_issue === "requiere_revision")
              && <ManualAdjustment key={`manual-${selected}`} order={selected} onResumed={refreshAfterAdjustment} />}
            <form onSubmit={ask}>
              <label htmlFor="brief">Mensaje</label>
              <textarea id="brief" rows={3} maxLength={1800} value={question} disabled={chatPending} onChange={(event) => setQuestion(event.currentTarget.value)} />
              <button className="button secondary" type="submit" disabled={chatPending || !selected || !question.trim()}>Enviar a Sofía</button>
            </form>
            {answer && <p aria-live="polite" className="answer">{answer}</p>}
          </section>
        </div>
        {unlimited && brands && <BrandEditor brands={brands} onSave={(saved) => {
          setBrands((current) => [...(current ?? []).filter((brand) => brand.id !== saved.id), saved]
            .sort((left, right) => left.display_name.localeCompare(right.display_name, "es")));
        }} />}
        {unlimited && brandFault && <section className="panel recent">
          <p role="alert">{brandFault}</p>
          <button className="button secondary" type="button" onClick={() => {
            setBrandFault("");
            setBrandRetry((attempt) => attempt + 1);
          }}>Volver a cargar marcas</button>
        </section>}
        <section className="panel recent"><h2>Producciones recientes</h2>
          {orders.length ? <ul>{orders.map((order, index) => <li key={order.id}><span>Producción {orders.length - index}{order.brand && ` · ${order.brand.display_name}`}</span><time dateTime={new Date(order.created_at * 1000).toISOString()}>{new Date(order.created_at * 1000).toLocaleDateString("es-CL")}</time><b>{productionStatus(order, ordersCheckedAt)}</b></li>)}</ul> : <p>Tus producciones aparecerán aquí después de la primera subida.</p>}
        </section>
      </>}
      {section === "entregas" && <><section className="heading"><div><p className="eyebrow">Estudio audiovisual</p><h1>Entregas</h1></div></section>
        <section className="panel"><h2>Kits de campaña</h2>{kits.length ?
          <ul>{kits.map((kit) => <li key={kit.id}><span>{kit.title}</span><a href={kit.url} download>Descargar ZIP con MP4</a>
            {kit.prores && <a href={kit.prores.url} download>Descargar ProRes 4444</a>}</li>)}</ul> :
          <p>Aquí podrás descargar los kits aprobados de tus producciones.</p>}</section></>}
      {section === "plan" && <><section className="heading"><div><p className="eyebrow">Estudio audiovisual</p><h1>Mi plan</h1></div></section>
        <div className="plans">{plans.map((item) => <section key={item.id} className={`panel ${plan?.plan_id === item.id ? "current" : ""}`}>
          <h2>{item.name}</h2><b>{item.reels}</b><p>{item.summary}</p>
          {plan?.plan_id === item.id ? <span className="current-label">Plan actual</span> : checkoutAvailable === true && item.id !== "free" &&
            <button className="button secondary" disabled={billingBusy} onClick={() => void checkout(item.id)}>Elegir {item.name}</button>}
        </section>)}</div>
        <div className="billing-status">
          {checkoutAvailable === false && <p role="status">Los pagos de prueba están suspendidos.</p>}
          {checkoutAvailable === true && <button className="button secondary" disabled={billingBusy} onClick={() => void resumeCheckout()}>Retomar suscripción de prueba</button>}
          {checkoutId && <button className="button secondary" disabled={billingBusy} onClick={() => void reconcileCheckout()}>Consultar suscripción</button>}
          {billingStatus && <p aria-live="polite">{billingStatus}</p>}</div></>}
    </div>}
  </main>;
}
