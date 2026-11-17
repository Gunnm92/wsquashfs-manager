// saves.js — Sauvegardes entre clients : état, lecture, liste, bandeau de sélection.

import { api } from "./api.js";
import { paintInitials, putArt, showArt } from "./art.js";
import { $, bigSize, el, emptyState, icon, size, state, storage, when } from "./core.js";
import { notice } from "./detail/autorun.js";
import { openDetail } from "./detail/detail.js";
import { paintClients, showView } from "./header.js";
import { renderLibrary, todoChip } from "./library.js";
import { closeMenu, menus, placeMenu, toggleMenu } from "./menu.js";
import { connected } from "./mode.js";
import { onMachine } from "./saves-machines.js";
import { openSaveDelete, pullGames, pushFrom, pushManager, pushReserve, pushSelection, reachManagerTargets, syncResultNode, syncRow } from "./saves-ops.js";
import { paintSettingsClients } from "./settings.js";

// Onglet « Sauvegardes » (services/saves.py via /api/saves/*) : bandeau des clients,
// tableau jeux × clients, sélection et barre d'actions. Une opération à la fois : la
// tâche « saves » est suivie jusqu'à la fin (followTask), puis l'état est relu.

export const RESERVE = "__reserve__";          // source : le manager (copie des sauvegardes)

export const AUTO = "auto";                    // source : le client suggéré (la plus lourde), jeu par jeu

export const PRESENT_STATES = new Set(["présente", "identique", "plus avancée", "en retard"]);

// Nom affiché d'une ligne : nom lisible d'un dossier déclaré ou d'un jeu Switch, sinon le jeu
export const gameLabel = (g) => g.label || g.game;

// Sections du tableau : une par système Batocera de l'image du jeu (dossier sous roms/), puis Switch,
// Dossiers, et « Autres » (aucune image trouvée). Ordre : Jeux PC, autres systèmes, Switch, Dossiers, Autres.
const SYSTEM_LABELS = { win: "Jeux PC", arcadepc: "Arcade PC", "type-x": "Taito Type X", "type-x2": "Taito Type X²",
  nesicax: "NESiCAxLive", nesicax2: "NESiCAxLive 2", exaarcadia: "exA-Arcadia", adrenaline: "Adrenaline", unis: "UNIS",
  segaalls: "Sega ALLS", segaer: "Sega Europa-R", seganu: "Sega Nu", segare: "Sega RingEdge", segarw: "Sega RingWide",
  konamipc: "Konami PC", rawthrills: "Raw Thrills", globalvr: "Global VR", namcoes3: "Namco ES3", namco2x6: "Namco System 2x6",
  ps3: "PlayStation 3" };

const sectionOf = (g) => g.kind === "switch" ? "switch" : g.kind === "dossier" ? "dossier" : `sys:${g.system || ""}`;

const sectionTitle = (key) => key === "switch" ? "Switch" : key === "dossier" ? "Dossiers" : key === "sys:" ? "Autres (image introuvable)"
  : SYSTEM_LABELS[key.slice(4)] || key.slice(4);

const sectionRank = (key) => key === "sys:win" ? 0 : key === "switch" ? 2 : key === "dossier" ? 3 : key === "sys:" ? 4 : 1;

// Sections repliées (clic sur leur titre), mémorisées dans le navigateur
const savesCollapsed = new Set((() => { try { return JSON.parse(storage.get("wsfs-saves-collapsed") || "[]"); } catch { return []; } })());

export const OS_ICONS = ["linux", "steamos", "batocera"];          // icônes choisissables d'un client

export const OS_LABELS = { linux: "Linux", steamos: "SteamOS", batocera: "Batocera" };

