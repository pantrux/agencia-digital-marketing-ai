"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { observeBrowserTimings } from "./browser-timing";
import { quotaNotice } from "./quota";
import { maxEditStyleLength, readSofiaStyles } from "./sofia-styles";
import { uploadVideo, uploadVideos, type OutputEdit, type OutputFormat, type OutputResolution } from "./upload";

type Person = { email: string; display_name: string | null };
type Plan = { plan_id: string; monthly_reels: number | null; used: number; max_resolution: OutputResolution;
  allow_prores4444: 0 | 1; production_available: boolean; period_start?: string };
type BrandFields = { display_name: string; audience: string; tone: string };
type Brand = BrandFields & { id: string };
type Order = { id: string; project_slug: string; status: string; created_at: number;
  brand?: Pick<Brand, "id" | "display_name">;
  received_at?: number | null; brief_confirmed_at?: number | null; production_issue?: "qa_rechazado" | "requiere_revision" | null };
const socialLabels = { facebook: "Facebook", instagram: "Instagram", whatsapp: "WhatsApp" } as const;
type SocialChannel = keyof typeof socialLabels;
type SocialLinks = Partial<Record<SocialChannel, string>>;
type BriefFields = { ad_engine?: "hyperframes"; ad_contacts?: SocialLinks & { channel: SocialChannel }; ad_cta?: string; ad_brand?: string; objective: string; audience: string; tone: string };
type BriefStatus = { ad_available?: boolean; brief: (BriefFields & { confirmed_at: number }) | null; ready: boolean; brand?: Brand };
const emptyBrand: BrandFields = { display_name: "", audience: "", tone: "" };
type Kit = { id: string; order_id: string; title: string; url: string; prores?: { bytes: number; url: string } };
type Dashboard = { person: Person; plan: Plan; orders: Order[]; kits: Kit[] };
type Checkout = { checkout_id: string; checkout_enabled: boolean; checkout_url: string | null; billing_status: string; status: string };
type NoCheckout = { checkout: null; checkout_enabled: boolean };
type Section = "produccion" | "entregas" | "plan";
type ChatTurn = { role: "usuario" | "sofia"; text: string; id: string };
type SourceStatus = {id:string; name:string; upload_status:string; synced_at:number | null};
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
      return now - order.brief_confirmed_at >= deliveryDelaySeconds ? "En producción · sigue en curso" : "En producción";
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
    ARCHIVOS_INVALIDOS: "Selecciona entre 2 y 32 vídeos para el montaje.",
    VIDEO_SUPERA_DURACION: "El vídeo supera los 10 minutos. Recórtalo antes de subirlo.",
    VIDEO_CORTO_PARA_REEL: "Los vídeos deben sumar al menos 30 segundos para crear un reel.",
    VIDEO_NO_VERIFICABLE: "Tu navegador no pudo leer el vídeo. Prueba con un MP4 en H.264.",
    SUBIDA_NO_DISPONIBLE: "La carga de vídeos aún está en preparación.",
    PAGO_NO_DISPONIBLE: "La activación de planes pagados todavía está en preparación.",
    CHECKOUT_DE_PRUEBA_PAUSADO: "Los pagos de prueba están suspendidos.",
    CHECKOUT_EN_CURSO: "Ya tienes una suscripción de prueba en curso. Usa «Retomar suscripción de prueba».",
    CHAT_NO_DISPONIBLE: "El chat aún no está disponible para esta producción.",
    REFERENCIA_INVALIDA: "Ingresa una dirección HTTPS pública sin contraseñas ni claves en el enlace.",
    REFERENCIA_ESPERAR: "Espera unos segundos antes de consultar otra referencia.",
    REFERENCIA_NO_DISPONIBLE: "La consulta de referencias aún no está disponible.",
    BRIEF_ESPERA_CRUDO: "La copia del vídeo todavía está en preparación. Vuelve a comprobar el estado.",
    BRIEF_YA_CONFIRMADO: "Esta producción ya tiene un brief confirmado. Vuelve a comprobar el estado.",
    CTA_DESTINO_INVALIDO: "Revisa el enlace del canal elegido. Usa el perfil de Facebook o Instagram, o un enlace https://wa.me/ con el número internacional.",
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

