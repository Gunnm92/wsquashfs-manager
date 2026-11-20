// menu.js — Menus déroulants et menus d'actions : ouverture, positionnement, fermeture.

import { el, hideAnimated, icon } from "./core.js";

export const menus = { open: null };

// Le déclencheur d'un menu dit s'il est ouvert (aria-expanded)
export function markOpen(menu, trigger) {
  menu.trigger = trigger;
  trigger?.setAttribute("aria-expanded", "true");
}

// Fermeture en fondu (classe is-leaving), puis retrait du menu
export function closeMenu() {
  const menu = menus.open;
  if (!menu) return;
  menus.open = null;
  menu.trigger?.setAttribute("aria-expanded", "false");
  hideAnimated(menu, "is-leaving", 200, () => menu.remove());
}

// Menu sous (ou au-dessus de) son déclencheur, aligné à droite ; position fixe : il n'est
// pas rogné par les tableaux et barres qui défilent, et suit son déclencheur au défilement
// (il se ferme si celui-ci sort de l'écran). Les menus des filtres restent en position absolue.
// Le menu est rattaché à <body>, pas à son déclencheur : un ancêtre transformé (lignes et vues
// animées ; Safari garde une transformation neutre après une animation « both ») servirait sinon
// de repère à la position fixe, et le menu s'afficherait décalé.
function positionMenu(menu, wrap) {
  const r = wrap.getBoundingClientRect(), h = menu.offsetHeight;
  menu.style.position = "fixed";
  menu.style.left = "auto";
  menu.style.right = `${Math.max(8, innerWidth - r.right)}px`;
  menu.style.top = "auto";
  menu.style.bottom = "auto";
  if (r.bottom + 6 + h <= innerHeight - 8) menu.style.top = `${r.bottom + 6}px`;
  else menu.style.bottom = `${innerHeight - r.top + 6}px`;
}

export function placeMenu(menu, wrap) {
  menu.owner = wrap;
  document.body.append(menu);
  menu.dataset.fixed = "1";
  positionMenu(menu, wrap);
}

// Ouvre un menu sous (ou au-dessus de) son déclencheur ; un second clic le referme. Menu d'actions
// (« ⋯ », menu d'un client, durée d'essai) : même composant que le menu ⚙ (.menu, .menu-item) ;
// `list` : liste de choix d'une barre (source, cibles), composant des listes déroulantes (.dd-menu).
export function openMenuNode(wrap, nodes, trigger = wrap.querySelector("[aria-haspopup]"), { list = false } = {}) {
  const open = menus.open?.owner === wrap;
  closeMenu();
  if (open) return;
  menus.open = el("div", { className: list ? "dd-menu end sv-menu" : "menu act-menu", role: "menu" }, ...nodes);
  placeMenu(menus.open, wrap);
  markOpen(menus.open, trigger);
  menus.open.querySelector("button:not([disabled])")?.focus({ preventScroll: true });
}

// Filtre en liste déroulante (Images, Sauvegardes) : le bouton affiche le choix actif (« Prefix : Wine »
// devient « Wine », bordure accent) ; le menu liste « tous » puis chaque choix avec son nombre de jeux.
// options : [valeur, icône ou null, ton, libellé, nombre] ; onChoose(valeur), "" pour tous.
export function filterDropdown({ label, all, total, options, value, onChoose }) {
  const current = options.find(([v]) => v === value);
  const button = el("button", { type: "button", className: "dd-btn", title: label },
    current?.[1] ? el("span", { className: `dot-ic ${current[2]}` }, icon(current[1])) : null,
    el("span", { textContent: current ? current[3] : label }), icon("chevd", "xs"));
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", "false");
  const wrap = el("div", { className: `dd${current ? " active" : ""}` }, button);
  button.onclick = () => {
    const wasOpen = menus.open?.parentElement === wrap;
    closeMenu();
    if (wasOpen) return;
    const item = (v, iconName, tone, text, n) => {
      const b = el("button", { type: "button", className: v === value ? "on" : "" },
        iconName ? el("span", { className: `dot-ic ${tone}` }, icon(iconName)) : el("span", { className: "dot-ic t-off" }),
        el("span", { textContent: text }), el("span", { className: "n", textContent: n ?? "" }),
        v === value ? icon("check", "sm") : el("span", { className: "nocheck" }));
      b.onclick = () => { closeMenu(); onChoose(v); };
      return b;
    };
    menus.open = el("div", { className: "dd-menu", role: "listbox" }, item("", null, "", all, total), ...options.map((o) => item(...o)));
    wrap.append(menus.open);
    markOpen(menus.open, button);
    // Menu plus large que l'espace à droite (téléphone, dernier filtre) : décalé pour rester à l'écran
    const wrapLeft = wrap.getBoundingClientRect().left;
    menus.open.style.left = `${Math.max(8 - wrapLeft, Math.min(0, innerWidth - 8 - wrapLeft - menus.open.offsetWidth))}px`;
    menus.open.querySelector("button.on, button")?.focus();
  };
  return wrap;
}

export function init() {
  document.addEventListener("click", (event) => { if (menus.open && !event.target.closest(".dd") && !menus.open.contains(event.target)) closeMenu(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && menus.open) { closeMenu(); event.stopImmediatePropagation(); } });
  addEventListener("scroll", () => {
    if (!menus.open?.dataset.fixed) return;
    const wrap = menus.open.owner, r = wrap.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) closeMenu();
    else positionMenu(menus.open, wrap);
  }, true);
}
