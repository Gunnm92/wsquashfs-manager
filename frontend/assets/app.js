"use strict";
// wsquashfs-manager — interface (aucune étape de build : chargé tel quel par index.html).
// L'aide est un fragment séparé (assets/help.html), chargé à sa première ouverture.

// ------------------------------------------------------------------ état, outils

const state = {
  items: [], byId: new Map(), selected: new Set(), current: null,
  sort: { key: "name", dir: 1 }, keys: [], tasks: [],
  filter: { system: "", type: "", runner: "", pad: "", extra: "", special: "" },
  layout: "list",
  autorun: null,          // dernière lecture de l'autorun de la fiche
  pendingKeys: new Set(), // clés ajoutées au formulaire, pas encore renseignées
};
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
};
const storage = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* navigation privée */ } },
};
state.layout = storage.get("wsfs-layout") === "grid" ? "grid" : "list";

const icon = (name, cls = "") => {
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
const badge = (text, kind = "") => el("span", { className: `chip plain ${TONES[kind] || "t-neutral"}`, textContent: text });
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
const withTip = (node, title, lines, focusable = true) => {
  node.dataset.tipTitle = title;
  node.dataset.tip = lines.join("\n");
  if (focusable) node.tabIndex = 0;
  node.setAttribute("aria-label", lines.length ? `${title} : ${lines.join(", ")}` : title);
  return node;
};
const dotIc = (iconName, tone, title, lines = [], focusable = false) =>
  withTip(el("span", { className: `dot-ic ${tone}` }, icon(iconName)), title, lines, focusable);
// Lien vers une section (ou la carte d'une clé) de l'aide
const helpLink = (anchor, child = "aide", title = "Voir dans l'aide") =>
  el("a", { href: `#${anchor}`, title, onclick: (event) => { event.preventDefault(); event.stopPropagation(); openHelp(anchor); } }, child);
const TYPE_HELP = { wine_batocera: "h-batocera", wine_other: "h-batocera", wine_proton: "h-proton", game_only: "h-solo" };

const size = (n) => n >= 1e9 ? `${(n / 1e9).toFixed(1).replace(".", ",")} Go` : n >= 1e6 ? `${(n / 1e6).toFixed(0)} Mo`
  : n >= 1e3 ? `${(n / 1e3).toFixed(0)} Ko` : `${n} o`;
const when = (t) => t ? new Date(t * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : "";
const cap = (s) => s ? s[0].toUpperCase() + s.slice(1) : s;

let toastTimer = null;
function toast(message, iconName = "check", error = false) {
  const node = $("toast");
  node.replaceChildren(icon(iconName), el("span", { textContent: message }));
  node.classList.toggle("err", error);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, error ? 6000 : 3500);
}

// Hauteur de l'en-tête : la fiche et l'aide se calent dessous
new ResizeObserver(([entry]) => document.documentElement.style.setProperty("--top-h",
  `${Math.round(entry.target.getBoundingClientRect().height)}px`)).observe(document.querySelector("header.top"));

// ------------------------------------------------------------------ illustrations
// Jaquette d'ES, sinon icône de l'exécutable : chargée à la demande, 4 à la
// fois, gardée pour la session. Repli : initiales sur un dégradé.

const art = { cache: new Map(), done: new Map(), waiting: [], active: 0 };
const hueOf = (name) => [...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
const initials = (name) => name.split(/[\s\-:]+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
function paintInitials(node, name) {
  const hue = hueOf(name);
  node.textContent = initials(name);
  node.style.background = `linear-gradient(145deg, oklch(0.56 0.12 ${hue}), oklch(0.3 0.07 ${(hue + 40) % 360}))`;
  node.classList.remove("icon", "es");
}
// Version de l'illustration : change avec l'image et avec l'exécutable de
// CMD (adresse différente, donc jamais l'ancienne servie par le cache HTTP)
const artVersion = (item) => `${item.mtime}|${item.dir || ""}|${item.cmd || ""}`;
function loadArt(item) {
  const key = `${item.id}:${artVersion(item)}`;
  if (!art.cache.has(key)) {
    art.cache.set(key, new Promise((resolve) => { art.waiting.push({ item, resolve }); pumpArt(); })
      .then((result) => { art.done.set(key, result); return result; }));
  }
  return art.cache.get(key);
}
function pumpArt() {
  while (art.active < 4 && art.waiting.length) {
    const { item, resolve } = art.waiting.shift();
    art.active++;
    fetch(`/api/image/art?id=${encodeURIComponent(item.id)}&v=${encodeURIComponent(artVersion(item))}`, { headers: { "x-wsfs-token": storage.get("wsfs-token") || "" } })
      .then(async (r) => r.ok ? { url: URL.createObjectURL(await r.blob()), kind: r.headers.get("X-Art-Kind") || "icon" } : null)
      .catch(() => null)
      .then((result) => { art.active--; resolve(result); pumpArt(); });
  }
}
function putArt(node, a, animate) {
  node.textContent = "";
  node.style.background = "";
  node.classList.add(a.kind);
  node.append(el("img", { src: a.url, alt: "", className: animate ? "fade-in" : "" }));
}
// Vrai si l'illustration était déjà connue (posée sans attendre)
function artReady(node, item) {
  const key = `${item.id}:${artVersion(item)}`;
  if (!art.done.has(key)) return false;
  const a = art.done.get(key);
  if (a) putArt(node, a, false); else paintInitials(node, item.name);
  return true;
}
async function showArt(node, item) {
  if (artReady(node, item)) return;
  paintInitials(node, item.name);
  const a = await loadArt(item);
  if (!a || node.dataset.id !== item.id) return;
  putArt(node, a, true);
}
const artObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    artObserver.unobserve(entry.target);
    const item = state.byId.get(entry.target.dataset.id);
    if (item) showArt(entry.target, item);
  }
}, { rootMargin: "200px" });
function thumb(item, className = "thumb") {
  const node = el("span", { className });
  node.dataset.id = item.id;
  if (!artReady(node, item)) {
    paintInitials(node, item.name);
    artObserver.observe(node);
  }
  return node;
}

const emptyState = (iconName, title, hint) => el("div", { className: "empty-state" }, icon(iconName),
  el("strong", { textContent: title }), hint ? el("span", { textContent: hint }) : null);

// ------------------------------------------------------------------ API

let loginWaiters = null;

async function api(path, { method = "GET", body, timeout = 120000 } = {}) {
  const headers = { "x-wsfs-token": storage.get("wsfs-token") || "" };
  if (body !== undefined) headers["content-type"] = "application/json";
  // Délai maximal : une requête perdue en route (proxy, disque saturé par
  // un empaquetage) ne doit pas laisser la page en attente sans fin
  let response;
  try {
    response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeout) });
  } catch (error) {
    throw new Error(error.name === "TimeoutError" ? `le serveur n'a pas répondu en ${Math.round(timeout / 60000)} min (réessayer)` : `connexion impossible : ${error.message}`);
  }
  if (response.status === 401 && path !== "/login") {
    await askLogin();
    return api(path, { method, body, timeout });
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 404 && data.detail === "Not Found") {
    // Route inconnue (pas une image inconnue) : le serveur sert une ancienne
    // version de l'API alors que la page, elle, est à jour.
    throw new Error("API introuvable : le serveur tourne sur une ancienne version, le redémarrer.");
  }
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(" ; ") : data.detail;
    throw new Error(detail || `erreur ${response.status}`);
  }
  return data;
}

function askLogin() {
  if (!loginWaiters) {
    loginWaiters = [];
    $("login-err").hidden = true;
    $("dlg-login").showModal();
  }
  return new Promise((resolve) => loginWaiters.push(resolve));
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const { token } = await api("/login", { method: "POST", body: { password: $("login-pw").value } });
    storage.set("wsfs-token", token);
    $("dlg-login").close();
    const waiters = loginWaiters || [];
    loginWaiters = null;
    waiters.forEach((resolve) => resolve());
  } catch (error) {
    $("login-err").textContent = error.message;
    $("login-err").hidden = false;
  }
});
$("dlg-login").addEventListener("cancel", (event) => event.preventDefault());

// ------------------------------------------------------------------ bibliothèque

const TYPES = { wine_batocera: "Prefix Wine (Batocera)", wine_proton: "Prefix Proton", wine_other: "Autre prefix Wine",
  game_only: "Jeu seul", unknown: "Type inconnu" };
const TYPE_LOOK = { wine_batocera: ["wine", "t-accent"], wine_other: ["wine", "t-neutral"], wine_proton: ["atom", "t-sage"],
  game_only: ["app", "t-neutral"], unknown: ["alert", "t-err"] };
const STATES = { ok: ["Prêt", "Image saine"], in_use: ["En cours", "Jeu lancé : aucune reconstruction possible"],
  modified: ["Tâche", "Une tâche est en file pour cette image"], has_old: [".old", "Ancienne version conservée : à valider"] };
// Filtres (menus déroulants à côté de la recherche) : chaque choix montre la
// même icône, de la même couleur, que dans la colonne « Type et réglages »
const FILTERS = [
  { key: "system", label: "Système", all: "Tous les systèmes", options: () => [...new Set(state.items.map((i) => i.system).filter(Boolean))].sort().map((v) => [v, null, "", cap(v)]) },
  { key: "type", label: "Type", all: "Tous les types", options: () => [["wine", "wine", "t-accent", "Prefix Wine"], ["wine_proton", "atom", "t-sage", "Prefix Proton"], ["game_only", "app", "t-neutral", "Jeu seul"]] },
  { key: "runner", label: "Runner", all: "Tous les runners", options: () => [["default", null, "", "Par défaut"], ["custom", "cpu", "t-accent", "Personnalisé"], ["joined", "clip", "t-accent", "Joint à l'image"]] },
  { key: "pad", label: "Manettes", all: "Toutes les manettes", options: () => [["hidraw", "usb", "t-sage", "HIDRAW"], ["xinput", "gamepad", "t-off", "XInput"]] },
  { key: "extra", label: "Extras", all: "Tous", options: () => [["keys", "keyboard", "t-neutral", "Touches clavier"], ["fakeping", "wifi", "t-neutral", "Relais fakeping"]] },
  { key: "special", label: "État", all: "Tous les états", options: () => [["noversion", "tag", "t-accent", "Sans version"], ["old", "history", "t-accent", "Ancienne image (.old)"],
    ["inuse", "play", "t-accent", "En cours"], ["noautorun", "terminal", "t-accent", "Sans autorun"], ["override", "filepen", "t-accent", "Surcharge à inscrire"]] },
];
const SORTS = { name: (i) => i.name, size: (i) => i.size, saves_size: (i) => i.saves_present ? i.saves_size : -1, state: (i) => i.state };

// Colonne et filtre : « défaut » ou « personnalisé » ; le détail est dans l'info-bulle
const runnerLabel = (i) => i.runner === "default" ? "défaut" : "personnalisé";
function runnerDetails(i) {
  const joined = i.joined_runners || [];
  const used = i.proton || i.wine;
  const lines = [];
  if (i.runner === "custom") lines.push(`RUNNER=${i.runner_path || "?"}`, "binaire wine de la machine");
  else if (i.runner === "default") lines.push("choisi par le lanceur selon le type d'image");
  else {
    lines.push(`${i.runner === "proton" ? "PROTON" : "WINE"}=${used}`);
    lines.push(i.runner_joined ? `joint à l'image (runners/${used})` : "installé sur la machine");
  }
  const unused = joined.filter((n) => !(i.runner_joined && n.toLowerCase() === (used || "").toLowerCase()));
  if (unused.length) lines.push(`runners/ : ${unused.join(", ")} (non utilisé)`);
  return lines;
}

