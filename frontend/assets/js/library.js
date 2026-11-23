// library.js — Liste des images : chargement, filtres, tri, lignes et jaquettes, sélection, pastilles.

import { api } from "./api.js";
import { thumb } from "./art.js";
import { countLabel, layoutToggle, paintTodo, plural, selectAllBox, sortHeader, toggleSelbar, todoChip } from "./collection.js";
import { $, bigSize, cap, dotIc, el, emptyState, icon, size, stagger, state, toast } from "./core.js";
import { markCurrent, openDetail, renderDetailHeader } from "./detail/detail.js";
import { chosenClient, paintRefreshAgo } from "./header.js";
import { gamesOf, imageFile, inst, instPins, machineOf } from "./install.js";
import { closeMenu, filterDropdown } from "./menu.js";
import { applyMode } from "./mode.js";
import { paintSelectionActions } from "./selbar.js";
import { loadSaves } from "./saves.js";
import { openTasks } from "./tasks.js";

export const TYPES = { wine_batocera: "wine", wine_proton: "proton", wine_other: "wine", game_only: "aucun", unknown: "inconnu" };

// Détail affiché après le nom court (info-bulle, fiche) : « wine — prefix Batocera (system.reg) »
const TYPE_DETAIL = { wine_batocera: "prefix Batocera (system.reg)", wine_proton: "prefix Proton", wine_other: "autre prefix Wine",
  game_only: "jeu seul, sans prefix", unknown: "prefix non reconnu" };

const typeLong = (t) => TYPE_DETAIL[t] ? `${TYPES[t]} — ${TYPE_DETAIL[t]}` : (TYPES[t] || t);

const TYPE_LOOK = { wine_batocera: ["wine", "t-accent"], wine_other: ["wine", "t-neutral"], wine_proton: ["atom", "t-violet"],
  game_only: ["app", "t-neutral"], unknown: ["alert", "t-err"] };

export const STATES = { ok: ["Prêt", "Image saine"], in_use: ["En cours", "Jeu lancé : aucune reconstruction possible"],
  modified: ["Tâche", "Une tâche est en file pour cette image"], has_old: [".old", "Ancienne version conservée : à supprimer ou restaurer"] };

// Filtres (menus déroulants à côté de la recherche) : chaque choix montre la
// même icône, de la même couleur, que dans la colonne « Prefix et réglages ».
// Le client se choisit dans l'en-tête (il filtre la liste, voir scopeOf).
const FILTERS = [
  { key: "kind", label: "Type", all: "Images et dossiers", options: () => [["image", "layers", "", "images"], ["folder", "folder", "t-accent", "dossiers de jeu"]] },
  { key: "system", label: "Système", all: "Tous les systèmes", options: () => [...new Set(state.items.map((i) => i.system).filter(Boolean))].sort().map((v) => [v, null, "", cap(v)]) },
  { key: "type", label: "Prefix", all: "Tous les prefix", options: () => [["wine", "wine", "t-accent", "wine"], ["wine_proton", "atom", "t-violet", "proton"], ["game_only", "app", "t-neutral", "aucun"]] },
  { key: "runner", label: "Runner", all: "Tous les runners", options: () => [["default", null, "", "aucun"], ["custom", "cpu", "t-accent", "custom"], ["joined", "clip", "t-accent", "intégré"]] },
  { key: "pad", label: "Manette", all: "Toutes les manettes", options: () => [["hidraw", "dualsense", "t-violet", "DualSense"], ["xinput", "xbox", "t-off", "Xbox"]] },
  { key: "extra", label: "Extras", all: "Tous", options: () => [["keys", "keyboard", "t-neutral", "Touches clavier"], ["fakeping", "wifi", "t-neutral", "Relais fakeping"]] },
];