export const sv = {
  machines: [],          // clients joints à leur état (kind, removable, ok, games, message)
  games: [],             // { game, reserve, machines: [état par client] }
  loaded: false, error: "", seq: 0,
  todo: "", selected: new Set(), unchecked: new Set(),   // todo : pastille « À traiter » active ; unchecked : cibles décochées
  source: AUTO,
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

const imageIds = () => new Map(state.items.map((i) => [gameOfItem(i), i.id]));   // jeu → identifiant d'image

export const busy = () => Boolean(sv.op);

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

export const rowTargets = (g) => { const reach = reachableIds(); return lateIn(g).filter((id) => reach.includes(id)); };

const absentIn = (g) => reachableIds().filter((id) => (stateOf(g, id)?.state ?? "absente") === "absente");

// Identique : les clients qui ont le jeu ont la même version. Différences : une au moins est en retard.
const identical = (g) => g.machines.some((s) => s.state === "identique");

const differs = (g) => lateIn(g).length > 0;

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

const shortDate = (t) => {
  if (!t) return "";
  const d = new Date(t * 1000), p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}/${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

// « 246 Mo · 13/11 10:02 » : taille et date d'une version (la date seule n'est pas fiable)
const sizeDate = (bytes, t) => [bytes ? size(bytes) : "", shortDate(t)].filter(Boolean).join(" · ");

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
}

// Vue Sauvegardes : le client choisi en haut de page (state.client), ou « Toutes », filtre la liste.
// Chaque ligne montre la synchro de ce client (ou de l'ensemble) et une seule action.
const SYNC_LABEL = { ok: "À jour", send: "À envoyer", get: "À récupérer", conflict: "Conflit", none: "Non partagée",
  absent: "Absente", off: "Hors ligne" };

const SYNC_ICON = { ok: "check", send: "arrow", get: "arrow", conflict: "alert", none: "info", absent: "info", off: "wifi" };

const CONFLICT_TITLE = "Modifiée sur le client depuis le dernier échange avec le manager : à départager, rien n'est envoyé";

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
function dayText(t) {
  const d = new Date(t * 1000), now = new Date();
  const days = Math.round((new Date(now.getFullYear(), now.getMonth(), now.getDate())
    - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
  if (days === 0) return `aujourd'hui, ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
  if (days === 1) return "hier";
  return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" });
}

// Synchro d'un jeu : kind (ok, send, get, conflict, none, absent, off), et le bouton de la ligne
// (button ; act : action, ou "menu" pour ouvrir le menu « ⋯ »). Un client qui n'a pas le jeu n'est
// jamais servi par défaut : son installation se fait dans le menu de la ligne.
function syncOf(g, scope) {
  const reach = reachableIds();
  if (!scope) {
    if (g.machines.some((s) => s.conflict && reach.includes(s.id))) return { kind: "conflict", title: CONFLICT_TITLE, button: "Choisir…", act: "menu" };
    // Copie la plus récente : celle du manager (les clients en retard la reçoivent), ou celle d'un client
    const here = g.machines.filter((s) => reach.includes(s.id) && PRESENT_STATES.has(s.state));
    if (g.reserve?.newest > Math.max(0, ...here.map((s) => s.newest))) {
      const lagging = here.filter((s) => s.fingerprint && s.fingerprint.toLowerCase() !== (g.reserve.fingerprint || "").toLowerCase()).map((s) => s.id);
      if (lagging.length) return { kind: "get", title: `Le manager a une version plus récente : la copier sur ${names(lagging)}`, button: "Récupérer", act: () => pushManager(g, lagging) };
    }
    const targets = rowTargets(g);
    if (targets.length) return { kind: "send", title: `Envoyer la version la plus avancée à ${names(targets)}`, button: "Envoyer", act: () => syncRow(g) };
    if (pullNeeded(g, AUTO)) return { kind: "send", title: "Copier la version la plus avancée sur le manager", button: "Envoyer", act: () => pullGames([g], AUTO) };
    const places = reach.filter((id) => onMachine(g, id)).length + (g.reserve ? 1 : 0);
    return places <= 1 ? { kind: "none", title: "Une seule copie : aucun autre client ne la partage" }
      : { kind: "ok", title: "Toutes les copies sont à jour" };
  }
  if (!scope.ok) return { kind: "off", title: `${scope.name} est hors ligne : rien ne peut être envoyé avant son retour` };
  const s = stateOf(g, scope.id), st = s?.state ?? "absente";
  if (s?.conflict) return { kind: "conflict", title: CONFLICT_TITLE, button: "Choisir…", act: "menu" };
  if (st === "absente") {
    const holders = reach.filter((id) => id !== scope.id && onMachine(g, id)).length;
    return holders ? { kind: "absent", title: `Pas de sauvegarde de ce jeu sur ${scope.name} : l'installer depuis le menu de la ligne` }
      : { kind: "none", title: `Pas de sauvegarde de ce jeu sur ${scope.name}, ni sur un autre client joignable` };
  }
  if (st === "en retard") {
    const from = suggestedOf(g);
    if (from) return { kind: "get", title: `Copier la version de ${from.name} sur ${scope.name}`, button: "Récupérer", act: () => pushFrom(g, from.id, [scope.id]) };
  }
  if (g.reserve?.fingerprint && s.fingerprint && g.reserve.fingerprint.toLowerCase() !== s.fingerprint.toLowerCase() && g.reserve.newest > s.newest) {
    return { kind: "get", title: `Le manager a une version plus récente : la copier sur ${scope.name}`, button: "Récupérer", act: () => pushManager(g, [scope.id]) };
  }
  const targets = lateIn(g).filter((id) => reach.includes(id));
  if (st === "plus avancée" && targets.length) return { kind: "send", title: `Envoyer à ${names(targets)}`, button: "Envoyer", act: () => pushFrom(g, scope.id, targets) };
  if (pullNeeded(g, scope.id)) return { kind: "send", title: "Copier cette version vers le manager", button: "Envoyer", act: () => pullGames([g], scope.id) };
  return { kind: "ok", title: `${scope.name} a la version du manager` };
}

// Lignes du client choisi (ou de toutes) : jeux avec une copie sur un client joignable ou sur le manager.
// Hors ligne, seule la copie du manager est connue (dernière synchro).
function svBase(scope) {
  const reach = reachableIds(), offline = Boolean(scope && !scope.ok);
  return sv.games.filter((g) => (offline ? Boolean(g.reserve) : Boolean(g.reserve) || reach.some((id) => onMachine(g, id))))
    .map((g) => ({ g, sync: syncOf(g, scope), copy: copyOf(g, scope) }));
}

// Pastille « À traiter » active et recherche
function svVisible(base) {
  const q = $("s-search").value.trim().toLowerCase();
  return base.filter((r) => (!q || r.g.game.toLowerCase().includes(q) || gameLabel(r.g).toLowerCase().includes(q))
    && (sv.todo !== "conflict" || r.sync.kind === "conflict")
    && (sv.todo !== "sync" || r.sync.kind === "send" || r.sync.kind === "get"))
    .sort((a, b) => gameLabel(a.g).localeCompare(gameLabel(b.g), "fr", { numeric: true, sensitivity: "base" }));
}

let svSig = null;           // client, pastille et recherche : les lignes entrent en cascade

function paintSvList() {
  const scope = scopeMachine(), base = svBase(scope), visible = svVisible(base);
  const total = base.reduce((n, r) => n + (r.copy?.size || 0), 0);
  $("sv-count").textContent = `${base.length} sauvegarde${base.length > 1 ? "s" : ""}`;
  // Hors ligne, les tailles viennent de la copie du manager (dernière synchro), pas du client
  const sizeWhere = !scope ? "sur tous les clients" : scope.ok ? `sur ${scope.name}` : "à la dernière synchro";
  $("sv-size").textContent = base.length ? `${bigSize(total, 1)} ${sizeWhere}` : "";
  const off = Boolean(scope && !scope.ok);
  $("sv-offline").hidden = !off;
  $("sv-offline").replaceChildren(...(off ? [icon("wifi"), el("span", { textContent: `${scope.name} est hors ligne : sauvegardes connues à sa dernière synchro. Rien ne peut être envoyé avant son retour.` })] : []));

  const conflicts = base.filter((r) => r.sync.kind === "conflict").length;
  const syncs = base.filter((r) => r.sync.kind === "send" || r.sync.kind === "get").length;
  const toggle = (key) => () => { sv.todo = sv.todo === key ? "" : key; paintSvList(); };
  const chips = [];
  if (conflicts) chips.push(todoChip(conflicts, conflicts > 1 ? "conflits" : "conflit", "alert", "warn", sv.todo === "conflict", toggle("conflict")));
  if (syncs) chips.push(todoChip(syncs, "à synchroniser", "refresh", "accent", sv.todo === "sync", toggle("sync")));
  $("sv-todo").replaceChildren(...chips);
  $("sv-todo-label").hidden = !chips.length;
  const todoName = { conflict: "Conflits", sync: "À synchroniser" }[sv.todo] || "";
  $("s-clear-filter").hidden = !todoName;
  $("s-clear-filter-label").textContent = todoName;
  $("s-visible").textContent = `${visible.length} sur ${base.length} · ${scope ? scope.name : "Toutes"}`;

  const sig = [scope?.id ?? "all", sv.todo, $("s-search").value.trim()].join("|");
  const enter = sig !== svSig;
  svSig = sig;
  // Sections (Jeux PC, Switch, Dossiers…) : groupes repliables dans le même tableau
  const sections = [...new Set(visible.map((r) => sectionOf(r.g)))]
    .sort((a, b) => sectionRank(a) - sectionRank(b) || sectionTitle(a).localeCompare(sectionTitle(b), "fr"));
  const multi = sections.length > 1;
  const folded = (key) => multi && savesCollapsed.has(key);
  const shown = visible.filter((r) => !folded(sectionOf(r.g)));
  const all = el("input", { type: "checkbox", checked: shown.length > 0 && shown.every((r) => sv.selected.has(r.g.game)),
    title: "Tout sélectionner (filtre actuel)" });
  all.onchange = () => { shown.forEach((r) => (all.checked ? sv.selected.add(r.g.game) : sv.selected.delete(r.g.game))); paintSvList(); paintSelection(); };
  $("s-thead").replaceChildren(all, el("span", { textContent: "Jeu ↑" }), el("span", { className: "c-size r", textContent: "Taille" }),
    el("span", { className: "c-when", textContent: "Dernière partie" }), el("span", { textContent: "Synchro" }), el("span"));

  const groupRow = (key, count) => {
    const button = el("button", { type: "button", className: `sv-group-btn${folded(key) ? " folded" : ""}`, "aria-expanded": String(!folded(key)),
      title: folded(key) ? "Afficher cette section" : "Replier cette section" },
      icon("chevd", "sm"), el("span", { textContent: sectionTitle(key) }), el("span", { className: "count", textContent: count }));
    button.onclick = () => {
      savesCollapsed.has(key) ? savesCollapsed.delete(key) : savesCollapsed.add(key);
      storage.set("wsfs-saves-collapsed", JSON.stringify([...savesCollapsed]));
      paintSvList();
    };
    return el("div", { className: "sv-group" }, button);
  };
  const rows = sections.flatMap((key) => {
    const list = visible.filter((r) => sectionOf(r.g) === key);
    return [...(multi ? [groupRow(key, list.length)] : []), ...(folded(key) ? [] : list.map((r) => svListRow(r, enter)))];
  });
  if (!visible.length) rows.push(el("div", { className: "svempty" }, base.length
    ? emptyState("search", "Aucun jeu pour ces filtres", "Élargir la recherche ou retirer le filtre.")
    : emptyState("save", "Aucune sauvegarde", "Les clients associés n'ont aucune sauvegarde de partie.")));
  $("s-rows").replaceChildren(...rows);
}

// Une ligne : case, vignette et jeu, taille, dernière partie, synchro et son bouton, menu « ⋯ »
function svListRow({ g, sync, copy }, enter) {
  const scope = scopeMachine();
  const selected = sv.selected.has(g.game);
  const box = el("input", { type: "checkbox", checked: selected, title: "Sélectionner" });
  box.onclick = (event) => event.stopPropagation();
  const more = el("button", { type: "button", className: "iconbtn bare sm", title: "Autres actions", "aria-label": `Autres actions pour ${gameLabel(g)}`, "aria-haspopup": "menu" },
    icon("more", "sm"));
  const wrap = el("div", { className: "dd end sv-more" }, more);
  more.onclick = (event) => { event.stopPropagation(); toggleMenu(wrap, () => rowItems(g)); };
  const primary = sync.button ? el("button", { type: "button", className: "btn secondary sm sv-act", textContent: sync.button, disabled: busy() }) : null;
  if (primary) {
    primary.onclick = (event) => {
      event.stopPropagation();
      if (sync.act === "menu") more.click(); else sync.act();
    };
  }
  const whenText = copy ? [dayText(copy.t), copy.where && copy.where !== scope?.name ? `sur ${copy.where}` : ""].filter(Boolean).join(", ") : "";
  const row = el("div", { className: `grow item${selected ? " selected" : ""}${enter ? " enter" : ""}` },
    box,
    el("div", { className: "iname" }, saveThumb(g, imageIds().get(g.game)),
      el("div", { className: "t" }, el("span", { className: "n", textContent: gameLabel(g), title: gameLabel(g) }))),
    el("span", { className: "gsize c-size", textContent: copy ? size(copy.size) : "—" }),
    el("span", { className: "sv-when c-when", textContent: whenText || "—", title: copy ? when(copy.t) : "" }),
    el("div", { className: `sync ${sync.kind}`, title: sync.title + (g.check ? " · la plus lourde n'est pas la plus récente : à vérifier" : "") },
      el("span", { className: "sync-lbl" }, icon(SYNC_ICON[sync.kind], "sm"), el("span", { textContent: SYNC_LABEL[sync.kind] })),
      primary),
    wrap);
  row.dataset.id = g.game;
  row.onclick = (event) => {
    if (event.target.closest("button, input")) return;
    box.checked = !box.checked;
    box.onchange();
  };
  box.onchange = () => {
    box.checked ? sv.selected.add(g.game) : sv.selected.delete(g.game);
    row.classList.toggle("selected", box.checked);
    paintSelection();
  };
  return row;
}

export function paintSavesView() {
  paintClients();
  const none = sv.loaded && sv.machines.length === 0;
  const diff = sv.games.filter(differs).length;
  $("count-saves").textContent = diff || "";
  $("count-saves").title = diff ? `${diff} jeu(x) dont les versions diffèrent entre clients` : "";
  $("s-loading").hidden = sv.loaded || Boolean(sv.error);
  $("s-error").replaceChildren(...(sv.error ? [notice("err", sv.error)] : []));
  $("s-empty").hidden = !none;
  $("s-body").hidden = !sv.loaded || none;
  $("sv-head").hidden = $("s-body").hidden;
  if (!$("s-body").hidden) {
    paintSvList();
    paintStatus();
  }
  paintSelection();
}

// Vignette d'une ligne : illustration de l'image (jeu PC de la bibliothèque, comme la vue Images),
// jaquette du gamelist.xml de Batocera (jeu Switch, /api/saves/art), sinon initiales.
const svArt = { cache: new Map(), done: new Map() };

function saveThumb(g, imgId) {
  const node = el("span", { className: "thumb sv-thumb", "aria-hidden": "true" });
  const item = imgId ? state.byId.get(imgId) : null;
  if (item) {
    node.dataset.id = item.id;          // showArt vérifie que la vignette est toujours celle de l'image
    showArt(node, item);
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

// Menu « … » d'une ligne
function rowItems(g) {
  const idle = !busy();
  const reach = reachableIds(), top = sourceOf(g);
  // Depuis un client qui a le jeu vers les autres qui l'ont dans une autre version
  const others = (s) => presentIn(g).filter((o) => o.id !== s.id && o.fingerprint !== s.fingerprint && reach.includes(o.id)).map((o) => o.id);
  const img = imageIds().get(g.game);
  return [
    ...(img ? [["Ouvrir la fiche de l'image", "image", false, () => { showView("images"); openDetail(img); }]] : []),
    ...presentIn(g).filter((s) => reach.includes(s.id) && others(s).length)
      .map((s) => [`Sync ${s.name} → ${names(others(s))}`, "arrow", !idle, () => pushFrom(g, s.id, others(s))]),
    // Installer sur un client qui n'a pas le jeu : choix explicite, jamais par défaut
    ...(top ? absentIn(g).map((id) => [`Installer sur ${machineName(id)} (depuis ${top.name})`, "arrow", !idle, () => pushFrom(g, top.id, [id])]) : []),
    [!top ? "Copier vers le manager" : inManager(g, top) ? "Copier vers le manager (déjà identique)"
      : `Copier vers le manager (${top.name} → Manager)`, "save", !idle || !top || inManager(g, top), () => pullGames([g], AUTO)],
    ["Sync depuis le manager (Manager → clients)", "server", !idle || !g.reserve || !reachManagerTargets(g).length, () => pushReserve(g)],
    ["Supprimer…", "trash", !idle, () => openSaveDelete([g]), true],
  ];
}

export function paintStatus() {
  const nodes = [];
  if (sv.op) nodes.push(el("div", { className: "notice info", role: "status" }, el("span", { className: "spinner" }), el("span", { textContent: sv.op.text })));
  if (sv.result) nodes.push(sv.result.error && !sv.result.task ? notice("err", sv.result.error) : syncResultNode(sv.result.task, true));
  $("s-status").replaceChildren(...nodes);
}

// Barre de sélection : source → cibles (cases dans un menu), Sync, Copier vers le manager
function paintSelection() {
  const bar = $("s-selection"), n = sv.selected.size;
  bar.hidden = n === 0 || !sv.loaded;
  if (bar.hidden) return;
  $("s-count").textContent = n > 1 ? `${n} jeux` : "1 jeu";
  const options = [[AUTO, "La plus avancée (par jeu)"], ...sv.machines.filter((m) => m.ok).map((m) => [m.id, m.name]),
    [RESERVE, "Manager"]];
  if (!options.some(([value]) => value === sv.source)) sv.source = AUTO;
  $("s-source").replaceChildren(...options.map(([value, label]) => new Option(label, value)));
  $("s-source").value = sv.source;
  const games = sv.games.filter((g) => sv.selected.has(g.game));
  const targets = pushTargets();
  $("s-targets").textContent = `Cibles · ${targets.length}`;
  $("s-targets").disabled = busy();
  const pushable = !busy() && games.length && targets.length && (sv.source !== RESERVE || games.some((g) => g.reserve));
  $("s-push").disabled = !pushable;
  $("s-push").title = targets.length ? "" : "Aucune cible : coche au moins un autre client joignable";
  const toPull = sv.source === RESERVE ? [] : games.filter((g) => pullNeeded(g, sv.source));
  $("s-pull").disabled = busy() || !toPull.length;
  $("s-pull").title = sv.source === RESERVE ? "Choisis un client, ou « La plus avancée », comme source"
    : games.length && !toPull.length ? "Le manager a déjà ces versions" : "Copier les sauvegardes sélectionnées sur le manager";
  $("s-delete").disabled = busy();
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
  // Cibles : cases par client joignable (la source choisie est grisée)
  $("s-targets").onclick = () => {
    const wrap = $("s-targets-dd"), open = menus.open?.parentElement === wrap;
    closeMenu();
    if (open) return;
    const boxes = sv.machines.filter((m) => m.ok).map((m) => {
      const box = el("input", { type: "checkbox", checked: !sv.unchecked.has(m.id) && m.id !== sv.source, disabled: m.id === sv.source });
      box.onchange = () => { box.checked ? sv.unchecked.delete(m.id) : sv.unchecked.add(m.id); paintSelection(); };
      return el("label", { className: "dd-check" }, box, el("span", { textContent: m.name }));
    });
    menus.open = el("div", { className: "dd-menu", role: "group" }, ...(boxes.length ? boxes : [el("span", { className: "muted dd-check", textContent: "Aucun client joignable" })]));
    placeMenu(menus.open, wrap);
  };
  $("s-source").onchange = () => { sv.source = $("s-source").value; paintSelection(); };
  $("s-search").addEventListener("input", paintSvList);
  $("s-clear-filter").onclick = () => { sv.todo = ""; paintSvList(); };
  setInterval(pollSaves, SAVES_POLL_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollSaves(); });
  $("s-clear").onclick = () => { sv.selected.clear(); paintSvList(); paintSelection(); };
  $("s-push").onclick = () => pushSelection();
  $("s-pull").onclick = () => pullGames(sv.games.filter((g) => sv.selected.has(g.game) && pullNeeded(g, sv.source)), sv.source);
  $("s-delete").onclick = () => openSaveDelete(sv.games.filter((g) => sv.selected.has(g.game)));
}
