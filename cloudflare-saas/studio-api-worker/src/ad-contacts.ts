import { HttpError } from "./platform.js";

const names = { facebook: "Facebook", instagram: "Instagram", whatsapp: "WhatsApp" } as const;
type Channel = keyof typeof names;
export type Contacts = { channel: Channel } & Partial<Record<Channel, string>>;

function invalid(): never { throw new HttpError(422, "CTA_DESTINO_INVALIDO"); }

function contactUrl(value: unknown, channel: Channel): string {
  if (typeof value !== "string" || value.length > 128) return invalid();
  const input = value.trim();
  if (!/^https:\/\/[a-z.]+\/[a-zA-Z0-9._/?=]+$/u.test(input)) return invalid();
  const url = new URL(input);
  const host = url.hostname.replace(/^www\./u, "");
  const slug = url.pathname.slice(1).replace(/\/$/u, "");
  if (channel === "facebook" && host === "facebook.com") {
    if (slug === "profile.php" && /^\?id=[1-9][0-9]{3,24}$/u.test(url.search)) {
      return `https://www.facebook.com/profile.php${url.search}`;
    }
    if (!url.search && /^[a-zA-Z0-9.]{3,60}$/u.test(slug) && !["login", "share", "sharer", "watch", "reels", "groups", "events", "marketplace", "profile.php"].includes(slug.toLowerCase())) {
      return `https://www.facebook.com/${slug}`;
    }
  }
  if (channel === "instagram" && host === "instagram.com" && !url.search
    && /^[a-zA-Z0-9._]{1,30}$/u.test(slug) && !["p", "reel", "reels", "stories", "accounts", "explore", "direct"].includes(slug.toLowerCase())
    && /[a-zA-Z0-9_]/u.test(slug)) return `https://www.instagram.com/${slug}`;
  if (channel === "whatsapp" && host === "wa.me" && !url.search && /^[1-9][0-9]{6,14}$/u.test(slug)) {
    return `https://wa.me/${slug}`;
  }
  return invalid();
}

export function parseAdContacts(value: unknown): Contacts | undefined {
  if (value === undefined) return undefined;
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalid();
  const input = value as Record<string, unknown>;
  const channel = input.channel;
  if (typeof channel !== "string" || !Object.hasOwn(names, channel)
    || Object.keys(input).some(key => key !== "channel" && !Object.hasOwn(names, key))) return invalid();
  const result: Contacts = { channel: channel as Channel };
  for (const key of Object.keys(names) as Channel[]) {
    if (input[key] !== undefined) result[key] = contactUrl(input[key], key);
  }
  if (!result[result.channel]) return invalid();
  return result;
}

export function storedAdContacts(value: string | null | undefined): Contacts | undefined {
  return value && value !== "{}" ? parseAdContacts(JSON.parse(value)) : undefined;
}

export function contactCta(contacts: Contacts): string {
  return "Escríbenos por " + names[contacts.channel];
}

export function adDispatchFields(cta: string | null, brand: string | null, raw: string | null, engine: string | null) {
  if (!cta) return {};
  const contacts = storedAdContacts(raw);
  return { ad_cta: cta, ad_brand: brand ?? "", ...(engine === "hyperframes" ? { ad_engine: engine } : {}), ...(contacts
    ? { ad_destination: { channel: contacts.channel, url: contacts[contacts.channel]! } } : {}) };
}
