// saves.js — Sauvegardes entre clients : état, lecture, liste, bandeau de sélection.

import { api } from "./api.js";
import { paintInitials, putArt, showArt } from "./art.js";
import { countLabel, detailPanel, layoutToggle, markCurrent, paintTodo, plural, selectAllBox, sortHeader, toggleSelbar, todoChip } from "./collection.js";
import { $, ago, bigSize, el, emptyState, icon, setUnavailable, size, stagger, state, storage, when } from "./core.js";
import { notice } from "./detail/autorun.js";
import { openDetail } from "./detail/detail.js";
import { paintClients, showView } from "./header.js";
import { renderLibrary } from "./library.js";
import { filterDropdown, menus, openMenuNode } from "./menu.js";
import { loadActivity } from "./activity.js";
import { busyIcons } from "./aicons.js";
import { connected } from "./mode.js";
import { menuItem, onMachine } from "./saves-machines.js";
import { openResolve, openRestore } from "./saves-resolve.js";
import { openSaveDelete, pullGames, pushFrom, pushManager, pushReserve, pushSelection, reachManagerTargets, syncResultNode } from "./saves-ops.js";
import { paintSettingsClients } from "./settings.js";

// Onglet « Sauvegardes » (services/saves.py via /api/saves/*) : bandeau des clients,
// tableau jeux × clients, sélection et barre d'actions. Une opération à la fois : la
// tâche « saves » est suivie jusqu'à la fin (followTask), puis l'état est relu.

export const RESERVE = "__reserve__";          // source : le manager (copie des sauvegardes)

export const AUTO = "auto";                    // source : le client suggéré (la plus lourde), jeu par jeu

export const PRESENT_STATES = new Set(["présente", "identique", "plus avancée", "en retard"]);

// Nom affiché d'une ligne : nom lisible d'un dossier déclaré ou d'un jeu Switch, sinon le jeu
export const gameLabel = (g) => g.label || g.game;

// Plateformes (filtre de la barre) : une par système Batocera de l'image du jeu (dossier sous roms/), puis
// Switch, Dossiers, et « Autres » (aucune image trouvée). Ordre : Jeux PC, autres systèmes, Switch, Dossiers, Autres.
const SYSTEM_LABELS = { win: "Jeux PC", arcadepc: "Arcade PC", "type-x": "Taito Type X", "type-x2": "Taito Type X²",
  nesicax: "NESiCAxLive", nesicax2: "NESiCAxLive 2", exaarcadia: "exA-Arcadia", adrenaline: "Adrenaline", unis: "UNIS",
  segaalls: "Sega ALLS", segaer: "Sega Europa-R", seganu: "Sega Nu", segare: "Sega RingEdge", segarw: "Sega RingWide",
  konamipc: "Konami PC", rawthrills: "Raw Thrills", globalvr: "Global VR", namcoes3: "Namco ES3", namco2x6: "Namco System 2x6",
  ps3: "PlayStation 3" };

const sectionOf = (g) => g.kind === "switch" ? "switch" : g.kind === "dossier" ? "dossier" : `sys:${g.system || ""}`;

const sectionTitle = (key) => key === "switch" ? "Switch" : key === "dossier" ? "Dossiers" : key === "sys:" ? "Autres (image introuvable)"
  : SYSTEM_LABELS[key.slice(4)] || key.slice(4);

const sectionRank = (key) => key === "sys:win" ? 0 : key === "switch" ? 2 : key === "dossier" ? 3 : key === "sys:" ? 4 : 1;

// Filtre de plateforme et tri des colonnes, mémorisés dans le navigateur (relus par init : core.js
// n'est pas encore initialisé quand ce module est évalué)
const SORT_KEY = "wsfs-saves-sort", PLATFORM_KEY = "wsfs-saves-platform", LAYOUT_KEY = "wsfs-saves-layout";

const SAVES = ["sauvegarde", "sauvegardes"], GAMES = ["jeu", "jeux"];

// Fiche d'un jeu (colonne de la liste réduite, panneau en glissement), créée au premier usage
let panel = null;
const sheet = () => panel ??= detailPanel($("saves-main"), $("sv-detail"));

let paintLayout = () => {};      // posé par init : bascule liste / jaquettes

export const OS_ICONS = ["linux", "steamos", "steambox", "batocera"];   // icônes choisissables d'un client

export const OS_LABELS = { linux: "Linux", steamos: "SteamOS", steambox: "SteamBox", batocera: "Batocera" };

export const sv = {
  machines: [],          // clients joints à leur état (kind, removable, ok, games, message)
  games: [],             // { game, reserve, machines: [état par client] }
  loaded: false, error: "", seq: 0,
  todo: "", selected: new Set(), unchecked: new Set(),   // todo : pastille « À traiter » active ; unchecked : cibles décochées
  source: AUTO,
  platform: "",          // plateforme filtrée ("" : toutes)
  sort: { key: "name", dir: 1 },   // colonne triée : name, size, played, synced
  current: null,         // jeu dont la fiche est ouverte
  layout: "list",        // liste ou jaquettes, comme Images
  op: null,              // { text } pendant une opération
  result: null,          // { task, error } de la dernière opération
};

// Nom de jeu comme services/saves.game_name : nom de fichier sans .wsquashfs ni .pc
export function gameOfItem(i) {
  let name = (i.path || i.id).split("/").pop();
  for (const suffix of [".wsquashfs", ".pc"]) {
    if (name.toLowerCase().endsWith(suffix) && name.length > suffix.length) name = name.slice(0, -suffix.length);
  }
  return name;
}

// Jeu → identifiant de sa fiche : l'image d'abord ; à défaut son dossier `*.pc` (même nom, mêmes
// sauvegardes). Une seule entrée par jeu : l'image l'emporte quand les deux existent.
const imageIds = () => {
  const ids = new Map();
  for (const i of state.items) if (i.kind === "folder") ids.set(gameOfItem(i), i.id);
  for (const i of state.items) if (i.kind !== "folder") ids.set(gameOfItem(i), i.id);
  return ids;
};
// Jeu présent à la fois comme image et comme dossier `*.pc` (la fiche de l'image est ouverte)
const imageAndFolder = (game) => state.items.some((i) => i.kind === "folder" && gameOfItem(i) === game)
  && state.items.some((i) => i.kind !== "folder" && gameOfItem(i) === game);

