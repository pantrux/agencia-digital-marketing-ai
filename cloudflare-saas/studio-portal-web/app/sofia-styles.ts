export const maxEditStyleLength = 500;

export function readSofiaStyles(question: string, reply: string) {
  if (!/\b(?:tres|3)\s+(?:estilos|propuestas|opciones)\s+de\s+edici[oó]n\b/iu.test(question)) return null;

  const text = reply.replace(/\r\n?/gu, "\n");
  const markers = [...text.matchAll(/^[ \t]{0,3}(\d{1,2})[.)][ \t]+/gmu)];
  if (markers.length !== 3 || markers.some((marker, index) => marker[1] !== String(index + 1))) return null;

  const intro = text.slice(0, markers[0]!.index).trim();
  const choices = [];
  let outro = "";
  for (const [index, marker] of markers.entries()) {
    let content = text.slice(marker.index + marker[0].length, markers[index + 1]?.index).trim();
    if (index === 2) {
      const closing = /\n[ \t]*¿[^¿?\n]+\?$/u.exec(content);
      if (closing && /\b(?:prefieres|eliges|elegir[ií]as|te quedas|te gusta(?:r[ií]a)?)\b/iu.test(closing[0])) {
        outro = closing[0].trim();
        content = content.slice(0, closing.index).trim();
      }
    }
    const heading = /^(?:\*\*([^*\n]{1,80})\*\*[ \t]*:?|([^:\n]{1,80}):)[ \t]*/u.exec(content);
    const title = (heading?.[1] ?? heading?.[2] ?? "").replace(/:$/u, "").trim();
    const detail = content.slice(heading?.[0].length ?? 0).trim();
    if ((!title && !detail) || title.length + detail.length + (title && detail ? 2 : 0) > maxEditStyleLength) return null;
    choices.push({ title, detail });
  }
  return { intro, choices, outro };
}