// Client choisi dans l'en-tête : la liste ne montre que les images installées sur lui.
// status : « ok » (filtre appliqué), « loading » (état pas encore lu), « error », « offline » ;
// sauf « ok », la bibliothèque entière reste affichée (jamais 0 image par défaut).
function scopeOf() {
  const client = chosenClient();
  if (!client) return null;
  if (!client.ok) return { client, status: "offline" };
  if (!inst.loaded) return { client, status: inst.error ? "error" : "loading" };
  const files = new Set(gamesOf(machineOf(client.id)));
  return { client, status: "ok", items: state.items.filter((i) => files.has(imageFile(i))) };
}

// Images de la portée courante (toute la bibliothèque sauf client filtrant)
function scopedItems() {
  const scope = scopeOf();
  return scope?.status === "ok" ? scope.items : state.items;
}

const IMAGES = ["image", "images"];

let paintLayout = () => {};      // posé par init : bascule liste / jaquettes

const SORTS = { name: (i) => i.name, size: (i) => i.size, saves_size: (i) => i.saves_present ? i.saves_size : -1, state: (i) => i.state };

// Colonne et filtre : « aucun », « custom » ou « intégré » ; le détail est dans l'info-bulle
const runnerLabel = (i) => i.runner === "default" ? "aucun" : i.runner_joined ? "intégré" : "custom";

function runnerDetails(i) {
  const joined = i.joined_runners || [];
  const used = i.proton || i.wine;
  const lines = [];
  if (i.runner === "custom") lines.push(`RUNNER=${i.runner_path || "?"}`, "binaire wine du client");
  else if (i.runner === "default") lines.push("choisi par le lanceur selon le prefix de l'image");
  else {
    lines.push(`${i.runner === "proton" ? "PROTON" : "WINE"}=${used}`);
    lines.push(i.runner_joined ? `intégré à l'image (runners/${used})` : "installé sur le client");
  }
  const unused = joined.filter((n) => !(i.runner_joined && n.toLowerCase() === (used || "").toLowerCase()));
  if (unused.length) lines.push(`runners/ : ${unused.join(", ")} (non utilisé)`);
  return lines;
}

// Pastilles de la colonne Profil : type, runner, manettes, extras, surcharge
function profileBadges(i) {
  const [typeIcon, typeTone] = TYPE_LOOK[i.type] || TYPE_LOOK.unknown;
  const out = [dotIc(typeIcon, typeTone, `Prefix : ${typeLong(i.type)}`)];
  if (i.runner !== "default") out.push(dotIc(i.runner_joined ? "clip" : "cpu", "t-accent", i.runner_joined ? "Runner intégré" : "Runner custom", runnerDetails(i)));
  out.push(i.hidraw ? dotIc("dualsense", "t-violet", "Manette : DualSense") : dotIc("xbox", "t-off", "Manette : Xbox"));
  for (const extra of i.extras || []) {
    if (extra === ".keys") out.push(dotIc("keyboard", "t-neutral", "Touches clavier", [".wsquashfs.keys à côté de l'image"]));
    else if (extra === "fakeping") out.push(dotIc("wifi", "t-neutral", "Relais fakeping", ["dinput8.dll"]));
    else out.push(dotIc("sparkles", "t-neutral", "Extra", [extra]));
  }
  if ((i.override || []).length) out.push(dotIc("filepen", "t-accent", "Surcharge à inscrire", i.override));
  return el("div", { className: "profile c-profile" }, ...out);
}

const stateBadge = (i) => {
  const [label, tip] = STATES[i.state] || [i.state, ""];
  return el("span", { className: `state ${i.state} c-state`, textContent: label, title: `${label} — ${tip}` });
};

const versionLabel = (i) => el("span", { className: `ver${i.version ? "" : " none"}`, textContent: i.version || "sans version" });

// Dossier de jeu (`*.pc`) : lancé directement, sans image .wsquashfs. Marqué dans la liste et les jaquettes.
const kindFlag = (i) => i.kind === "folder"
  ? el("span", { className: "flag kind", textContent: "dossier", title: "Dossier de jeu (.pc) : lancé directement, sans image .wsquashfs" })
  : null;

