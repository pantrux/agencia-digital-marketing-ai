import { expect, it } from "vitest";
import { readSofiaStyles } from "../cloudflare-saas/studio-portal-web/app/sofia-styles.js";

const request = "Propón tres estilos de edición distintos para esta producción.";
const reply = `Aquí tienes tres estilos para el anuncio:

1. **Natural y relajante:** Música acústica y transiciones suaves.

2. **Dinámico y moderno**: Cortes rápidos y subtítulos animados.

3. Elegante y minimalista: Planos cuidados y gráficos sencillos.

¿Cuál de los tres estilos prefieres?`;

it("separa las tres propuestas de la introducción y la pregunta final", () => {
  expect(readSofiaStyles(request, reply)).toEqual({
    intro: "Aquí tienes tres estilos para el anuncio:",
    choices: [
      { title: "Natural y relajante", detail: "Música acústica y transiciones suaves." },
      { title: "Dinámico y moderno", detail: "Cortes rápidos y subtítulos animados." },
      { title: "Elegante y minimalista", detail: "Planos cuidados y gráficos sencillos." },
    ],
    outro: "¿Cuál de los tres estilos prefieres?",
  });
});

it("conserva como conversación una lista que responde a otra pregunta", () => {
  expect(readSofiaStyles("¿Cómo preparo mi anuncio?", "1. Graba el producto.\n2. Sube el vídeo.\n3. Comparte la entrega.")).toBeNull();
});

it.each(["¿Con cuál de las opciones te quedas?", "¿Cuál encaja mejor con tu marca?"])(
  "conserva los párrafos de la tercera propuesta antes de %s", (closing) => {
    const detail = "Planos cuidados y gráficos sencillos.\n\nEl logo aparece al final.";
    const multiline = reply.slice(0, reply.indexOf("3. Elegante"))
      + ["3. **Elegante y minimalista**", detail, closing].join("\n\n");
    const styles = readSofiaStyles(request, multiline);
    expect(styles?.choices[2]).toEqual({ title: "Elegante y minimalista", detail });
    expect(styles?.outro).toBe(closing);
  },
);

it("deja editable el brief si faltan opciones o una propuesta supera el límite", () => {
  expect(readSofiaStyles(request, "1. Editorial: Pausado.\n2. Dinámico: Cortes rápidos.")).toBeNull();
  expect(readSofiaStyles(request, reply.replace("Planos cuidados y gráficos sencillos.", "Texto ".repeat(100)))).toBeNull();
});
