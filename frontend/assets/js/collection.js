// collection.js — Mécanismes communs aux vues en liste (Images, Dossiers, Sauvegardes) : en-têtes triables,
// bascule liste / jaquettes, « tout sélectionner », barre de sélection, fiche latérale, en-tête de page.
// Des fonctions ciblées : chaque vue garde son état, ses colonnes, ses cartes et ses actions.

import { $, el, hideAnimated, icon, showAnimated, storage } from "./core.js";

// « 3 images », « 1 image » : le nombre suivi du nom, au pluriel dès 2 (`unit` : [singulier, pluriel])
export const plural = (n, [one, many]) => `${n} ${n > 1 ? many : one}`;

// Compteur visible, à droite de la barre d'outils : « 12 sur 87 » si un filtre masque des lignes, sinon
// « 87 images ». Sans `unit`, toujours « 12 sur 87 ».
export const countLabel = (visible, total, unit = null) => unit && visible === total ? plural(total, unit) : `${visible} sur ${total}`;

// En-têtes triables. `sort` : { key, dir } courant (dir : 1 croissant, -1 décroissant) ; `onSort({ key, dir })`
// reçoit le nouveau tri (à la vue de le garder, le mémoriser, redessiner). Renvoie `sortBtn(key, label, cls, opts)` :
// un bouton d'en-tête avec sa flèche ; un second clic inverse le sens, un autre en-tête repart de `first`
// (1, ou -1 pour une date : la plus récente d'abord) ; `title` : infobulle facultative.
export function sortHeader(sort, onSort) {
  return (key, label, cls = "", { first = 1, title = "" } = {}) => {
    const arrow = sort.key === key ? (sort.dir > 0 ? " ↑" : " ↓") : "";
    const b = el("button", { type: "button", className: cls, textContent: label + arrow });
    if (title) b.title = title;
    b.onclick = () => onSort({ key, dir: sort.key === key ? -sort.dir : first });
    return b;
  };
}

// Bascule liste / jaquettes (deux pastilles `data-layout` dans `box`, la pastille active porte .on).
// `key` : clé de mémorisation (storage) ; `get()` / `set(layout)` : lecture et écriture dans l'état de la vue ;
// `onChange()` : redessiner. `list` et `grid` : identifiants des deux conteneurs, masqués tour à tour.
// Relit le choix mémorisé et branche les pastilles ; renvoie `paint()` à appeler à chaque rendu.
export function layoutToggle(box, { key, get, set, onChange, list, grid }) {
  set(storage.get(key) === "grid" ? "grid" : "list");
  box.addEventListener("click", (event) => {
    const b = event.target.closest("button[data-layout]");
    if (!b || b.dataset.layout === get()) return;
    set(b.dataset.layout);
    storage.set(key, get());
    onChange();
  });
  return () => {
    for (const b of box.children) b.classList.toggle("on", b.dataset.layout === get());
    $(list).hidden = get() === "grid";
    $(grid).hidden = get() !== "grid";
  };
}

// Case « tout sélectionner » d'un en-tête (ou d'une case déjà dans la page) : cochée quand toutes les lignes
// visibles (`ids`) le sont dans `selected` (un Set) ; la cocher ou la décocher les ajoute ou les retire toutes,
// puis `onChange()` redessine. Sans `box`, crée la case de l'en-tête de liste.
export function selectAllBox(ids, selected, onChange, box = null) {
  box ??= el("input", { type: "checkbox", title: "Tout sélectionner (filtre actuel)" });
  box.checked = ids.length > 0 && ids.every((id) => selected.has(id));
  box.onchange = () => { ids.forEach((id) => (box.checked ? selected.add(id) : selected.delete(id))); onChange(); };
  return box;
}

// Barre de sélection : visible quand `count` > 0 (entrée et sortie animées). `label` : le nœud du compteur,
// écrit « 3 jeux » ou « 1 jeu » (`unit` : [singulier, pluriel]). Renvoie vrai si la barre est affichée.
export function toggleSelbar(bar, count, label, unit) {
  if (!count) { hideAnimated(bar, "is-leaving", 200); return false; }
  showAnimated(bar, "is-leaving");
  label.textContent = plural(count, unit);
  return true;
}

// Fiche latérale d'une vue : `main` reçoit .with-detail (la colonne de la liste se réduit), `aside` glisse
// dedans. open() l'affiche, close() la retire en glissement (la colonne ne reprend sa place qu'à la fin),
// isOpen() dit si elle est affichée.
export function detailPanel(main, aside) {
  return {
    open() { main.classList.add("with-detail"); showAnimated(aside, "is-leaving"); },
    close() { hideAnimated(aside, "is-leaving", 200, () => main.classList.remove("with-detail")); },
    isOpen: () => !aside.hidden,
  };
}

// Ligne ou jaquette de la fiche ouverte : .current sur le nœud `[data-id]` de `selector` portant `id` (null : aucun)
export function markCurrent(selector, id) {
  for (const node of document.querySelectorAll(selector)) node.classList.toggle("current", node.dataset.id === id);
}

// Pastille « À traiter » : nombre, libellé, icône ; un clic la bascule
export function todoChip(n, label, iconName, tone, on, onClick) {
  const chip = el("button", { type: "button", className: `todo-chip ${tone}${on ? " on" : ""}` },
    icon(iconName, "sm"), el("b", { textContent: n }), el("span", { textContent: label }));
  chip.setAttribute("aria-pressed", String(on));
  chip.onclick = onClick;
  return chip;
}

// Pastilles « À traiter » de l'en-tête de page et filtre actif rappelé dans la barre (un clic le retire).
// `ids` : { todo, clear } — le conteneur des pastilles (`<todo>` et son libellé `<todo>-label`) et le bouton
// du filtre actif (`<clear>` et son libellé `<clear>-label`) ; `chips` : les pastilles non vides ;
// `active` : libellé du filtre actif, "" s'il n'y en a pas.
export function paintTodo({ todo, clear }, chips, active) {
  $(todo).replaceChildren(...chips);
  $(`${todo}-label`).hidden = !chips.length;
  $(clear).hidden = !active;
  $(`${clear}-label`).textContent = active;
}