// Pastilles de la colonne Profil : type, runner, manettes, extras, surcharge
function profileBadges(i) {
  const [typeIcon, typeTone] = TYPE_LOOK[i.type] || TYPE_LOOK.unknown;
  const out = [dotIc(typeIcon, typeTone, TYPES[i.type] || i.type)];
  if (i.runner !== "default") out.push(dotIc(i.runner_joined ? "clip" : "cpu", "t-accent", "Runner personnalisé", runnerDetails(i)));
  out.push(i.hidraw ? dotIc("usb", "t-sage", "Manettes : HIDRAW") : dotIc("gamepad", "t-off", "Manettes : XInput"));
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

// Lecture longue de la bibliothèque (nouvelles images, scan complet) :
// bandeau au-dessus de la liste, avec le temps écoulé
const SCAN_TIMEOUT = 60 * 60 * 1000;     // une nouvelle image est ouverte une fois : plusieurs minutes possibles
let busyTimer = null, busyDepth = 0;
function libBusy(text, hint) {
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
  $("btn-refresh").classList.add("spin");
  return () => {
    if (--busyDepth > 0) return;
    clearInterval(busyTimer);
    box.hidden = true;
    $("btn-refresh").classList.remove("spin");
  };
}

async function loadLibrary(refresh = false) {
  // Bandeau seulement si la lecture dure : un chargement depuis le cache ne clignote pas
  let done = null;
  const slow = setTimeout(() => { done = libBusy(refresh ? "Lecture de toutes les images" : "Chargement de la bibliothèque",
    "Les images ajoutées ou modifiées sont ouvertes une fois ; les autres viennent du cache."); }, 600);
  try {
    state.items = await api(`/api/library${refresh ? "?refresh=true" : ""}`, { timeout: SCAN_TIMEOUT });
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
  $("count-images").textContent = state.items.length;
  renderLibrary();
  loadRecovery();
  if (state.current && state.byId.has(state.current.id)) {
    state.current = state.byId.get(state.current.id);
    renderDetailHeader();
  }
}

let openMenu = null;
function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
document.addEventListener("click", (event) => { if (openMenu && !event.target.closest(".dd")) closeMenu(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeMenu(); });

function renderFilters() {
  const f = state.filter;
  closeMenu();
  $("filters").replaceChildren(...FILTERS.map((def) => {
    // Seuls les choix utiles : au moins un jeu, mais pas tous (le choix en cours reste affiché)
    const total = state.items.length;
    const options = def.options().map((o) => [...o, state.items.filter((i) => matches(i, def.key, o[0])).length])
      .filter((o) => o[0] === f[def.key] || (o[4] > 0 && o[4] < total));
    if (!options.length) { f[def.key] = ""; return null; }
    const current = options.find(([v]) => v === f[def.key]);
    if (!current) f[def.key] = "";
    const button = el("button", { type: "button", className: "dd-btn", title: def.label },
      current?.[1] ? el("span", { className: `dot-ic ${current[2]}` }, icon(current[1])) : null,
      el("span", { textContent: current ? current[3] : def.label }), icon("chevd", "xs"));
    button.setAttribute("aria-haspopup", "listbox");
    const wrap = el("div", { className: `dd${current ? " active" : ""}` }, button);
    button.onclick = () => {
      const wasOpen = openMenu?.parentElement === wrap;
      closeMenu();
      if (wasOpen) return;
      const choose = (value) => { f[def.key] = value; renderLibrary(); };
      const item = (value, iconName, tone, label, n) => {
        const b = el("button", { type: "button", className: value === f[def.key] ? "on" : "" },
          iconName ? el("span", { className: `dot-ic ${tone}` }, icon(iconName)) : el("span", { className: "dot-ic t-off" }),
          el("span", { textContent: label }), el("span", { className: "n", textContent: n ?? "" }),
          value === f[def.key] ? icon("check", "sm") : el("span", { className: "nocheck" }));
        b.onclick = () => choose(value);
        return b;
      };
      openMenu = el("div", { className: "dd-menu", role: "listbox" }, item("", null, "", def.all, total), ...options.map((o) => item(...o)));
      wrap.append(openMenu);
      openMenu.querySelector("button.on, button")?.focus();
    };
    return wrap;
  }).filter(Boolean));
  for (const b of $("layout").children) b.classList.toggle("on", b.dataset.layout === state.layout);
}

// Une image correspond-elle à la valeur d'un filtre ?
function matches(i, key, value) {
  if (!value) return true;
  switch (key) {
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

function filtered() {
  const q = $("f-search").value.trim().toLowerCase();
  return state.items.filter((i) =>
    (!q || [i.name, i.cmd, i.dir, i.version].some((v) => v && v.toLowerCase().includes(q)))
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

function renderLibrary() {
  renderFilters();
  const visible = sorted(filtered());
  $("visible-count").textContent = visible.length === state.items.length ? `${visible.length}` : `${visible.length} / ${state.items.length}`;
  const grid = state.layout === "grid";
  $("list").hidden = grid;
  $("grid").hidden = !grid;

  const empty = state.items.length
    ? emptyState("search", "Aucune image pour ces filtres", "Élargir la recherche ou retirer un filtre.")
    : emptyState("layers", "Aucune image", "Choisir les systèmes à gérer dans « Réglages ».");
  if (grid) {
    $("grid").replaceChildren(...visible.map((i) => {
      const card = el("div", { className: "gcard" },
        el("div", { className: "gart" }, thumb(i, "gcover"), selectBox(i), stateBadge(i), profileBadges(i)),
        el("div", { className: "meta" }, el("div", { className: "n", textContent: i.name, title: i.name }),
          el("div", { className: "sub" }, versionLabel(i), el("span", { textContent: size(i.size) }))));
      card.dataset.id = i.id;
      card.onclick = () => openDetail(i.id);
      return card;
    }));
    if (!visible.length) $("grid").append(el("div", { className: "fcard-empty" }, empty));
    $("rows").replaceChildren();
  } else {
    const allSelected = visible.length > 0 && visible.every((i) => state.selected.has(i.id));
    const all = el("input", { type: "checkbox", checked: allSelected, title: "Tout sélectionner (filtre actuel)" });
    all.onchange = () => { visible.forEach((i) => all.checked ? state.selected.add(i.id) : state.selected.delete(i.id)); renderLibrary(); };
    const sortBtn = (key, label, cls = "") => {
      const arrow = state.sort.key === key ? (state.sort.dir > 0 ? " ↑" : " ↓") : "";
      const b = el("button", { className: cls, textContent: label + arrow });
      b.onclick = () => { state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : 1 }; renderLibrary(); };
      return b;
    };
    $("thead").replaceChildren(all, sortBtn("name", "Jeu"), el("span", { className: "c-profile", textContent: "Type et réglages" }),
      sortBtn("size", "Taille", "r"), sortBtn("saves_size", "Sauveg.", "r c-saves"), sortBtn("state", "État", "c-state"));
    $("rows").replaceChildren(...visible.map((i) => {
      const row = el("div", { className: "grow item" },
        selectBox(i),
        el("div", { className: "gname" }, thumb(i), el("div", { className: "t" },
          el("span", { className: "n", textContent: i.name, title: i.name }),
          el("span", { className: "sub" }, versionLabel(i), el("span", { textContent: "·" }), el("span", { textContent: cap(i.system) || "—" })))),
        profileBadges(i),
        el("span", { className: "gsize", textContent: size(i.size) }),
        el("span", { className: `gsize c-saves${i.saves_present ? "" : " none"}`, textContent: i.saves_present ? size(i.saves_size) : "—" }),
        stateBadge(i));
      row.dataset.id = i.id;
      row.onclick = () => openDetail(i.id);
      return row;
    }));
    if (!visible.length) $("rows").append(empty);
    $("grid").replaceChildren();
  }
  markCurrent();
  renderStats();
  renderSelection();
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

// Chiffres clés de la bibliothèque ; un clic filtre (deuxième clic : retire le filtre)
function renderStats() {
  const items = state.items, special = state.filter.special;
  const total = items.reduce((n, i) => n + (i.size || 0), 0);
  const count = (f) => items.filter(f).length;
  const tiles = [
    ["", "Images", items.length, size(total), "layers", "t-sage", "Toute la bibliothèque"],
    ["noversion", "Sans version", count((i) => !i.version), null, "tag", "t-accent", "GAME_VERSION non renseignée"],
    ["old", "Ancienne image (.old)", count((i) => i.has_old), null, "history", "t-accent", "À valider ou à restaurer"],
    ["override", "Surcharge à inscrire", count((i) => (i.override || []).length), null, "filepen", "t-accent", "Réglage appliqué hors de l'image"],
    ["inuse", "En cours", count((i) => i.state === "in_use"), null, "play", "t-accent", "Jeux lancés"],
    ["noautorun", "Sans autorun", count((i) => !i.cmd), null, "terminal", "t-accent", "Aucune commande CMD dans l'image"],
  ];
  $("stats").replaceChildren(...tiles.map(([filter, label, n, extra, iconName, tone, tip]) => {
    const tile = el("button", { type: "button", title: tip,
      className: `stat${special === filter && filter ? " active" : ""}${n || !filter ? "" : " zero"}` },
      el("span", { className: `disc ${tone}` }, icon(iconName)),
      el("span", { className: "txt" }, el("span", { className: "n", textContent: n }, extra ? el("small", { textContent: extra }) : null),
        el("span", { className: "label", textContent: label })));
    tile.onclick = () => { state.filter.special = filter && special !== filter ? filter : ""; renderLibrary(); };
    return tile;
  }));
}

function renderSelection() {
  const n = state.selected.size;
  $("selection").hidden = n === 0;
  $("sel-count").textContent = n > 1 ? `${n} images` : "1 image";
}

$("f-search").addEventListener("input", renderLibrary);
for (const b of $("layout").children) {
  b.onclick = () => { state.layout = b.dataset.layout; storage.set("wsfs-layout", state.layout); renderLibrary(); };
}
$("btn-refresh").onclick = () => loadLibrary();
$("btn-sel-clear").onclick = () => { state.selected.clear(); renderLibrary(); };

// ------------------------------------------------------------------ fiche

async function openDetail(id) {
  if (state.current?.id === id && !$("detail").hidden) return;
  if (!leaveEdit()) return;
  state.current = state.byId.get(id);
  delete $("u-anchor").dataset.edited;
  $("u-source").value = ""; $("u-version").value = "";
  state.pendingKeys.clear();
  $("main").classList.add("with-detail");
  $("detail").hidden = false;
  markCurrent();
  renderDetailHeader();
  showTab("info");
  await loadAutorun();
}

function closeDetail() {
  if (!leaveEdit()) return;
  $("detail").hidden = true;
  $("main").classList.remove("with-detail");
  state.current = null;
  markCurrent();
}

function renderDetailHeader() {
  const i = state.current;
  $("d-title").textContent = i.name;
  $("d-id").textContent = i.id;
  $("d-cover").dataset.id = i.id;
  showArt($("d-cover"), i);
  const [typeIcon, typeTone] = TYPE_LOOK[i.type] || TYPE_LOOK.unknown;
  const [stateLabel] = STATES[i.state] || [i.state];
  $("d-chips").replaceChildren(...[
    chip(typeIcon, TYPES[i.type] || i.type, typeTone, TYPE_HELP[i.type] || null),
    i.version ? chip("tag", `v${i.version.replace(/^v/i, "")}`, "t-sage", null, "GAME_VERSION")
      : chip("tag", "sans version", "t-accent", "hk-GAME_VERSION", "GAME_VERSION absente"),
    chip("drive", size(i.size), "t-neutral", null, "Taille de l'image"),
    i.runner_joined ? chip("clip", "runner joint", "t-accent", "h-runner") : null,
    i.hidraw ? chip("usb", "HIDRAW", "t-sage", "hk-HIDRAW") : chip("gamepad", "XInput", "t-neutral", "hk-HIDRAW"),
    i.state !== "ok" && i.state !== "has_old" ? chip(i.state === "in_use" ? "play" : "clock", stateLabel, "t-accent") : null,
    (i.override || []).length ? chip("filepen", "surcharge", "t-accent", "h-ar-override") : null].filter(Boolean));
  const facts = [
    ["file", "Fichier", i.path, true], ["clock", "Modifiée", when(i.mtime)],
    ["terminal", "Commande", i.cmd || "—", true], ["folder", "Dossier", i.dir || "(racine)", true],
    ["cpu", "Runner", `${runnerLabel(i)} — ${runnerDetails(i).join(" · ")}`],
    ["monitor", "DXVK / VKD3D", `${i.dxvk ? "oui" : "non"} / ${i.vkd3d ? "oui" : "non"}`],
    ["sparkles", "Extras", (i.extras || []).join(", ") || "—"],
    ["filepen", "Surcharge", (i.override || []).length ? `${i.override.join(", ")} (à inscrire dans l'image)` : "aucune"],
    ["save", "Sauvegardes", i.saves_present ? size(i.saves_size) : "aucune"],
  ];
  $("d-info").replaceChildren(...facts.map(([iconName, k, v, mono]) =>
    el("div", {}, icon(iconName), el("dt", { textContent: k }), el("dd", { className: mono ? "mono" : "", textContent: v }))));
  $("d-saves-reset").disabled = !i.saves_present;
  if ($("d-links-result").dataset.id !== i.id) {      // autre image : constat à refaire
    $("d-links-result").dataset.id = i.id;
    $("d-links-result").className = "msg";
    $("d-links-result").textContent = "Vérifie que l'image ne dépend pas d'un runner installé sur la machine.";
  }

  const old = $("d-old");
  old.replaceChildren();
  if (i.state === "in_use") old.append(el("div", { className: "notice" }, icon("play"), el("span", { textContent: "Jeu en cours : aucune reconstruction possible." })));
  if (i.has_old) {
    const keep = el("button", { className: "btn primary sm" }, icon("check", "sm"), "Valider");
    const back = el("button", { className: "btn secondary sm" }, icon("undo", "sm"), "Revenir à l'ancienne");
    keep.title = "Supprimer l'ancienne image (.old)";
    keep.onclick = () => oldAction("validate", "Supprimer définitivement l'ancienne image (.old) ?");
    back.onclick = () => oldAction("restore", "Remplacer l'image actuelle par l'ancienne (.old) ? La reconstruction sera perdue.");
    old.append(el("div", { className: "notice" }, icon("history"), el("div", { className: "body" },
      el("span", { textContent: "Une ancienne version (.old) est conservée : vérifier le jeu, puis valider ou revenir en arrière." }),
      el("div", { className: "row" }, keep, back))));
  }
}

async function oldAction(action, question) {
  if (!confirm(question)) return;
  try {
    await api(`/api/image/old/${action}?id=${encodeURIComponent(state.current.id)}`, { method: "POST" });
    await loadLibrary();
    await loadAutorun();
    toast(action === "validate" ? "Ancienne version supprimée" : "Ancienne version remise");
  } catch (error) {
    alert(error.message);
  }
}

function showTab(name) {
  for (const b of $("d-tabs").children) b.classList.toggle("active", b.dataset.tab === name);
  for (const pane of document.querySelectorAll("[data-pane]")) pane.hidden = pane.dataset.pane !== name;
  if (name === "files") loadFiles();
  if (name === "history") renderHistory();
}
$("d-tabs").onclick = (event) => { const b = event.target.closest("button[data-tab]"); if (b) showTab(b.dataset.tab); };
$("d-close").onclick = closeDetail;
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
  if (!$("tasks-panel").hidden) { setTasksOpen(false); return; }
  if ($("detail").hidden || event.target.closest?.("input, textarea, select")) return;
  state.editing ? leaveEdit() : closeDetail();
});

// ------------------------------------------------------------------ consulter / modifier
// La fiche se consulte ; « Modifier l'autorun » et « Nouvelle version » la
// passent en mode édition (liseré, pied Annuler / Enregistrer). Rien n'est
// écrit avant l'enregistrement ; quitter avec des changements demande confirmation.

const EDITS = { autorun: ["edit-autorun", "Modification de l'autorun"], version: ["edit-version", "Nouvelle version du jeu"] };
function enterEdit(kind) {
  state.editing = kind;
  $("detail").classList.add("editing");
  $("view-mode").hidden = true;
  for (const [k, [pane]] of Object.entries(EDITS)) $(pane).hidden = k !== kind;
  $("edit-banner").hidden = false;
  $("edit-title").textContent = EDITS[kind][1];
  if (kind === "autorun") {
    state.pendingKeys.clear();
    $("a-issues").replaceChildren(); $("a-result").replaceChildren(); $("a-proposal").replaceChildren();
    $("a-unset").hidden = true;
    state.baseline = $("a-editor").value;
    syncForm();
  } else {
    delete $("u-anchor").dataset.edited;
    $("u-source").value = ""; $("u-version").value = "";
    loadUpdateDefaults();
  }
  markDirty();
  $("detail").scrollTop = 0;
}
function isDirty() {
  if (state.editing === "autorun") return $("a-editor").value !== state.baseline;
  if (state.editing === "version") return Boolean($("u-source").value.trim() || $("u-version").value.trim() || $("u-anchor").dataset.edited);
  return false;
}
function markDirty() { $("edit-dirty").hidden = !isDirty(); }
// Quitte le mode édition (confirmation si des changements seraient perdus) ; faux si refusé
function leaveEdit(force = false) {
  if (!state.editing) return true;
  if (!force && isDirty() && !confirm("Abandonner les modifications non enregistrées ?")) return false;
  const was = state.editing;
  state.editing = null;
  $("detail").classList.remove("editing");
  for (const [pane] of Object.values(EDITS)) $(pane).hidden = true;
  $("edit-banner").hidden = true;
  $("view-mode").hidden = false;
  if (was === "autorun" && !force) { $("a-editor").value = state.baseline; syncForm(); }
  return true;
}
$("d-edit-autorun").onclick = () => enterEdit("autorun");
$("d-edit-version").onclick = () => enterEdit("version");
$("a-cancel").onclick = () => leaveEdit();
$("u-cancel").onclick = () => leaveEdit();
for (const id of ["u-source", "u-anchor", "u-version"]) $(id).addEventListener("input", markDirty);

// Surligne la fiche ouverte sans redessiner la liste
function markCurrent() {
  for (const node of document.querySelectorAll("#rows [data-id], #grid [data-id]")) {
    node.classList.toggle("current", node.dataset.id === state.current?.id);
    node.classList.toggle("selected", state.selected.has(node.dataset.id));
  }
}

// ------------------------------------------------------------------ autorun : texte ⇄ formulaire
// Mêmes règles que le serveur (services/autorun.py) : clé en début de ligne,
// première occurrence retenue ; une clé modifiée garde sa place, une clé
// ajoutée va en fin de fichier ; vider un champ retire la clé.

const KEY_RE = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/;
const KEY_ICONS = { CMD: "terminal", DIR: "folder", GAME_VERSION: "tag", PRE: "play", ATTACH: "link", ATTACH_PROCESS: "cpu",
  WINE: "wine", PROTON: "atom", RUNNER: "drive", ARCH: "cpu", LANG: "book", ENV: "code", HIDRAW: "usb",
  SAVEDIR: "folder", SAVEFILES: "file", DXVK: "monitor", VKD3D: "monitor", D7VK: "monitor", VIRTUAL_DESKTOP: "grid",
  NVAPI: "cpu", VKREFLEX: "zap", FSR: "monitor", FSR4: "monitor", ESYNC: "zap", FSYNC: "zap" };

function editorLines() {
  const raw = $("a-editor").value.replace(/\r\n/g, "\n");
  const trailing = raw.endsWith("\n");
  const content = trailing ? raw.slice(0, -1) : raw;
  return { lines: content ? content.split("\n") : [], trailing };
}

function setEditorLines(lines, trailing) {
  $("a-editor").value = lines.join("\n") + (trailing && lines.length ? "\n" : "");
}

function getKey(lines, key) {
  for (const line of lines) {
    const m = KEY_RE.exec(line);
    if (m && m[1] === key) return m[2];
  }
  return null;
}

function setKey(key, value) {
  let { lines, trailing } = editorLines();
  const index = lines.findIndex((line) => KEY_RE.exec(line)?.[1] === key);
  if (value === "") {
    lines = lines.filter((line) => KEY_RE.exec(line)?.[1] !== key);
  } else if (index >= 0) {
    lines[index] = `${key}=${value}`;
  } else {
    lines.push(`${key}=${value}`);
    if (lines.length === 1) trailing = true;
  }
  if (value !== "") state.pendingKeys.delete(key);
  setEditorLines(lines, trailing);
  syncForm();
}

// Valeurs proposées par clé (datalists), y compris les runners de la machine de jeu
function buildForm() {
  $("a-lists").replaceChildren(...state.keys.map(({ key, values }) =>
    el("datalist", { id: `v-${key}` }, ...values.map((v) => new Option(v, v)))));
  for (const key of ["PROTON", "WINE"]) if (!$(`v-${key}`)) $("a-lists").append(el("datalist", { id: `v-${key}` }));
}
const keyInfo = (key) => state.keys.find((k) => k.key === key);

function kvRow(key, value) {
  const info = keyInfo(key);
  const input = el("input", { id: `k-${key}`, value: value ?? "", placeholder: info ? "valeur" : "", autocomplete: "off", spellcheck: false });
  if ($(`v-${key}`)) input.setAttribute("list", `v-${key}`);
  input.addEventListener("change", () => setKey(key, input.value.trim()));
  const remove = el("button", { className: "iconbtn", title: "Retirer la clé" }, icon("x", "sm"));
  remove.onclick = () => { state.pendingKeys.delete(key); setKey(key, ""); };
  const row = el("div", { className: "kv", title: info?.help || "Clé non reconnue : conservée, ignorée par Batocera" },
    el("span", { className: "disc" }, icon(KEY_ICONS[key] || "terminal")),
    el("span", { className: "k" }, info ? helpLink(`hk-${key}`, key, `${key} dans l'aide`) : key),
    input, remove);
  row.dataset.key = key;
  return row;
}

function syncForm() {
  const { lines } = editorLines();
  const inFile = [...new Set(lines.map((l) => KEY_RE.exec(l)?.[1]).filter(Boolean))];
  const shown = [...inFile, ...[...state.pendingKeys].filter((k) => !inFile.includes(k))];
  const form = $("a-form");
  const current = [...form.children].map((r) => r.dataset.key);
  if (current.join("\n") === shown.join("\n")) {
    // Mêmes clés : seules les valeurs changent (le champ en cours de saisie est laissé tel quel)
    for (const key of shown) {
      const input = $(`k-${key}`);
      if (input && document.activeElement !== input) input.value = getKey(lines, key) ?? "";
    }
  } else {
    const focused = document.activeElement?.id;
    form.replaceChildren(...shown.map((key) => kvRow(key, getKey(lines, key))));
    if (focused && $(focused) && form.contains($(focused))) $(focused).focus();
  }
  const unset = state.keys.filter(({ key }) => !shown.includes(key));
  $("a-unset").replaceChildren(...unset.map(({ key, help }) => {
    const b = el("button", { title: help }, icon(KEY_ICONS[key] || "terminal", "xs"), key);
    b.onclick = () => { state.pendingKeys.add(key); syncForm(); $(`k-${key}`)?.focus(); };
    return b;
  }));
  markDirty();
  const unknown = inFile.filter((k) => !keyInfo(k));
  $("a-note").textContent = unknown.length
    ? `Clés non reconnues (conservées, ignorées par Batocera) : ${unknown.join(", ")}`
    : !state.autorun ? "" : state.autorun.found ? `${inFile.length} réglage(s)` : "Pas d'autorun dans l'image : il sera créé.";
}

$("a-editor").addEventListener("input", syncForm);
$("a-addkey").onclick = () => { $("a-unset").hidden = !$("a-unset").hidden; };
$("a-tabs").onclick = (event) => {
  const tab = event.target.closest("button[data-atab]")?.dataset.atab;
  if (!tab) return;
  for (const b of $("a-tabs").children) b.classList.toggle("on", b.dataset.atab === tab);
  for (const pane of document.querySelectorAll("[data-apane]")) pane.hidden = pane.dataset.apane !== tab;
};

async function loadAutorun() {
  const id = state.current.id;
  $("a-issues").replaceChildren();
  $("a-result").replaceChildren();
  $("a-proposal").replaceChildren();
  try {
    const data = await api(`/api/image/autorun?id=${encodeURIComponent(id)}`);
    if (state.current?.id !== id) return;
    state.autorun = data;
    // L'éditeur montre l'autorun effectif : image + surcharge, ce que le lanceur applique
    $("a-editor").value = (data.effective ?? data.text).replace(/\r\n/g, "\n");
    renderOverrideBox(data);
    $("e-override-note").replaceChildren(...(data.override ? [notice("", "Une surcharge est active : le formulaire montre l'autorun effectif (image + surcharge).", "filepen")] : []));
    $("a-eol").value = data.eol === "\n" ? "lf" : "crlf";
  } catch (error) {
    state.autorun = null;
    $("a-editor").value = "";
    $("a-result").append(el("div", { className: "notice err" }, icon("alert"), el("span", { textContent: `Lecture impossible : ${error.message}` })));
  }
  syncForm();
  renderArView();
}

// Autorun en lecture : une ligne par réglage, lien vers l'aide
function renderArView() {
  const data = state.autorun;
  const box = $("ar-view");
  if (!data) { box.replaceChildren(); return; }
  const overridden = new Set(data.override?.keys || []);
  const lines = (data.effective ?? data.text).replace(/\r\n/g, "\n").split("\n").filter((l) => l.trim());
  if (!lines.length) { box.replaceChildren(el("p", { className: "arview-empty", textContent: "Pas d'autorun dans l'image. « Modifier l'autorun » pour en créer un." })); return; }
  box.replaceChildren(...lines.map((line) => {
    const m = KEY_RE.exec(line);
    if (!m) return el("div", {}, el("span", { className: "rem", textContent: line }));
    const [, key, value] = m;
    return el("div", { title: keyInfo(key)?.help || "" },
      el("dt", {}, keyInfo(key) ? helpLink(`hk-${key}`, key, `${key} dans l'aide`) : key),
      el("dd", { textContent: value || "(vide : défaut du lanceur)" }),
      overridden.has(key) ? el("span", { className: "chip t-accent", textContent: "surcharge" }) : el("span"));
  }));
}

function autorunBody() {
  const { lines, trailing } = editorLines();
  return {
    lines, trailing_eol: trailing || !state.autorun?.found,
    eol: $("a-eol").value === "crlf" ? "\r\n" : "\n",
    encoding: state.autorun?.encoding || "utf-8",
    base_sha: state.autorun?.sha ?? null,
    when: whenOf(),
  };
}

const notice = (kind, text, iconName = kind === "err" ? "alert" : "info") =>
  el("div", { className: `notice ${kind}` }, icon(iconName), el("span", { textContent: text }));
function showIssues(list) {
  $("a-issues").replaceChildren(...list.map((x) => el("li", { className: x.level, textContent: x.message })));
}
function confidence(level) {
  return el("span", { className: `conf-${level}`, textContent: { high: "sûr", medium: "probable", low: "incertain" }[level] || level });
}
const proposalItems = (items) => el("ul", { className: "items" }, ...items.map((i) => el("li", { className: i.written ? "" : "default" },
  el("code", { textContent: `${i.key}=${i.value}` }), " — ", i.justification, " (", confidence(i.confidence), ")",
  i.written ? "" : el("div", { className: "muted", textContent: "→ déjà le comportement par défaut du lanceur : rien à ajouter à l'autorun" }))));


$("a-propose").onclick = async () => {
  const id = state.current.id, box = $("a-proposal");
  box.replaceChildren(el("div", { className: "notice info" }, el("span", { className: "spinner" }), el("span", { textContent: "Analyse de l'image (lecture des exécutables)…" })));
  try {
    const r = await api(`/api/image/analyze?id=${encodeURIComponent(id)}`);
    if (state.current?.id !== id) return;
    // Ce que la proposition change par rapport à l'autorun en cours d'édition
    const norm = (key, v) => {
      let x = (v || "").trim();
      if (["CMD", "DIR"].includes(key)) x = x.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
      return x;
    };
    const { lines } = editorLines();
    const changes = [];
    for (const line of r.generated.text.replace(/\r\n/g, "\n").split("\n")) {
      const m = KEY_RE.exec(line);
      if (!m) continue;
      const before = getKey(lines, m[1]);
      if (before === null || norm(m[1], before) !== norm(m[1], m[2])) changes.push({ key: m[1], value: m[2], before });
    }
    const analysis = el("details", {}, el("summary", { textContent: "Voir l'analyse" }),
      el("div", { style: "margin-top:8px" }, proposalItems(r.generated.items)));
    const warnings = r.generated.warnings.length ? [el("ul", { className: "issues" }, ...r.generated.warnings.map((w) => el("li", { textContent: w })))] : [];
    if (!r.generated.text.trim()) {
      box.replaceChildren(notice("", "Rien à proposer : aucun exécutable Windows trouvé dans l'image.", "alert"), ...warnings);
      return;
    }
    if (!changes.length) {
      box.replaceChildren(el("div", { className: "proposal" },
        el("div", { className: "head" }, icon("check"), el("strong", { textContent: "Rien à proposer : l'autorun actuel correspond déjà à l'analyse." })),
        ...warnings, analysis));
      return;
    }
    const use = el("button", { className: "btn primary sm" }, icon("check", "sm"), changes.length > 1 ? `Appliquer ces ${changes.length} changements` : "Appliquer ce changement");
    use.onclick = () => {
      for (const c of changes) setKey(c.key, c.value);
      box.replaceChildren(notice("ok", "Changements placés dans le formulaire : vérifier, puis essayer ou reconstruire.", "check"));
    };
    box.replaceChildren(el("div", { className: "proposal" },
      el("div", { className: "head" }, icon("sparkles"), el("strong", { textContent: changes.length > 1 ? `${changes.length} changements proposés` : "1 changement proposé" }),
        r.engine ? el("span", { className: "chip", textContent: r.engine }) : null),
      el("div", { className: "diff" }, ...changes.map((c) => el("div", { className: "add",
        textContent: c.before === null ? `+ ${c.key}=${c.value}` : `~ ${c.key} : ${c.before} → ${c.value}` }))),
      ...warnings, analysis,
      el("div", { className: "row" }, use)));
  } catch (error) {
    box.replaceChildren(notice("err", error.message));
  }
};
$("a-check").onclick = async () => {
  try {
    const data = await api(`/api/image/autorun/preview?id=${encodeURIComponent(state.current.id)}`, { method: "POST", body: autorunBody() });
    showIssues(data.issues);
    $("a-result").replaceChildren(notice(data.issues.length ? "" : "ok",
      !data.changed ? "Aucun changement." : data.issues.length ? "Avertissements ci-dessus (non bloquants)." : "Aucun problème détecté.",
      data.issues.length ? "alert" : "check"));
  } catch (error) {
    $("a-result").replaceChildren(notice("err", error.message));
  }
};
$("a-save").onclick = async () => {
  if (!confirm("Reconstruire l'image avec cet autorun ? L'original sera conservé en .old jusqu'à validation.")) return;
  try {
    const data = await api(`/api/image/autorun?id=${encodeURIComponent(state.current.id)}`, { method: "POST", body: autorunBody() });
    const hadOverride = Boolean(state.autorun?.override);
    leaveEdit(true);
    showTab("autorun");
    const issues = data.issues.length ? [el("ul", { className: "issues" }, ...data.issues.map((x) => el("li", { className: x.level, textContent: x.message })))] : [];
    if (data.override_removed) {
      $("ar-result").replaceChildren(notice("ok", "Autorun identique à l'image : surcharge supprimée, pas de reconstruction.", "check"), ...issues);
      await loadLibrary();
      await loadAutorun();
      return;
    }
    $("ar-result").replaceChildren(notice("ok", hadOverride
      ? "Reconstruction ajoutée à la file : l'autorun effectif sera inscrit dans l'image, puis la surcharge supprimée."
      : "Reconstruction ajoutée à la file : l'image sera reconstruite avec cet autorun.", "check"), ...issues);
    queued();
  } catch (error) {
    $("a-result").replaceChildren(notice("err", error.message));
  }
};

function renderOverrideBox(data) {
  const box = $("a-override-box");
  box.replaceChildren();
  if (!data?.override) return;
  const commit = el("button", { className: "btn primary sm" }, icon("hammer", "sm"), "Inscrire dans l'image");
  const drop = el("button", { className: "btn secondary sm" }, icon("trash", "sm"), "Supprimer la surcharge");
  commit.onclick = () => commitOverrides([state.current.id]);
  drop.onclick = async () => {
    if (!confirm("Supprimer la surcharge ? Le jeu reprendra l'autorun de l'image.")) return;
    try {
      await api(`/api/image/override?id=${encodeURIComponent(state.current.id)}`, { method: "DELETE" });
      await loadLibrary();
      await loadAutorun();
    } catch (error) { alert(error.message); }
  };
  box.append(el("div", { className: "notice", style: "margin:0 0 12px" }, icon("filepen"), el("div", { className: "body" },
    el("span", { textContent: `Surcharge active (${data.override.keys.join(", ")}) : appliquée par le lanceur, mais pas par Batocera tant qu'elle n'est pas inscrite dans l'image. Le formulaire montre l'autorun effectif.` }),
    el("pre", { className: "bat", textContent: data.override.text }),
    el("div", { className: "row" }, commit, drop))));
}

$("a-override").onclick = async () => {
  try {
    const data = await api(`/api/image/override?id=${encodeURIComponent(state.current.id)}`, { method: "PUT", body: autorunBody() });
    leaveEdit(true);
    await loadLibrary();
    await loadAutorun();          // recharge l'autorun effectif
    showTab("autorun");
    $("ar-result").replaceChildren(notice("ok", data.keys.length
      ? `Surcharge écrite (${data.keys.join(", ")}) : effet au prochain lancement, sans reconstruction. Une fois validée, « Inscrire dans l'image ».`
      : "Identique à l'image : surcharge supprimée.", "zap"),
      ...(data.issues.length ? [el("ul", { className: "issues" }, ...data.issues.map((x) => el("li", { className: x.level, textContent: x.message })))] : []));
  } catch (error) {
    $("a-result").replaceChildren(notice("err", error.message));
  }
};

async function commitOverrides(ids) {
  const withOverride = ids.filter((id) => (state.byId.get(id)?.override || []).length);
  if (!withOverride.length) { alert("Aucune image sélectionnée n'a de surcharge."); return; }
  if (!confirm(`Inscrire la surcharge dans ${withOverride.length} image(s) ? Chaque image est reconstruite (original gardé en .old), puis sa surcharge supprimée.`)) return;
  try {
    const data = await api("/api/mass/override/commit", { method: "POST", body: { ids: withOverride, when: whenOf() } });
    reportSkipped(data.skipped);
    queued();
  } catch (error) { alert(error.message); }
}

// ------------------------------------------------------------------ montée de version

async function loadUpdateDefaults() {
  const id = state.current.id;
  $("u-result").replaceChildren();
  $("u-run").disabled = true;
  try {
    const d = await api(`/api/image/update?id=${encodeURIComponent(id)}`);
    if (state.current?.id !== id) return;
    if (!$("u-anchor").dataset.edited) $("u-anchor").value = d.anchor;
    $("u-version").placeholder = d.version ? `actuelle : ${d.version} — proposée d'après l'exécutable` : "proposée d'après l'exécutable";
  } catch (error) {
    $("u-result").replaceChildren(notice("err", error.message));
  }
}
for (const id of ["u-source", "u-anchor", "u-version"]) $(id).addEventListener("input", () => { $("u-run").disabled = true; });
$("u-delete").addEventListener("change", () => { $("u-run").disabled = true; });
$("u-anchor").addEventListener("input", () => { $("u-anchor").dataset.edited = "1"; });

const updateBody = () => ({ source: $("u-source").value.trim(), anchor: $("u-anchor").value.trim(),
  delete_missing: $("u-delete").checked, clean_saves: $("u-saves").checked,
  version: $("u-version").value.trim() || null, when: whenOf() });

function fileList(title, items, render) {
  if (!items.length) return null;
  return el("details", {}, el("summary", { textContent: title }),
    el("div", { className: "filelist", textContent: items.map(render).join("\n") }));
}

$("u-preview").onclick = async () => {
  $("u-result").replaceChildren(el("div", { className: "notice info" }, el("span", { className: "spinner" }), el("span", { textContent: "Comparaison de la source et de l'image…" })));
  try {
    const r = await api(`/api/image/update/preview?id=${encodeURIComponent(state.current.id)}`, { method: "POST", body: updateBody() });
    if (r.version && !$("u-version").value) $("u-version").value = r.version;
    const more = (n, shown) => n > shown ? ` (${shown} affichés)` : "";
    $("u-result").replaceChildren(
      el("h3", { textContent: r.changed ? `Dans ${r.anchor || "la racine"} : ${r.added_count} ajouté(s), ${r.replaced_count} remplacé(s), ${r.deleted_count} supprimé(s), ${r.same} identique(s)` : "Rien à mettre à jour" }),
      el("dl", {}, el("dt", { textContent: "Version" }), el("dd", { textContent: `${r.version_before || "—"} → ${r.version || "inchangée"}${r.version_hint ? ` (${r.version_hint})` : ""}` }),
        el("dt", { textContent: "À copier" }), el("dd", { textContent: size(r.copy_size) }),
        el("dt", { textContent: "Disque" }), el("dd", { textContent: `${size(r.space.needed)} nécessaires (ancienne image gardée en .old), ${size(r.space.free)} libres` })),
      ...[fileList(`Ajoutés${more(r.added_count, r.added.length)}`, r.added, (a) => `+ ${a.path}  (${size(a.size)})`),
        fileList(`Remplacés${more(r.replaced_count, r.replaced.length)}`, r.replaced, (x) => `~ ${x.path}  (${size(x.before)} → ${size(x.after)})`),
        fileList(`Supprimés${more(r.deleted_count, r.deleted.length)}`, r.deleted, (d) => `- ${d}`),
        fileList(`Masqués par les sauvegardes${more(r.masked_count, r.masked.length)}`, r.masked, (m) => `! ${m}`)].filter(Boolean),
      ...(r.warnings.length ? [el("ul", { className: "issues" }, ...r.warnings.map((w) => el("li", { textContent: w })))] : []),
      ...(r.blocked ? [notice("", r.blocked, "alert")] : []));
    $("u-run").disabled = !r.changed || Boolean(r.blocked);
  } catch (error) {
    $("u-result").replaceChildren(notice("err", error.message));
  }
};

$("u-run").onclick = async () => {
  if (!confirm("Mettre à jour l'image ? L'original sera conservé en .old jusqu'à validation.")) return;
  try {
    await api(`/api/image/update?id=${encodeURIComponent(state.current.id)}`, { method: "POST", body: updateBody() });
    $("u-run").disabled = true;
    leaveEdit(true);
    queued("Mise à jour ajoutée à la file de tâches");
  } catch (error) {
    $("u-result").replaceChildren(notice("err", error.message));
  }
};

// ------------------------------------------------------------------ base de connaissances

$("a-kb").onclick = () => {
  const { lines } = editorLines();
  const autorun = Object.fromEntries(lines.map((l) => KEY_RE.exec(l)).filter(Boolean).map((m) => [m[1], m[2]]));
  const exe = (autorun.CMD || "").startsWith('"') ? autorun.CMD.split('"')[1] : (autorun.CMD || "").split(" ")[0];
  const dir = (autorun.DIR || "").replace(/^["']|["']$/g, "").replace(/\\/g, "/").replace(/\/+$/, "");
  $("kb-game").value = state.current.name;
  $("kb-prefer").textContent = exe ? [dir, exe].filter(Boolean).join("/") : "—";
  $("kb-prefer-on").checked = false;
  $("kb-prefer-on").disabled = !exe;
  const keys = Object.entries(autorun).filter(([k]) => !["CMD", "DIR", "GAME_VERSION"].includes(k));
  $("kb-keys").replaceChildren(...(keys.length ? keys.map(([k, v]) => {
    const box = el("input", { type: "checkbox", checked: true });
    box.dataset.key = k; box.dataset.value = v;
    return el("label", { className: "row" }, box, `${k}=${v}`);
  }) : [el("span", { className: "muted", textContent: "aucun réglage hors CMD/DIR/GAME_VERSION dans l'autorun" })]));
  $("kb-note").value = "";
  $("kb-err").hidden = true;
  $("dlg-kb").showModal();
};

$("kb-save").onclick = async () => {
  const keys = Object.fromEntries([...$("kb-keys").querySelectorAll("input:checked")].map((b) => [b.dataset.key, b.dataset.value]));
  const body = { game: $("kb-game").value.trim(), keys, note: $("kb-note").value.trim(),
    prefer: $("kb-prefer-on").checked ? $("kb-prefer").textContent : null };
  try {
    if (!body.game) throw new Error("Nom du jeu requis.");
    if (!Object.keys(keys).length && !body.prefer) throw new Error("Rien à mémoriser : cocher un réglage ou l'exécutable.");
    await api("/api/knowledge", { method: "POST", body });
    $("dlg-kb").close();
    $("a-result").replaceChildren(notice("ok", "Ajouté à la base : appliqué aux prochaines propositions pour ce jeu.", "bookmark"));
  } catch (error) {
    $("kb-err").textContent = error.message;
    $("kb-err").hidden = false;
  }
};

// ------------------------------------------------------------------ fichiers, historique

let filesTimer = null;
async function loadFiles() {
  const id = state.current.id, q = $("files-q").value.trim();
  $("files-count").textContent = "lecture…";
  try {
    const data = await api(`/api/image/files?id=${encodeURIComponent(id)}&q=${encodeURIComponent(q)}`);
    if (state.current?.id !== id) return;
    $("files-count").textContent = q ? `${data.matches} sur ${data.total} entrées` : `${data.total} entrées`;
    $("files-list").replaceChildren(...data.files.map((path) => {
      const dir = path.endsWith("/");
      return el("div", { className: dir ? "dir" : "", title: path }, icon(dir ? "folder" : "file"), el("span", { textContent: path }));
    }), data.matches > data.files.length ? el("div", { className: "more", textContent: `… ${data.matches - data.files.length} de plus : affiner la recherche` }) : null);
  } catch (error) {
    $("files-count").textContent = error.message;
  }
}
$("files-q").addEventListener("input", () => { clearTimeout(filesTimer); filesTimer = setTimeout(loadFiles, 250); });

async function renderHistory() {
  if (!state.current) return;
  const id = state.current.id;
  let tasks;
  try {
    // File en cours + journal permanent (conservé après « Effacer l'historique »)
    tasks = await api(`/api/tasks?image=${encodeURIComponent(id)}`);
  } catch (error) {
    $("d-history").replaceChildren(notice("err", error.message));
    return;
  }
  if (state.current?.id !== id) return;
  const opened = new Set([...$("d-history").querySelectorAll(".task.open")].map((n) => n.dataset.id));
  $("d-history").replaceChildren(...(tasks.length ? tasks.map((t) => taskNode(t, opened)) : [el("p", { className: "muted", textContent: "Aucune opération enregistrée." })]));
}

// ------------------------------------------------------------------ tâches

const TASK_KINDS = { autorun: ["hammer", "Reconstruction"], update: ["upload", "Montée de version"], files: ["file", "Fichiers et registre"],
  verify: ["shield", "Vérification"], pack: ["package", "Empaquetage"], runner: ["clip", "Runner joint"], trial: ["flask", "Lancement d'essai"] };
const TASK_STATES = { pending: ["clock", "en attente", "t-neutral"], running: ["refresh", "en cours", "t-accent"], done: ["check", "terminée", "t-sage"],
  failed: ["alert", "échec", "t-err"], cancelled: ["x", "annulée", "t-neutral"] };

function taskNode(t, opened = new Set()) {
  const [kindIcon, kindLabel] = TASK_KINDS[t.kind] || ["tasks", t.kind];
  let [stIcon, stLabel, tone] = TASK_STATES[t.status] || ["info", t.status, "t-neutral"];
  if (t.status === "pending" && t.night) { stIcon = "moon"; stLabel = state.config ? `nuit ${state.config.night_start}` : "cette nuit"; }
  if (t.status === "running") stLabel = `${Math.round((t.progress || 0) * 100)} %`;
  const progress = t.status === "done" ? 100 : t.status === "running" || t.status === "failed" ? Math.round((t.progress || 0) * 100) : 0;
  const node = el("div", { className: `task ${t.status}${opened.has(t.id) ? " open" : ""}` },
    el("span", { className: `disc ${tone}` }, icon(kindIcon)),
    el("div", { className: "mid" },
      el("div", { className: "line", title: `${t.image || ""} — ${t.title}` }, el("strong", { textContent: t.image || kindLabel }), " ", el("span", { textContent: t.title || kindLabel })),
      el("div", { className: "bar" }, el("div", { style: `width:${progress}%` })),
      t.status === "running" && t.phase ? el("div", { className: "muted", style: "font-size:11.5px", textContent: t.phase }) : null,
      t.error ? el("div", { className: "err-line", textContent: t.error }) : null),
    el("span", { className: "st", title: TASK_STATES[t.status]?.[1] || t.status }, icon(stIcon, `sm${t.status === "running" ? " spinning" : ""}`), stLabel));
  node.dataset.id = t.id;
  const more = el("div", { className: "more" });
  const actions = el("div", { className: "row" }, el("span", { className: "when-at spacer", textContent: `créée le ${when(t.created_at)}` }));
  if (t.status === "pending" && t.night) {
    const now = el("button", { className: "btn secondary sm" }, icon("zap", "sm"), "Lancer maintenant");
    now.onclick = async (event) => {
      event.stopPropagation();
      try { await api(`/api/tasks/${t.id}/now`, { method: "POST" }); } catch (error) { alert(error.message); }
      refreshTasks();
    };
    actions.append(now);
  }
  if (t.status === "pending" || t.status === "running") {
    const cancel = el("button", { className: "btn danger sm" }, icon("x", "sm"), "Annuler");
    cancel.onclick = async (event) => {
      event.stopPropagation();
      try { await api(`/api/tasks/${t.id}/cancel`, { method: "POST" }); } catch (error) { alert(error.message); }
      refreshTasks();
    };
    actions.append(cancel);
  }
  more.append(actions);
  const result = t.kind === "trial" ? t.params?.result : null;
  if (result) more.append(trialReport(t, result));
  if ((t.log || []).length) more.append(el("pre", { className: "log", textContent: t.log.join("\n") }));
  node.append(more);
  node.onclick = (event) => { if (!event.target.closest(".more")) node.classList.toggle("open"); };
  return node;
}

function trialReport(t, r) {
  const box = el("div", { className: `notice ${r.ok ? "ok" : "err"}`, style: "margin:0" }, icon(r.ok ? "check" : "alert"),
    el("div", { className: "body" }, el("strong", { textContent: r.verdict })));
  const body = box.lastChild;
  for (const w of r.windows || []) body.append(el("div", { className: "mono", style: "font-size:12px", textContent: `fenêtre : ${w.name} [${w.state || "normale"}]` }));
  if ((r.errors || []).length) body.append(el("ul", { className: "issues" }, ...r.errors.map((e) => el("li", { textContent: `${e.meaning} — ${e.line}` }))));
  if (r.screenshot) {
    const show = el("button", { className: "btn secondary sm" }, icon("image", "sm"), "Voir la capture");
    show.onclick = async (event) => {
      event.stopPropagation();
      try {
        const response = await fetch(`/api/tasks/${t.id}/screenshot`, { headers: { "x-wsfs-token": storage.get("wsfs-token") || "" } });
        if (!response.ok) throw new Error(`erreur ${response.status}`);
        show.replaceWith(el("img", { src: URL.createObjectURL(await response.blob()), alt: "Capture de l'essai" }));
      } catch (error) { alert(error.message); }
    };
    body.append(el("div", {}, show));
  }
  return box;
}

let tasksTimer = null;
let activeBefore = new Set();
async function refreshTasks() {
  clearTimeout(tasksTimer);
  try {
    state.tasks = await api("/api/tasks");
  } catch {
    tasksTimer = setTimeout(refreshTasks, 10000);
    return;
  }
  const active = state.tasks.filter((t) => t.status === "pending" || t.status === "running");
  const opened = new Set([...document.querySelectorAll("#tasks-list .task.open")].map((n) => n.dataset.id));
  $("tasks-list").replaceChildren(...(state.tasks.length ? state.tasks.slice(0, 100).map((t) => taskNode(t, opened))
    : [el("p", { className: "muted", style: "margin:4px", textContent: "Aucune tâche." })]));
  const running = active.some((t) => t.status === "running");
  const failed = state.tasks.filter((t) => t.status === "failed").length;
  const nightly = active.filter((t) => t.status === "pending" && t.night).length;
  const summary = active.length ? `${active.length} tâche${active.length > 1 ? "s" : ""}${nightly ? ` · ${nightly} cette nuit` : ""}` : "Tâches";
  $("btn-tasks").classList.toggle("busy", running);
  // Rien en cours ni en échec : le bouton se réduit à son icône
  $("btn-tasks").classList.toggle("quiet", !active.length && !failed);
  $("tasks-count").textContent = active.length || "";
  $("tasks-fail").hidden = !failed;
  $("tasks-fail").textContent = `${failed} en échec`;
  $("btn-tasks").title = `File de tâches — ${summary}${failed ? `, ${failed} en échec` : ""}`;
  $("tasks-summary").textContent = active.length ? `${active.length} active(s)` : state.tasks.length ? "aucune active" : "";
  // Une tâche vient de se terminer : l'image a changé
  const nowActive = new Set(active.map((t) => t.id));
  if ([...activeBefore].some((id) => !nowActive.has(id))) {
    await loadLibrary();
    if (state.current) await loadAutorun();
    if (!$("folders-main").hidden) loadFolders();
  }
  activeBefore = nowActive;
  if (state.current && !$("detail").hidden && !document.querySelector('[data-pane="history"]').hidden) renderHistory();
  tasksTimer = setTimeout(refreshTasks, active.length ? 1500 : 10000);
}
function setTasksOpen(open) {
  $("tasks-panel").hidden = !open;
  $("btn-tasks").setAttribute("aria-expanded", open);
}
const openTasks = () => setTasksOpen(true);
// Une action vient d'entrer dans la file
function queued(message = whenOf() === "night" ? "Programmé pour cette nuit" : "Ajouté à la file de tâches") {
  toast(message, whenOf() === "night" ? "moon" : "check");
  refreshTasks();
}
$("btn-tasks").onclick = (event) => { event.stopPropagation(); setTasksOpen($("tasks-panel").hidden); };
$("tasks-close").onclick = () => setTasksOpen(false);
// Un clic ailleurs referme le panneau des tâches
document.addEventListener("click", (event) => {
  if (!$("tasks-panel").hidden && !event.target.closest("#tasks-panel, #btn-tasks, dialog")) setTasksOpen(false);
});
$("tasks-clear").onclick = async () => { await api("/api/tasks/finished", { method: "DELETE" }); refreshTasks(); };

// ------------------------------------------------------------------ machine de jeu
// Désactivée, cette machine ou un conteneur Docker (Réglages) : sans elle, pas
// de lancement d'essai ni d'état du lanceur ; le reste fonctionne.

const hostOn = () => Boolean(state.config && state.config.game_host && state.config.game_host !== "off");
const hostName = () => state.config?.game_host_name || "la machine de jeu";
function applyHost() {
  const on = hostOn();
  document.querySelector(".agroup .trial").hidden = !on;
  $("d-saves-copy").hidden = !on;
  $("d-links").title = on ? `Liens du prefix vers un runner hors de l'image ou absent, et réparation avec les runners de ${hostName()}`
    : "Liens du prefix vers un runner hors de l'image ou absent";
}
const HOST_HINTS = {
  off: "Pas de lancement d'essai ni de diagnostic. Le reste fonctionne.",
  local: "Le lanceur est installé sur cette machine, avec un écran.",
  docker: "Le lanceur tourne dans un conteneur, joint par l'API Docker.",
  agent: "Le lanceur tourne sur la machine de jeu : wsquashfs-launcher --agent.",
};
// Connexion établie : les paramètres de connexion se replient (résumé), sauf
// si l'on demande à les modifier ; mode changé = champs visibles.
function paintConnection(mode = $("cfg-host").dataset.mode) {
  const saved = state.config?.game_host;
  const connected = state.hostStatus?.state === "ok" && mode === saved;
  const folded = connected && !state.hostEditing && (mode === "agent" || mode === "docker");
  $("cfg-docker").hidden = mode !== "docker" || folded;
  $("cfg-agent").hidden = mode !== "agent" || folded;
  $("host-summary").hidden = !folded;
  $("host-maint").hidden = !(state.hostStatus?.state === "ok" && mode === saved);
  if (folded) {
    const c = state.config;
    $("host-summary-text").textContent = mode === "agent"
      ? `Agent : ${c.agent_url}${c.agent_fingerprint ? " · empreinte vérifiée" : ""}`
      : `Conteneur : ${c.steambox_container} · ${c.docker_host}`;
  }
}
$("host-edit").onclick = () => { state.hostEditing = true; paintConnection(); };

// Maintenance du lanceur : chaque action demande confirmation, le résultat s'affiche sous les boutons
const MAINT = {
  list: ["Lister les jeux", ""],
  umount: ["Démonter les jeux", "Un jeu en cours sera arrêté."],
  clean: ["Nettoyer", "Supprime les copies de travail, les doublons des sauvegardes et les anciennes archives (backups). Ce que les jeux ont écrit est gardé."],
  saves: ["Copier les sauvegardes de tous les jeux", "Copie les sauvegardes de partie dans la ludothèque (saves/<système>/<jeu>). Rien n'est supprimé."],
};
for (const button of document.querySelectorAll("[data-maint]")) {
  button.onclick = async () => {
    const action = button.dataset.maint, [label, warning] = MAINT[action];
    if (action !== "list" && !confirm(`${label} ?\n\n${warning}`)) return;
    const out = $("maint-out");
    out.hidden = false;
    out.textContent = `${label}…`;
    const buttons = document.querySelectorAll("[data-maint]");
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const r = await api("/api/game_host/maintenance", { method: "POST", body: { action }, timeout: 900000 });
      out.textContent = (r.output || "").trim() || `${label} : terminé (code ${r.code})`;
      if (r.code !== 0) out.textContent += `\n\nCode de retour : ${r.code}`;
    } catch (error) {
      out.textContent = `${label} : ${error.message}`;
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
  };
}
// Réglages en onglets : Dossiers, Machine de jeu, Nuit
function showCfgPane(name) {
  for (const b of $("cfg-tabs").children) b.classList.toggle("on", b.dataset.cpane === name);
  for (const pane of document.querySelectorAll(".cfg-pane")) pane.hidden = pane.dataset.paneName !== name;
}
$("cfg-tabs").onclick = (event) => {
  const b = event.target.closest("button[data-cpane]");
  if (b) showCfgPane(b.dataset.cpane);
};

function paintHostForm(mode) {
  for (const b of $("cfg-host").children) {
    b.classList.toggle("on", b.dataset.host === mode);
    b.setAttribute("aria-checked", b.dataset.host === mode);
  }
  $("cfg-host").dataset.mode = mode;
  $("cfg-host-hint").textContent = HOST_HINTS[mode];
  paintConnection(mode);
  $("cfg-host-adv").hidden = mode === "off" || mode === "agent";
  $("host-diag-row").hidden = mode === "off";
}
$("cfg-host").onclick = (event) => {
  const b = event.target.closest("button[data-host]");
  if (b) paintHostForm(b.dataset.host);
};
const hostBody = () => ({ game_host: $("cfg-host").dataset.mode || "off", steambox_container: $("cfg-container").value.trim(),
  docker_host: $("cfg-docker-host").value.trim(), steambox_roms: $("cfg-roms-in").value.trim(),
  game_user: $("cfg-game-user").value.trim(), session_process: $("cfg-session").value.trim(),
  agent_url: $("cfg-agent-url").value.trim(), agent_token: $("cfg-agent-token").value.trim(),
  agent_pem: state.agentPem || "" });

// Association d'un agent : certificat récupéré, empreinte à confirmer avant d'enregistrer
function agentReset() {
  state.agentPem = null;
  $("cfg-agent-confirm").checked = false;
  $("cfg-agent-confirm-row").hidden = true;
}
for (const id of ["cfg-agent-url", "cfg-agent-token"]) $(id).addEventListener("input", agentReset);
$("cfg-agent-probe").onclick = async () => {
  const result = $("cfg-agent-result");
  agentReset();
  result.className = "msg";
  result.replaceChildren(el("span", { className: "row" }, el("span", { className: "spinner" }), "connexion à l'agent…"));
  try {
    const r = await api("/api/config/agent/probe", { method: "POST",
      body: { url: $("cfg-agent-url").value.trim(), token: $("cfg-agent-token").value.trim() } });
    state.agentPem = r.pem;
    const who = r.info ? `${r.info.host} · utilisateur ${r.info.user} · agent ${r.info.version}`
      : "jeton non saisi : il ne peut pas être vérifié (il reste celui déjà enregistré, le cas échéant)";
    result.replaceChildren(el("div", {}, el("strong", { textContent: "Empreinte du certificat" })),
      el("div", { className: "mono", style: "font-size:12px;overflow-wrap:anywhere", textContent: r.fingerprint }),
      el("div", { className: "muted", style: "margin-top:6px", textContent: who }));
    $("cfg-agent-confirm-row").hidden = false;
  } catch (error) {
    result.className = "notice err";
    result.textContent = error.message;
  }
};

// ------------------------------------------------------------------ état du lanceur (--doctor)

async function loadDoctor(refresh = false) {
  const body = $("doc-body");
  if (refresh || !state.doctor) body.replaceChildren(el("div", { className: "row muted" }, el("span", { className: "spinner" }), `vérification sur ${hostName()}…`));
  $("doc-refresh").disabled = true;
  try {
    state.doctor = await api(`/api/launcher/doctor${refresh ? "?refresh=1" : ""}`);
    renderDoctor();
    runnerSuggestions();
  } catch (error) {
    body.replaceChildren(notice("", `État du lanceur indisponible : ${error.message}`, "alert"));
  } finally {
    $("doc-refresh").disabled = false;
  }
}

function renderDoctor() {
  const d = state.doctor;
  $("doc-age").textContent = d.age ? `il y a ${Math.round(d.age / 60)} min` : "à l'instant";
  const ok = !d.sections.some((s) => s.items.some((i) => i.status === "missing"));
  const marks = { ok: "check", missing: "x", optional: "minus" };
  $("doc-body").replaceChildren(
    ...d.sections.flatMap((section) => [
      section.title ? el("h4", { textContent: section.title }) : null,
      ...section.items.map((item) => el("div", { className: `item ${item.status}` },
        el("span", { className: "disc" }, icon(marks[item.status] || "info")),
        el("div", {}, el("span", { textContent: item.text }),
          item.details.length ? el("span", { className: "details", textContent: item.details.join("\n") }) : null)))]),
    d.verdict ? (ok ? el("div", { className: "verdict", textContent: d.verdict }) : notice("err", d.verdict)) : null);
}

// Runners installés sur la machine de jeu : proposés pour PROTON= et WINE=
function runnerSuggestions() {
  const names = [...new Set((state.doctor?.runners || []).map((r) => r.name))];
  for (const key of ["PROTON", "WINE"]) {
    const list = $(`v-${key}`);
    if (!list) continue;
    const have = new Set([...list.options].map((o) => o.value));
    for (const name of names) {
      const proton = /proton/i.test(name);
      if (!have.has(name) && (key === "PROTON") === proton) list.append(new Option(`${name} (${hostName()})`, name));
    }
  }
}
$("doc-refresh").onclick = () => loadDoctor(true);
$("host-diag").onclick = () => { $("dlg-doctor").showModal(); loadDoctor(true); };
$("host-check").onclick = () => loadHostStatus(true);

// Voyant : vert quand la machine de jeu répond, rouge sinon
async function loadHostStatus(refresh = false) {
  const box = $("host-status"), text = $("host-status-text");
  box.className = "host-status";
  text.textContent = "vérification…";
  try {
    const s = await api(`/api/game_host/status${refresh ? "?refresh=true" : ""}`);
    state.hostStatus = s;
    paintConnection();
    box.classList.add(s.state);
    text.textContent = s.state === "ok" ? `Connectée · ${s.name}`
      : s.state === "off" ? "Désactivée" : `Non connectée · ${s.detail}`;
    text.title = s.detail || "";
  } catch (error) {
    state.hostStatus = { state: "ko" };
    paintConnection();
    box.classList.add("ko");
    text.textContent = `Non connectée · ${error.message}`;
  }
}

$("btn-config").onclick = async () => {
  let cfg, systems = [], systemsError = null;
  try {
    cfg = await api("/api/config");
  } catch (error) {
    alert(`Réglages indisponibles : ${error.message}`);
    return;
  }
  try {
    systems = await api("/api/config/systems");
  } catch (error) {
    // Serveur plus ancien que la page : la fenêtre s'ouvre quand même, chemins à saisir
    systemsError = `Liste des systèmes indisponible (${error.message}) — chemins à saisir ci-dessous.`;
  }
  const chosen = new Set(cfg.roms_dirs.map((d) => d.replace(/\/+$/, "")));
  const roots = [...new Set(systems.map((x) => x.root))];
  const box = (path, label, detail, root = false) => {
    const input = el("input", { type: "checkbox", checked: chosen.has(path), value: path });
    input.dataset.path = path;
    return el("label", { className: root ? "root" : "", title: path }, input, el("span", { textContent: label }),
      detail ? el("span", { className: "n", textContent: detail }) : null);
  };
  $("cfg-systems").replaceChildren(
    ...roots.map((root) => box(root, `${root.split("/").filter(Boolean).pop() || root} (tout)`, "tous les systèmes", true)),
    ...systems.map((x) => box(x.path, x.name, [x.images && `${x.images}`, x.folders && `${x.folders} dossier(s)`].filter(Boolean).join(" · "))));
  const known = new Set([...roots, ...systems.map((x) => x.path)]);
  $("cfg-dirs").value = [...chosen].filter((d) => !known.has(d)).join("\n");
  $("cfg-info").replaceChildren(...[
    ["Sauvegardes", cfg.saves_dir], ["Dossier de travail", cfg.tmp_dir], ["Reconstruction", cfg.rebuild_mode],
    ["Rétention .old", cfg.old_retention_days == null ? "manuelle" : `${cfg.old_retention_days} jours`],
  ].flatMap(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: v ?? "—" })]));
  $("cfg-container").value = cfg.steambox_container || "";
  $("cfg-docker-host").value = cfg.docker_host || "";
  $("cfg-roms-in").value = cfg.steambox_roms || "";
  $("cfg-game-user").value = cfg.game_user || "";
  $("cfg-session").value = cfg.session_process ?? "";
  $("cfg-agent-url").value = cfg.agent_url || "";
  $("cfg-agent-token").value = "";
  $("cfg-agent-token").placeholder = cfg.agent_token_set ? "jeton enregistré : laisser vide pour le garder" : "affiché par « wsquashfs-launcher --pair »";
  agentReset();
  $("cfg-agent-result").className = "msg";
  $("cfg-agent-result").textContent = cfg.agent_fingerprint
    ? `Associé. Empreinte enregistrée : ${cfg.agent_fingerprint}` : "Pas encore associé.";
  paintHostForm(cfg.game_host || "off");
  $("doc-title").textContent = `wsquashfs-launcher --doctor — ${cfg.game_host_name}`;
  state.hostEditing = false;
  loadHostStatus();
  $("cfg-night-start").value = cfg.night_start || "01:00";
  $("cfg-night-end").value = cfg.night_end || "07:00";
  $("cfg-err").textContent = systemsError || "";
  $("cfg-err").hidden = !systemsError;
  if (!systems.length) $("cfg-dirs").closest("details").open = true;
  showCfgPane("dirs");
  $("dlg-config").showModal();
};
$("cfg-save").onclick = async () => {
  try {
    const dirs = [...$("cfg-systems").querySelectorAll("input:checked")].map((i) => i.dataset.path)
      .concat($("cfg-dirs").value.split("\n").map((d) => d.trim()).filter(Boolean));
    if (!dirs.length) throw new Error("Cocher au moins un système.");
    if ($("cfg-host").dataset.mode === "agent") {
      const url = $("cfg-agent-url").value.trim();
      const reuse = url === state.config?.agent_url && state.config?.agent_fingerprint && !state.agentPem
        && !$("cfg-agent-token").value.trim();
      if (!reuse && !(state.agentPem && $("cfg-agent-confirm").checked)) {
        throw new Error("Associer l'agent et confirmer son empreinte avant d'enregistrer.");
      }
    }
    state.config = await api("/api/config/night", { method: "PUT",
      body: { start: $("cfg-night-start").value, end: $("cfg-night-end").value } });
    const before = JSON.stringify([state.config.game_host, state.config.steambox_container, state.config.docker_host,
      state.config.steambox_roms, state.config.game_user, state.config.session_process]);
    state.config = await api("/api/config/game_host", { method: "PUT", body: hostBody() });
    if (JSON.stringify([state.config.game_host, state.config.steambox_container, state.config.docker_host,
      state.config.steambox_roms, state.config.game_user, state.config.session_process]) !== before) state.doctor = null;
    fillWhen();
    applyHost();
    loadHostStatus(true);
  } catch (error) {
    $("cfg-err").textContent = error.message;
    $("cfg-err").hidden = false;
    return;
  }
  // Le serveur lit les images des dossiers choisis avant de répondre : long
  // pour des dossiers nouveaux. Une erreur de saisie revient tout de suite ;
  // passé un instant, la fenêtre se ferme et le bandeau de la liste prend le relais.
  const dirs = [...$("cfg-systems").querySelectorAll("input:checked")].map((i) => i.dataset.path)
    .concat($("cfg-dirs").value.split("\n").map((d) => d.trim()).filter(Boolean));
  const save = $("cfg-save");
  save.disabled = true;
  save.replaceChildren(el("span", { className: "spinner" }), "Lecture des dossiers…");
  $("cfg-err").hidden = true;
  let done = null;
  const handOver = setTimeout(() => {
    $("dlg-config").close();
    showView("images");
    done = libBusy("Lecture des dossiers choisis",
      "Chaque nouvelle image est ouverte une fois pour lire son autorun et sa version : compter quelques secondes par image. Les prochaines ouvertures viendront du cache.");
  }, 1500);
  try {
    await api("/api/config/roms_dirs", { method: "PUT", body: { roms_dirs: dirs }, timeout: SCAN_TIMEOUT });
    clearTimeout(handOver);
    if ($("dlg-config").open) $("dlg-config").close();
    done?.();
    done = null;
    await loadLibrary();
    toast(`Réglages enregistrés : ${state.items.length} image(s)`);
  } catch (error) {
    clearTimeout(handOver);
    if ($("dlg-config").open) {
      $("cfg-err").textContent = error.message;
      $("cfg-err").hidden = false;
    } else {
      toast(`Réglages : ${error.message}`, "alert", true);
    }
  } finally {
    done?.();
    save.disabled = false;
    save.textContent = "Enregistrer";
  }
};
for (const button of document.querySelectorAll("[data-close]")) button.onclick = () => button.closest("dialog").close();

// ------------------------------------------------------------------ actions en masse

function opRow(op = { op: "set", key: "", value: "" }) {
  const kind = el("select", {}, new Option("Poser / modifier", "set"), new Option("Retirer", "remove"), new Option("Remplacer tout", "replace"));
  const key = el("input", { placeholder: "CLÉ", value: op.key || "", className: "code" });
  key.setAttribute("list", "known-keys");
  const value = el("input", { placeholder: "valeur", value: op.value || "", className: "code" });
  const template = el("textarea", { placeholder: "CMD=…\nDIR=…\nGAME_VERSION=…", value: op.template || "" });
  const remove = el("button", { className: "iconbtn", title: "Retirer l'opération" }, icon("x"));
  const row = el("div", { className: "op" }, kind, key, value, remove);
  const update = () => {
    const replace = kind.value === "replace";
    key.hidden = replace;
    value.hidden = replace;
    value.style.visibility = kind.value === "remove" ? "hidden" : "";   // garde la grille alignée
    if (replace && !template.isConnected) row.insertBefore(template, remove);
    if (!replace) template.remove();
    row.style.gridTemplateColumns = replace ? "11rem 1fr auto" : "";
    $("m-run").disabled = true;
  };
  kind.value = op.op;
  kind.onchange = update;
  for (const input of [key, value, template]) input.addEventListener("input", () => { $("m-run").disabled = true; });
  remove.onclick = () => { row.remove(); $("m-run").disabled = true; };
  row.read = () => kind.value === "replace" ? { op: "replace", template: template.value }
    : kind.value === "remove" ? { op: "remove", key: key.value.trim() }
    : { op: "set", key: key.value.trim(), value: value.value };
  update();
  return row;
}

const massBody = () => ({ ids: [...state.selected], ops: [...$("m-ops").children].map((row) => row.read()), when: whenOf(), target: $("m-target").value });

$("btn-mass").onclick = () => {
  $("m-count").textContent = state.selected.size;
  if (!$("m-ops").children.length) $("m-ops").append(opRow());
  $("m-result").replaceChildren();
  $("m-run").disabled = true;
  $("dlg-mass").showModal();
};
$("m-add").onclick = () => $("m-ops").append(opRow());
$("m-target").onchange = () => { $("m-run").disabled = true; };

function lineDiff(before, after) {
  const a = (before || "").replace(/\r\n/g, "\n").split("\n").filter(Boolean);
  const b = after.replace(/\r\n/g, "\n").split("\n").filter(Boolean);
  const out = el("div", { className: "diff" });
  for (const line of a) if (!b.includes(line)) out.append(el("div", { className: "del", textContent: `- ${line}` }));
  for (const line of b) if (!a.includes(line)) out.append(el("div", { className: "add", textContent: `+ ${line}` }));
  return out;
}
const spaceNotices = (space) => space.map((s) => notice(s.needed > s.free ? "err" : "info",
  `${s.path} : ${size(s.needed)} nécessaires (anciennes images gardées en .old), ${size(s.free)} libres`, "drive"));
const waiting = (text) => el("div", { className: "notice info" }, el("span", { className: "spinner" }), el("span", { textContent: text }));

$("m-preview").onclick = async () => {
  $("m-result").replaceChildren(waiting("Calcul de l'aperçu…"));
  try {
    const data = await api("/api/mass/autorun/preview", { method: "POST", body: massBody() });
    const items = data.items.map((item) => {
      const head = el("div", { className: "row" }, el("strong", { textContent: item.name || item.id }));
      if (item.error) head.append(badge(item.error, "err"));
      else if (!item.changed) head.append(badge("inchangé"));
      else if (item.blocked) head.append(badge(item.blocked, "warn"));
      else head.append(badge($("m-target").value === "override" ? "surcharge" : "à reconstruire", "ok"));
      const node = el("div", { className: "preview-item" }, head);
      if (item.changed) node.append(lineDiff(item.before, item.after));
      if (item.issues?.length) node.append(el("ul", { className: "issues" }, ...item.issues.map((x) => el("li", { textContent: x.message }))));
      return node;
    });
    const overrideMode = $("m-target").value === "override";
    $("m-result").replaceChildren(el("h3", { textContent: overrideMode
      ? `${data.ready} surcharge(s) à écrire — effet au prochain lancement, sans reconstruction`
      : `${data.ready} image(s) à reconstruire` }), ...spaceNotices(data.space), ...items);
    $("m-run").disabled = data.ready === 0;
    $("m-run").textContent = `Lancer ${data.ready} ${overrideMode ? "surcharge(s)" : "reconstruction(s)"}`;
  } catch (error) {
    $("m-result").replaceChildren(notice("err", error.message));
  }
};

$("m-run").onclick = async () => {
  try {
    const data = await api("/api/mass/autorun", { method: "POST", body: massBody() });
    const skipped = data.skipped.map((s) => el("li", { textContent: `${s.id} : ${s.reason}` }));
    const done = data.overrides ? `${data.overrides.length} surcharge(s) écrite(s) : effet au prochain lancement.` : `${data.tasks.length} tâche(s) créée(s).`;
    if (data.overrides) loadLibrary();
    $("m-result").replaceChildren(notice("ok", done, "check"), ...(skipped.length ? [el("ul", { className: "issues" }, ...skipped)] : []));
    $("m-run").disabled = true;
    if (!data.overrides) queued();
  } catch (error) {
    $("m-result").replaceChildren(notice("err", error.message));
  }
};

$("btn-mass-commit").onclick = () => commitOverrides([...state.selected]);

$("btn-mass-validate").onclick = async () => {
  const ids = [...state.selected].filter((id) => state.byId.get(id)?.has_old);
  if (!ids.length) { alert("Aucune image sélectionnée n'a de .old."); return; }
  if (!confirm(`Supprimer définitivement l'ancienne version de ${ids.length} image(s) ?`)) return;
  try {
    const data = await api("/api/mass/old/validate", { method: "POST", body: { ids } });
    if (data.skipped.length) alert(data.skipped.map((s) => `${s.id} : ${s.reason}`).join("\n"));
    toast("Anciennes versions supprimées");
  } catch (error) { alert(error.message); }
  loadLibrary();
};

// ------------------------------------------------------------------ fichiers et registre en masse

function fileOpRow(kind, values = {}) {
  const input = (placeholder, value = "", style = "") => el("input", { placeholder, value, style, className: "code" });
  const remove = el("button", { className: "iconbtn", title: "Retirer l'opération" }, icon("x"));
  let row, read;
  if (kind === "copy") {
    const dest = input("destination dans l'image, ex. {exe_dir}/dinput8.dll", values.dest);
    const source = input("fichier source sur le serveur, ex. /config/tools/dinput8.dll", values.source);
    row = el("div", { className: "op", style: "grid-template-columns:7rem 1fr 1fr auto" }, el("div", {}, badge("ajouter", "ok")), dest, source, remove);
    read = () => ({ copy: { dest: dest.value.trim(), source: source.value.trim() } });
  } else if (kind === "delete") {
    const path = input("chemin dans l'image", values.path);
    row = el("div", { className: "op", style: "grid-template-columns:7rem 1fr auto" }, el("div", {}, badge("supprimer", "err")), path, remove);
    read = () => ({ delete: path.value.trim() });
  } else {
    const file = el("select", {}, new Option("user.reg", "user.reg"), new Option("system.reg", "system.reg"));
    file.value = values.file || "user.reg";
    const key = input("clé, ex. Software\\Wine\\DllOverrides", values.key);
    const name = input("nom", values.name, "max-width:9rem");
    const value = input("valeur", values.value, "max-width:11rem");
    row = el("div", { className: "op", style: "grid-template-columns:7rem 1fr auto auto auto" }, file, key, name, value, remove);
    read = () => ({ reg: { file: file.value, key: key.value.trim(), name: name.value.trim(), value: value.value } });
  }
  remove.onclick = () => { row.remove(); $("f-run").disabled = true; };
  row.addEventListener("input", () => { $("f-run").disabled = true; });
  row.read = read;
  return row;
}

function filesBody() {
  const body = { ids: [...state.selected], copy: [], delete: [], reg: [], when: whenOf() };
  for (const row of $("f-ops").children) {
    const [kind, value] = Object.entries(row.read())[0];
    body[kind].push(value);
  }
  return body;
}

$("btn-files").onclick = () => {
  $("f-count").textContent = state.selected.size;
  $("f-result").replaceChildren();
  $("f-run").disabled = true;
  $("dlg-files").showModal();
};
for (const button of document.querySelectorAll("[data-add]")) {
  button.onclick = () => { $("f-ops").append(fileOpRow(button.dataset.add)); $("f-run").disabled = true; };
}
$("f-preset-fakeping").onclick = () => {
  const source = prompt("Chemin de dinput8.dll (relais fakeping compilé) sur le serveur :", storage.get("wsfs-fakeping") || "");
  if (source === null) return;
  storage.set("wsfs-fakeping", source.trim());
  $("f-ops").replaceChildren(
    fileOpRow("copy", { dest: "{exe_dir}/dinput8.dll", source: source.trim() }),
    fileOpRow("reg", { file: "user.reg", key: "Software\\Wine\\AppDefaults\\{exe_name}\\DllOverrides", name: "dinput8", value: "native,builtin" }));
  $("f-run").disabled = true;
};

const ACTIONS = { add: ["ajouté", "ok"], replace: ["remplacé", "warn"], delete: ["supprimé", "err"], reg: ["registre", "info"], same: ["identique", ""] };

$("f-preview").onclick = async () => {
  $("f-result").replaceChildren(waiting("Calcul de l'aperçu…"));
  try {
    const data = await api("/api/mass/files/preview", { method: "POST", body: filesBody() });
    const items = data.items.map((item) => {
      const head = el("div", { className: "row" }, el("strong", { textContent: item.name || item.id }));
      if (item.error) head.append(badge(item.error, "err"));
      else if (!item.changed) head.append(badge("inchangé"));
      else if (item.blocked) head.append(badge(item.blocked, "warn"));
      else head.append(badge("à appliquer", "ok"));
      const node = el("div", { className: "preview-item" }, head);
      for (const a of item.actions || []) {
        const [label, kind] = ACTIONS[a.action] || [a.action, ""];
        node.append(el("div", { className: "row mono" }, badge(label, kind), a.path, el("span", { className: "muted", textContent: a.detail })));
      }
      if (item.warnings?.length) node.append(el("ul", { className: "issues" }, ...item.warnings.map((w) => el("li", { textContent: w }))));
      return node;
    });
    $("f-result").replaceChildren(el("h3", { textContent: `${data.ready} image(s) à modifier` }), ...spaceNotices(data.space), ...items);
    $("f-run").disabled = data.ready === 0;
    $("f-run").textContent = `Lancer ${data.ready} tâche(s)`;
  } catch (error) {
    $("f-result").replaceChildren(notice("err", error.message));
  }
};

$("f-run").onclick = async () => {
  try {
    const data = await api("/api/mass/files", { method: "POST", body: filesBody() });
    const skipped = data.skipped.map((s) => el("li", { textContent: `${s.id} : ${s.reason}` }));
    $("f-result").replaceChildren(notice("ok", `${data.tasks.length} tâche(s) créée(s).`, "check"),
      ...(skipped.length ? [el("ul", { className: "issues" }, ...skipped)] : []));
    $("f-run").disabled = true;
    queued();
  } catch (error) {
    $("f-result").replaceChildren(notice("err", error.message));
  }
};

// ------------------------------------------------------------------ vérification, essai, sauvegardes

function reportSkipped(skipped) {
  if (skipped.length) alert(skipped.map((s) => `${s.id} : ${s.reason}`).join("\n"));
}

async function verify(ids) {
  const data = await api("/api/mass/verify", { method: "POST", body: { ids } });
  reportSkipped(data.skipped);
  queued("Vérification ajoutée à la file");
}

async function resetSaves(ids) {
  const names = ids.filter((id) => state.byId.get(id)?.saves_present);
  if (!names.length) { alert("Aucune image sélectionnée n'a de sauvegardes."); return; }
  if (!confirm(`Réinitialiser les sauvegardes de ${names.length} jeu(x) ? Elles sont renommées en <jeu>.avant-<date>, pas supprimées.`)) return;
  const data = await api("/api/mass/saves/reset", { method: "POST", body: { ids: names } });
  reportSkipped(data.skipped);
  toast("Sauvegardes mises de côté", "save");
  await loadLibrary();
}

$("d-trial").onclick = async () => {
  const duration = Number($("d-trial-duration").value);
  if (!confirm(`Lancer « ${state.current.name} » sur ${hostName()} pendant ${duration} s ? Le jeu s'affiche sur l'écran de la session : ne rien lancer pendant ce temps. Il est ensuite arrêté.`)) return;
  try {
    await api(`/api/image/trial?id=${encodeURIComponent(state.current.id)}`, { method: "POST", body: { duration, when: "now" } });
    queued("Lancement d'essai ajouté à la file");
  } catch (error) { alert(error.message); }
};
$("d-verify").onclick = () => verify([state.current.id]).catch((error) => alert(error.message));
$("d-saves-reset").onclick = () => resetSaves([state.current.id]).catch((error) => alert(error.message));
$("d-saves-copy").onclick = async () => {
  const button = $("d-saves-copy");
  button.disabled = true;
  try {
    const r = await api("/api/game_host/maintenance", { method: "POST", timeout: 900000,
      body: { action: "saves", id: state.current.id } });
    const out = (r.output || "").split("\n").filter((l) => l.startsWith("  ") && !l.startsWith("  dans ")).map((l) => l.trim()).join("\n");
    if (r.code !== 0) alert(`Copie des sauvegardes : ${(r.output || "").trim() || `code ${r.code}`}`);
    else toast(out || "Sauvegardes copiées", "save");
  } catch (error) { alert(error.message); } finally { button.disabled = false; }
};
$("btn-verify").onclick = () => verify([...state.selected]).catch((error) => alert(error.message));
$("btn-saves-reset").onclick = () => resetSaves([...state.selected]).catch((error) => alert(error.message));

// Info-bulle : délégation sur tout élément [data-tip], positionnée sous lui
{
  const tip = $("tip");
  const show = (target) => {
    tip.replaceChildren(el("strong", { textContent: target.dataset.tipTitle || "" }),
      ...target.dataset.tip.split("\n").filter(Boolean).map((line) => el("span", { className: /^[A-Z_]+=/.test(line) ? "code" : "", textContent: line })));
    tip.hidden = false;
    const r = target.getBoundingClientRect(), t = tip.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left), innerWidth - t.width - 8);
    const below = r.bottom + 6 + t.height < innerHeight;
    tip.style.left = `${left}px`;
    tip.style.top = `${below ? r.bottom + 6 : r.top - t.height - 6}px`;
  };
  const hide = () => { tip.hidden = true; };
  for (const type of ["mouseover", "focusin"]) document.addEventListener(type, (event) => {
    const target = event.target.closest?.("[data-tip]");
    target ? show(target) : hide();
  });
  for (const type of ["mouseout", "focusout"]) document.addEventListener(type, (event) => {
    if (event.target.closest?.("[data-tip]") && !event.relatedTarget?.closest?.("[data-tip]")) hide();
  });
  addEventListener("scroll", hide, true);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") hide(); });
}

// Liens du runner : constat et réparation proposée (runner de la machine de jeu joint ou complété)
$("d-links").onclick = async () => {
  const id = state.current.id, box = $("d-links-result");
  box.className = "msg";
  box.replaceChildren(el("span", { className: "row" }, el("span", { className: "spinner" }), `analyse de l'image${hostOn() ? ` et des runners de ${hostName()}` : ""}…`));
  $("d-links").disabled = true;
  try {
    const r = await api(`/api/image/links?id=${encodeURIComponent(id)}`);
    if (state.current?.id !== id) return;
    if (!r.issues.length) {
      box.replaceChildren(notice("ok", "Aucun lien vers un runner hors de l'image ou absent : image autonome.", "check"));
      return;
    }
    box.replaceChildren(
      el("ul", { className: "issues" }, ...r.issues.map((i) => el("li", { className: i.level }, i.message, " ", helpLink("h-runner")))),
      ...r.suggestions.map((sug) => {
        const apply = el("button", {}, icon("clip", "sm"), sug.action === "join" ? "Joindre le runner" : "Compléter le runner");
        apply.onclick = () => applyRunner(id, sug, apply);
        return el("div", { className: "suggestion" },
          el("strong", { textContent: sug.title }),
          el("p", { textContent: `${sug.detail}. ${sug.rewritten ? `${sug.rewritten} lien(s) réécrit(s) en relatif, ` : ""}${sug.repaired} réparé(s), ${sug.remaining} restant(s) ; ${size(sug.size)} ajoutés à l'image (avant compression).` }),
          ...(sug.warnings || []).map((w) => notice("", w, "alert")),
          el("div", { className: "split", style: "margin-top:8px" }, apply, whenButton()));
      }),
      ...(r.suggestions.length ? [] : [r.steambox_error
        ? (hostOn() ? notice("err", `Réparation impossible, ${hostName()} injoignable : ${r.steambox_error}`)
          : notice("", "Réparation automatique indisponible : elle reprend un runner installé sur la machine de jeu (Réglages → Machine de jeu). Sinon, joindre le runner à la main (voir l'aide).", "info"))
        : el("p", { className: "muted", textContent: `Aucun runner de ${hostName()} ne contient les fichiers visés : joindre le runner à la main (voir l'aide).` })]));
  } catch (error) {
    box.replaceChildren(notice("err", error.message));
  } finally {
    $("d-links").disabled = false;
  }
};

async function applyRunner(id, sug, button) {
  const what = sug.action === "join"
    ? `copier ${sug.runner} (${size(sug.size)}) dans runners/${sug.dest}/ et poser ${sug.key}=${sug.dest}`
    : `copier ${sug.sub} de ${sug.runner} (${size(sug.size)}) dans runners/${sug.dest}/`;
  if (!confirm(`Reconstruire « ${state.current.name} » : ${what}, ${sug.rewritten} lien(s) réécrit(s). L'original est gardé en .old.`)) return;
  button.disabled = true;
  try {
    await api(`/api/image/runner?id=${encodeURIComponent(id)}`, { method: "POST", body: {
      source: sug.source, sub: sug.sub, dest: sug.dest, set_key: sug.set_key, when: whenOf() } });
    button.replaceChildren(icon("check", "sm"), "Envoyé dans la file");
    queued();
  } catch (error) {
    button.disabled = false;
    alert(error.message);
  }
}

// ------------------------------------------------------------------ vues

let lastView = "images";       // vue à retrouver en refermant l'aide
function showView(name) {
  document.body.dataset.view = name;
  for (const b of $("views").children) b.classList.toggle("active", b.dataset.view === name);
  $("main").hidden = name !== "images";
  $("folders-main").hidden = name !== "folders";
  $("help-main").hidden = name !== "help";
  $("btn-help").classList.toggle("active", name === "help");
  $("btn-help").setAttribute("aria-pressed", name === "help");
  if (name !== "help") lastView = name;
  storage.set("wsfs-view", name);
  if (name === "folders") loadFolders();
  if (name === "help") return loadHelp();
  return Promise.resolve();
}
$("views").onclick = (event) => {
  const button = event.target.closest("button[data-view]");
  if (button) { showView(button.dataset.view); window.scrollTo(0, 0); }
};
$("btn-help").onclick = () => {
  if ($("help-main").hidden) { showView("help"); window.scrollTo(0, 0); } else showView(lastView);
};

// ------------------------------------------------------------------ aide
// Fragment assets/help.html : une page à la fois (menu de gauche) ; la
// recherche parcourt toutes les pages et ne garde que les blocs [data-f].

let helpLoading = null;
function loadHelp() {
  if (!helpLoading) {
    helpLoading = fetch("assets/help.html", { cache: "no-cache" })
      .then((r) => { if (!r.ok) throw new Error(`erreur ${r.status}`); return r.text(); })
      .then((html) => { $("help-main").innerHTML = html; setupHelp(); })
      .catch((error) => {
        helpLoading = null;
        $("help-main").replaceChildren(notice("err", `Aide indisponible : ${error.message}`));
      });
  }
  return helpLoading;
}

function showHelpPage(page) {
  for (const b of document.querySelectorAll(".help-nav button")) b.classList.toggle("on", b.dataset.page === page);
  for (const p of document.querySelectorAll(".hpage")) p.hidden = p.dataset.page !== page;
  storage.set("wsfs-help-page", page);
}

async function openHelp(anchor) {
  await showView("help");
  if ($("help-q")?.value) { $("help-q").value = ""; filterHelp(""); }
  const target = $(anchor) || (anchor.startsWith("hk-") ? $("h-keys") : null);
  if (!target) return;
  const page = target.closest(".hpage");
  if (page) showHelpPage(page.dataset.page);
  if (target.tagName === "DETAILS") target.open = true;
  target.scrollIntoView({ block: target.matches("details.key") ? "center" : "start" });
  target.classList.remove("flash");
  void target.offsetWidth;          // relance l'animation
  target.classList.add("flash");
}

function setupHelp() {
  showHelpPage(storage.get("wsfs-help-page") || "start");
  for (const b of document.querySelectorAll(".help-nav button")) {
    b.onclick = () => {
      if ($("help-q").value) { $("help-q").value = ""; filterHelp(""); }
      showHelpPage(b.dataset.page);
      window.scrollTo(0, 0);
    };
  }
  // Liens internes : ouvrent la bonne page, sans toucher à l'adresse
  $("help-main").addEventListener("click", (event) => {
    const a = event.target.closest('a[href^="#"]');
    if (!a) return;
    event.preventDefault();
    openHelp(a.getAttribute("href").slice(1));
  });
  // Un seul guide ouvert à la fois
  $("help-main").addEventListener("toggle", (event) => {
    const d = event.target;
    if (!d.open || !d.matches?.("details.guide") || $("help-q").value) return;
    for (const other of d.parentElement.querySelectorAll(":scope > details.guide[open]")) if (other !== d) other.open = false;
  }, true);
  let timer = null;
  $("help-q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => filterHelp($("help-q").value), 150); });
}

function filterHelp(raw) {
  const q = raw.trim().toLowerCase();
  const body = $("help-body");
  const items = [...body.querySelectorAll("[data-f]")];
  const pages = [...body.querySelectorAll(".hpage")];
  body.classList.toggle("searching", Boolean(q));
  if (!q) {
    for (const item of items) item.hidden = false;
    for (const h of body.querySelectorAll(".hpage > h2, .hpage > p:not([data-f]), .hpage > .lead")) h.hidden = false;
    showHelpPage(storage.get("wsfs-help-page") || "start");
    $("help-nores").hidden = true;
    return;
  }
  let any = false;
  for (const item of items) {
    const match = item.textContent.toLowerCase().includes(q);
    item.hidden = !match;
    if (match && item.tagName === "DETAILS") item.open = true;
  }
  // Titres et introductions masqués pendant la recherche ; une page sans résultat disparaît
  for (const h of body.querySelectorAll(".hpage > h2, .hpage > p:not([data-f]), .hpage > .lead")) h.hidden = true;
  for (const page of pages) {
    const shown = page.querySelector("[data-f]:not([hidden])");
    page.hidden = !shown;
    any ||= Boolean(shown);
  }
  $("help-nores").hidden = any;
}

// ------------------------------------------------------------------ dossiers à empaqueter

const folders = { items: [], selected: new Set(), state: "", system: "",
  layout: storage.get("wsfs-folder-layout") === "grid" ? "grid" : "list", sort: { key: "folder", dir: 1 } };

async function loadFolders() {
  try {
    folders.items = await api("/api/folders");
  } catch (error) {
    $("g-rows").replaceChildren(notice("err", error.message));
    return;
  }
  $("count-folders").textContent = folders.items.filter((f) => !f.image_exists).length || "";
  const ids = new Set(folders.items.map((f) => f.id));
  for (const id of [...folders.selected]) if (!ids.has(id)) folders.selected.delete(id);
  renderFolders();
}

function visibleFolders() {
  const q = $("g-search").value.trim().toLowerCase(), { system, state: st } = folders;
  return folders.items.filter((f) => (!q || f.folder.toLowerCase().includes(q)) && (!system || f.system === system)
    && (st !== "todo" || !f.image_exists) && (st !== "done" || f.image_exists)
    && (st !== "noautorun" || !f.has_autorun))
    .sort((a, b) => a.folder.localeCompare(b.folder, "fr", { numeric: true, sensitivity: "base" }));
}

// État d'un dossier : libellé et classe de pastille (.state), rang pour le tri
function folderStatus(f) {
  if (f.task) return f.task === "running" ? ["empaquetage en cours", "in_use", 0] : ["en file", "in_use", 1];
  return f.image_exists ? ["déjà empaqueté", "ok", 3] : ["à empaqueter", "modified", 2];
}

function folderProfile(f) {
  return el("div", { className: "profile" },
    f.task ? dotIc("clock", "t-accent", f.task === "running" ? "Empaquetage en cours" : "Empaquetage en file") : null,
    dotIc("terminal", f.has_autorun ? "t-sage" : "t-accent", f.has_autorun ? "autorun.cmd présent" : "Sans autorun : il sera proposé"),
    dotIc("layers", f.image_exists ? "t-sage" : "t-off", f.image_exists ? "Image .wsquashfs déjà présente" : "Pas encore d'image"));
}

function toggleFolder(f) {
  folders.selected.has(f.id) ? folders.selected.delete(f.id) : folders.selected.add(f.id);
  renderFolders();
}

function renderFolderList(visible) {
  const { key, dir } = folders.sort;
  const text = (f) => key === "system" ? `${f.system || ""} ${f.folder}` : f.folder;
  const rows = [...visible].sort((a, b) => (key === "state" ? folderStatus(a)[2] - folderStatus(b)[2] : 0) * dir
    || text(a).localeCompare(text(b), "fr", { numeric: true, sensitivity: "base" }) * (key === "state" ? 1 : dir));
  const all = el("input", { type: "checkbox", checked: rows.length > 0 && rows.every((f) => folders.selected.has(f.id)),
    title: "Tout sélectionner (filtre actuel)" });
  all.onchange = () => { rows.forEach((f) => all.checked ? folders.selected.add(f.id) : folders.selected.delete(f.id)); renderFolders(); };
  const sortBtn = (k, label, cls = "") => {
    const b = el("button", { className: cls, textContent: label + (key === k ? (dir > 0 ? " ↑" : " ↓") : "") });
    b.onclick = () => { folders.sort = { key: k, dir: key === k ? -dir : 1 }; renderFolders(); };
    return b;
  };
  $("g-thead").replaceChildren(all, sortBtn("folder", "Dossier"), sortBtn("system", "Système"),
    el("span", { className: "c-profile", textContent: "Contenu", title: "autorun.cmd, image .wsquashfs" }), sortBtn("state", "État", "c-state"));
  $("g-list-rows").replaceChildren(...rows.map((f) => {
    const selected = folders.selected.has(f.id), [label, cls] = folderStatus(f);
    const box = el("input", { type: "checkbox", checked: selected, title: "Sélectionner" });
    box.onclick = (event) => event.stopPropagation();
    box.onchange = () => toggleFolder(f);
    const row = el("div", { className: `grow frow item${selected ? " selected" : ""}` },
      box,
      el("div", { className: "gname" }, el("span", { className: "thumb icon" }, icon("folder")),
        el("div", { className: "t" }, el("span", { className: "n", textContent: f.folder, title: f.folder }),
          el("span", { className: "sub" }, el("span", { textContent: `.${f.kind}` })))),
      el("span", { className: "c-system", textContent: cap(f.system) || "—" }),
      folderProfile(f),
      el("span", { className: `state ${cls}`, textContent: label, title: label }));
    row.onclick = () => toggleFolder(f);
    return row;
  }));
  if (!rows.length) $("g-list-rows").append(folders.items.length
    ? emptyState("search", "Aucun dossier pour ces filtres", null)
    : emptyState("folder", "Aucun dossier à empaqueter", "Les dossiers *.pc et *.wine des systèmes choisis dans « Réglages » apparaissent ici."));
}

function renderFolders() {
  const systems = [...new Set(folders.items.map((f) => f.system).filter(Boolean))].sort();
  if (folders.system && !systems.includes(folders.system)) folders.system = "";
  $("g-system").hidden = systems.length < 2;
  $("g-system").replaceChildren(...[["", "Tous"], ...systems.map((s) => [s, cap(s)])].map(([value, label]) => {
    const b = el("button", { className: folders.system === value ? "on" : "", textContent: label });
    b.onclick = () => { folders.system = value; renderFolders(); };
    return b;
  }));
  for (const b of $("g-state").children) b.classList.toggle("on", b.dataset.state === folders.state);
  const visible = visibleFolders(), list = folders.layout === "list";
  for (const b of $("g-layout").children) b.classList.toggle("on", b.dataset.layout === folders.layout);
  $("g-list").hidden = !list;
  $("g-rows").hidden = list;
  $("g-all-label").hidden = list;              // en liste : case de l'en-tête
  if (list) {
    $("g-rows").replaceChildren();
    renderFolderList(visible);
    renderFolderSelection();
    return;
  }
  $("g-list-rows").replaceChildren();
  $("g-all").checked = visible.length > 0 && visible.every((f) => folders.selected.has(f.id));
  $("g-rows").replaceChildren(...visible.map((f) => {
    const selected = folders.selected.has(f.id);
    const status = folderStatus(f)[0];
    const card = el("div", { className: `fcard${selected ? " selected" : ""}${f.image_exists ? " done" : ""}`, tabIndex: 0, role: "checkbox" },
      el("span", { className: "disc" }, icon(selected || f.image_exists ? "check" : "folder")),
      el("div", { className: "t" }, el("div", { className: "n", textContent: f.folder, title: f.folder }),
        el("div", { className: "sub", textContent: `${cap(f.system) || "—"} · .${f.kind} · ${status}` })),
      folderProfile(f));
    card.setAttribute("aria-checked", selected);
    const toggle = () => toggleFolder(f);
    card.onclick = toggle;
    card.onkeydown = (event) => { if (event.key === " " || event.key === "Enter") { event.preventDefault(); toggle(); } };
    return card;
  }));
  if (!visible.length) $("g-rows").append(el("div", { className: "fcard-empty" }, folders.items.length
    ? emptyState("search", "Aucun dossier pour ces filtres", null)
    : emptyState("folder", "Aucun dossier à empaqueter", "Les dossiers *.pc et *.wine des systèmes choisis dans « Réglages » apparaissent ici.")));
  renderFolderSelection();
}

function renderFolderSelection() {
  $("g-selection").hidden = folders.selected.size === 0;
  $("g-count").textContent = folders.selected.size > 1 ? `${folders.selected.size} dossiers` : "1 dossier";
}

$("g-search").addEventListener("input", renderFolders);
$("g-layout").onclick = (event) => {
  const b = event.target.closest("button[data-layout]");
  if (!b) return;
  folders.layout = b.dataset.layout;
  storage.set("wsfs-folder-layout", folders.layout);
  renderFolders();
};
$("g-state").onclick = (event) => {
  const b = event.target.closest("button[data-state]");
  if (b) { folders.state = b.dataset.state; renderFolders(); }
};
$("g-all").onchange = () => { visibleFolders().forEach((f) => $("g-all").checked ? folders.selected.add(f.id) : folders.selected.delete(f.id)); renderFolders(); };
$("g-clear").onclick = () => { folders.selected.clear(); renderFolders(); };

$("g-delete").onclick = async () => {
  const ids = folders.items.filter((f) => folders.selected.has(f.id) && f.image_exists).map((f) => f.id);
  if (!ids.length) { alert("Aucun dossier sélectionné n'a encore son image .wsquashfs."); return; }
  if (!confirm(`Supprimer DÉFINITIVEMENT ${ids.length} dossier(s) déjà empaqueté(s) ? L'image de chacun est vérifiée avant.`)) return;
  try {
    const data = await api("/api/folders/delete", { method: "POST", body: { ids } });
    reportSkipped(data.skipped);
  } catch (error) { alert(error.message); }
  loadFolders();
};

// Revue : une carte par dossier, analysée l'une après l'autre
const review = { cards: [] };

function renderCard(card) {
  const r = card.report, node = card.node;
  const include = el("input", { type: "checkbox", checked: card.include, disabled: card.sent, title: "Inclure ce dossier" });
  include.onchange = () => { card.include = include.checked; node.classList.toggle("off", !card.include); updatePackButton(); };
  const head = el("div", { className: "head" }, include, el("strong", { textContent: card.folder.folder }),
    badge(r.prefix === "none" ? "jeu seul" : `prefix ${r.prefix}${r.arch ? " " + r.arch : ""}`, r.prefix === "none" ? "" : "violet"),
    r.engine ? badge(r.engine, "info") : null,
    el("span", { className: "muted", style: "font-size:12.5px", textContent: `${size(r.size)} · ${r.file_count} fichiers` }),
    card.folder.image_exists ? badge("image déjà présente", "err") : null,
    card.sent ? badge("envoyé dans la file", "ok") : null);
  node.replaceChildren(head);
  node.classList.toggle("off", !card.include);

  const choice = el("div", { className: "row" });
  const radio = (value, label, disabled = false) => {
    const input = el("input", { type: "radio", name: `src-${card.folder.id}`, value, checked: card.source === value, disabled });
    input.onchange = () => { card.source = value; renderCard(card); updatePackButton(); };
    return el("label", {}, input, label);
  };
  choice.append(radio("existing", "Autorun existant", !r.existing), radio("generated", "Autorun proposé"),
    el("span", { className: "muted", style: "font-size:12.5px", textContent: `recommandé : ${r.recommended === "existing" ? "existant" : "proposé"}` }));
  node.append(choice);

  const left = el("div"), right = el("div");
  if (card.source === "existing" && r.existing) {
    left.append(el("pre", { className: "bat", textContent: r.existing.text }));
    if (r.existing.issues.length) left.append(el("ul", { className: "issues" }, ...r.existing.issues.map((i) => el("li", { className: i.level, textContent: i.message }))));
  } else {
    const select = el("select", {}, ...r.candidates.map((c) => new Option(`${c.path}  (score ${c.score}${c.bits ? `, ${c.bits} bits` : ""}${c.version ? `, v${c.version}` : ""})`, c.path)));
    select.value = r.chosen || "";
    select.onchange = () => analyzeCard(card, select.value);
    const text = el("textarea", { value: card.text, spellcheck: false });
    text.oninput = () => { card.text = text.value; updatePackButton(); };
    left.append(r.candidates.length ? el("label", { className: "muted" }, "Exécutable ", select) : notice("err", "Aucun exécutable Windows : jeu absent ou version Linux ?"), text);
    const issues = r.generated.issues;     // validation de la proposition d'origine
    if (issues.length) left.append(el("ul", { className: "issues" }, ...issues.map((i) => el("li", { className: i.level, textContent: i.message }))));
  }
  right.append(proposalItems(r.generated.items));
  if (r.generated.warnings.length) right.append(el("ul", { className: "issues" }, ...r.generated.warnings.map((w) => el("li", { textContent: w }))));
  for (const bat of r.bats) {
    right.append(el("div", { className: "muted mono", style: "font-size:12px", textContent: bat.path }), el("pre", { className: "bat", textContent: bat.content }));
    if (bat.suspicious.length) right.append(el("ul", { className: "issues" }, ...bat.suspicious.map((x) => el("li", { textContent: x }))));
  }
  if (r.excluded.length) right.append(el("details", {}, el("summary", { textContent: `${r.excluded.length} exécutable(s) écarté(s)` }),
    el("ul", { className: "items", style: "margin-top:6px" }, ...r.excluded.map((e) => el("li", { textContent: `${e.path} — ${e.reason}` })))));
  node.append(el("div", { className: "cols" }, left, right));
  if (r.link_issues?.length) node.append(el("ul", { className: "issues" },
    ...r.link_issues.map((i) => el("li", { className: i.level }, i.message, " ", helpLink("h-runner")))));
}

async function analyzeCard(card, exe = null) {
  card.node.replaceChildren(el("div", { className: "head" }, el("span", { className: "spinner" }), el("strong", { textContent: card.folder.folder }), el("span", { className: "muted", textContent: "analyse…" })));
  try {
    const r = await api(`/api/folders/analyze?id=${encodeURIComponent(card.folder.id)}${exe ? `&exe=${encodeURIComponent(exe)}` : ""}`);
    card.report = r;
    card.text = r.generated.text;
    if (!exe) card.source = r.recommended;
    card.include = !card.folder.image_exists && !card.folder.task && (card.source === "existing" || /^CMD=/m.test(card.text));
    renderCard(card);
  } catch (error) {
    card.include = false;
    card.node.replaceChildren(notice("err", `${card.folder.folder} : ${error.message}`));
  }
  updatePackButton();
}

function updatePackButton() {
  const ready = review.cards.filter((c) => c.include && !c.sent && c.report && (c.source === "existing" || /^CMD=/m.test(c.text)));
  $("p-run").disabled = ready.length === 0;
  $("p-run").replaceChildren(icon("package", "sm"), `Empaqueter ${ready.length} dossier(s)`);
  return ready;
}

$("g-review").onclick = async () => {
  const selected = folders.items.filter((f) => folders.selected.has(f.id));
  $("p-count").textContent = selected.length;
  $("p-result").replaceChildren();
  review.cards = selected.map((folder) => ({ folder, node: el("div", { className: "pcard" }), include: false, source: "generated", text: "" }));
  $("p-cards").replaceChildren(...review.cards.map((c) => c.node));
  updatePackButton();
  $("dlg-pack").showModal();
  for (const [index, card] of review.cards.entries()) {
    if (!$("dlg-pack").open) break;
    $("p-progress").textContent = `analyse ${index + 1}/${review.cards.length}…`;
    await analyzeCard(card);
  }
  $("p-progress").textContent = "";
};

$("p-delete").onchange = () => {
  if ($("p-delete").checked && !confirm("Supprimer chaque dossier une fois son image créée et vérifiée ? Sans retour possible.")) $("p-delete").checked = false;
};

$("p-run").onclick = async () => {
  // autorun null : celui du dossier est gardé tel quel
  const cards = updatePackButton();
  const items = cards.map((c) => ({ id: c.folder.id, autorun: c.source === "existing" ? null : c.text }));
  try {
    const data = await api("/api/folders/pack", { method: "POST", body: { items, rename_wine: $("p-rename").checked, delete_source: $("p-delete").checked, when: whenOf() } });
    const skipped = data.skipped.map((s) => el("li", { textContent: `${s.id} : ${s.reason}` }));
    // Un dossier envoyé est renommé (.pc → .wine) : ne plus le renvoyer
    const refused = new Set(data.skipped.map((s) => s.id));
    for (const card of cards) {
      if (!refused.has(card.folder.id)) { card.sent = true; card.include = false; renderCard(card); }
    }
    updatePackButton();
    $("p-result").replaceChildren(notice("ok", `${data.tasks.length} empaquetage(s) ajouté(s) à la file.`, "check"),
      ...(skipped.length ? [el("ul", { className: "issues" }, ...skipped)] : []));
    queued();
    loadFolders();
  } catch (error) {
    $("p-result").replaceChildren(notice("err", error.message));
  }
};

// ------------------------------------------------------------------ maintenant / cette nuit
// Un seul choix pour toute la page (mémorisé) : chaque bouton .when le bascule.

const whenOf = () => storage.get("wsfs-when") === "night" ? "night" : "now";
function paintWhen(button) {
  const night = whenOf() === "night";
  const range = state.config ? ` (${state.config.night_start}–${state.config.night_end})` : "";
  button.replaceChildren(icon(night ? "moon" : "zap", "sm"));
  button.title = night ? `Programmé cette nuit${range} — cliquer pour lancer maintenant` : `Lancé maintenant — cliquer pour programmer cette nuit${range}`;
  button.setAttribute("aria-label", night ? `Quand : cette nuit${range}` : "Quand : maintenant");
}
function whenButton() {
  const button = el("button", { className: "when", type: "button" });
  paintWhen(button);
  return button;
}
function fillWhen() {
  for (const button of document.querySelectorAll("button.when")) paintWhen(button);
}
document.addEventListener("click", (event) => {
  const button = event.target.closest("button.when");
  if (!button) return;
  event.stopPropagation();
  storage.set("wsfs-when", whenOf() === "night" ? "now" : "night");
  fillWhen();
  toast(whenOf() === "night" ? "Les prochaines actions seront programmées cette nuit" : "Les prochaines actions partent tout de suite",
    whenOf() === "night" ? "moon" : "zap");
});

// ------------------------------------------------------------------ démarrage

(async () => {
  fillWhen();
  renderFilters();
  state.keys = await api("/api/autorun/keys");
  try { state.config = await api("/api/config"); } catch { state.config = null; }
  fillWhen();
  applyHost();
  $("known-keys").replaceChildren(...state.keys.map((k) => new Option(k.key, k.key)));
  buildForm();
  await loadLibrary();
  refreshTasks();
  if (["folders", "help"].includes(storage.get("wsfs-view"))) showView(storage.get("wsfs-view"));
})();
