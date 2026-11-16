import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./studio.css";

export const metadata: Metadata = {
  title: "E-scale Studio",
  description: "Espacio de producción audiovisual.",
};

export default function Layout({ children }: { children: ReactNode }) {
  return <html lang="es-CL"><body>{children}</body></html>;
}