// Lecture longue de la bibliothèque (nouvelles images, scan complet) :
// bandeau au-dessus de la liste, avec le temps écoulé
export const SCAN_TIMEOUT = 60 * 60 * 1000;     // une nouvelle image est ouverte une fois : plusieurs minutes possibles

let busyTimer = null, busyDepth = 0;

export function libBusy(text, hint) {
  busyDepth++;
  const start = Date.now();
  const box = $("lib-loading");
  const paint = () => {
    const s = Math.round((Date.now() - start) / 1000);
    box.replaceChildren(el("span", { className: "spinner" }), el("div", { className: "body" },
      el("strong", { textContent: `${text} — ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` }),
      hint ? el("span", { textContent: hint }) : null));
  };
  clearInterval(busyTimer);
  paint();
  box.hidden = false;
  busyTimer = setInterval(paint, 1000);
  $("btn-menu").classList.add("spin");
  return () => {
    if (--busyDepth > 0) return;
    clearInterval(busyTimer);
    box.hidden = true;
    $("btn-menu").classList.remove("spin");
  };
}

// refresh : relire toutes les images ; saves : recalculer la taille des sauvegardes (Actualiser)
export async function loadLibrary(refresh = false, saves = false) {
  // Bandeau seulement si la lecture dure : un chargement depuis le cache ne clignote pas
  let done = null;
  const slow = setTimeout(() => { done = libBusy(refresh ? "Lecture de toutes les images" : "Chargement de la bibliothèque",
    "Les images ajoutées ou modifiées sont ouvertes une fois ; les autres viennent du cache."); }, 600);
  try {
    const query = [refresh && "refresh=true", saves && "saves=true"].filter(Boolean).join("&");
    state.items = await api(`/api/library${query ? `?${query}` : ""}`, { timeout: SCAN_TIMEOUT });
    $("status").textContent = "";
  } catch (error) {
    $("status").textContent = `erreur : ${error.message}`;
    toast(`Bibliothèque : ${error.message}`, "alert", true);
    return;
  } finally {
    clearTimeout(slow);
    done?.();
  }
  state.byId = new Map(state.items.map((i) => [i.id, i]));
  for (const id of [...state.selected]) if (!state.byId.has(id)) state.selected.delete(id);
  state.loadedAt = Date.now();
  paintRefreshAgo();
  renderLibrary();
  loadRecovery();
  if (state.current && state.byId.has(state.current.id)) {
    state.current = state.byId.get(state.current.id);
    renderDetailHeader();
  }
}

export function renderFilters() {
  const f = state.filter;
  closeMenu();
  $("filters").replaceChildren(...FILTERS.map((def) => {
    // Seuls les choix utiles : au moins un jeu, mais pas tous (le choix en cours reste affiché)
    const base = scopedItems(), total = base.length;
    const options = def.options().map((o) => [...o, base.filter((i) => matches(i, def.key, o[0])).length])
      .filter((o) => o[0] === f[def.key] || (o[4] > 0 && o[4] < total));
    if (!options.length) { f[def.key] = ""; return null; }
    if (!options.some(([v]) => v === f[def.key])) f[def.key] = "";
    return filterDropdown({ ...def, total, options, value: f[def.key], onChoose: (value) => { f[def.key] = value; renderLibrary(); } });
  }).filter(Boolean));
}

// Une image correspond-elle à la valeur d'un filtre ?
function matches(i, key, value) {
  if (!value) return true;
  switch (key) {
    case "kind": return value === "folder" ? i.kind === "folder" : i.kind !== "folder";
    case "system": return i.system === value;
    case "type": return value === "wine" ? ["wine_batocera", "wine_other"].includes(i.type) : i.type === value;
    case "runner": return value === "default" ? i.runner === "default" : value === "joined" ? Boolean(i.runner_joined) : i.runner !== "default";
    case "pad": return (value === "hidraw") === (i.hidraw === true);
    case "extra": return (i.extras || []).includes(value === "keys" ? ".keys" : value);
    case "special": return value === "noversion" ? !i.version : value === "old" ? Boolean(i.has_old)
      : value === "inuse" ? i.state === "in_use" : value === "noautorun" ? !i.cmd : value === "override" ? (i.override || []).length > 0 : true;
    default: return true;
  }
}

