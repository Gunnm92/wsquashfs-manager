// aicons.js — Icônes animées du manager : clients, manettes (réglage d'une image) et actions groupées.
// Dessinées avec Krea 2 (référence : les icônes Lucide de l'interface), vectorisées (vtracer),
// découpées en parties nommées (aicons-data.js) ; animations en CSS au survol (css/aicons.css).

import { AICONS } from "./aicons-data.js";

const NS = "http://www.w3.org/2000/svg";

// Icône animée (SVG en currentColor) ; null si le nom n'en a pas : icon() retombe sur le sprite
export function aicon(name, cls = "") {
  const parts = AICONS[name];
  if (!parts) return null;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", `ico aico aico-${name} ${cls}`.trim());
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const [part, d] of parts) {
    const path = document.createElementNS(NS, "path");
    if (part) path.setAttribute("class", `p-${part}`);
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

// Emplacements écrits dans le HTML : <span data-aico="nom" data-cls="sm"></span>
export function hydrateAicons(root = document) {
  root.querySelectorAll("[data-aico]").forEach((slot) => {
    const svg = aicon(slot.dataset.aico, slot.dataset.cls || "");
    if (svg) slot.replaceWith(svg);
  });
}
