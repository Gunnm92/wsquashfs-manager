// menu.js — Menus déroulants et menus d'actions : ouverture, positionnement, fermeture.

import { el } from "./core.js";
import { menuItem } from "./saves-machines.js";

export const menus = { open: null };

export function closeMenu() { if (menus.open) { menus.open.remove(); menus.open = null; } }

// Menu sous (ou au-dessus de) son déclencheur, aligné à droite ; position fixe : il n'est
// pas rogné par les tableaux et barres qui défilent, et suit son déclencheur au défilement
// (il se ferme si celui-ci sort de l'écran). Les menus des filtres restent en position absolue.
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
  wrap.append(menu);
  menu.dataset.fixed = "1";
  positionMenu(menu, wrap);
}

// Ouvre un menu sous (ou au-dessus de) son déclencheur ; un second clic le referme
export function openMenuNode(wrap, nodes) {
  const open = menus.open?.parentElement === wrap;
  closeMenu();
  if (open) return;
  menus.open = el("div", { className: "dd-menu end sv-menu", role: "menu" }, ...nodes);
  placeMenu(menus.open, wrap);
  menus.open.querySelector("button:not([disabled])")?.focus({ preventScroll: true });
}

// Menu d'action (même mécanique que les filtres de la liste) : items → [libellé, icône, désactivé, action, danger]
export function toggleMenu(wrap, items) {
  openMenuNode(wrap, items().map(([label, iconName, disabled, action, danger]) => {
    const b = menuItem(label, iconName, action, { danger: Boolean(danger) });
    b.disabled = Boolean(disabled);
    return b;
  }));
}

export function init() {
  document.addEventListener("click", (event) => { if (menus.open && !event.target.closest(".dd")) closeMenu(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && menus.open) { closeMenu(); event.stopImmediatePropagation(); } });
  addEventListener("scroll", () => {
    if (!menus.open?.dataset.fixed) return;
    const wrap = menus.open.parentElement, r = wrap.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) closeMenu();
    else positionMenu(menus.open, wrap);
  }, true);
}