function filtered(items) {
  const q = $("f-search").value.trim().toLowerCase();
  return items.filter((i) =>
    (!q || [i.name, i.cmd, i.dir, i.version].some((v) => v && v.toLowerCase().includes(q)))
    && matches(i, "special", state.filter.special)
    && FILTERS.every((def) => matches(i, def.key, state.filter[def.key])));
}

function sorted(items) {
  const value = SORTS[state.sort.key];
  return [...items].sort((a, b) => {
    const x = value(a) ?? "", y = value(b) ?? "";
    const cmp = typeof x === "number" && typeof y === "number" ? x - y
      : String(x).localeCompare(String(y), "fr", { numeric: true, sensitivity: "base" });
    return cmp * state.sort.dir || a.name.localeCompare(b.name, "fr", { numeric: true, sensitivity: "base" });
  });
}

function selectBox(i) {
  const box = el("input", { type: "checkbox", checked: state.selected.has(i.id), title: "Sélectionner" });
  box.onclick = (event) => event.stopPropagation();
  box.onchange = () => {
    box.checked ? state.selected.add(i.id) : state.selected.delete(i.id);
    box.closest("[data-id]").classList.toggle("selected", box.checked);
    renderSelection();
  };
  return box;
}

let listSig = null;      // filtre, client, recherche, tri et présentation : les lignes entrent en cascade

// Bandeau et pastille de chargement du client choisi (sous le titre, pas de liste vide trompeuse)
function paintScope(scope) {
  const note = $("lib-scope");
  const offline = scope?.status === "offline", error = scope?.status === "error";
  note.hidden = !offline && !error;
  note.replaceChildren(...(note.hidden ? [] : [icon("wifi"), el("div", { className: "body" }, el("span", {
    textContent: offline ? `${scope.client.name} est hors ligne : liste des images installées indisponible. La liste complète reste affichée.`
      : `État d'installation de ${scope.client.name} indisponible : la liste complète reste affichée.` }))]));
  $("lib-busy").hidden = !scope || !(scope.status === "loading" || inst.loading);
}

export function renderLibrary() {
  renderFilters();
  const scope = scopeOf(), items = scope?.status === "ok" ? scope.items : state.items;
  const visible = sorted(filtered(items)), base = items.length;
  const total = items.reduce((n, i) => n + (i.size || 0), 0);
  const images = (n) => plural(n, IMAGES);
  $("count-images").textContent = state.items.length;      // onglet : le total (Sauvegardes et Dossiers : à traiter)
  $("lib-count").textContent = scope?.status === "ok" ? `${images(base)} sur ${scope.client.name}` : images(base);
  $("lib-size").textContent = bigSize(total, 2);
  $("visible-count").textContent = countLabel(visible.length, base, IMAGES);
  paintScope(scope);
  renderTodo();
  const grid = state.layout === "grid";
  paintLayout();
  const scopeKey = scope?.status === "ok" ? scope.client.id : "";
  const sig = [JSON.stringify(state.filter), scopeKey, $("f-search").value, state.sort.key, state.sort.dir, state.layout].join("|");
  const enter = sig !== listSig;
  listSig = sig;

  const empty = scope?.status === "ok" && !base ? emptyState("drive", `Aucune image installée sur ${scope.client.name}`, "Choisir « Tous » pour voir toute la bibliothèque.")
    : base ? emptyState("search", "Aucune image pour ces filtres", "Élargir la recherche ou retirer un filtre.")
    : emptyState("layers", "Aucune image", "Choisir les systèmes à gérer dans « Réglages ».");
  if (grid) {
    $("grid").replaceChildren(...visible.map((i) => {
      const card = el("div", { className: `gcard${state.selected.has(i.id) ? " selected" : ""}` },
        el("div", { className: "gart" }, thumb(i, "gcover", "poster"), selectBox(i), stateBadge(i), profileBadges(i), instPins(i)),
        el("div", { className: "meta" }, el("div", { className: "n", textContent: i.name, title: i.name }),
          el("div", { className: "sub" }, versionLabel(i), kindFlag(i), el("span", { textContent: size(i.size) }))));
      card.dataset.id = i.id;
      card.onclick = () => openDetail(i.id);
      return card;
    }));
    if (!visible.length) $("grid").append(el("div", { className: "fcard-empty" }, empty));
    $("rows").replaceChildren();
  } else {
    const all = selectAllBox(visible.map((i) => i.id), state.selected, renderLibrary);
    const sortBtn = sortHeader(state.sort, (sort) => { state.sort = sort; renderLibrary(); });
    $("thead").replaceChildren(all, sortBtn("name", "Jeu"), el("span", { className: "c-prefix", textContent: "Prefix" }),
      sortBtn("size", "Taille", "r"), sortBtn("saves_size", "Sauveg.", "r c-saves"), sortBtn("state", "État", "c-state"),
      el("span", { className: "c-pad", textContent: "Manette" }), el("span", { className: "c-profile", textContent: "Réglages" }));
    $("rows").replaceChildren(...stagger(visible.map((i) => listRow(i, enter))));
    if (!visible.length) $("rows").append(empty);
    $("grid").replaceChildren();
  }
  markCurrent();
  renderSelection();
}

