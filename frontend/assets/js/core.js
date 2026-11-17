// core.js — État partagé, outils DOM, formatage, notifications et infobulles.

import { openHelp } from "./help.js";

export const state = {
  items: [], byId: new Map(), selected: new Set(), current: null,
  sort: { key: "name", dir: 1 }, keys: [], tasks: [],
  filter: { system: "", type: "", runner: "", pad: "", extra: "", special: "" },
  layout: "list",
  autorun: null,          // dernière lecture de l'autorun de la fiche
  pendingKeys: new Set(), // clés ajoutées au formulaire, pas encore renseignées
  tab: null,              // onglet affiché dans la fiche
  baseline: "",           // autorun tel que chargé ou enregistré (détecte les changements)
  client: null,           // client affiché dans l'en-tête (identifiant, ou « all »)
  loadedAt: 0,            // dernier chargement de la bibliothèque (menu : « il y a N min »)
};

export const $ = (id) => document.getElementById(id);

export const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
};

export const storage = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* navigation privée */ } },
};

export const icon = (name, cls = "") => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", `ico ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
};

// Puce texte (anciens « badges ») : la sorte donne le ton
const TONES = { info: "t-accent", warn: "t-accent", ok: "t-sage", err: "t-err", violet: "t-sage", cyan: "t-sage", "": "t-neutral" };

export const badge = (text, kind = "") => el("span", { className: `chip plain ${TONES[kind] || "t-neutral"}`, textContent: text });

const chip = (iconName, label, tone, anchor = null, title = "") => {
  const node = el(anchor ? "a" : "span", { className: `chip ${tone}`, title }, icon(iconName, "sm"), label);
  if (anchor) {
    node.href = `#${anchor}`;
    node.title = title || "Voir dans l'aide";
    node.onclick = (event) => { event.preventDefault(); event.stopPropagation(); openHelp(anchor); };
  }
  return node;
};

const dash = () => el("span", { className: "dash", textContent: "—" });

// Info-bulle (#tip) au survol ou au focus clavier : titre + lignes de détail
export const withTip = (node, title, lines, focusable = true) => {
  node.dataset.tipTitle = title;
  node.dataset.tip = lines.join("\n");
  if (focusable) node.tabIndex = 0;
  node.setAttribute("aria-label", lines.length ? `${title} : ${lines.join(", ")}` : title);
  return node;
};

export const dotIc = (iconName, tone, title, lines = [], focusable = false) =>
  withTip(el("span", { className: `dot-ic ${tone}` }, icon(iconName)), title, lines, focusable);

// Lien vers une section (ou la carte d'une clé) de l'aide
export const helpLink = (anchor, child = "aide", title = "Voir dans l'aide") =>
  el("a", { href: `#${anchor}`, title, onclick: (event) => { event.preventDefault(); event.stopPropagation(); openHelp(anchor); } }, child);

const TYPE_HELP = { wine_batocera: "h-batocera", wine_other: "h-batocera", wine_proton: "h-proton", game_only: "h-solo" };

export const size = (n) => n >= 1e9 ? `${(n / 1e9).toFixed(1).replace(".", ",")} Go` : n >= 1e6 ? `${(n / 1e6).toFixed(0)} Mo`
  : n >= 1e3 ? `${(n / 1e3).toFixed(0)} Ko` : `${n} o`;

// Taille en To au-delà du téraoctet (« 4,29 To ») ; sinon comme size()
export const bigSize = (n, digits = 1) => n >= 1e12 ? `${(n / 1e12).toFixed(digits).replace(".", ",")} To` : size(n);

export const when = (t) => t ? new Date(t * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : "";

export const cap = (s) => s ? s[0].toUpperCase() + s.slice(1) : s;

// Action impossible : elle reste visible, grisée (disabled + aria-disabled), la raison en infobulle
// (title). Sans raison, elle est rendue à l'état normal avec son titre d'origine.
export function setUnavailable(node, reason) {
  if (node.dataset.titleBase === undefined) node.dataset.titleBase = node.title || "";
  node.disabled = Boolean(reason);
  if (reason) node.setAttribute("aria-disabled", "true");
  else node.removeAttribute("aria-disabled");
  node.title = reason || node.dataset.titleBase;
  return node;
}

// Animations : une entrée joue l'animation CSS de l'élément dès qu'il redevient visible (hidden
// retiré) ; une sortie passe par la classe `cls` (animation CSS de sortie), puis hidden.

export const motionOff = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// Relance une animation CSS déjà posée sur un nœud affiché : retire la classe, force un reflow, la remet
export function replay(node, cls) {
  node.classList.remove(cls);
  void node.offsetWidth;
  node.classList.add(cls);
}

// Sortie animée : joue `cls` pendant `ms`, puis masque le nœud et appelle `done`.
// Mouvement réduit : masqué tout de suite. Une seconde sortie pendant la première ne fait rien.
export function hideAnimated(node, cls, ms, done) {
  if (node.hidden) { done?.(); return; }
  if (node.classList.contains(cls)) return;
  const finish = () => {
    clearTimeout(node.leaveTimer);
    node.classList.remove(cls);
    node.hidden = true;
    done?.();
  };
  if (motionOff()) { finish(); return; }
  node.classList.add(cls);
  node.leaveTimer = setTimeout(finish, ms);
}

// Visible et pas en sortie : base des bascules (un clic pendant une sortie doit rouvrir, pas refermer)
export const isShown = (node) => !node.hidden && !node.classList.contains("is-leaving");

// Affichage : annule une sortie en cours (ouverture rapide après fermeture) puis rend le nœud visible
export function showAnimated(node, cls) {
  clearTimeout(node.leaveTimer);
  node.classList.remove(cls);
  node.hidden = false;
}

// Décale l'entrée en cascade des lignes : --i (0, 1, 2…) plafonné à 12, soit 30 ms par ligne au plus
export function stagger(nodes) {
  nodes.forEach((node, i) => node.style.setProperty("--i", String(i)));
  return nodes;
}

let toastTimer = null;

export function toast(message, iconName = "check", error = false) {
  const node = $("toast");
  node.replaceChildren(icon(iconName), el("span", { textContent: message }));
  node.classList.toggle("err", error);
  showAnimated(node, "is-leaving");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => hideAnimated(node, "is-leaving", 180), error ? 6000 : 3500);
}

export const emptyState = (iconName, title, hint) => el("div", { className: "empty-state" }, icon(iconName),
  el("strong", { textContent: title }), hint ? el("span", { textContent: hint }) : null);

export function init() {
  state.layout = storage.get("wsfs-layout") === "grid" ? "grid" : "list";
  // Hauteur de l'en-tête : la fiche et l'aide se calent dessous
  new ResizeObserver(([entry]) => document.documentElement.style.setProperty("--top-h",
    `${Math.round(entry.target.getBoundingClientRect().height)}px`)).observe(document.querySelector("header.top"));
}