export const busy = () => Boolean(sv.op);

// Raisons affichées sur les actions impossibles (infobulle, action grisée)
const BUSY = "Une opération est déjà en cours";
const NO_IMAGE = "Pas d'image dans la bibliothèque";

export const machineName = (id) => sv.machines.find((m) => m.id === id)?.name ?? id;

export const names = (ids) => ids.map(machineName).join(", ");

export const reachableIds = () => sv.machines.filter((m) => m.ok).map((m) => m.id);

const targetIds = () => reachableIds().filter((id) => !sv.unchecked.has(id));

export const pushTargets = () => targetIds().filter((id) => id !== sv.source);

export const stateOf = (g, id) => g.machines.find((s) => s.id === id);

export const suggestedOf = (g) => g.machines.find((s) => s.state === "plus avancée");   // la suggérée (la plus lourde)

export const presentIn = (g) => g.machines.filter((s) => PRESENT_STATES.has(s.state));

// Un client sans le jeu (« absente » : stockage plus petit, jeu pas installé) n'est pas une
// différence : Sync ne vise que les clients « en retard ». L'installer : menu « … ».
export const lateIn = (g) => g.machines.filter((s) => s.state === "en retard").map((s) => s.id);

const absentIn = (g) => reachableIds().filter((id) => (stateOf(g, id)?.state ?? "absente") === "absente");

// Différences : une au moins est en retard.

// Source par défaut d'un jeu : la suggérée, sinon un client qui l'a (seul ou identique)
const sourceOf = (g) => suggestedOf(g) || presentIn(g).find((s) => reachableIds().includes(s.id));

// Le manager a déjà la version du client `s` (même empreinte, casse ignorée) : rien à copier
const inManager = (g, s) => Boolean(g.reserve?.fingerprint && s?.fingerprint
  && g.reserve.fingerprint.toLowerCase() === s.fingerprint.toLowerCase());

// Jeu à copier vers le manager depuis `source` (un client ou AUTO) : une version qu'il n'a pas encore
const pullNeeded = (g, source) => {
  const from = source === AUTO ? sourceOf(g) : stateOf(g, source);
  return Boolean(from && from.fingerprint && !inManager(g, from));
};

export const osIcon = (key, cls = "") => icon(OS_ICONS.includes(key) || key === "server" ? key : "linux", cls);

export async function loadSaves() {
  if (!connected()) return;
  const seq = ++sv.seq;               // seule la dernière lecture compte
  paintSavesView();
  try {
    const [status, listed] = await Promise.all([api("/api/saves/status"), api("/api/saves/machines")]);
    if (seq !== sv.seq) return;
    const kinds = new Map(listed.machines.map((m) => [m.id, m]));
    sv.machines = status.machines.map((m) => ({ ...(kinds.get(m.id) || {}), ...m }));
    sv.games = status.games;
    sv.loaded = true;
    sv.error = "";
    sv.selected = new Set([...sv.selected].filter((g) => sv.games.some((x) => x.game === g)));
    if (sv.source !== AUTO && sv.source !== RESERVE && !reachableIds().includes(sv.source)) sv.source = AUTO;
  } catch (error) {
    if (seq !== sv.seq) return;
    sv.error = error.message;
  }
  paintSavesView();
  paintSettingsClients();
  if (!$("main").hidden) renderLibrary();      // la colonne « Sauveg. » des images
  loadActivity();                              // jeux en cours et manettes relus chez les agents
}

// Vue Sauvegardes : le client choisi en haut de page (state.client), ou « Toutes », filtre la liste.
// Chaque ligne montre la synchro de ce client (ou de l'ensemble) et une seule action.
const SYNC_LABEL = { ok: "À jour", send: "À envoyer", get: "À récupérer", conflict: "Conflit", none: "Non partagée",
  absent: "Absente", off: "Hors ligne" };

const SYNC_ICON = { ok: "check", send: "arrow", get: "arrow", conflict: "alert", none: "info", absent: "info", off: "wifi" };

const CONFLICT_TITLE = "Modifiée sur le client et sur le manager depuis leur dernier échange : les deux versions sont gardées, choisis celle à conserver";

// Client choisi en haut de page, ou null pour « Toutes »
const scopeMachine = () => (connected() && state.client !== "all" ? sv.machines.find((m) => m.id === state.client) : null) ?? null;

// Copie affichée pour un jeu : la plus récente entre celle du client choisi (ou des clients
// joignables) et celle du manager. `where` nomme son origine.
function copyOf(g, scope) {
  const reach = reachableIds();
  const cands = g.machines.filter((s) => (scope ? s.id === scope.id : reach.includes(s.id)) && PRESENT_STATES.has(s.state) && s.newest)
    .map((s) => ({ t: s.newest, size: s.size, where: s.name }));
  if (g.reserve?.newest) cands.push({ t: g.reserve.newest, size: g.reserve.size, where: g.reserve.source || "" });
  return cands.sort((a, b) => b.t - a.t)[0] ?? null;
}