// Une ligne de la liste : case, jaquette et nom (version), préfixe, taille, sauvegardes, état, manette, réglages actifs
const PREFIX_LABEL = { wine_batocera: "Wine", wine_other: "Wine", wine_proton: "Proton", game_only: "Aucun", unknown: "Inconnu" };

function listRow(i, enter) {
  const row = el("div", { className: `grow item${state.selected.has(i.id) ? " selected" : ""}${enter ? " enter" : ""}` },
    selectBox(i),
    el("div", { className: "iname" }, thumb(i), el("div", { className: "t" },
      el("span", { className: "n", textContent: i.name, title: i.name }), versionLabel(i), kindFlag(i), instPins(i))),
    el("span", { className: `prefix c-prefix${i.type === "game_only" ? " none" : ""}`, textContent: PREFIX_LABEL[i.type] || "Inconnu" }),
    el("span", { className: "gsize", textContent: size(i.size) }),
    el("span", { className: "gsize c-saves", textContent: i.saves_present ? size(i.saves_size) : "" }),
    listState(i),
    padCell(i),
    listFlags(i));
  row.dataset.id = i.id;
  row.onclick = () => openDetail(i.id);
  return row;
}

// Manette : le logo seul (le nom en infobulle) ; DualSense ressort, Xbox (le défaut) reste discrète
function padCell(i) {
  const name = i.hidraw ? "DualSense" : "Xbox";
  return el("span", { className: `c-pad pad ${i.hidraw ? "hidraw" : "xinput"}`, role: "img", ariaLabel: `Manette : ${name}`,
    title: i.hidraw ? "Manette : DualSense (accès direct, clé HIDRAW)" : "Manette : Xbox" }, icon(i.hidraw ? "dualsense" : "xbox"));
}

// Réglages actifs seulement : commande pendant le jeu, surcharge à inscrire
function listFlags(i) {
  const flags = [];
  if (i.attach) flags.push(el("span", { className: "flag attach", textContent: "ATTACH", title: `Commande pendant le jeu : ${i.attach}` }));
  if ((i.override || []).length) flags.push(el("span", { className: "flag warn", textContent: "Surcharge", title: "Surcharge à inscrire dans l'image" }));
  return el("span", { className: "flags c-profile" }, ...flags);
}

