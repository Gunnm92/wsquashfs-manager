// aicons.js — Icônes animées du manager : actions, interface, clients et manettes.
// - Jeux libres via Iconify (aicons-iconify.js, tools/icons/iconify.py) : line-md, animées en SMIL (le trait
//   se dessine), figées dessinées au repos et rejouées au survol ; variante en boucle pendant le travail.
//   Lucide, Tabler, Iconoir : fixes, leur trait se dessine au survol (css/aicons.css, .aico-draw).
// - Flux 2 Klein (aicons-data.js, tools/icons/build_icons.py) pour ce qui n'existe pas ailleurs (console SteamOS),
//   découpées en parties nommées animées en CSS.

import { AICONS } from "./aicons-data.js";
import { ICONIFY } from "./aicons-iconify.js";

const NS = "http://www.w3.org/2000/svg";
const reduced = matchMedia("(prefers-reduced-motion: reduce)");
// Éléments dont le survol ou le focus joue l'icône ; une ligne du tableau n'en fait pas partie :
// là, l'icône n'est qu'une information
const TRIGGER = 'button, a, [role^="menuitem"], .mcard, .dd-btn, .dot-ic, .st-pad';

const svgOf = (name, cls, extra = "") => {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", `ico aico aico-${name}${extra} ${cls}`.trim());
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.dataset.aico = name;
  return svg;
};

// Une icône SMIL au repos : arrêtée sur sa dernière image (le trait dessiné). Avant l'insertion dans la page,
// la ligne de temps du SVG n'existe pas encore : on recommence à l'image suivante.
function settle(svg) {
  const stop = () => { svg.pauseAnimations(); svg.setCurrentTime(60); };
  stop();
  if (!svg.isConnected) requestAnimationFrame(stop);
}

const play = (svg) => { svg.setCurrentTime(0); svg.unpauseAnimations(); };

// Les masques line-md ont un id fixe : unique par icône, sinon url(#id) vise le premier exemplaire de la page
// (souvent masqué) et l'icône devient un carré plein
let uid = 0;
const ownIds = (body) => {
  const n = ++uid;
  return body.replace(/id="([^"]+)"/g, `id="$1-${n}"`).replace(/url\(#([^)]+)\)/g, `url(#$1-${n})`);
};

// Icône animée ; null si le nom n'en a pas : icon() retombe sur le sprite
export function aicon(name, cls = "") {
  const set = ICONIFY[name];
  if (set) {
    const svg = svgOf(name, cls, set.draw ? " aico-set aico-draw" : " aico-set aico-smil");
    svg.innerHTML = ownIds(set.body);
    if (!set.draw) settle(svg);
    return svg;
  }
  const parts = AICONS[name];
  if (!parts) return null;
  const svg = svgOf(name, cls);
  for (const [part, d] of parts) {
    const path = document.createElementNS(NS, "path");
    if (part) path.setAttribute("class", `p-${part}`);
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

// Rejoue le dessin des icônes SMIL d'un élément (survol, fin d'une tâche)
export function playIcons(root) {
  if (reduced.matches || !root) return;
  root.querySelectorAll("svg.aico-smil").forEach(play);
}

// Travail en cours : les icônes qui ont une variante en boucle la prennent, puis reviennent au repos
export function busyIcons(root, on) {
  root?.querySelectorAll("svg.aico-smil").forEach((svg) => {
    const set = ICONIFY[svg.dataset.aico];
    if (!set?.loop) return;
    svg.innerHTML = ownIds(on ? set.loop : set.body);
    if (on && !reduced.matches) play(svg); else settle(svg);
  });
}

// Survol ou focus clavier d'une commande : son icône rejoue une fois (pas pendant un travail)
function onEnter(event) {
  const target = event.target.closest?.(TRIGGER);
  if (!target || target.classList.contains("is-busy") || target.contains(event.relatedTarget)) return;
  playIcons(target);
}
document.addEventListener("pointerover", onEnter);
document.addEventListener("focusin", (event) => { if (event.target.matches?.(":focus-visible")) onEnter(event); });

// Emplacements écrits dans le HTML : <span data-aico="nom" data-cls="sm"></span>
export function hydrateAicons(root = document) {
  root.querySelectorAll("span[data-aico]").forEach((slot) => {
    const svg = aicon(slot.dataset.aico, slot.dataset.cls || "");
    if (svg) slot.replaceWith(svg);
  });
}