function ConfirmBrief({ order, suggestion }: { order: string; suggestion: {id:string; text:string; target:"objective" | "style"} | null }) {
  const path = `/orders/${encodeURIComponent(order)}/brief`;
  const [draft, setDraft] = useState<Partial<BriefFields>>({});
  const [current, setCurrent] = useState<BriefStatus | null>(null);
  const [pending, setPending] = useState(false);
  const [fault, setFault] = useState("");
  const [checkAttempt, setCheckAttempt] = useState(0);
  const waitingForVideo = !current?.ready && !current?.brief;
  const [links, setLinks] = useState<SocialLinks>({});
  const [channel, setChannel] = useState<SocialChannel | "">("");
  const [hyperframes, setHyperframes] = useState(false);
  const [creativeDirection, setCreativeDirection] = useState("");
  const enteredLinks = Object.fromEntries(
    Object.entries(links).filter(([, url]) => url.trim()).map(([name, url]) => [name, url.trim()]),
  );
  const needsChannel = channel ? !enteredLinks[channel] : Object.keys(enteredLinks).length > 0;
  const cta = draft.ad_cta?.trim() || (channel ? "Escríbenos por " + socialLabels[channel] : "");
  const adBrand = draft.ad_brand ?? current?.brand?.display_name ?? "";
  const fields = {
    objective: draft.objective ?? "",
    audience: draft.audience ?? current?.brand?.audience ?? "",
    tone: draft.tone ?? current?.brand?.tone ?? "",
  };

  useEffect(() => {
    if (!suggestion) return;
    if (suggestion.target === "style") setCreativeDirection(suggestion.text);
    else setDraft((previous) => ({...previous, objective: suggestion.text}));
    document.getElementById(suggestion.target === "style" ? "brief-style" : "brief-objective")?.focus();
  }, [suggestion]);

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
    if (pending || current?.brief || !fields.objective.trim() || !fields.audience.trim() || (cta && !adBrand.trim()) || (current?.ad_available && needsChannel)) return;
    const objective = fields.objective.trim() + (creativeDirection.trim() ? `\n\nDirección creativa: ${creativeDirection.trim()}` : "");
    if (objective.length > 800) { setFault("El objetivo y la dirección creativa juntos deben tener hasta 800 caracteres."); return; }
    setPending(true);
    setFault("");
    try {
      const ad = current?.ad_available && cta ? {
        ad_cta: cta,
        ad_brand: adBrand.trim(),
        ...(hyperframes ? { ad_engine: "hyperframes" } : {}),
        ...(channel ? { ad_contacts: { ...enteredLinks, channel } } : {}),
      } : {};
      setCurrent(await api<BriefStatus>(path, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...fields, objective, ...ad }),
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
        {confirmed.tone && <><dt>Tono</dt><dd>{confirmed.tone}</dd></>}
        {confirmed.ad_cta && <><dt>Marca del anuncio</dt><dd>{confirmed.ad_brand}</dd><dt>Llamada a la acción</dt><dd>{confirmed.ad_cta}</dd></>}
        {confirmed.ad_contacts && <><dt>Destino del CTA</dt><dd>{confirmed.ad_contacts[confirmed.ad_contacts.channel]}</dd></>}
        {confirmed.ad_engine && <><dt>Motion graphics</dt><dd>HyperFrames</dd></>}</dl></div>
      : <form onSubmit={submit}>
        <p>Puedes copiar un mensaje del chat al objetivo, editarlo y confirmar el brief para enviarlo al equipo.</p>
        <label htmlFor="brief-objective">Objetivo</label>
        <textarea id="brief-objective" rows={2} required maxLength={800} value={fields.objective} disabled={pending}
          onChange={(event) => setDraft({ ...draft, objective: event.currentTarget.value })} />
        <label htmlFor="brief-style">Estilo de edición elegido (opcional)</label>
        <textarea id="brief-style" rows={3} maxLength={maxEditStyleLength} value={creativeDirection} disabled={pending}
          placeholder="Describe el estilo que elegiste: ritmo, música, subtítulos y animaciones."
          onChange={(event) => setCreativeDirection(event.currentTarget.value)} />
        <label htmlFor="brief-audience">Audiencia</label>
        <textarea id="brief-audience" rows={2} required maxLength={480} value={fields.audience} disabled={pending}
          onChange={(event) => setDraft({ ...draft, audience: event.currentTarget.value })} />
        <label htmlFor="brief-tone">Tono (opcional)</label>
        <textarea id="brief-tone" rows={1} maxLength={240} value={fields.tone} disabled={pending}
          onChange={(event) => setDraft({ ...draft, tone: event.currentTarget.value })} />
        {current?.ad_available && <>
          <fieldset>
            <legend>Perfiles para el anuncio</legend>
            {(Object.keys(socialLabels) as SocialChannel[]).map((name) => <div key={name}>
              <label htmlFor={`brief-social-${name}`}>{socialLabels[name]} (opcional)</label>
              <input id={`brief-social-${name}`} type="url" maxLength={128} disabled={pending}
                value={links[name] ?? ""} placeholder={name === "whatsapp" ? "https://wa.me/56912345678" : `https://www.${name}.com/tu-perfil`}
                onChange={(event) => {
                  const url = event.currentTarget.value;
                  setLinks({ ...links, [name]: url });
                  if (name === channel && !url.trim()) setChannel("");
                }} />
            </div>)}
            <label htmlFor="brief-cta-destination">Destino del CTA</label>
            <select id="brief-cta-destination" value={channel} disabled={pending}
              onChange={(event) => setChannel(event.currentTarget.value as SocialChannel | "")}>
              <option value="">Sin perfil</option>
              {(Object.keys(socialLabels) as SocialChannel[]).map((name) =>
                <option key={name} value={name} disabled={!links[name]?.trim()}>{socialLabels[name]}</option>)}
            </select>
            {needsChannel && <p role="status">Selecciona uno de los perfiles ingresados para el CTA.</p>}
          </fieldset>
          <label htmlFor="brief-ad-cta">Llamada a la acción para anuncio de 30 segundos (opcional)</label>
          <input id="brief-ad-cta" maxLength={70} value={draft.ad_cta ?? ""} disabled={pending}
            placeholder={channel ? cta : "Escríbenos para conocer más"}
            onChange={(event) => setDraft({ ...draft, ad_cta: event.currentTarget.value })} />
          {cta && <>
            <label htmlFor="brief-ad-brand">Marca que se verá al cierre</label>
            <input id="brief-ad-brand" maxLength={80} value={adBrand} required disabled={pending}
              onChange={(event) => setDraft({ ...draft, ad_brand: event.currentTarget.value })} />
            <label htmlFor="brief-hyperframes">
              <input id="brief-hyperframes" type="checkbox" checked={hyperframes} disabled={pending}
                onChange={(event) => setHyperframes(event.currentTarget.checked)} /> Motion graphics con HyperFrames
            </label>
          </>}
        </>}
        {current?.ready ? <button className="button" type="submit" disabled={pending || !fields.objective.trim() || !fields.audience.trim() || Boolean(cta && !adBrand.trim()) || Boolean(current?.ad_available && needsChannel)}>Confirmar brief</button>
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

type DeliveryRevision = {id:string; base_kit_id:string; status:"pending" | "processing" | "completed" | "failed";
  result_kit_id:string | null; failure_stage:"direction" | "quality" | null};

function DeliveryRevisionPanel({order, kit, onPublished}: {order:string; kit:string; onPublished:() => Promise<void>}) {
  const path = `/orders/${encodeURIComponent(order)}/delivery-revisions`;
  const [enabled,setEnabled] = useState(false);
  const [items,setItems] = useState<DeliveryRevision[]>([]);
  const [instructions,setInstructions] = useState("");
  const [sending,setSending] = useState(false);
  const [fault,setFault] = useState("");
  const [check,setCheck] = useState(0);
  const attempt = useRef<{text:string; key:string} | null>(null);
  const noticed = useRef(new Set<string>());
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const result = await api<{revisions:DeliveryRevision[]}>(path,{signal:controller.signal,cache:"no-store"});
        if (controller.signal.aborted) return;
        setEnabled(true); setItems(result.revisions); setFault("");
        for (const item of result.revisions) {
          if (item.status === "completed" && item.result_kit_id && !noticed.current.has(item.id)) {
            noticed.current.add(item.id);
            void onPublished().catch(() => {});
          }
        }
        if (!result.revisions.some((item) => item.status === "pending" || item.status === "processing")) return;
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof Error && error.message === "RUTA_NO_DISPONIBLE") {setEnabled(false); return;}
        setFault(message(error));
      }
      timer = setTimeout(() => void poll(),statusRefreshMs);
    }
    void poll();
    return () => {controller.abort(); clearTimeout(timer);};
  },[path,check,onPublished]);
  async function submit(event:FormEvent) {
    event.preventDefault();
    const text = instructions.trim();
    if (!text || sending) return;
    if (attempt.current?.text !== text) attempt.current = {text,key:crypto.randomUUID()};
    setSending(true); setFault("");
    try {
      await api(path,{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":attempt.current.key},
        body:JSON.stringify({base_kit_id:kit,instructions:text})});
      setInstructions(""); attempt.current = null; setCheck((value) => value+1);
    } catch (error) {setFault(message(error));}
    finally {setSending(false);}
  }
  if (!enabled) return null;
  const latest = items.find((item) => item.base_kit_id === kit);
  const busy = items.some((item) => item.status === "pending" || item.status === "processing");
  return <div className="production-brief">
    {latest?.status === "pending" && <p role="status">Tu solicitud de cambios está en espera.</p>}
    {latest?.status === "processing" && <p role="status">El equipo está preparando una nueva versión.</p>}
    {latest?.status === "failed" && <p role="status">La nueva versión requiere revisión del equipo antes de poder entregarse.</p>}
    {latest?.status === "completed" && latest.result_kit_id && <p role="status">Tu nueva versión está disponible en el chat y en Entregas.</p>}
    {fault && <p role="alert">{fault}</p>}
    <form onSubmit={(event) => void submit(event)}>
      <label htmlFor={`revision-${kit}`}>¿Qué te gustaría cambiar?</label>
      <textarea id={`revision-${kit}`} maxLength={800} rows={2} value={instructions} disabled={sending || busy}
        onChange={(event) => setInstructions(event.currentTarget.value)} />
      <button className="button secondary" type="submit" disabled={sending || busy || !instructions.trim()}>Solicitar nueva versión</button>
    </form>
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
  const [files, setFiles] = useState<File[]>([]);
  const [resolution, setResolution] = useState<OutputResolution>("1080p");
  const [format, setFormat] = useState<OutputFormat>("mp4");
  const [edit, setEdit] = useState<OutputEdit>("full");
  const [progress, setProgress] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [question, setQuestion] = useState("");
  const [briefSuggestion, setBriefSuggestion] = useState<{order:string; id:string; text:string; target:"objective" | "style"} | null>(null);
  const [referenceUrl, setReferenceUrl] = useState("");
  const [referenceStatus, setReferenceStatus] = useState("");
  const [referenceBusy, setReferenceBusy] = useState(false);
  const [sourceProgress, setSourceProgress] = useState<SourceStatus[]>([]);
  const [conversations, setConversations] = useState<Record<string, ChatTurn[]>>({});
  const [videoFaults, setVideoFaults] = useState<Record<string, boolean>>({});
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
  const selectedState = orders.find((order) => order.id === selected)?.status;
  const selectedConversation = conversations[selected] ?? [];
  const canEditBrief = orders.some((order) => order.id === selected && awaitingDelivery(order) && !order.brief_confirmed_at);
  const renewalNotice = plan?.production_available ? quotaNotice(plan) : null;
  const unlimited = plan?.plan_id === "ilimitado";
  useEffect(() => {
    if (!person || !selected) return;
    const controller = new AbortController();
    void api<{ turns: { request_id: string; message: string; reply: string }[] }>(
      `/chat/history?order_id=${encodeURIComponent(selected)}`,
      { signal: controller.signal, cache: "no-store" },
    ).then(({ turns }) => {
      if (controller.signal.aborted) return;
      setConversations((current) => {
        const saved = turns.flatMap((turn) => [
          { role: "usuario" as const, text: turn.message, id: turn.request_id + "-usuario" },
          { role: "sofia" as const, text: turn.reply, id: turn.request_id + "-sofia" },
        ]);
        const ids = new Set(saved.map((turn) => turn.id));
        return { ...current, [selected]: [...saved, ...(current[selected] ?? []).filter((turn) => !ids.has(turn.id))] };
      });
    }).catch((fault: unknown) => {
      if (!controller.signal.aborted) setError(message(fault));
    });
    return () => controller.abort();
  }, [person, selected]);
  useEffect(() => {
    setSourceProgress([]);
    if (!person || !selected) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const {assets} = await api<{assets:SourceStatus[]}>(`/orders/${encodeURIComponent(selected)}/sources`,
          {signal:controller.signal,cache:"no-store"});
        if (controller.signal.aborted) return;
        setSourceProgress(assets);
        if (assets.every((asset) => asset.synced_at !== null) || selectedState === "cancelled" || selectedState === "failed") return;
      } catch (fault) {
        if (controller.signal.aborted) return;
        if (fault instanceof Error && fault.message === "MATERIALES_NO_ENCONTRADOS" && !busy) return;
      }
      timer = setTimeout(() => void poll(), statusRefreshMs);
    }
    void poll();
    return () => {controller.abort(); clearTimeout(timer);};
  }, [person, selected, busy, selectedState]);
  useEffect(() => {
    setReferenceUrl("");
    setReferenceStatus("");
    if (!person || !selected) return;
    const controller = new AbortController();
    void api<{reference: {source_url:string; status:string; title:string; description:string} | null}>(
      `/orders/${encodeURIComponent(selected)}/reference`, {signal:controller.signal, cache:"no-store"},
    ).then(({reference}) => {
      if (controller.signal.aborted || !reference) return;
      setReferenceUrl(reference.source_url);
      setReferenceStatus(reference.status === "available"
        ? `Referencia guardada. Sofía puede consultar el título, la descripción y un fragmento del texto público disponible.`
        : "No pude leer el perfil. Si requiere iniciar sesión, cuéntame su estilo en el chat.");
    }).catch(() => { if (!controller.signal.aborted) setReferenceStatus("No fue posible cargar la referencia guardada."); });
    return () => controller.abort();
  }, [person, selected]);
  const refreshAfterAdjustment = useCallback(async () => {
    const [current, deliveries] = await Promise.all([
      api<{orders:Order[]}>("/orders", { cache: "no-store" }),
      api<{kits:Kit[]}>("/kits", { cache: "no-store" }),
    ]);
    setOrders(current.orders);
    setKits(deliveries.kits);
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
        if (section === "entregas" || current.some((order) => order.status === "completed")) {
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
    if (!files.length || busy || !plan?.production_available) return;
    setError("");
    setBusy(true);
    setStatus("Verificando el archivo…");
    setProgress(0);
    try {
      const report = (part: number) => {
        setProgress(part);
        setStatus(part < 100 ? "Enviando material…" : "Verificando la carga…");
      };
      const onCreated = (created: { id: string; project_slug: string }) => {
        setOrders((current) => current.some((item) => item.id === created.id) ? current : [
          { ...created, status: "queued", created_at: Math.floor(Date.now() / 1000) }, ...current,
        ]);
        setSelected(created.id);
      };
      const outputResolution = plan.max_resolution === "4k" ? resolution : "1080p";
      const outputFormat = plan.allow_prores4444 === 1 ? format : "mp4";
      const brand = unlimited && brands?.some((item) => item.id === selectedBrand) ? selectedBrand : undefined;
      const order = files.length === 1
        ? await uploadVideo(files[0]!, api, report, outputResolution, outputFormat, brand, edit, onCreated)
        : await uploadVideos(files, api, report, outputResolution, outputFormat, brand, edit, onCreated);
      setSelected(order.id);
      setStatus("Material recibido. Tu producción está en preparación.");
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
    await sendQuestion(question);
  }

  async function saveReference(event: FormEvent) {
    event.preventDefault();
    if (!selected || !referenceUrl.trim() || referenceBusy) return;
    const order = selected;
    setReferenceBusy(true);
    setReferenceStatus("Consultando la referencia pública…");
    try {
      const result = await api<{reference:{status:string; title:string; description:string}}>(
        `/orders/${encodeURIComponent(order)}/reference`, {
          method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({url:referenceUrl.trim()}),
        });
      if (order !== selected) return;
      setReferenceStatus(result.reference.status === "available"
        ? `Referencia guardada. Sofía puede consultar el título, la descripción y un fragmento del texto público disponible.`
        : "No pude leer el perfil. Si requiere iniciar sesión, cuéntame su estilo en el chat.");
    } catch (fault) { setReferenceStatus(message(fault)); }
    finally { setReferenceBusy(false); }
  }

  async function sendQuestion(value: string) {
    const brief = value.trim();
    if (chatPending || !brief || !selected) return;
    let attempt = chatAttempt.current;
    if (attempt?.order !== selected || attempt.message !== brief) {
      attempt = { order: selected, message: brief, key: crypto.randomUUID() };
      chatAttempt.current = attempt;
    }
    setChatPending(true);
    setError("");
    setConversations((current) => {
      const history = current[attempt.order] ?? [];
      return history.some((turn) => turn.id === attempt.key + "-usuario") ? current : {
        ...current, [attempt.order]: [...history, { role: "usuario", text: attempt.message, id: attempt.key + "-usuario" }],
      };
    });
    try {
      const { text } = await api<{ text: string }>("/chat", {
        method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": attempt.key },
        body: JSON.stringify({ order_id: attempt.order, message: attempt.message }),
      });
      setConversations((current) => {
        const history = current[attempt.order] ?? [];
        return history.some((turn) => turn.id === attempt.key + "-sofia") ? current : {
          ...current, [attempt.order]: [...history, { role: "sofia", text, id: attempt.key + "-sofia" }],
        };
      });
      setQuestion((current) => current.trim() === brief ? "" : current);
      chatAttempt.current = null;
    } catch (fault) {
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
      <nav className="workspace-nav" aria-label="Secciones del estudio"><span className="workspace-nav-title">Tu espacio</span>{(["produccion", "entregas", "plan"] as const).map((tab) =>
        <button key={tab} aria-current={section === tab ? "page" : undefined} onClick={() => { setSection(tab); setError(""); }}>
          {{ produccion: "Proyectos", entregas: "Galería y entregas", plan: "Mi plan" }[tab]}
        </button>)}</nav>
      {error && <p role="alert" className="alert">{error}</p>}
      {section === "produccion" && <>
        <section className="heading"><div><p className="eyebrow">Estudio audiovisual</p><h1>Producción</h1></div>
          {plan && <p className="quota">{plan.monthly_reels === null ? `${plan.used} producciones este mes` : `${plan.used} de ${plan.monthly_reels} producciones este mes`}</p>}</section>
        <div className="columns">
          <section className="panel"><h2>Nuevo proyecto</h2>
            {renewalNotice && <p role="status">{renewalNotice}</p>}
            {plan?.production_available ? <><p>MP4, MOV o WebM · Hasta 1 GiB y 10 minutos por vídeo</p>
            <form onSubmit={begin}>
              <label htmlFor="video" className="drop"><b>{files.length === 1 ? files[0]?.name : files.length ? `${files.length} vídeos seleccionados` : "Seleccionar vídeos"}</b>
                <span>{files.length ? `${(files.reduce((sum, item) => sum + item.size, 0) / 1024 ** 2).toFixed(1)} MiB en total` : "Elige uno o varios archivos para tu producción."}</span></label>
              <input id="video" className="file" type="file" multiple accept=".mp4,.mov,.webm" disabled={busy} onChange={(event) => {
                const chosen = Array.from(event.currentTarget.files ?? []);
                setFiles(chosen); setStatus(""); setProgress(null); setError("");
              }} />
              {files.length > 1 && <p>El equipo combinará los materiales según el montaje elegido.</p>}
              {unlimited && brands && brands.length > 0 && <div className="production-choice">
                <label htmlFor="production-brand">Marca de la producción</label>
                <select id="production-brand" value={selectedBrand} disabled={busy}
                  onChange={(event) => setSelectedBrand(event.currentTarget.value)}>
                  <option value="">Sin marca</option>
                  {brands.map((brand) => <option key={brand.id} value={brand.id}>{brand.display_name}</option>)}
                </select>
              </div>}
              <div className="production-choice">
                <label htmlFor="output-edit">Montaje</label>
                <select id="output-edit" value={edit} disabled={busy}
                  onChange={(event) => setEdit(event.currentTarget.value === "reel" ? "reel" : "full")}>
                  <option value="full">Montaje de hasta 10 minutos</option>
                  <option value="reel">Reel por escenas · 30–45 segundos</option>
                </select>
              </div>
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
              <button className="button" type="submit" disabled={!files.length || busy}>{busy ? "En proceso…" : "Subir e iniciar"}</button>
            </form></> : <p>{plan ? message(new Error("PRODUCCION_NO_HABILITADA")) : "Consultando disponibilidad…"}</p>}
          </section>
          <section className="panel chat-panel"><div className="chat-header"><div className="sofia-avatar" aria-hidden="true">S</div><div><h2>Conversación con Sofía</h2><p>Tu asesora para esta producción</p></div></div>
            <div className="production-choice">
              <label htmlFor="order">Producción</label>
              <select id="order" value={selected} disabled={chatPending || referenceBusy} onChange={(event) => { setSelected(event.currentTarget.value); setQuestion(""); }}>
                <option value="">Selecciona una producción</option>
                {orders.map((order, index) => <option value={order.id} key={order.id}>
                  Producción {orders.length - index}{order.brand && ` · ${order.brand.display_name}`} · {productionStatus(order, ordersCheckedAt)}
                </option>)}
              </select>
            </div>
            {selected && <form className="production-choice" onSubmit={(event) => void saveReference(event)}>
              <label htmlFor="brand-reference">Sitio web o perfil de referencia</label>
              <input id="brand-reference" type="url" placeholder="https://…" maxLength={2048}
                value={referenceUrl} disabled={referenceBusy} onChange={(event) => setReferenceUrl(event.currentTarget.value)} />
              <button className="button secondary" type="submit" disabled={referenceBusy || !referenceUrl.trim()}>
                {referenceBusy ? "Consultando…" : "Compartir referencia"}
              </button>
              {referenceStatus && <p role="status">{referenceStatus}</p>}
            </form>}
            {selected && orders.find((order) => order.id === selected) && <p className="chat-status" role="status">
              Estado de la producción · {productionStatus(orders.find((order) => order.id === selected)!, ordersCheckedAt)}
            </p>}
            {sourceProgress.length > 1 && <details>
              <summary>Estado de los materiales · {sourceProgress.filter((item) => item.synced_at !== null).length} de {sourceProgress.length} recibidos</summary>
              <ul>{sourceProgress.map((item) => <li key={item.id}>{item.name} · {item.synced_at !== null
                ? "Recibido en el estudio" : item.upload_status === "completed"
                ? "Copia al estudio pendiente o en curso" : item.upload_status === "aborted"
                ? "Carga interrumpida" : "Pendiente de carga o en curso"}</li>)}</ul>
            </details>}
            {selected && orders.some((order) => order.id === selected && (awaitingDelivery(order)
              || order.production_issue === "requiere_revision")) && <ConfirmBrief key={selected} order={selected}
                suggestion={briefSuggestion?.order === selected ? briefSuggestion : null} />}
            {selected && orders.some((order) => order.id === selected && order.production_issue === "requiere_revision")
              && <ManualAdjustment key={`manual-${selected}`} order={selected} onResumed={refreshAfterAdjustment} />}
            <div className="chat-history" role="log" aria-label="Conversación con Sofía" aria-live="polite">
              {!selected && <p>Inicia una producción o selecciona un proyecto para conversar con Sofía.</p>}
              {selected && !selectedConversation.length && <p>Cuéntale a Sofía qué quieres lograr. Pueden definir el enfoque, la audiencia y el estilo.</p>}
              {selectedConversation.map((turn, index) => {
                const previous = selectedConversation[index - 1];
                const styles = canEditBrief && turn.role === "sofia" && previous?.role === "usuario"
                  ? readSofiaStyles(previous.text, turn.text) : null;
                if (styles) return <div key={turn.id} className="chat-turn sofia">
                  <strong>Sofía</strong>
                  {styles.intro && <p>{styles.intro}</p>}
                  <div className="chat-style-options" role="group" aria-label="Estilos de edición propuestos">
                    {styles.choices.map(({ title, detail }, option) => <button key={option} className="chat-style" type="button" onClick={() => {
                      const text = title && detail ? `${title}: ${detail}` : title || detail;
                      setError("");
                      setBriefSuggestion({ order: selected, id: crypto.randomUUID(), text, target: "style" });
                    }}>
                      {title && <span className="chat-style-title">{title}</span>}
                      {detail && <span>{detail}</span>}
                      <span className="chat-style-action">Usar este estilo</span>
                    </button>)}
                  </div>
                  {styles.outro && <p>{styles.outro}</p>}
                </div>;
                return <div key={turn.id} className={`chat-turn ${turn.role}`}>
                  <strong>{turn.role === "sofia" ? "Sofía" : "Tú"}</strong><p>{turn.text}</p>
                  {canEditBrief &&
                    <><button type="button" onClick={(event) => {
                      const container = event.currentTarget.closest(".chat-turn");
                      const selection = window.getSelection();
                      const text = selection && container?.contains(selection.anchorNode) && container.contains(selection.focusNode)
                        ? selection.toString().trim() || turn.text : turn.text;
                      if (text.length > 800) { setError("El texto supera 800 caracteres. Selecciona una parte más breve para el objetivo."); return; }
                      setError(""); setBriefSuggestion({order:selected, id:crypto.randomUUID(), text, target:"objective"});
                    }}>
                      Copiar al objetivo del brief
                    </button><button type="button" onClick={(event) => {
                      const container = event.currentTarget.closest(".chat-turn");
                      const selection = window.getSelection();
                      const text = selection && container?.contains(selection.anchorNode) && container.contains(selection.focusNode)
                        ? selection.toString().trim() || turn.text : turn.text;
                      if (text.length > maxEditStyleLength) { setError("El texto supera 500 caracteres. Selecciona una parte más breve para el estilo."); return; }
                      setError(""); setBriefSuggestion({order:selected, id:crypto.randomUUID(), text, target:"style"});
                    }}>
                      Usar como estilo de edición
                    </button></>}
                </div>;
              })}
              {orders.find((order) => order.id === selected)?.status === "completed" &&
                kits.filter((kit) => kit.order_id === selected).map((kit) => <div className="chat-turn sofia" key={kit.id}>
                  <strong>Entrega aprobada</strong>
                  {videoFaults[kit.id] ? <div role="status"><p>No se pudo cargar la vista previa. Puedes descargar el kit para ver el video.</p>
                    <button type="button" onClick={() => setVideoFaults((current) => ({ ...current, [kit.id]: false }))}>Reintentar vista previa</button></div> :
                    <video controls preload="metadata" playsInline aria-label={kit.title}
                      src={`/api/kits/${encodeURIComponent(kit.id)}/video`}
                      onError={() => setVideoFaults((current) => ({ ...current, [kit.id]: true }))} />}
                  <a href={kit.url} download>Descargar kit</a>
                  {kit.prores && <a href={kit.prores.url} download>Descargar gráfica ProRes 4444</a>}
                  <DeliveryRevisionPanel order={kit.order_id} kit={kit.id} onPublished={refreshAfterAdjustment} />
                </div>)}
              {chatPending && <p role="status">Sofía está revisando tu consulta…</p>}
            </div>
            {selected && <div className="chat-suggestions" aria-label="Ideas para conversar con Sofía">
              <button type="button" disabled={chatPending} onClick={() => void sendQuestion(
                "Propón tres estilos de edición distintos para esta producción. Usa una lista del 1 al 3 con un título breve en negrita y una descripción corta que reúna ritmo, música, subtítulos y gráficos animados. Pregúntame cuál prefiero después de la lista.",
              )}>Explorar tres estilos</button>
              <button type="button" disabled={chatPending} onClick={() => void sendQuestion(
                "Ayúdame a definir el objetivo, la audiencia, el tono y el llamado a la acción de este vídeo. Hazme preguntas de una en una.",
              )}>Definir mi idea</button>
              <button type="button" disabled={chatPending} onClick={() => document.getElementById("brand-reference")?.focus()}>Compartir una referencia</button>
            </div>}
            <form onSubmit={ask}>
              <label htmlFor="brief">Mensaje</label>
              <textarea id="brief" rows={3} maxLength={1800} value={question} disabled={chatPending} onChange={(event) => setQuestion(event.currentTarget.value)} />
              <button className="button secondary" type="submit" disabled={chatPending || !selected || !question.trim()}>Enviar a Sofía</button>
            </form>
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
          {orders.length ? <ul>{orders.map((order, index) => <li key={order.id}><button type="button" className="project-select" disabled={chatPending || referenceBusy} onClick={() => { setSelected(order.id); setQuestion(""); }}>
            Producción {orders.length - index}{order.brand && ` · ${order.brand.display_name}`}
          </button><time dateTime={new Date(order.created_at * 1000).toISOString()}>{new Date(order.created_at * 1000).toLocaleDateString("es-CL")}</time><b>{productionStatus(order, ordersCheckedAt)}</b></li>)}</ul> : <p>Tus producciones aparecerán aquí después de la primera subida.</p>}
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