// État : « Prêt » reste gris ; seules les exceptions sont en couleur
function listState(i) {
  const [cls, label, tip] = (i.override || []).length ? ["warn", "Surcharge", "Surcharge à inscrire : réglage appliqué hors de l'image"]
    : [i.state, ...(STATES[i.state] || [i.state, ""])];
  return el("span", { className: `state ${cls} c-state`, textContent: label, title: `${label} — ${tip}` });
}

// Images absentes ou illisibles dont l'ancienne version (.old) est saine
// (coupure pendant un échange, par exemple) : remise en un clic
async function loadRecovery() {
  let damaged = [];
  try { damaged = await api("/api/recovery"); } catch { /* serveur plus ancien */ }
  $("recovery").replaceChildren(...damaged.map((d) => {
    const fix = el("button", { className: "btn danger sm", textContent: "Remettre l'ancienne version" });
    fix.onclick = async () => {
      if (!confirm(`Remettre l'ancienne version de ${d.id} (${size(d.old_size)}) ?`)) return;
      try {
        await api("/api/recovery/restore", { method: "POST", body: { old: d.old } });
        await loadLibrary();
      } catch (error) { alert(error.message); }
    };
    return el("div", { className: "notice err" }, icon("alert"),
      el("div", { className: "body" }, el("span", { textContent: `${d.id} : ${d.problem}. L'ancienne version (.old) est intacte.` }),
        el("div", { className: "row" }, fix)));
  }));
}

// Pastilles « À traiter » : seules celles dont le compteur est non nul ; un clic filtre, un deuxième le retire
const SPECIALS = [   // [filtre, libellés (un, plusieurs), icône, ton, test]
  ["noversion", ["sans version", "sans version"], "tag", "accent", (i) => !i.version],
  ["override", ["surcharge à inscrire", "surcharges à inscrire"], "filepen", "warn", (i) => (i.override || []).length > 0],
  ["old", ["ancienne version à supprimer ou restaurer", "anciennes versions à supprimer ou restaurer"], "history", "accent", (i) => Boolean(i.has_old)],
  ["inuse", ["en cours", "en cours"], "play", "accent", (i) => i.state === "in_use"],
  ["noautorun", ["sans autorun", "sans autorun"], "terminal", "accent", (i) => !i.cmd],
];

export function renderTodo() {
  const special = state.filter.special, items = scopedItems();
  const chips = [];
  for (const [id, [one, many], iconName, tone, test] of SPECIALS) {
    const n = items.filter(test).length;
    if (!n) continue;
    const on = special === id;
    chips.push(todoChip(n, n > 1 ? many : one, iconName, tone, on, () => {
      state.filter.special = on ? "" : id;
      renderLibrary();
    }));
  }
  const failed = state.tasks.filter((t) => t.status === "failed").length;
  if (failed) chips.push(todoChip(failed, `${failed > 1 ? "tâches" : "tâche"} en échec`, "alert", "err", false, openTasks));
  const active = SPECIALS.find((d) => d[0] === special);      // filtre actif, rappelé dans la barre
  paintTodo({ todo: "todo", clear: "filter-clear" }, chips, active ? cap(active[1][0]) : "");
}

function renderSelection() {
  toggleSelbar($("selection"), state.selected.size, $("sel-count"), IMAGES);
  paintSelectionActions();
}

// Relit la configuration (Réglages, client d'essai…) : Actualiser, fin des Réglages
async function loadConfig() {
  try { state.config = await api("/api/config"); } catch { /* garde la configuration déjà lue */ }
  applyMode();
}

export function init() {
  $("filter-clear").onclick = () => { state.filter.special = ""; renderLibrary(); };
  $("f-search").addEventListener("input", renderLibrary);
  paintLayout = layoutToggle($("layout"), { key: "wsfs-layout", get: () => state.layout, set: (v) => { state.layout = v; },
    onChange: renderLibrary, list: "list", grid: "grid" });
  paintLayout();
  $("btn-refresh").onclick = () => { loadLibrary(false, true); loadConfig(); loadSaves(); };
  $("btn-sel-clear").onclick = () => { state.selected.clear(); renderLibrary(); };
}