// « aujourd'hui, 00:06 » · « hier » · « 12/11 »
export function dayText(t) {
  const d = new Date(t * 1000), now = new Date();
  const days = Math.round((new Date(now.getFullYear(), now.getMonth(), now.getDate())
    - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
  if (days === 0) return `aujourd'hui, ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
  if (days === 1) return "hier";
  return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" });
}

// Synchro d'un jeu d'après la décision du backend (`action` de chaque client) : kind (ok, send, get,
// conflict, none, absent, off), et le bouton de la ligne (button ; act : action). Un client qui n'a pas
// le jeu n'est jamais servi par défaut : son installation se fait dans le menu de la ligne.
function syncOf(g, scope) {
  const reach = reachableIds();
  const conflict = { kind: "conflict", title: CONFLICT_TITLE, button: "Départager…", act: () => openResolve(g) };
  const ids = scope ? [scope.id] : reach;
  const mine = g.machines.filter((s) => ids.includes(s.id));
  if (scope && !scope.ok) return { kind: "off", title: `${scope.name} est hors ligne : rien ne peut être envoyé avant son retour` };
  if (g.conflicts?.length || mine.some((s) => s.action === "conflit")) return conflict;
  const up = mine.filter((s) => s.action === "remonter").sort((a, b) => (b.newest || 0) - (a.newest || 0))[0];
  if (up) {
    return { kind: "send", title: `${up.name} est en avance sur le manager : sa copie remonte au manager`, button: "Envoyer au manager", act: () => pullGames([g], up.id) };
  }
  const down = mine.filter((s) => s.action === "descendre").map((s) => s.id);
  if (down.length) {
    return { kind: "get", title: `${names(down)} ${down.length > 1 ? "sont en retard" : "est en retard"} sur le manager : ${down.length > 1 ? "ils reçoivent" : "il reçoit"} sa version`,
      button: "Récupérer du manager", act: () => pushManager(g, down) };
  }
  if (scope && (stateOf(g, scope.id)?.state ?? "absente") === "absente") {
    const holders = reach.filter((id) => id !== scope.id && onMachine(g, id)).length;
    return holders ? { kind: "absent", title: `Pas de sauvegarde de ce jeu sur ${scope.name} : l'installer depuis le menu de la ligne` }
      : { kind: "none", title: `Pas de sauvegarde de ce jeu sur ${scope.name}, ni sur un autre client joignable` };
  }
  const places = reach.filter((id) => onMachine(g, id)).length + (g.reserve ? 1 : 0);
  if (places <= 1) return { kind: "none", title: "Une seule copie : aucun autre client ne la partage" };
  return { kind: "ok", title: scope ? `${scope.name} a la version du manager` : "Toutes les copies sont à jour" };
}

// Le jeu est présent sur ce client : une copie, quel que soit son état. « absente » (pas de copie)
// ne compte pas ; « injoignable » (client hors ligne) reste compté, la copie est connue à la dernière synchro.
const presentOn = (g, id) => (stateOf(g, id)?.state ?? "absente") !== "absente";

// Lignes du client choisi (ou de toutes) : jeux avec une copie sur un client joignable ou sur le manager.
// Hors ligne, seule la copie du manager est connue (dernière synchro). Client choisi : seulement les jeux
// présents sur lui (les lignes « Absente » ne sont pas listées ; l'installer se fait depuis le menu d'un jeu).
function svBase(scope) {
  const reach = reachableIds(), offline = Boolean(scope && !scope.ok);
  return sv.games.filter((g) => (offline ? Boolean(g.reserve) : Boolean(g.reserve) || reach.some((id) => onMachine(g, id))))
    .filter((g) => !scope || presentOn(g, scope.id))
    .map((g) => ({ g, sync: syncOf(g, scope), copy: copyOf(g, scope),
      // Dernière synchro : du client choisi, sinon du jeu (la plus récente)
      synced: (scope ? stateOf(g, scope.id)?.synced : g.synced_at) || 0 }));
}

// Valeur de tri d'une ligne ; à égalité, le nom
const SORTERS = { size: (r) => r.copy?.size || 0, played: (r) => r.copy?.t || 0, synced: (r) => r.synced };
const byName = (a, b) => gameLabel(a.g).localeCompare(gameLabel(b.g), "fr", { numeric: true, sensitivity: "base" });

// Pastille « À traiter » active, plateforme, recherche ; tri de la colonne choisie
function svVisible(base) {
  const q = $("s-search").value.trim().toLowerCase();
  const { key, dir } = sv.sort, value = SORTERS[key];
  return base.filter((r) => (!q || r.g.game.toLowerCase().includes(q) || gameLabel(r.g).toLowerCase().includes(q))
    && (!sv.platform || sectionOf(r.g) === sv.platform)
    && (sv.todo !== "conflict" || r.sync.kind === "conflict")
    && (sv.todo !== "sync" || r.sync.kind === "send" || r.sync.kind === "get"))
    .sort((a, b) => (value ? dir * (value(a) - value(b)) : 0) || (value ? byName(a, b) : dir * byName(a, b)));
}

// Filtre Plateforme (comme les filtres des Images) : seulement s'il y a plusieurs plateformes
function paintPlatformFilter(base) {
  const keys = [...new Set(base.map((r) => sectionOf(r.g)))]
    .sort((a, b) => sectionRank(a) - sectionRank(b) || sectionTitle(a).localeCompare(sectionTitle(b), "fr"));
  if (sv.platform && !keys.includes(sv.platform)) sv.platform = "";
  const options = keys.map((k) => [k, null, "", sectionTitle(k), base.filter((r) => sectionOf(r.g) === k).length]);
  $("s-filters").replaceChildren(...(keys.length > 1 || sv.platform ? [filterDropdown({
    label: "Plateforme", all: "Toutes les plateformes", total: base.length, options, value: sv.platform,
    onChoose: (value) => { sv.platform = value; storage.set(PLATFORM_KEY, value); paintSvList(); } })] : []));
}

let svSig = null;           // client, pastille et recherche : les lignes entrent en cascade

function paintSvList() {
  const scope = scopeMachine(), base = svBase(scope), visible = svVisible(base);
  const total = base.reduce((n, r) => n + (r.copy?.size || 0), 0);
  // En-tête comme Images : « N sauvegardes » (« sur <client> » s'il est choisi), la taille seule ;
  // hors ligne, les tailles viennent de la copie du manager (dernière synchro), dit l'infobulle
  const saves = (n) => plural(n, SAVES);
  $("sv-count").textContent = scope ? `${saves(base.length)} sur ${scope.name}` : saves(base.length);
  $("sv-size").textContent = base.length ? bigSize(total, 2) : "";
  $("sv-size").title = !scope ? "Sur tous les clients" : scope.ok ? `Sur ${scope.name}` : `${scope.name} hors ligne : tailles à sa dernière synchro`;
  const off = Boolean(scope && !scope.ok);
  $("sv-offline").hidden = !off;
  $("sv-offline").replaceChildren(...(off ? [icon("wifi"), el("span", { textContent: `${scope.name} est hors ligne : sauvegardes connues à sa dernière synchro. Rien ne peut être envoyé avant son retour.` })] : []));

  const conflicts = base.filter((r) => r.sync.kind === "conflict").length;
  const syncs = base.filter((r) => r.sync.kind === "send" || r.sync.kind === "get").length;
  const toggle = (key) => () => { sv.todo = sv.todo === key ? "" : key; paintSvList(); };
  const chips = [];
  if (conflicts) chips.push(todoChip(conflicts, conflicts > 1 ? "conflits" : "conflit", "alert", "warn", sv.todo === "conflict", toggle("conflict")));
  if (syncs) chips.push(todoChip(syncs, "à synchroniser", "refresh", "accent", sv.todo === "sync", toggle("sync")));
  paintTodo({ todo: "sv-todo", clear: "s-clear-filter" }, chips, { conflict: "Conflits", sync: "À synchroniser" }[sv.todo] || "");
  $("s-visible").textContent = countLabel(visible.length, base.length, SAVES);

  const sig = [scope?.id ?? "all", sv.todo, sv.platform, sv.sort.key, sv.sort.dir, $("s-search").value.trim()].join("|");
  const enter = sig !== svSig;
  svSig = sig;
  const all = selectAllBox(visible.map((r) => r.g.game), sv.selected, () => { paintSvList(); paintSelection(); });
  // En-têtes triables, comme dans Images ; une date se trie d'abord de la plus récente
  const head = sortHeader(sv.sort, (sort) => { sv.sort = sort; storage.set(SORT_KEY, JSON.stringify(sort)); paintSvList(); });
  const sortBtn = (key, label, cls = "", first = 1) => head(key, label, cls, { first,
    title: key === "synced" ? "Trier par dernière synchro : ce qui a changé récemment" : `Trier par ${label.toLowerCase()}` });
  $("s-thead").replaceChildren(all, sortBtn("name", "Jeu"), sortBtn("size", "Taille", "c-size r", -1),
    sortBtn("played", "Dernière partie", "c-when", -1), sortBtn("synced", "Synchro", "", -1), el("span"));
  paintPlatformFilter(base);

  const images = imageIds();
  const grid = sv.layout === "grid";
  paintLayout();
  if (grid) {
    $("s-grid").replaceChildren(...visible.map((r) => svCard(r, images)));
    if (!visible.length) $("s-grid").append(el("div", { className: "fcard-empty" }, base.length
      ? emptyState("search", "Aucun jeu pour ces filtres", "Élargir la recherche ou retirer le filtre.")
      : emptyState("save", "Aucune sauvegarde", "Les clients associés n'ont aucune sauvegarde de partie.")));
    $("s-rows").replaceChildren();
    return;
  }
  $("s-grid").replaceChildren();
  const rows = visible.map((r) => svListRow(r, enter, images));
  if (!visible.length) rows.push(el("div", { className: "svempty" }, base.length
    ? emptyState("search", "Aucun jeu pour ces filtres", "Élargir la recherche ou retirer le filtre.")
    : emptyState("save", "Aucune sauvegarde", "Les clients associés n'ont aucune sauvegarde de partie.")));
  $("s-rows").replaceChildren(...stagger(rows));
}

// Ouvre la fiche de l'image du jeu (onglet Images)
const openImage = (id) => { showView("images"); openDetail(id); };

// Une ligne : case, vignette et jeu, taille, dernière partie, synchro et son bouton, menu « ⋯ »
function svListRow({ g, sync, copy, synced }, enter, images) {
  const scope = scopeMachine();
  const selected = sv.selected.has(g.game);
  const img = images.get(g.game);
  const box = el("input", { type: "checkbox", checked: selected, title: "Sélectionner" });
  box.onclick = (event) => event.stopPropagation();
  const more = el("button", { type: "button", className: "iconbtn bare sm", title: "Autres actions", "aria-label": `Autres actions pour ${gameLabel(g)}`, "aria-haspopup": "menu", "aria-expanded": "false" },
    icon("more", "sm"));
  const wrap = el("div", { className: "dd end sv-more" }, more);
  more.onclick = (event) => { event.stopPropagation(); openMenuNode(wrap, rowItems(g)); };
  const primary = sync.button ? el("button", { type: "button", className: "btn secondary sm sv-act", textContent: sync.button, disabled: busy() }) : null;
  if (primary) {
    primary.onclick = (event) => {
      event.stopPropagation();
      sync.act();
    };
  }
  // Nom et vignette : ouvrent la fiche de l'image ; sans image dans la bibliothèque, ils ne le sont pas
  const thumb = saveThumb(g, img);
  const name = gameLabel(g);
  const nameNode = img
    ? el("button", { type: "button", className: "n sv-open", textContent: name, title: `Ouvrir la fiche de l'image · ${name}` })
    : el("span", { className: "n", textContent: name, title: NO_IMAGE });
  if (img) {
    nameNode.onclick = (event) => { event.stopPropagation(); openImage(img); };
    thumb.classList.add("sv-open");
    thumb.title = `Ouvrir la fiche de l'image · ${name}`;
    thumb.onclick = (event) => { event.stopPropagation(); openImage(img); };
  } else {
    thumb.title = NO_IMAGE;
  }
  const whenText = copy ? [dayText(copy.t), copy.where && copy.where !== scope?.name ? `sur ${copy.where}` : ""].filter(Boolean).join(", ") : "";
  const row = el("div", { className: `grow item${selected ? " selected" : ""}${enter ? " enter" : ""}` },
    box,
    el("div", { className: "iname" }, thumb,
      el("div", { className: "t" }, nameNode)),
    el("span", { className: "gsize c-size", textContent: copy ? size(copy.size) : "—" }),
    el("span", { className: "sv-when c-when", textContent: whenText || "—", title: copy ? when(copy.t) : "" }),
    el("div", { className: `sync ${sync.kind}`, title: sync.title + (g.check ? " · la plus lourde n'est pas la plus récente : à vérifier" : "")
      + (synced ? ` · dernière synchro le ${when(synced)}` : " · jamais synchronisé") },
      el("span", { className: "sync-txt" },
        el("span", { className: "sync-lbl" }, icon(SYNC_ICON[sync.kind], "sm"), el("span", { textContent: SYNC_LABEL[sync.kind] })),
        el("span", { className: "sync-ago", textContent: synced ? ago(synced) : "jamais synchronisé" })),
      primary),
    wrap);
  row.dataset.id = g.game;
  // Clic sur la ligne : la fiche du jeu, comme dans Images ; la case coche la ligne
  if (sv.current === g.game) row.classList.add("current");
  row.onclick = (event) => {
    if (event.target.closest("button, input")) return;
    openSave(g.game);
  };
  box.onchange = () => {
    box.checked ? sv.selected.add(g.game) : sv.selected.delete(g.game);
    row.classList.toggle("selected", box.checked);
    paintSelection();
  };
  return row;
}

// Jaquette d'un jeu (vue « Jaquettes ») : affiche, case de sélection, pastille de synchro ; clic : sa fiche
function svCard({ g, sync, synced }, images) {
  const img = images.get(g.game);
  const box = el("input", { type: "checkbox", checked: sv.selected.has(g.game), title: "Sélectionner", "aria-label": `Sélectionner ${gameLabel(g)}` });
  box.onclick = (event) => event.stopPropagation();
  box.onchange = () => {
    box.checked ? sv.selected.add(g.game) : sv.selected.delete(g.game);
    card.classList.toggle("selected", box.checked);
    paintSelection();
  };
  const badge = el("span", { className: `sv-badge ${sync.kind}`, title: `${SYNC_LABEL[sync.kind]} · ${sync.title}` }, icon(SYNC_ICON[sync.kind], "xs"));
  const card = el("div", { className: `gcard sv-card${sv.selected.has(g.game) ? " selected" : ""}${sv.current === g.game ? " current" : ""}` },
    el("div", { className: "gart" }, saveThumb(g, img, "gcover", "poster"), box, badge),
    el("div", { className: "meta" }, el("div", { className: "n", textContent: gameLabel(g), title: gameLabel(g) }),
      el("div", { className: "sub" }, el("span", { className: `sync-mini ${sync.kind}`, textContent: SYNC_LABEL[sync.kind] }),
        el("span", { textContent: synced ? ago(synced) : "jamais", title: synced ? `Dernière synchro le ${when(synced)}` : "Jamais synchronisé" }))));
  card.dataset.id = g.game;
  card.onclick = () => openSave(g.game);
  return card;
}

export function paintSavesView() {
  paintClients();
  const none = sv.loaded && sv.machines.length === 0;
  // Compteur de l'onglet : comme les pastilles « À traiter » (client choisi, ou tous)
  const kinds = svBase(scopeMachine()).map((r) => r.sync.kind);
  const conflicts = kinds.filter((k) => k === "conflict").length, syncs = kinds.filter((k) => k === "send" || k === "get").length;
  $("count-saves").textContent = conflicts + syncs || "";
  $("count-saves").title = [conflicts ? `${conflicts} conflit${conflicts > 1 ? "s" : ""}` : "", syncs ? `${syncs} à synchroniser` : ""].filter(Boolean).join(", ");
  $("s-loading").hidden = sv.loaded || Boolean(sv.error);
  $("s-error").replaceChildren(...(sv.error ? [notice("err", sv.error)] : []));
  $("s-empty").hidden = !none;
  $("s-body").hidden = !sv.loaded || none;
  $("sv-head").hidden = $("s-body").hidden;
  if (!$("s-body").hidden) {
    paintSvList();
    paintStatus();
  }
  paintSaveDetail();
  paintSelection();
}

// Vignette d'une ligne : illustration de l'image (jeu PC de la bibliothèque, comme la vue Images),
// jaquette du gamelist.xml de Batocera (jeu Switch, /api/saves/art), sinon initiales.
const svArt = { cache: new Map(), done: new Map() };

function saveThumb(g, imgId, className = "thumb sv-thumb", source = "image") {
  const node = el("span", { className, "aria-hidden": "true" });
  const item = imgId ? state.byId.get(imgId) : null;
  if (item) {
    node.dataset.id = item.id;          // showArt vérifie que la vignette est toujours celle de l'image
    showArt(node, item, source);
    return node;
  }
  if (g.kind === "switch" && svArt.done.has(g.game)) {
    const a = svArt.done.get(g.game);
    a ? putArt(node, a, false) : paintInitials(node, gameLabel(g));
    return node;
  }
  paintInitials(node, gameLabel(g));
  if (g.kind === "switch") {
    if (!svArt.cache.has(g.game)) {
      svArt.cache.set(g.game, fetch(`/api/saves/art?game=${encodeURIComponent(g.game)}`, { headers: { "x-wsfs-token": storage.get("wsfs-token") || "" } })
        .then(async (r) => r.ok ? { url: URL.createObjectURL(await r.blob()), kind: r.headers.get("X-Art-Kind") || "es" } : null)
        .catch(() => null)
        .then((a) => { svArt.done.set(g.game, a); return a; }));
    }
    svArt.cache.get(g.game).then((a) => { if (a) putArt(node, a, true); });
  }
  return node;
}

// Menu « … » d'une ligne : une action impossible reste visible, grisée, avec sa raison
function rowItems(g) {
  const busyReason = busy() ? BUSY : "";
  const reach = reachableIds(), top = sourceOf(g), scope = scopeMachine();
  // Depuis un client qui a le jeu vers les autres qui l'ont dans une autre version
  const others = (s) => presentIn(g).filter((o) => o.id !== s.id && o.fingerprint !== s.fingerprint && reach.includes(o.id)).map((o) => o.id);
  const img = imageIds().get(g.game);
  // Copie envoyée au manager : celle du client choisi, sinon la plus récente des clients joignables
  const from = scope ? (reach.includes(scope.id) ? stateOf(g, scope.id) : null)
    : g.machines.filter((s) => reach.includes(s.id) && PRESENT_STATES.has(s.state)).sort((a, b) => (b.newest || 0) - (a.newest || 0))[0];
  const fromOk = from && PRESENT_STATES.has(from.state);
  const item = (label, iconName, reason, action, danger = false) => setUnavailable(menuItem(label, iconName, action, { danger }), reason);
  const conflict = Boolean(g.conflicts?.length || g.machines.some((s) => s.action === "conflit"));
  // En conflit, une copie ne remplace une autre qu'en passant par Départager (les deux y sont comparées)
  const held = busyReason || (conflict ? "En conflit : choisis la version à garder avec « Départager… »" : "");
  return [
    item("Ouvrir la fiche de l'image", "image", img ? "" : NO_IMAGE, () => openImage(img)),
    ...(conflict ? [item("Départager…", "alert", busyReason, () => openResolve(g))] : []),
    item("Envoyer au manager", "save", held || (!fromOk ? "Aucune copie sur un client joignable" : inManager(g, from) ? "Le manager a déjà cette version" : ""),
      () => pullGames([g], from.id)),
    item("Récupérer du manager", "server", held || (!g.reserve ? "Pas de copie sur le manager"
      : !reachManagerTargets(g).length ? "Aucun client joignable n'a une autre version" : ""), () => pushReserve(g)),
    ...presentIn(g).filter((s) => reach.includes(s.id) && others(s).length)
      .map((s) => item(`Copier de ${s.name} vers ${names(others(s))}`, "arrow", held, () => pushFrom(g, s.id, others(s)))),
    // Installer sur un client qui n'a pas le jeu : choix explicite, jamais par défaut
    ...(top ? absentIn(g).map((id) => item(`Installer sur ${machineName(id)} (depuis ${top.name})`, "arrow", busyReason, () => pushFrom(g, top.id, [id]))) : []),
    item("Supprimer…", "trash", busyReason, () => openSaveDelete([g]), true),
  ];
}

// ------------------------------------------------------------------ fiche d'un jeu
// Même gabarit que la fiche d'une image : en-tête (jaquette, nom, ligne d'état), actions du jeu (celle que
// propose la synchro, la fiche de l'image), puis Clients et Manager avec, sur chaque ligne, les actions qui
// la concernent en icônes (pas de menu : la fiche sert à voir d'un coup ce qui peut être fait), et Conflit.

// Bouton qui a lancé l'opération en cours : son icône travaille en boucle jusqu'à la fin (la fiche est
// redessinée pendant l'opération, d'où la clé gardée dans sv.opAct). Après l'image d'arrêt posée par aicon().
function working(button, key) {
  if (!sv.op || sv.opAct !== key) return;
  button.classList.add("is-busy");
  button.setAttribute("aria-busy", "true");
  requestAnimationFrame(() => busyIcons(button, true));
}

const STATE_TEXT = { "à jour": ["À jour", ""], remonter: ["En avance", "accent"], descendre: ["En retard", "accent"],
  conflit: ["Conflit", "warn"] };
const STATUS_TONE = { ok: "", send: "accent", get: "accent", conflict: "warn", none: "idle", absent: "idle", off: "idle" };

export function openSave(game) {
  sv.current = game;
  sheet().open();
  paintSvList();
  paintSaveDetail();
}

export function closeSave() {
  sv.current = null;
  sheet().close();
  markCurrent("#s-rows [data-id], #s-grid [data-id]", null);
}

function paintSaveDetail() {
  if (!sv.current) return;
  const scope = scopeMachine();
  const g = sv.games.find((x) => x.game === sv.current);
  if (!g) { closeSave(); return; }
  if (!sv.op) sv.opAct = null;          // aucune opération : la dernière action retenue est oubliée
  const sync = syncOf(g, scope), copy = copyOf(g, scope);
  const synced = (scope ? stateOf(g, scope.id)?.synced : g.synced_at) || 0;
  const img = imageIds().get(g.game);
  const sep = () => el("span", { className: "sep", textContent: "·" });

  $("svd-cover").replaceChildren(saveThumb(g, img, "cover"));
  $("svd-title").textContent = gameLabel(g);
  $("svd-title").title = gameLabel(g);
  $("svd-status").replaceChildren(
    el("span", { className: `st ${STATUS_TONE[sync.kind] ?? ""}`, textContent: SYNC_LABEL[sync.kind], title: sync.title }),
    ...(copy ? [sep(), el("span", { textContent: size(copy.size) })] : []),
    sep(), el("span", { textContent: synced ? `synchro ${ago(synced)}` : "jamais synchronisé", title: synced ? when(synced) : "" }));

  // Actions : une seule principale (celle que propose la synchro), la fiche de l'image, puis « ⋯ »
  const primary = sync.button ? el("button", { type: "button", className: "btn primary sm", disabled: busy() },
    icon(sync.kind === "conflict" ? "alert" : "sync", "sm"), el("span", { textContent: sync.button })) : null;
  if (primary) primary.onclick = () => { sv.opAct = "primary"; sync.act(); };
  if (primary) working(primary, "primary");
  const imageBtn = img ? el("button", { type: "button", className: "btn secondary sm", title: "Ouvrir la fiche de l'image dans Images" },
    icon("image", "sm"), el("span", { textContent: "Fiche de l'image" })) : null;
  if (imageBtn) imageBtn.onclick = () => openImage(img);
  $("svd-actions").replaceChildren(...[primary, imageBtn].filter(Boolean));
  $("svd-actions").hidden = !primary && !imageBtn;
  $("svd-op").replaceChildren(...(sv.op ? [el("div", { className: "notice info", role: "status" },
    el("span", { className: "spinner" }), el("span", { textContent: sv.op.text }))] : []));

  const row = (label, value, action = null) => el("div", { className: "frow" }, el("span", { className: "k", textContent: label }),
    el("span", { className: "v" }, value instanceof Node ? value : document.createTextNode(String(value))), action || el("span"));
  const card = (title, rows, link = null) => el("div", {},
    el("div", { className: "dsec-row" }, el("p", { className: "dsec", textContent: title }), link),
    el("div", { className: "dcard" }, ...rows));
  const linkBtn = (text, action) => { const b = el("button", { type: "button", className: "linklike", textContent: text }); b.onclick = action; return b; };

  // Actions d'une ligne, en icônes : seulement celles qui ont un sens ici ; pendant une opération, grisées
  const busyReason = busy() ? BUSY : "";
  const conflict = Boolean(g.conflicts?.length || g.machines.some((s) => s.action === "conflit"));
  const act = (iconName, label, fn, danger = false) => {
    const b = el("button", { type: "button", className: `iconbtn bare sm${danger ? " danger" : ""}`, title: label, "aria-label": label }, icon(iconName, "sm"));
    b.onclick = () => { sv.opAct = label; fn(); };
    setUnavailable(b, busyReason);
    working(b, label);
    return b;
  };
  const acts = (...nodes) => el("span", { className: "svd-acts" }, ...nodes.filter(Boolean));
  const reach = reachableIds(), top = sourceOf(g);
  const clientActs = (m, s) => {
    if (!m.ok || s?.state === "injoignable") return null;
    if (!s || s.state === "absente") {
      // Pas de copie : l'installer, depuis le manager s'il l'a, sinon depuis le client le plus avancé
      if (g.reserve) return acts(act("install", `Installer sur ${m.name} (version du manager)`, () => pushManager(g, [m.id])));
      return top ? acts(act("install", `Installer sur ${m.name} (depuis ${top.name})`, () => pushFrom(g, top.id, [m.id]))) : null;
    }
    const others = presentIn(g).filter((o) => o.id !== s.id && o.fingerprint !== s.fingerprint && reach.includes(o.id)).map((o) => o.id);
    return acts(
      !conflict && !inManager(g, s) ? act("save", `Envoyer la copie de ${m.name} au manager`, () => pullGames([g], m.id)) : null,
      !conflict && s.action === "descendre" && g.reserve ? act("install", `Récupérer la version du manager sur ${m.name}`, () => pushManager(g, [m.id])) : null,
      !conflict && others.length ? act("sync", `Copier de ${m.name} vers ${names(others)}`, () => pushFrom(g, m.id, others)) : null,
      act("trash", `Supprimer la sauvegarde de ${m.name}…`, () => openSaveDelete([g], m.id), true));
  };

  // Clients : état de la copie de chacun, taille, dernière partie, dernière synchro, ses actions
  const clientRows = sv.machines.map((m) => {
    const s = stateOf(g, m.id);
    const label = el("span", { className: "svd-client" }, osIcon(m.icon, "sm"), el("span", { textContent: m.name }));
    let stateNode;
    if (!m.ok || s?.state === "injoignable") stateNode = el("span", { className: "svd-state idle", textContent: "Hors ligne" });
    else if (!s || s.state === "absente") stateNode = el("span", { className: "svd-state idle", textContent: "Pas de copie" });
    else {
      const [text, tone] = STATE_TEXT[s.action] || ["Présente", ""];
      stateNode = el("span", { className: `svd-state ${tone}`, textContent: text });
    }
    const facts = s && PRESENT_STATES.has(s.state)
      ? [size(s.size), s.newest ? `partie ${dayText(s.newest)}` : "", s.synced ? `synchro ${ago(s.synced)}` : ""].filter(Boolean).join(" · ") : "";
    const value = el("span", { className: "svd-facts" }, stateNode, facts ? el("span", { className: "muted", textContent: facts }) : null);
    return el("div", { className: "frow" }, el("span", { className: "k" }, label), el("span", { className: "v" }, value), clientActs(m, s) || el("span"));
  });

  // Manager : la copie de référence, d'où elle vient, quand elle a été reçue, versions archivées
  const r = g.reserve;
  const received = r?.fetched_at ? Date.parse(r.fetched_at) / 1000 : 0;
  const managerRows = r ? [
    row("Taille", size(r.size)),
    ...(r.newest ? [row("Dernière partie", dayText(r.newest))] : []),
    ...(received ? [row("Reçue", el("span", { textContent: ago(received), title: when(received) }))] : []),
    ...(r.source ? [row("Venue de", r.source)] : []),
    row("Archives", r.archives ? `${r.archives} version${r.archives > 1 ? "s" : ""} précédente${r.archives > 1 ? "s" : ""}` : "aucune"),
  ] : [row("Copie", el("span", { className: "faint", textContent: "aucune : le manager n'a pas encore reçu ce jeu" }))];

  // Manager : envoyer sa version aux clients joignables qui en ont une autre, supprimer sa copie
  const managerActs = r ? acts(
    !conflict && reachManagerTargets(g).length ? act("install", `Envoyer la version du manager à ${names(reachManagerTargets(g))}`, () => pushReserve(g)) : null,
    r.archives ? act("history", "Restaurer une version précédente…", () => openRestore(g)) : null,
    act("trash", "Supprimer la copie du manager…", () => openSaveDelete([g], "manager"), true)) : null;
  const cards = [card("Clients", clientRows), card("Manager", managerRows, managerActs)];
  if (g.conflicts?.length) {
    cards.push(card("Conflit", g.conflicts.map((v) => row(v.source || machineName(v.machine_id) || "Client",
      [size(v.size), v.newest ? `partie ${dayText(v.newest)}` : ""].filter(Boolean).join(" · "))),
      linkBtn("Départager…", () => openResolve(g))));
  }
  const twin = imageAndFolder(g.game)
    ? [el("p", { className: "dnote", textContent: `Le dossier ${g.game}.pc existe aussi : mêmes sauvegardes, lancé directement.` })] : [];
  $("svd-body").replaceChildren(...twin, ...cards);
}

export function paintStatus() {
  const nodes = [];
  if (sv.op) nodes.push(el("div", { className: "notice info", role: "status" }, el("span", { className: "spinner" }), el("span", { textContent: sv.op.text })));
  if (sv.result) nodes.push(sv.result.error && !sv.result.task ? notice("err", sv.result.error) : syncResultNode(sv.result.task, true));
  $("s-status").replaceChildren(...nodes);
}

// Libellé de la source et des cibles dans la barre
const sourceLabel = () => sv.source === AUTO ? "La plus avancée" : sv.source === RESERVE ? "Manager" : machineName(sv.source);

function targetsLabel(targets) {
  if (!targets.length) return "Aucune cible";
  return targets.length <= 2 ? names(targets) : `${targets.length} clients`;
}

// Raison pour laquelle « Synchroniser » ne peut pas partir (vide : possible)
function syncReason(games, targets) {
  if (busy()) return BUSY;
  if (!targets.length) return "Aucune cible : coche au moins un autre client joignable";
  if (sv.source === RESERVE) return games.some((g) => g.reserve) ? "" : "Aucun jeu sélectionné n'est sur le manager";
  if (sv.source === AUTO) {
    return games.some((g) => suggestedOf(g) && targets.some((id) => lateIn(g).includes(id)))
      ? "" : "Rien à synchroniser : les cibles cochées ont déjà ces versions";
  }
  return games.some((g) => PRESENT_STATES.has(stateOf(g, sv.source)?.state)) ? ""
    : `${machineName(sv.source)} n'a aucun des jeux sélectionnés`;
}

// Menu « Copier depuis » : la proposition par défaut, les clients (hors ligne grisés), le manager
function sourceItems(games) {
  const pick = (value) => () => { sv.source = value; paintSelection(); };
  const choice = (label, iconName, value, reason = "", tag = "") => setUnavailable(menuItem(label, iconName, pick(value),
    { checked: sv.source === value, role: "menuitemradio", tag }), reason);
  const onManager = games.some((g) => g.reserve);
  return [
    el("div", { className: "dd-sub", textContent: "Copier depuis" }),
    choice("La plus avancée (par jeu)", "sparkles", AUTO, "", "recommandé"),
    ...sv.machines.map((m) => choice(m.name, OS_ICONS.includes(m.icon) ? m.icon : "linux", m.id,
      m.ok ? "" : `${m.name} est hors ligne : rien ne peut partir de ce client`)),
    choice("Manager", "server", RESERVE, onManager ? "" : "Aucun jeu sélectionné n'est sur le manager"),
  ];
}

// Menu « vers » : une case par client ; le client source et les clients hors ligne sont grisés
function targetItems() {
  return sv.machines.map((m) => {
    const reason = !m.ok ? `${m.name} est hors ligne : rien ne peut être envoyé avant son retour`
      : m.id === sv.source ? "C'est la source de la copie" : "";
    const box = el("input", { type: "checkbox", checked: !reason && !sv.unchecked.has(m.id), disabled: Boolean(reason) });
    box.onchange = () => { box.checked ? sv.unchecked.delete(m.id) : sv.unchecked.add(m.id); paintSelection(); };
    return el("label", { className: "dd-check", title: reason }, box, el("span", { textContent: m.name }));
  });
}

// Barre de sélection : « Copier depuis [source] → vers [cibles] », Synchroniser, Copier vers le manager,
// Supprimer… ; une action impossible reste visible, grisée, avec sa raison (setUnavailable)
function paintSelection() {
  if (!toggleSelbar($("s-selection"), sv.loaded ? sv.selected.size : 0, $("s-count"), GAMES)) return;
  if (!sv.machines.some((m) => m.id === sv.source) && sv.source !== AUTO && sv.source !== RESERVE) sv.source = AUTO;
  const games = sv.games.filter((g) => sv.selected.has(g.game));
  const targets = pushTargets();
  const others = reachableIds().filter((id) => id !== sv.source);

  const source = $("s-source");
  source.replaceChildren(el("span", { textContent: sourceLabel() }), icon("chevd", "sm"));
  setUnavailable(source, busy() ? BUSY : "");

  const dest = $("s-targets");
  dest.replaceChildren(el("span", { textContent: targetsLabel(targets) }), icon("chevd", "sm"));
  setUnavailable(dest, busy() ? BUSY : others.length ? "" : "Aucun autre client joignable à qui copier");

  setUnavailable($("s-push"), syncReason(games, targets));

  const toPull = sv.source === RESERVE ? [] : games.filter((g) => pullNeeded(g, sv.source));
  setUnavailable($("s-pull"), busy() ? BUSY : sv.source === RESERVE ? "La source est le manager : choisis un client, ou « La plus avancée »"
    : toPull.length ? "" : "Le manager a déjà ces versions");

  setUnavailable($("s-delete"), busy() ? BUSY : "");
}

// Rafraîchissement automatique de l'onglet Sauvegardes : un client qui lance son agent (ou
// l'arrête) change d'état sans recharger la page. Seulement onglet affiché et page visible, sans
// opération en cours, menu ni dialogue ouvert, et jamais deux lectures à la fois.
const SAVES_POLL_MS = 20000;

let savesPolling = false;

async function pollSaves() {
  if (savesPolling || !connected() || document.hidden || document.body.dataset.view !== "saves" || !sv.loaded
    || busy() || menus.open || document.querySelector("dialog[open]")) return;
  savesPolling = true;
  try { await loadSaves(); } finally { savesPolling = false; }
}

export function init() {
  // Menus de la barre : d'où part la copie, et vers quels clients (cases)
  $("s-source").onclick = () => openMenuNode($("s-source-dd"), sourceItems(sv.games.filter((g) => sv.selected.has(g.game))), $("s-source"), { list: true });
  $("s-targets").onclick = () => openMenuNode($("s-targets-dd"), [el("div", { className: "dd-sub", textContent: "Vers" }), ...targetItems()], $("s-targets"), { list: true });
  $("s-search").addEventListener("input", paintSvList);
  $("svd-close").onclick = closeSave;
  sv.platform = storage.get(PLATFORM_KEY) || "";
  paintLayout = layoutToggle($("s-layout"), { key: LAYOUT_KEY, get: () => sv.layout, set: (v) => { sv.layout = v; },
    onChange: paintSvList, list: "s-list", grid: "s-grid" });
  try {
    const stored = JSON.parse(storage.get(SORT_KEY) || "null");
    if (["name", "size", "played", "synced"].includes(stored?.key) && Math.abs(stored.dir) === 1) sv.sort = stored;
  } catch { /* tri mémorisé illisible : tri par nom */ }
  $("s-clear-filter").onclick = () => { sv.todo = ""; paintSvList(); };
  setInterval(pollSaves, SAVES_POLL_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollSaves(); });
  $("s-clear").onclick = () => { sv.selected.clear(); paintSvList(); paintSelection(); };
  $("s-push").onclick = () => pushSelection();
  $("s-pull").onclick = () => pullGames(sv.games.filter((g) => sv.selected.has(g.game) && pullNeeded(g, sv.source)), sv.source);
  $("s-delete").onclick = () => openSaveDelete(sv.games.filter((g) => sv.selected.has(g.game)));
}
