"use strict";
// wsquashfs-manager — interface (aucune étape de build : chargé tel quel par index.html).
// L'aide est un fragment séparé (assets/help.html), chargé à sa première ouverture.

// ------------------------------------------------------------------ état, outils

const state = {
  items: [], byId: new Map(), selected: new Set(), current: null,
  sort: { key: "name", dir: 1 }, keys: [], tasks: [],
  filter: { system: "", type: "", runner: "", pad: "", extra: "", special: "", inst: "" },
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
  // Machines associées (état de l'installation, voir « Installation sur les machines »)
  { key: "inst", label: "Installé sur…", all: "Quelle que soit la machine", options: () => instFilterOptions() },
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
function placeMenu(menu, wrap) {
  wrap.append(menu);
  menu.dataset.fixed = "1";
  positionMenu(menu, wrap);
}
addEventListener("scroll", () => {
  if (!openMenu?.dataset.fixed) return;
  const wrap = openMenu.parentElement, r = wrap.getBoundingClientRect();
  if (r.bottom < 0 || r.top > innerHeight) closeMenu();
  else positionMenu(openMenu, wrap);
}, true);

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
      // Menu plus large que l'espace à droite (téléphone, dernier filtre) : décalé pour rester à l'écran
      const wrapLeft = wrap.getBoundingClientRect().left;
      openMenu.style.left = `${Math.max(8 - wrapLeft, Math.min(0, innerWidth - 8 - wrapLeft - openMenu.offsetWidth))}px`;
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
    case "inst": return value === "none" ? isWsq(i) && installedOn(imageFile(i)).length === 0 : installedOn(imageFile(i)).includes(value);
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
        el("div", { className: "gart" }, thumb(i, "gcover"), selectBox(i), stateBadge(i), profileBadges(i), instPins(i)),
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
          el("span", { className: "sub" }, versionLabel(i), el("span", { textContent: "·" }), el("span", { textContent: cap(i.system) || "—" }), instPins(i))),
          sv.machines.length ? savesOpen(i, "sv-open-alt") : null),
        profileBadges(i),
        el("span", { className: "gsize", textContent: size(i.size) }),
        savesCell(i),
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
  renderInstBox();
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
  syncTaskList($("d-history"), tasks, el("p", { className: "muted", textContent: "Aucune opération enregistrée." }));
}

// ------------------------------------------------------------------ tâches

const TASK_KINDS = { autorun: ["hammer", "Reconstruction"], update: ["upload", "Montée de version"], files: ["file", "Fichiers et registre"],
  verify: ["shield", "Vérification"], pack: ["package", "Empaquetage"], runner: ["clip", "Runner joint"], trial: ["flask", "Lancement d'essai"],
  saves: ["save", "Sauvegardes entre machines"], install: ["drive", "Installation sur les machines"] };
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
      el("div", { className: "muted phase", style: "font-size:11.5px", hidden: !(t.status === "running" && t.phase), textContent: t.phase || "" }),
      t.error ? el("div", { className: "err-line", textContent: t.error }) : null),
    el("span", { className: "st", title: TASK_STATES[t.status]?.[1] || t.status }, icon(stIcon, `sm${t.status === "running" ? " spinning" : ""}`),
      el("span", { className: "st-label", textContent: stLabel })));
  node.dataset.id = t.id;
  node.dataset.key = taskKey(t);
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
  if (t.kind === "install" && t.params?.result) more.append(installResultNode(t.params.result));
  if ((t.log || []).length) more.append(el("pre", { className: "log", textContent: t.log.join("\n") }));
  node.append(more);
  node.onclick = (event) => { if (!event.target.closest(".more")) node.classList.toggle("open"); };
  return node;
}

// Ce qui oblige à reconstruire le nœud d'une tâche ; le reste (progression, phase) est mis à
// jour en place, sans recréer le nœud : sinon l'icône animée redémarre et la liste clignote
// à chaque relecture (toutes les 1,5 s pendant un transfert).
function taskKey(t) {
  return JSON.stringify([t.status, t.night, t.image, t.title, t.error, (t.log || []).length,
    Boolean(t.params?.result), state.config?.night_start]);
}
function updateTaskNode(node, t) {
  const progress = t.status === "done" ? 100 : Math.round((t.progress || 0) * 100);
  node.querySelector(".bar > div").style.width = `${progress}%`;
  if (t.status === "running") node.querySelector(".st-label").textContent = `${progress} %`;
  const phase = node.querySelector(".phase");
  phase.hidden = !(t.status === "running" && t.phase);
  if (phase.textContent !== (t.phase || "")) phase.textContent = t.phase || "";
}
// Liste de tâches réconciliée : nœuds gardés quand c'est la même tâche dans le même état ;
// la liste n'est réordonnée que si l'ordre ou le contenu change.
function syncTaskList(box, tasks, empty) {
  if (!tasks.length) {
    if (!box.querySelector(":scope > p.muted")) box.replaceChildren(empty);
    return;
  }
  const opened = new Set([...box.querySelectorAll(".task.open")].map((n) => n.dataset.id));
  const old = new Map([...box.querySelectorAll(":scope > .task")].map((n) => [n.dataset.id, n]));
  const nodes = tasks.map((t) => {
    const node = old.get(t.id);
    if (node && node.dataset.key === taskKey(t)) {
      updateTaskNode(node, t);
      return node;
    }
    return taskNode(t, opened);
  });
  const same = nodes.length === box.children.length && nodes.every((n, i) => box.children[i] === n);
  if (!same) box.replaceChildren(...nodes);
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
let installActive = new Set();     // tâches « install » en cours (leur fin recharge l'état des machines)
async function refreshTasks() {
  clearTimeout(tasksTimer);
  try {
    state.tasks = await api("/api/tasks");
  } catch {
    tasksTimer = setTimeout(refreshTasks, 10000);
    return;
  }
  const active = state.tasks.filter((t) => t.status === "pending" || t.status === "running");
  syncTaskList($("tasks-list"), state.tasks.slice(0, 100),
    el("p", { className: "muted", style: "margin:4px", textContent: "Aucune tâche." }));
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
  const installNow = new Set(active.filter((t) => t.kind === "install").map((t) => t.id));
  const installEnded = [...installActive].some((id) => !nowActive.has(id));
  installActive = installNow;
  if (installEnded) loadInstall();
  if ([...activeBefore].some((id) => !nowActive.has(id))) {
    await loadLibrary();
    if (state.current) await loadAutorun();
    if (!$("folders-main").hidden) loadFolders();
    if (!$("saves-main").hidden) loadSaves();
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
// Désactivée, cette machine ou un agent (Réglages) : sans elle, pas
// de lancement d'essai ni d'état du lanceur ; le reste fonctionne.

const hostOn = () => Boolean(state.config && state.config.game_host && state.config.game_host !== "off");
const hostName = () => state.config?.game_host_name || "la machine de jeu";
function applyHost() {
  const on = hostOn();
  document.querySelector(".agroup .trial").hidden = !on;
  $("d-saves-copy").hidden = !on;
  $("d-saves-sync").hidden = !on;
  $("d-links").title = on ? `Liens du prefix vers un runner hors de l'image ou absent, et réparation avec les runners de ${hostName()}`
    : "Liens du prefix vers un runner hors de l'image ou absent";
}
const HOST_HINTS = {
  off: "Pas de lancement d'essai, de diagnostic ni de maintenance du lanceur.",
  local: "Le lanceur est installé sur la machine du gestionnaire, avec un écran.",
  agent: "Le lanceur tourne sur une autre machine : choisis-la parmi les machines associées (onglet Sauvegardes), ou associe-en une autre (wsquashfs-launcher --pair y affiche le code, puis wsquashfs-launcher --agent y lance l'agent).",
};
// Connexion établie : les paramètres de connexion se replient (résumé), sauf
// si l'on demande à les modifier ; mode changé = champs visibles.
function paintConnection(mode = $("cfg-host").dataset.mode) {
  const saved = state.config?.game_host;
  const connected = state.hostStatus?.state === "ok" && mode === saved;
  const folded = connected && !state.hostEditing && mode === "agent";
  // Agent : une machine déjà associée (onglet Sauvegardes) ou « Autre » (code wsq://)
  const picked = hostPick();
  $("cfg-agent-pick").hidden = mode !== "agent" || folded || !state.hostMachines.length;
  $("cfg-agent").hidden = mode !== "agent" || folded || Boolean(picked);
  $("host-summary").hidden = !folded;
  if (folded) {
    const c = state.config;
    const m = state.hostMachines.find((x) => x.id === c.game_host_machine);
    $("host-summary-text").textContent = `Agent : ${m ? `${m.name} (${c.agent_url})` : c.agent_url}${c.agent_fingerprint ? " · empreinte vérifiée" : ""}`;
  }
  paintTools();
}
$("host-edit").onclick = () => { state.hostEditing = true; paintConnection(); };
// Machine associée choisie comme machine de jeu ("" : autre machine, par code wsq://)
const hostPick = () => (state.hostMachines.length ? $("cfg-agent-machine").value : "");
state.hostMachines = [];
async function loadHostMachines(current) {
  try {
    const data = await api("/api/saves/machines");
    state.hostMachines = data.machines.filter((m) => m.kind === "saved");
  } catch { state.hostMachines = []; }
  $("cfg-agent-machine").replaceChildren(...state.hostMachines.map((m) => new Option(m.url ? `${m.name} — ${m.url}` : m.name, m.id)),
    new Option("Autre machine (code wsq://)", ""));
  // Machine de jeu actuelle si elle est associée ; sans machine de jeu, la première ; sinon « Autre »
  const cfg = state.config || {};
  $("cfg-agent-machine").value = cfg.game_host_machine
    || (cfg.game_host !== "agent" && state.hostMachines[0]?.id) || "";
  paintConnection(current);
}
$("cfg-agent-machine").onchange = () => paintConnection();

// Outils : toujours présents, utilisables quand la machine de jeu ENREGISTRÉE
// répond (un mode choisi mais pas encore enregistré ne compte pas).
function paintTools() {
  const s = state.hostStatus?.state;
  const saved = state.config?.game_host || "off";
  const ready = s === "ok" && saved !== "off";
  const text = saved === "off" ? "Machine de jeu désactivée : le diagnostic et la maintenance du lanceur en ont besoin."
    : !s ? "Vérification de la connexion à la machine de jeu…"
    : ready ? "" : `Machine de jeu injoignable${state.hostStatus?.detail ? ` : ${state.hostStatus.detail}` : ""}.`;
  $("tools-off").hidden = ready;
  $("tools-off-text").textContent = text;
  $("tools-goto-host").hidden = !s && saved !== "off";
  $("tools-intro").hidden = !ready;
  $("tools-intro").textContent = `Actions immédiates sur ${hostName()} : elles ne passent pas par « Enregistrer ».`;
  $("tools-list").classList.toggle("off", !ready);
  for (const b of $("tools-list").querySelectorAll("button")) if (!state.maintBusy) b.disabled = !ready;
  document.querySelector('#cfg-tabs [data-cpane="tools"]').classList.toggle("dim", !ready);
}
$("tools-goto-host").onclick = () => showCfgPane("host");

// Sortie colorée du lanceur (codes ANSI, couleurs forcées pour la maintenance) → HTML.
// Gras, atténué et les 16 couleurs de base ; le reste des codes est ignoré.
const ANSI_RE = /\x1b\[([0-9;]*)m/g;
function stripAnsi(text) { return text.replace(ANSI_RE, ""); }
function ansiToHtml(text) {
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  let html = "", bold = false, dim = false, fg = null, last = 0, open = false;
  const flush = (chunk) => {
    if (!chunk) return;
    const cls = [bold && "ansi-b", dim && "ansi-dim", fg !== null && `ansi-${fg}`].filter(Boolean);
    html += cls.length ? `<span class="${cls.join(" ")}">${esc(chunk)}</span>` : esc(chunk);
  };
  text.replace(ANSI_RE, (match, params, offset) => {
    flush(text.slice(last, offset));
    last = offset + match.length;
    for (const p of (params || "0").split(";").map(Number)) {
      if (p === 0) { bold = false; dim = false; fg = null; }
      else if (p === 1) bold = true;
      else if (p === 2) dim = true;
      else if (p === 22) { bold = false; dim = false; }
      else if (p >= 30 && p <= 37) fg = p - 30;
      else if (p >= 90 && p <= 97) fg = p - 90 + 8;
      else if (p === 39) fg = null;
    }
    return match;
  });
  flush(text.slice(last));
  return html;
}

// Maintenance du lanceur : chaque action demande confirmation, le résultat s'affiche sous la liste
const MAINT = {
  list: ["Lister les jeux", ""],
  umount: ["Démonter les jeux", "Un jeu en cours sera arrêté."],
  clean: ["Nettoyer", "Supprime les copies de travail et les doublons des sauvegardes ; ne garde que les 3 dernières archives de --erase par jeu. Ce que les jeux ont écrit est gardé."],
  saves: ["Copier les sauvegardes de tous les jeux", "Copie les sauvegardes de partie dans la ludothèque (saves/<système>/<jeu>). Rien n'est supprimé."],
  sync: ["Synchroniser les sauvegardes", "Envoie et reçoit les sauvegardes de partie avec les autres machines associées du réseau local ; en cas de conflit, la plus lourde gagne (à taille égale, la plus récente) et l'autre est archivée. Les jeux en cours sont ignorés."],
  "sync-status": ["État de la synchro", ""],
};
// Sans confirmation : les actions qui ne modifient rien
const MAINT_QUIET = new Set(["list", "sync-status"]);
for (const button of document.querySelectorAll("[data-maint]")) {
  button.onclick = async () => {
    const action = button.dataset.maint, [label, warning] = MAINT[action];
    if (!MAINT_QUIET.has(action) && !confirm(`${label} sur ${hostName()} ?\n\n${warning}`)) return;
    const out = $("maint-out");
    out.hidden = false;
    out.textContent = `${label}…`;
    const buttons = $("tools-list").querySelectorAll("button");
    state.maintBusy = true;
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const r = await api("/api/game_host/maintenance", { method: "POST", body: { action }, timeout: 900000 });
      const text = (r.output || "").trim();
      if (text) out.innerHTML = ansiToHtml(text);
      else out.textContent = `${label} : terminé (code ${r.code})`;
      if (r.code !== 0) out.append(`\n\nCode de retour : ${r.code}`);
    } catch (error) {
      out.textContent = `${label} : ${error.message}`;
    } finally {
      state.maintBusy = false;
      paintTools();
    }
  };
}
// Réglages en onglets : Bibliothèque, Machine de jeu, Outils, Général
function showCfgPane(name) {
  for (const b of $("cfg-tabs").children) {
    b.classList.toggle("on", b.dataset.cpane === name);
    b.setAttribute("aria-selected", b.dataset.cpane === name);
  }
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
  $("cfg-host-adv").hidden = mode !== "local";
}
$("cfg-host").onclick = (event) => {
  const b = event.target.closest("button[data-host]");
  if (b) paintHostForm(b.dataset.host);
};
const hostBody = () => ({ game_host: $("cfg-host").dataset.mode || "off",
  session_process: $("cfg-session").value.trim(),
  agent_url: $("cfg-agent-url").value.trim(), agent_token: $("cfg-agent-token").value.trim(),
  agent_pem: state.agentPem || "" });

// Association d'un agent : certificat récupéré, empreinte à confirmer avant d'enregistrer
function agentReset() {
  state.agentPem = null;
  $("cfg-agent-confirm").checked = false;
  $("cfg-agent-confirm-row").hidden = true;
}
for (const id of ["cfg-agent-url", "cfg-agent-token"]) $(id).addEventListener("input", agentReset);
// Code d'association « wsq://JETON@HÔTE:PORT#EMPREINTE » : remplit adresse et jeton.
function readPairCode(code) {
  const m = /^wsq:\/\/([^@\s]+)@([^:\s/]+):(\d+)#([0-9A-Fa-f]{64})$/.exec(code.trim());
  return m ? { token: m[1], url: `https://${m[2]}:${m[3]}` } : null;
}
$("cfg-agent-probe").onclick = async () => {
  const result = $("cfg-agent-result");
  agentReset();
  result.className = "msg";
  result.replaceChildren(el("span", { className: "row" }, el("span", { className: "spinner" }), "connexion à l'agent…"));
  const code = $("cfg-agent-code").value.trim();
  const pair = code ? readPairCode(code) : null;
  if (code && !pair) {
    result.className = "notice err";
    result.textContent = "Code d'association illisible : copier le wsq://… affiché par « wsquashfs-launcher --pair ».";
    return;
  }
  if (pair) {
    $("cfg-agent-url").value = pair.url;
    $("cfg-agent-token").value = pair.token;
  }
  try {
    const r = await api("/api/config/agent/probe", { method: "POST",
      body: code ? { code } : { url: $("cfg-agent-url").value.trim(), token: $("cfg-agent-token").value.trim() } });
    state.agentPem = r.pem;
    if (r.paired) {
      // Certificat vérifié contre le code : pas de contrôle manuel d'empreinte
      $("cfg-agent-confirm").checked = true;
      result.className = "msg";
      result.textContent = `Associé. Empreinte vérifiée : ${r.fingerprint}`;
      return;
    }
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

// ------------------------------------------------------------------ sauvegardes entre machines
// Onglet « Sauvegardes » (services/saves.py via /api/saves/*) : bandeau des machines,
// tableau jeux × machines, sélection et barre d'actions. Une opération à la fois : la
// tâche « saves » est suivie jusqu'à la fin (followTask), puis l'état est relu.

const RESERVE = "__reserve__";          // source : le manager (copie des sauvegardes)
const AUTO = "auto";                    // source : la machine suggérée (la plus lourde), jeu par jeu
const PRESENT_STATES = new Set(["présente", "identique", "plus avancée", "en retard"]);
const SAVE_FILTERS = [["", "Tous"], ["diff", "Différences"], ["same", "Identiques"], ["reserve", "Sur le manager"]];
const OS_ICONS = ["linux", "steamos", "batocera"];          // icônes choisissables d'une machine
const OS_LABELS = { linux: "Linux", steamos: "SteamOS", batocera: "Batocera" };
const sv = {
  machines: [],          // machines jointes à leur état (kind, removable, ok, games, message)
  games: [],             // { game, reserve, machines: [état par machine] }
  loaded: false, error: "", seq: 0,
  filter: "", selected: new Set(), unchecked: new Set(),   // unchecked : cibles décochées
  source: AUTO,
  op: null,              // { text } pendant une opération
  result: null,          // { task, error } de la dernière opération
};

// Nom de jeu comme services/saves.game_name : nom de fichier sans .wsquashfs ni .pc
function gameOfItem(i) {
  let name = (i.path || i.id).split("/").pop();
  for (const suffix of [".wsquashfs", ".pc"]) {
    if (name.toLowerCase().endsWith(suffix) && name.length > suffix.length) name = name.slice(0, -suffix.length);
  }
  return name;
}
const imageIds = () => new Map(state.items.map((i) => [gameOfItem(i), i.id]));   // jeu → identifiant d'image
const busy = () => Boolean(sv.op);
const machineName = (id) => sv.machines.find((m) => m.id === id)?.name ?? id;
const names = (ids) => ids.map(machineName).join(", ");
const reachableIds = () => sv.machines.filter((m) => m.ok).map((m) => m.id);
const targetIds = () => reachableIds().filter((id) => !sv.unchecked.has(id));
const pushTargets = () => targetIds().filter((id) => id !== sv.source);
const stateOf = (g, id) => g.machines.find((s) => s.id === id);
const suggestedOf = (g) => g.machines.find((s) => s.state === "plus avancée");   // la suggérée (la plus lourde)
const presentIn = (g) => g.machines.filter((s) => PRESENT_STATES.has(s.state));
// Une machine sans le jeu (« absente » : stockage plus petit, jeu pas installé) n'est pas une
// différence : Sync ne vise que les machines « en retard ». L'installer : menu « … ».
const lateIn = (g) => g.machines.filter((s) => s.state === "en retard").map((s) => s.id);
const rowTargets = (g) => { const reach = reachableIds(); return lateIn(g).filter((id) => reach.includes(id)); };
const absentIn = (g) => reachableIds().filter((id) => (stateOf(g, id)?.state ?? "absente") === "absente");
// Identique : les machines qui ont le jeu ont la même version. Différences : une au moins est en retard.
const identical = (g) => g.machines.some((s) => s.state === "identique");
const differs = (g) => lateIn(g).length > 0;
// Source par défaut d'un jeu : la suggérée, sinon une machine qui l'a (seule ou identique)
const sourceOf = (g) => suggestedOf(g) || presentIn(g).find((s) => reachableIds().includes(s.id));
const shortDate = (t) => {
  if (!t) return "";
  const d = new Date(t * 1000), p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}/${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
// « 246 Mo · 13/11 10:02 » : taille et date d'une version (la date seule n'est pas fiable)
const sizeDate = (bytes, t) => [bytes ? size(bytes) : "", shortDate(t)].filter(Boolean).join(" · ");
const osIcon = (key, cls = "") => icon(OS_ICONS.includes(key) || key === "server" ? key : "linux", cls);

async function loadSaves() {
  const seq = ++sv.seq;               // seule la dernière lecture compte
  paintSaves();
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
  paintSaves();
  if (!$("main").hidden) renderLibrary();      // la colonne « Sauveg. » des images
}

function paintSaves() {
  const diff = sv.games.filter(differs).length;
  $("count-saves").textContent = diff || "";
  $("count-saves").title = diff ? `${diff} jeu(x) dont les versions diffèrent entre machines` : "";
  $("s-loading").hidden = sv.loaded || Boolean(sv.error);
  $("s-error").replaceChildren(...(sv.error ? [notice("err", sv.error)] : []));
  const none = sv.loaded && sv.machines.length === 0;
  $("s-empty").hidden = !none;
  $("s-body").hidden = !sv.loaded || none;
  paintChips();
  if (!$("s-body").hidden) {
    paintFilters();
    paintTable();
    paintStatus();
  }
  paintSelection();
}

// Bandeau : une carte par machine (icône du système, nom, état) ; clic : menu (icône, oubli).
// Le manager est une carte de plus, sans menu ; « Associer » est la dernière carte.
function paintChips() {
  const kept = sv.games.filter((g) => g.reserve).length;
  const manager = el("div", { className: "mcard mgr", title: "Copie des sauvegardes sur le manager : elle reste même si une machine est éteinte" },
    el("span", { className: "mcard-ico" }, icon("server", "lg")),
    el("span", { className: "mcard-txt" },
      el("span", { className: "mcard-name" }, el("b", { textContent: "Manager" })),
      el("span", { className: "mcard-sub", textContent: kept ? `${kept} jeu${kept > 1 ? "x" : ""} gardé${kept > 1 ? "s" : ""}` : "aucun jeu gardé" })));
  $("s-chips").replaceChildren(...sv.machines.map(machineCard), manager, $("s-pair"));
}

function machineCard(m) {
  const isHost = m.kind === "game_host";
  const title = m.ok ? `${m.url} : joignable` : `Injoignable : ${m.message || "pas de réponse"}`;
  const card = el("button", { type: "button", className: `mcard${m.ok ? "" : " off"}`, "aria-haspopup": "menu",
    title: isHost ? `${title} · configurée dans Réglages › Machine de jeu` : title },
    el("span", { className: "mcard-ico" }, osIcon(m.icon, "lg")),
    el("span", { className: "mcard-txt" },
      el("span", { className: "mcard-name" }, el("b", { textContent: m.name }),
        isHost ? el("span", { className: "chip plain t-accent", textContent: "machine de jeu" }) : null),
      el("span", { className: "mcard-sub" }, el("span", { className: "led" }),
        el("span", { textContent: m.ok ? `${m.games} jeu${m.games > 1 ? "x" : ""}` : "injoignable" }))),
    icon("chevd", "sm chev"));
  const wrap = el("div", { className: "dd" }, card);
  card.onclick = () => openMenuNode(wrap, machineMenu(m));
  return wrap;
}

// Menu d'une machine : icône (détectée ou choisie), puis oubli (sauf machine de jeu)
function machineMenu(m) {
  const choice = (label, key, checked, onpick) => menuItem(label, key, onpick, { checked, role: "menuitemradio" });
  const nodes = [
    el("div", { className: "dd-sub", textContent: "Icône" }),
    choice(`Détectée (${({ linux: "Linux", steamos: "SteamOS", batocera: "Batocera" })[m.os] || m.os || "Linux"})`, m.icon || "linux", !m.icon_set, () => setMachineIcon(m, null)),
    ...OS_ICONS.map((key) => choice(OS_LABELS[key], key, m.icon_set && m.icon === key, () => setMachineIcon(m, key))),
  ];
  if (m.removable) nodes.push(menuItem("Oublier cette machine", "trash", () => forgetMachine(m), { danger: true }));
  return nodes;
}

// Entrée de menu : bouton (cochée, danger) avec icône optionnelle
function menuItem(label, iconName, action, { checked = null, danger = false, role = "menuitem" } = {}) {
  const b = el("button", { type: "button", className: danger ? "danger" : "", role, "aria-checked": checked === null ? undefined : String(checked) },
    el("span", { className: "nocheck" }, checked ? icon("check", "sm") : null),
    icon(iconName, "sm"), el("span", { textContent: label }));
  b.onclick = () => { closeMenu(); action(); };
  return b;
}

async function setMachineIcon(m, key) {
  try {
    await api(`/api/saves/machines/${encodeURIComponent(m.id)}`, { method: "PATCH", body: { icon: key } });
    loadSaves();
  } catch (error) { toast(error.message, "alert", true); }
}

async function forgetMachine(m) {
  if (!confirm(`Oublier « ${m.name} » ? Le manager ne lira plus ses sauvegardes. Les sauvegardes déjà copiées sur le manager restent.`)) return;
  try {
    await api(`/api/saves/machines/${encodeURIComponent(m.id)}`, { method: "DELETE" });
    toast("Machine oubliée");
    loadSaves();
  } catch (error) { toast(error.message, "alert", true); }
}

// Ouvre un menu sous (ou au-dessus de) son déclencheur ; un second clic le referme
function openMenuNode(wrap, nodes) {
  const open = openMenu?.parentElement === wrap;
  closeMenu();
  if (open) return;
  openMenu = el("div", { className: "dd-menu end sv-menu", role: "menu" }, ...nodes);
  placeMenu(openMenu, wrap);
  openMenu.querySelector("button:not([disabled])")?.focus({ preventScroll: true });
}

// Menu d'action (même mécanique que les filtres de la liste) : items → [libellé, icône, désactivé, action, danger]
function toggleMenu(wrap, items) {
  openMenuNode(wrap, items().map(([label, iconName, disabled, action, danger]) => {
    const b = menuItem(label, iconName, action, { danger: Boolean(danger) });
    b.disabled = Boolean(disabled);
    return b;
  }));
}

// Filtres d'état, puis un filtre par machine (jeux présents sur elle)
const onMachine = (g, id) => PRESENT_STATES.has(stateOf(g, id)?.state);
const filteredMachine = () => (sv.filter.startsWith("m:") ? sv.filter.slice(2) : "");
function paintFilters() {
  if (filteredMachine() && !sv.machines.some((m) => m.id === filteredMachine())) sv.filter = "";
  const counts = { "": sv.games.length, diff: sv.games.filter(differs).length,
    same: sv.games.filter(identical).length, reserve: sv.games.filter((g) => g.reserve).length };
  const pill = (key, label, count, extra) => {
    const b = el("button", { type: "button", className: sv.filter === key ? "on" : "", title: extra || "" },
      ...(label instanceof Node ? [label] : [el("span", { textContent: label })]), el("span", { className: "count", textContent: count }));
    b.onclick = () => { sv.filter = key; paintSaves(); };
    return b;
  };
  const machines = sv.machines.map((m) => pill(`m:${m.id}`,
    el("span", { className: "sv-th" }, osIcon(m.icon, "sm"), el("span", { textContent: m.name })),
    m.ok ? sv.games.filter((g) => onMachine(g, m.id)).length : "?",
    m.ok ? `Jeux qui ont des sauvegardes sur ${m.name}` : `${m.name} : injoignable`));
  $("s-filter").replaceChildren(...SAVE_FILTERS.map(([key, label]) => pill(key, label, counts[key])),
    ...(machines.length ? [el("span", { className: "sv-sep", "aria-hidden": "true" }), ...machines] : []));
}

function visibleGames() {
  const q = $("s-search").value.trim().toLowerCase();
  const keep = (g) => {
    if (sv.filter === "diff") return differs(g);
    if (sv.filter === "same") return identical(g);
    if (sv.filter === "reserve") return Boolean(g.reserve);
    if (filteredMachine()) return onMachine(g, filteredMachine());
    return true;
  };
  return sv.games.filter((g) => (!q || g.game.toLowerCase().includes(q)) && keep(g))
    .sort((a, b) => a.game.localeCompare(b.game, "fr", { numeric: true, sensitivity: "base" }));
}

// Cellule d'une machine : badge (★ plus avancée, identique, en retard, — absente, ? injoignable),
// « ⚠ à vérifier » si la suggérée n'est pas la plus récente, « ← » si elle reçoit la version,
// puis « taille · date »
function machineCell(g, m, target) {
  const s = stateOf(g, m.id), st = s?.state ?? "absente";
  const [label, tone] = { "plus avancée": ["★ plus avancée", "t-accent"], identique: ["identique", "t-sage"],
    "en retard": ["en retard", "t-neutral"], présente: ["seule copie", "t-neutral"], injoignable: ["?", "t-err"] }[st] ?? ["—", ""];
  const detail = [`${m.name} : ${st}`, s?.newest ? when(s.newest) : "", st === "injoignable" ? (s?.message || m.message || "") : ""]
    .filter(Boolean).join(" · ");
  const cls = { "plus avancée": "sv-best", "en retard": "sv-old", absente: "sv-none" }[st] ?? "";
  const top = suggestedOf(g);
  const check = g.check && st === "plus avancée" ? checkMarker(g) : null;
  const incoming = target && top && st !== "plus avancée"
    ? el("span", { className: "sv-in", title: `Reçoit la version de ${top.name}`, textContent: "←" }) : null;
  return el("td", { className: cls, title: detail },
    el("div", { className: "sv-line" }, incoming,
      st === "absente" ? dash() : el("span", { className: `chip plain ${tone}`, textContent: label }), check),
    PRESENT_STATES.has(st) ? el("div", { className: "sv-date", textContent: sizeDate(s.size, s.newest) }) : null);
}

// Suggestion douteuse : la plus lourde n'est pas la plus récente
function checkMarker(g) {
  const other = g.machines.find((s) => s.name === g.check_by);
  const dated = other ? ` (${g.check_by}, ${when(other.newest)})` : "";
  return el("span", { className: "sv-check", tabIndex: 0, textContent: "⚠ à vérifier",
    title: `La plus lourde (${suggestedOf(g)?.name ?? "?"}) n'est pas la plus récente${dated} : vérifie avant de synchroniser` });
}

function paintTable() {
  closeMenu();
  const first = filteredMachine();
  const cols = first ? [...sv.machines.filter((m) => m.id === first), ...sv.machines.filter((m) => m.id !== first)] : sv.machines;
  const ids = imageIds(), games = visibleGames();
  const selectable = games;
  const all = $("s-all");
  all.checked = selectable.length > 0 && selectable.every((g) => sv.selected.has(g.game));
  all.onchange = () => {
    for (const g of selectable) (all.checked ? sv.selected.add(g.game) : sv.selected.delete(g.game));
    paintTable();
    paintSelection();
  };
  const head = el("tr", {}, el("th", { className: "sv-stick" }), el("th", { className: "sv-stick2", textContent: "Jeu" }),
    ...cols.map((m) => el("th", { title: m.url },
      el("span", { className: "sv-th" }, osIcon(m.icon, "sm"), el("span", { textContent: m.name })))),
    el("th", { title: "Copie des sauvegardes sur le manager" }, el("span", { className: "sv-th" }, icon("server", "sm"), el("span", { textContent: "Manager" }))),
    el("th", {}));
  const rows = games.map((g) => {
    // Toutes les lignes sont actionnables (les routes prennent le nom du jeu) ; seule la fiche
    // demande une image dans la bibliothèque (un jeu installé sur une seule machine n'en a pas)
    const img = ids.get(g.game), actionable = true;
    const box = el("input", { type: "checkbox", checked: sv.selected.has(g.game), title: "Sélectionner" });
    box.onchange = () => { box.checked ? sv.selected.add(g.game) : sv.selected.delete(g.game); paintTable(); paintSelection(); };
    const name = img
      ? el("button", { type: "button", className: "sv-name", textContent: g.game, title: "Ouvrir la fiche de l'image" })
      : el("span", { className: "sv-name", textContent: g.game, title: "Pas d'image dans la bibliothèque du manager" });
    if (img) name.onclick = () => { showView("images"); openDetail(img); };
    const top = suggestedOf(g), targets = differs(g) ? rowTargets(g) : [];
    const reserve = g.reserve
      ? el("td", { className: "sv-mgr", title: `Copie du manager : depuis ${g.reserve.source || "?"}, le ${when(g.reserve.newest)} · ${g.reserve.archives} version(s) précédente(s)` },
        el("span", { className: "sv-date", textContent: sizeDate(g.reserve.size, g.reserve.newest) }))
      : el("td", {}, dash());
    const actions = el("td", { className: "sv-act" });
    if (actionable && differs(g)) {
      const flow = targets.length ? `${top.name} → ${targets.length === 1 ? machineName(targets[0]) : `${targets.length} machines`}` : "";
      const sync = el("button", { type: "button", className: "btn primary sm sv-sync", disabled: busy() || !targets.length,
        title: targets.length ? `Sync de ${top.name} vers : ${names(targets)}` : "Aucune autre machine joignable à synchroniser" },
        icon("arrow", "sm"), el("b", { textContent: "Sync" }), flow ? el("span", { className: "sv-flow", textContent: flow }) : null);
      sync.onclick = () => syncRow(g);
      actions.append(sync);
    }
    if (actionable) {
      const wrap = el("div", { className: "dd end" }, el("button", { type: "button", className: "iconbtn bare sm", title: "Autres actions", "aria-label": `Autres actions pour ${g.game}`, "aria-haspopup": "menu" }, icon("chevd", "sm")));
      wrap.querySelector("button").onclick = () => toggleMenu(wrap, () => rowItems(g));
      actions.append(wrap);
    }
    return el("tr", { className: differs(g) ? "sv-diff" : "" },
      el("td", { className: "sv-stick" }, box), el("td", { className: "sv-stick2" }, name),
      ...cols.map((m) => machineCell(g, m, targets.includes(m.id))), reserve, actions);
  });
  if (!games.length) {
    rows.push(el("tr", {}, el("td", { colSpan: sv.machines.length + 4 },
      sv.games.length ? emptyState("search", "Aucun jeu pour ces filtres", "Élargir la recherche ou choisir « Tous ».")
        : emptyState("save", "Aucune sauvegarde", "Les machines associées n'ont aucune sauvegarde de partie."))));
  }
  $("s-table").replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows));
}

// Menu « … » d'une ligne
function rowItems(g) {
  const idle = !busy();
  const reach = reachableIds(), top = sourceOf(g);
  // Depuis une machine qui a le jeu vers les autres qui l'ont dans une autre version
  const others = (s) => presentIn(g).filter((o) => o.id !== s.id && o.fingerprint !== s.fingerprint && reach.includes(o.id)).map((o) => o.id);
  return [
    ...presentIn(g).filter((s) => reach.includes(s.id) && others(s).length)
      .map((s) => [`Sync ${s.name} → ${names(others(s))}`, "arrow", !idle, () => pushFrom(g, s.id, others(s))]),
    // Installer sur une machine qui n'a pas le jeu : choix explicite, jamais par défaut
    ...(top ? absentIn(g).map((id) => [`Installer sur ${machineName(id)} (depuis ${top.name})`, "arrow", !idle, () => pushFrom(g, top.id, [id])]) : []),
    [top ? `Copier vers le manager (${top.name} → Manager)` : "Copier vers le manager", "save", !idle || !top, () => pullGames([g], AUTO)],
    ["Sync depuis le manager (Manager → machines)", "server", !idle || !g.reserve || !reachManagerTargets(g).length, () => pushReserve(g)],
    ["Supprimer…", "trash", !idle, () => openSaveDelete([g]), true],
  ];
}

function paintStatus() {
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
  $("s-push").title = targets.length ? "" : "Aucune cible : coche au moins une autre machine joignable";
  $("s-pull").disabled = busy() || !games.length || sv.source === RESERVE;
  $("s-pull").title = sv.source === RESERVE ? "Choisis une machine, ou « La plus avancée », comme source" : "Copier les sauvegardes sélectionnées sur le manager";
  $("s-delete").disabled = busy();
}

// Cibles : cases par machine joignable (la source choisie est grisée)
$("s-targets").onclick = () => {
  const wrap = $("s-targets-dd"), open = openMenu?.parentElement === wrap;
  closeMenu();
  if (open) return;
  const boxes = sv.machines.filter((m) => m.ok).map((m) => {
    const box = el("input", { type: "checkbox", checked: !sv.unchecked.has(m.id) && m.id !== sv.source, disabled: m.id === sv.source });
    box.onchange = () => { box.checked ? sv.unchecked.delete(m.id) : sv.unchecked.add(m.id); paintSelection(); };
    return el("label", { className: "dd-check" }, box, el("span", { textContent: m.name }));
  });
  openMenu = el("div", { className: "dd-menu", role: "group" }, ...(boxes.length ? boxes : [el("span", { className: "muted dd-check", textContent: "Aucune machine joignable" })]));
  placeMenu(openMenu, wrap);
};
$("s-source").onchange = () => { sv.source = $("s-source").value; paintSelection(); };
$("s-search").addEventListener("input", paintTable);
$("s-refresh").onclick = () => loadSaves();
$("s-clear").onclick = () => { sv.selected.clear(); paintTable(); paintSelection(); };
$("s-push").onclick = () => pushSelection();
$("s-pull").onclick = () => pullGames(sv.games.filter((g) => sv.selected.has(g.game)), sv.source);
$("s-delete").onclick = () => openSaveDelete(sv.games.filter((g) => sv.selected.has(g.game)));

// Appairage : même code wsq:// que l'agent ; l'erreur reste dans le dialogue
function setPairError(text) {
  $("pair-err").textContent = text || "";
  $("pair-err").hidden = !text;
}
function openPair() {
  $("pair-code").value = "";
  setPairError("");
  $("dlg-pair").showModal();
  $("pair-code").focus();
}
$("s-pair").onclick = openPair;
$("s-empty-pair").onclick = openPair;
$("pair-save").onclick = async () => {
  const code = $("pair-code").value.trim(), button = $("pair-save");
  if (!code) { setPairError("Colle le code wsq:// affiché par wsquashfs-launcher --pair."); return; }
  button.disabled = true;
  setPairError("");
  try {
    const r = await api("/api/saves/machines", { method: "POST", body: { code } });
    $("dlg-pair").close();
    toast(r.replaced ? `Machine mise à jour : ${r.name}` : `Machine associée : ${r.name}`);
    loadSaves();
  } catch (error) {
    setPairError(error.message);
  } finally {
    button.disabled = false;
  }
};
$("pair-code").addEventListener("keydown", (event) => { if (event.key === "Enter") $("pair-save").click(); });

// Suppression des sauvegardes (#dlg-save-delete) : une case par machine joignable et une pour le
// manager ; pré-cochée si un des jeux y a une sauvegarde, sinon grisée. Valider : tâche « saves ».
let sdGames = [];
function openSaveDelete(games) {
  sdGames = games;
  const gameNames = games.map((g) => g.game);
  const total = gameNames.length;
  const shownNames = gameNames.slice(0, 3).map((n) => `« ${n} »`).join(", ");
  const rest = total - 3;
  $("sd-games").textContent = `${total} jeu${total > 1 ? "x" : ""} : ${shownNames}${rest > 0 ? ` et ${rest} autre${rest > 1 ? "s" : ""}` : ""}`;
  const row = (id, label, count, iconNode, detail) => {
    const box = el("input", { type: "checkbox", checked: count > 0, disabled: count === 0 });
    box.dataset.id = id;
    box.onchange = sdRefresh;
    return el("label", { className: `sd-row${count ? "" : " none"}` }, box, iconNode,
      el("span", {}, el("b", { textContent: label }),
        el("small", { textContent: count ? (count === total ? "tous les jeux" : `${count} jeu${count > 1 ? "x" : ""} sur ${total}`) : "aucune sauvegarde de ces jeux" })));
  };
  const machines = sv.machines.filter((m) => m.ok).map((m) =>
    row(m.id, m.name, games.filter((g) => PRESENT_STATES.has(stateOf(g, m.id)?.state)).length, osIcon(m.icon, "sm")));
  const manager = row("manager", "Manager", games.filter((g) => g.reserve).length, icon("server", "sm"));
  $("sd-targets").replaceChildren(...machines, manager);
  sdRefresh();
  $("dlg-save-delete").showModal();
}
// Le bouton reste désactivé tant que rien n'est coché (ou pendant une opération)
function sdRefresh() {
  $("sd-run").disabled = busy() || !$("sd-targets").querySelector("input:checked");
}
$("sd-run").onclick = () => {
  const checked = [...$("sd-targets").querySelectorAll("input:checked")].map((box) => box.dataset.id);
  const manager = checked.includes("manager");
  const machines = checked.filter((id) => id !== "manager");
  if (!machines.length && !manager) return;
  $("dlg-save-delete").close();
  runSaveOp("/api/saves/delete", { games: sdGames.map((g) => g.game), machines, manager });
};

// Suit une tâche de la file jusqu'à sa fin : onEnd(tâche) ou onEnd(null, message) ; onTick à chaque relevé
async function followTask(taskId, onEnd, onTick = null) {
  let misses = 0;
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    let task;
    try {
      task = await api(`/api/tasks/${taskId}`);
      misses = 0;
    } catch {
      if (++misses >= 10) { onEnd(null, "suivi interrompu : la tâche reste visible dans la file de tâches"); return; }
      continue;
    }
    if (task.status !== "pending" && task.status !== "running") { onEnd(task); return; }
    onTick?.(task);
  }
}
// Lignes d'un résultat : pull → results ; push → results ; push_from / push_auto → pull + push
function syncItems(task) {
  const r = task?.params?.result;
  if (!r) return null;
  if (r.pull || r.push) return [...(r.pull?.results || []), ...(r.push?.results || [])];
  return r.results || [];
}
const syncFailed = (items) => items.filter((item) => !item.ok);
// Résultat : ✓ / ✗ par jeu et machine ; failuresOnly : les échecs détaillés, les réussites en compte
function syncResultNode(task, failuresOnly = false) {
  const items = syncItems(task);
  if (items === null) return notice("err", task?.error || `Tâche ${TASK_STATES[task?.status]?.[1] || "sans résultat"}.`);
  if (!items.length) return notice("info", "Rien à faire.");
  const failed = syncFailed(items);
  const shown = failuresOnly ? failed : items;
  // Suppression : le message dit déjà où (« supprimée sur <machine> ») et si c'est archivé
  const removal = task?.params?.op === "delete";
  const rows = shown.slice(0, 40).map((item) => {
    // « envoyée vers <machine> » : le nom de la machine est déjà affiché à côté
    const message = item.machine && !removal ? item.message.replace(` vers ${item.machine}`, "") : item.message;
    const where = item.machine && !removal ? ` → ${item.machine}` : "";
    const archived = item.archived && !removal ? " · version précédente archivée" : "";
    return el("li", { className: item.ok ? "ok" : "err",
      textContent: `${item.ok ? "✓" : "✗"} ${item.game}${where} : ${message}${archived}` });
  });
  if (shown.length > 40) rows.push(el("li", { className: "muted", textContent: `… ${shown.length - 40} de plus` }));
  return el("div", { className: `sync-res${failed.length ? " err" : ""}` },
    el("strong", { textContent: failed.length ? `${failed.length} échec(s) sur ${items.length}` : `${items.length} opération(s) réussie(s)` }),
    shown.length ? el("ul", { className: "sync-list" }, ...rows) : el("p", { className: "muted", textContent: "Aucun échec." }));
}

// Une opération : POST (tâche « saves ») puis suivi ; à la fin, résultat et état relu
async function runSaveOp(path, body) {
  sv.result = null;
  sv.op = { text: "Démarrage…" };
  paintSaves();
  let taskId;
  try {
    ({ task: taskId } = await api(path, { method: "POST", body }));
  } catch (error) {
    sv.op = null;
    sv.result = { task: null, error: error.message };
    paintSaves();
    return;
  }
  refreshTasks();
  followTask(taskId, (task, error) => {
    sv.op = null;
    sv.result = { task, error };
    refreshTasks();
    loadSaves();
  }, (task) => {
    sv.op = { text: cap(task.phase || "En cours…") };
    paintStatus();
  });
}

const ARCHIVED = "La version écrasée sur chaque machine est archivée.";
// Cibles d'une copie du manager vers les machines : joignables, qui ont le jeu, pas déjà à sa version
const reachManagerTargets = (g) => reachableIds().filter((id) => onMachine(g, id) && (!g.reserve || stateOf(g, id)?.fingerprint !== g.reserve.fingerprint));

// Copie vers le manager : depuis `source` (une machine, ou AUTO : la suggérée, jeu par jeu)
function pullGames(games, source) {
  const names = games.map((g) => g.game);
  if (!names.length || source === RESERVE) return;
  runSaveOp("/api/saves/pull", { source, games: names });
}
function syncRow(g) {                       // la suggérée vers les autres machines joignables
  const from = suggestedOf(g), targets = rowTargets(g);
  if (!from || !targets.length) return;
  if (!confirm(`Sync « ${g.game} » : ${from.name} → ${names(targets)}.\n\n${ARCHIVED}`)) return;
  runSaveOp("/api/saves/push", { source: AUTO, targets, games: [g.game] });
}
function pushFrom(g, sourceId, targets) {
  if (!targets.length) return;
  if (!confirm(`Sync « ${g.game} » : ${machineName(sourceId)} → ${names(targets)}.\n\n${ARCHIVED}`)) return;
  runSaveOp("/api/saves/push", { source: sourceId, targets, games: [g.game] });
}
function pushReserve(g) {                   // copie du manager (sans source) vers les machines
  const targets = reachManagerTargets(g);
  if (!targets.length) return;
  if (!confirm(`Sync « ${g.game} » : Manager → ${names(targets)}.\n\n${ARCHIVED}`)) return;
  runSaveOp("/api/saves/push", { targets, games: [g.game] });
}
function pushSelection() {
  const games = sv.games.filter((g) => sv.selected.has(g.game));
  const targets = pushTargets();
  if (!games.length || !targets.length) return;
  const idsOf = (list) => list.map((g) => g.game);
  let body, lines;
  if (sv.source === RESERVE) {
    const onManager = games.filter((g) => g.reserve);
    if (!onManager.length) { toast("Aucun jeu sélectionné n'est sur le manager.", "alert", true); return; }
    body = { targets, games: idsOf(onManager) };
    lines = [`${onManager.length > 1 ? `${onManager.length} jeux` : `« ${onManager[0].game} »`} : Manager → ${names(targets)}.`];
    if (onManager.length < games.length) lines.push(`${games.length - onManager.length} jeu(x) absent(s) du manager, ignoré(s).`);
  } else if (sv.source === AUTO) {
    // Chaque jeu depuis sa suggérée ; les jeux identiques ne sont pas transférés
    // Cibles qui n'ont pas la version de la source (absentes ou plus anciennes)
    const moves = games.map((g) => {
      const from = suggestedOf(g);
      const to = from ? targets.filter((id) => lateIn(g).includes(id)) : [];
      return { g, from, to };
    }).filter((x) => x.to.length);
    if (!moves.length) { toast("Rien à synchroniser : aucune machine cochée n'a ces jeux dans une version en retard.", "info"); return; }
    body = { source: AUTO, targets, games: idsOf(games) };
    lines = moves.map(({ g, from, to }) => `Sync « ${g.game} » : ${from.name} → ${names(to)}.`);
    if (moves.length < games.length) lines.push(`${games.length - moves.length} jeu(x) déjà à jour, ignoré(s).`);
    lines.push("Les machines qui n'ont pas le jeu ne sont pas touchées.");
  } else {
    const dest = targets.filter((id) => id !== sv.source);
    body = { source: sv.source, targets, games: idsOf(games) };
    lines = [`${games.length > 1 ? `${games.length} jeux` : `« ${games[0].game} »`} : ${machineName(sv.source)} → ${names(dest)}.`,
      "Source choisie : les cibles reçoivent ses sauvegardes, même si elles n'ont pas encore le jeu."];
  }
  if (!confirm(`${lines.join("\n")}\n\n${ARCHIVED}`)) return;
  runSaveOp("/api/saves/push", body);
}

// Ouvre l'onglet filtré sur un jeu (liste des images, fiche)
function openSavesFor(game) {
  sv.filter = "";
  $("s-search").value = game;
  showView("saves");
  window.scrollTo(0, 0);
}

// Colonne « Sauveg. » de la liste : taille et, si une machine est associée, un accès à l'onglet
function savesOpen(i, cls) {
  const open = el("button", { type: "button", className: `iconbtn bare sm ${cls}`, title: "Sauvegardes entre machines de ce jeu", "aria-label": "Sauvegardes entre machines de ce jeu" },
    icon("save", "sm"));
  open.onclick = (event) => { event.stopPropagation(); openSavesFor(gameOfItem(i)); };
  return open;
}
function savesCell(i) {
  const cell = el("span", { className: `gsize c-saves sv-cell${i.saves_present ? "" : " none"}` },
    el("span", { textContent: i.saves_present ? size(i.saves_size) : "—" }));
  if (sv.machines.length) cell.append(savesOpen(i, "sv-open"));
  return cell;
}

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
  state.hostStatus = null;
  paintTools();
  try {
    const s = await api(`/api/game_host/status${refresh ? "?refresh=true" : ""}`);
    state.hostStatus = s;
    paintConnection();
    box.classList.add(s.state);
    text.textContent = s.state === "ok" ? `Connectée · ${s.name}`
      : s.state === "off" ? "Désactivée" : `Non connectée · ${s.detail}`;
    text.title = s.detail || "";
  } catch (error) {
    state.hostStatus = { state: "ko", detail: error.message };
    paintConnection();
    box.classList.add("ko");
    text.textContent = `Non connectée · ${error.message}`;
  }
}

// Bibliothèque : pour chaque dossier de base, « tout le dossier » (nouveaux
// systèmes compris) ou une sélection de systèmes. Enregistré tel quel dans
// roms_dirs : le dossier de base seul, ou les dossiers des systèmes choisis.
const trimSlash = (d) => d.replace(/\/+$/, "") || "/";
const countText = (images, folders) => [images && `${images} image${images > 1 ? "s" : ""}`,
  folders && `${folders} dossier${folders > 1 ? "s" : ""}`].filter(Boolean).join(" · ") || "vide";

function renderRoots(systems, chosen) {
  const roots = [...new Set(systems.map((x) => x.root))];
  const blocks = roots.map((root) => {
    const list = systems.filter((x) => x.root === root);
    const block = el("div", { className: "root-block" });
    block.dataset.root = root;
    const modes = el("div", { className: "pills on-s2 root-modes", role: "radiogroup" },
      el("button", { type: "button", role: "radio", textContent: "Tout le dossier" }),
      el("button", { type: "button", role: "radio", textContent: "Choisir les systèmes" }));
    modes.children[0].dataset.mode = "all";
    modes.children[1].dataset.mode = "pick";
    const chips = el("div", { className: "sys-toggles" }, ...list.map((x) => {
      const input = el("input", { type: "checkbox", checked: chosen.has(trimSlash(x.path)) });
      input.dataset.path = x.path;
      input.dataset.images = x.images;
      input.dataset.folders = x.folders;
      input.onchange = paintRootsTotal;
      return el("label", { title: x.path }, input, el("span", { textContent: x.name }),
        el("span", { className: "n", textContent: [x.images || "", x.folders ? `${x.folders} dossier(s)` : ""].filter(Boolean).join(" · ") }));
    }));
    const hint = el("p", { className: "dlg-intro root-hint" });
    const setMode = (mode) => {
      block.dataset.mode = mode;
      for (const b of modes.children) {
        b.classList.toggle("on", b.dataset.mode === mode);
        b.setAttribute("aria-checked", b.dataset.mode === mode);
      }
      const boxes = [...chips.querySelectorAll("input")];
      // Passer en sélection sans rien de coché : tout cocher, on décoche ce qu'on ne veut pas
      if (mode === "pick" && !boxes.some((i) => i.checked)) boxes.forEach((i) => { i.checked = true; });
      chips.classList.toggle("all", mode === "all");
      boxes.forEach((i) => { i.disabled = mode === "all"; });
      hint.textContent = mode === "all"
        ? "Tous les systèmes ci-dessous sont lus, et ceux ajoutés plus tard aussi."
        : "Seuls les systèmes cochés sont lus.";
      paintRootsTotal();
    };
    modes.onclick = (event) => {
      const b = event.target.closest("button[data-mode]");
      if (b) setMode(b.dataset.mode);
    };
    block.append(
      el("div", { className: "root-head" },
        el("div", { className: "root-name" }, el("b", { textContent: root.split("/").filter(Boolean).pop() || root }),
          el("span", { className: "mono muted", textContent: root })),
        modes),
      hint, chips);
    block.setMode = setMode;
    return block;
  });
  $("cfg-roots").replaceChildren(...blocks);
  for (const block of blocks) block.setMode(chosen.has(trimSlash(block.dataset.root)) ? "all" : "pick");
  if (!blocks.length) {
    $("cfg-roots").replaceChildren(el("p", { className: "muted", textContent: "Aucun système trouvé dans les dossiers de base : saisir les dossiers ci-dessous." }));
  }
  return new Set([...roots, ...systems.map((x) => x.path)].map(trimSlash));
}

function extraDirs() {
  return $("cfg-dirs").value.split("\n").map((d) => d.trim()).filter(Boolean);
}
function chosenDirs() {
  const dirs = [];
  for (const block of $("cfg-roots").querySelectorAll(".root-block")) {
    if (block.dataset.mode === "all") dirs.push(block.dataset.root);
    else dirs.push(...[...block.querySelectorAll("input:checked")].map((i) => i.dataset.path));
  }
  return [...new Set([...dirs, ...extraDirs()])];
}
function paintRootsTotal() {
  let images = 0, folders = 0;
  for (const block of $("cfg-roots").querySelectorAll(".root-block")) {
    for (const i of block.querySelectorAll("input")) {
      if (block.dataset.mode === "all" || i.checked) {
        images += Number(i.dataset.images) || 0;
        folders += Number(i.dataset.folders) || 0;
      }
    }
  }
  const extra = extraDirs().length;
  const total = $("cfg-total");
  const none = !images && !folders && !extra;
  total.classList.toggle("warn", none);
  total.textContent = none ? "Rien de sélectionné : cocher au moins un système."
    : `Sélection : ${countText(images, folders)}${extra ? ` + ${extra} autre(s) dossier(s)` : ""}`;
}
$("cfg-dirs").addEventListener("input", paintRootsTotal);

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
    systemsError = `Liste des systèmes indisponible (${error.message}) — dossiers à saisir dans « Autres dossiers ».`;
  }
  const chosen = new Set(cfg.roms_dirs.map(trimSlash));
  const known = renderRoots(systems, chosen);
  $("cfg-dirs").value = [...chosen].filter((d) => !known.has(d)).join("\n");
  $("cfg-extra").open = !systems.length || Boolean($("cfg-dirs").value);
  paintRootsTotal();
  $("cfg-info").replaceChildren(...[
    ["Dossiers de base", (cfg.roms_roots || []).join(", ") || "—"],
    ["Sauvegardes", cfg.saves_dir], ["Dossier de travail", cfg.tmp_dir], ["Reconstruction", cfg.rebuild_mode],
    ["Tâches simultanées", cfg.concurrency],
    ["Rétention .old", cfg.old_retention_days == null ? "manuelle" : `${cfg.old_retention_days} jours`],
  ].flatMap(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: v ?? "—" })]));
  $("cfg-session").value = cfg.session_process ?? "";
  $("cfg-agent-code").value = "";
  $("cfg-agent-url").value = cfg.agent_url || "";
  $("cfg-agent-token").value = "";
  $("cfg-agent-token").placeholder = cfg.agent_token_set ? "jeton enregistré : laisser vide pour le garder" : "affiché par « wsquashfs-launcher --pair »";
  agentReset();
  $("cfg-agent-result").className = "msg";
  $("cfg-agent-result").textContent = cfg.agent_fingerprint
    ? `Associé. Empreinte enregistrée : ${cfg.agent_fingerprint}` : "Pas encore associé.";
  state.hostEditing = false;
  paintHostForm(cfg.game_host || "off");
  loadHostMachines(cfg.game_host || "off");
  $("doc-title").textContent = `wsquashfs-launcher --doctor — ${cfg.game_host_name}`;
  $("maint-out").hidden = true;
  loadHostStatus();
  $("cfg-night-start").value = cfg.night_start || "01:00";
  $("cfg-night-end").value = cfg.night_end || "07:00";
  $("cfg-err").textContent = systemsError || "";
  $("cfg-err").hidden = !systemsError;
  showCfgPane("dirs");
  $("dlg-config").showModal();
};
$("cfg-save").onclick = async () => {
  const dirs = chosenDirs();
  try {
    if (!dirs.length) {
      showCfgPane("dirs");
      throw new Error("Bibliothèque : cocher au moins un système.");
    }
    if ($("cfg-host").dataset.mode === "agent" && !hostPick()) {
      const url = $("cfg-agent-url").value.trim();
      const reuse = url === state.config?.agent_url && state.config?.agent_fingerprint && !state.agentPem
        && !$("cfg-agent-token").value.trim();
      if (!reuse && !(state.agentPem && $("cfg-agent-confirm").checked)) {
        showCfgPane("host");
        throw new Error("Machine de jeu : associer l'agent et confirmer son empreinte avant d'enregistrer.");
      }
    }
    state.config = await api("/api/config/night", { method: "PUT",
      body: { start: $("cfg-night-start").value, end: $("cfg-night-end").value } });
    const before = JSON.stringify([state.config.game_host, state.config.session_process]);
    state.config = $("cfg-host").dataset.mode === "agent" && hostPick()
      ? await api("/api/config/game_host/machine", { method: "PUT", body: { id: hostPick() } })
      : await api("/api/config/game_host", { method: "PUT", body: hostBody() });
    if (JSON.stringify([state.config.game_host, state.config.session_process]) !== before) state.doctor = null;
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
      showCfgPane("dirs");
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
// Action sur un jeu de la fiche (save, sync) : résultat par jeu dans un toast.
async function gameSaveAction(button, body, label, doneText) {
  button.disabled = true;
  try {
    const r = await api("/api/game_host/maintenance", { method: "POST", timeout: 900000, body });
    // Lignes de résultat par jeu : « ␣␣✓ Jeu  message » (– ignoré, ✗ échec, → état).
    const plain = stripAnsi(r.output || "");
    const out = plain.split("\n").filter((l) => /^  [✓–✗→] /.test(l)).map((l) => l.trim()).join("\n");
    if (r.code !== 0) alert(`${label} : ${plain.trim() || `code ${r.code}`}`);
    else toast(out || doneText, "save");
  } catch (error) { alert(error.message); } finally { button.disabled = false; }
}
$("d-saves-copy").onclick = () => gameSaveAction($("d-saves-copy"), { action: "save", id: state.current.id },
  "Copie des sauvegardes", "Sauvegardes copiées");
$("d-saves-open").onclick = () => { if (state.current) openSavesFor(gameOfItem(state.current)); };
$("d-saves-sync").onclick = () => {
  if (!confirm(`Synchroniser les sauvegardes de « ${state.current.name} » avec les autres machines associées du réseau local ? En cas de conflit, la plus lourde gagne (à taille égale, la plus récente) et l'autre est archivée.`)) return;
  gameSaveAction($("d-saves-sync"), { action: "sync", id: state.current.id }, "Synchronisation", "Sauvegardes synchronisées");
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
        const label = { join: "Joindre le runner", complete: "Compléter le runner", relink: "Réécrire les liens" }[sug.action];
        const apply = el("button", {}, icon("clip", "sm"), label);
        apply.onclick = () => (sug.action === "relink" ? applyRelink : applyRunner)(id, sug, apply);
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

async function applyRelink(id, sug, button) {
  if (!confirm(`Reconstruire « ${state.current.name} » : réécrire ${sug.rewritten} lien(s) en relatif. L'original est gardé en .old.`)) return;
  button.disabled = true;
  try {
    await api(`/api/image/relink?id=${encodeURIComponent(id)}`, { method: "POST", body: { when: whenOf() } });
    button.replaceChildren(icon("check", "sm"), "Envoyé dans la file");
    queued();
  } catch (error) {
    button.disabled = false;
    alert(error.message);
  }
}

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
  $("saves-main").hidden = name !== "saves";
  $("help-main").hidden = name !== "help";
  $("btn-help").classList.toggle("active", name === "help");
  $("btn-help").setAttribute("aria-pressed", name === "help");
  if (name !== "help") lastView = name;
  storage.set("wsfs-view", name);
  if (name === "folders") loadFolders();
  if (name === "saves") loadSaves();
  if (name === "images" && state.items.length) renderLibrary();   // icônes « Sauveg. » à jour
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

// ------------------------------------------------------------------ installation sur les machines
// Une image .wsquashfs est copiée dans le dossier des jeux d'une machine associée (les machines de
// l'onglet Sauvegardes) : une tâche « install » par image, puis l'état des machines est relu à la fin.
// Désinstaller retire l'image de la machine ; elle reste dans la bibliothèque.

const inst = { machines: [], loaded: false, error: "", loading: false, seq: 0 };
let instDlg = null;             // dialogue ouvert : { mode, images, skipped, picked, edit, busy }
const GIB = 1024 ** 3;          // marge d'espace libre demandée au-delà de la taille à envoyer
const LINK_RATE = 100e6;        // débit supposé sans mesure (filaire), en octets/s

const isWsq = (i) => /\.wsquashfs$/i.test(i.path || i.id);
const imageFile = (i) => (i.path || i.id).split("/").pop();
const iconKey = (key) => OS_ICONS.includes(key) || key === "server" ? key : "linux";
const machineIcon = (id) => iconKey(sv.machines.find((m) => m.id === id)?.icon);
const machineOf = (id) => inst.machines.find((m) => m.id === id);
const machineNameOf = (id) => machineOf(id)?.name ?? id;
const gamesOf = (m) => m.games || [];
const selectedImages = () => [...state.selected].map((id) => state.byId.get(id)).filter(Boolean);
// Durée estimée : dernier débit réellement mesuré vers cette machine (m.rate), sinon le filaire supposé
function etaText(bytes, rate) {
  const min = Math.round(bytes / (rate || LINK_RATE) / 60);
  const span = min < 1 ? "moins d'1 min" : min < 60 ? `~${min} min` : `~${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")}`;
  const mo = (rate || LINK_RATE) / 1e6;
  return `${span} (${rate ? `débit mesuré ${mo < 10 ? mo.toFixed(1).replace(".", ",") : Math.round(mo)} Mo/s` : "débit supposé 100 Mo/s"})`;
}
// Identifiants des machines joignables où ce fichier est installé
const installedOn = (file) => inst.machines.filter((m) => m.ok && gamesOf(m).includes(file)).map((m) => m.id);

// Ce qui reste à envoyer sur une machine pour ces images (un envoi coupé est compté à sa reprise)
function instPlan(m, images) {
  const have = new Set(gamesOf(m));
  const parts = new Map((m.parts || []).map((p) => [p.name, p.size]));
  const todo = images.filter((i) => !have.has(imageFile(i)));
  const bytes = todo.reduce((n, i) => n + Math.max(0, (i.size || 0) - (parts.get(imageFile(i)) || 0)), 0);
  return { todo, bytes, done: images.length - todo.length, fits: (m.free || 0) >= bytes + GIB };
}
const canInstallOn = (m, plan) => m.ok && plan.todo.length > 0 && plan.fits;

async function loadInstall() {
  const seq = ++inst.seq;
  inst.loading = true;
  paintInstDialog();
  try {
    const status = await api("/api/install/status", { timeout: 60000 });
    if (seq !== inst.seq) return;
    inst.machines = status.machines;
    inst.loaded = true;
    inst.error = "";
  } catch (error) {
    if (seq !== inst.seq) return;
    inst.error = error.message;
  } finally {
    if (seq === inst.seq) inst.loading = false;
  }
  paintInstall();
}

function paintInstall() {
  if (state.items.length && !$("main").hidden) renderLibrary();   // filtre, pastilles
  renderInstBox();
  paintInstDialog();
}

// Pastilles discrètes : icône de chaque machine où l'image est installée (liste et jaquettes)
function instPins(i) {
  const ids = isWsq(i) ? installedOn(imageFile(i)) : [];
  if (!ids.length) return null;
  return el("span", { className: "inst-pins", title: `Installée sur : ${ids.map(machineNameOf).join(", ")}` },
    ...ids.map((id) => icon(machineIcon(id), "sm")));
}

// Filtre « Installé sur… » : une entrée par machine joignable, et « nulle part »
function instFilterOptions() {
  if (!inst.loaded) return [];
  return [...inst.machines.filter((m) => m.ok).map((m) => [m.id, machineIcon(m.id), "t-sage", `Installé sur ${m.name}`]),
    ["none", null, "", "Installé nulle part"]];
}

// Fiche : état d'une image .wsquashfs sur les machines, et boutons
function renderInstBox() {
  const i = state.current, box = $("d-inst");
  box.hidden = !i || !isWsq(i);
  if (box.hidden) return;
  const ids = installedOn(imageFile(i));
  const down = inst.machines.filter((m) => !m.ok).length;
  let text = ids.length ? `Installé sur : ${ids.map(machineNameOf).join(", ")}` : "Installé nulle part";
  if (!inst.loaded) text = inst.error ? "État des machines indisponible" : "Lecture des machines…";
  else if (down) text += ` · ${down} machine${down > 1 ? "s" : ""} injoignable${down > 1 ? "s" : ""} (non vérifiée${down > 1 ? "s" : ""})`;
  $("d-inst-where").textContent = text;
  $("d-inst-uninstall").hidden = !ids.length;
}

// ---- dialogues

const DLG_INST = { install: "dlg-install", uninstall: "dlg-uninstall" };
function openInstall(images) {
  const todo = images.filter(isWsq);
  if (!todo.length) { toast("Seules les images .wsquashfs s'installent", "alert", true); return; }
  instDlg = { mode: "install", images: todo, skipped: images.length - todo.length, picked: null, edit: null, busy: false };
  paintInstDialog();
  $(DLG_INST.install).showModal();
  loadInstall();                 // l'état affiché est relu à l'ouverture
}
function openUninstall(images) {
  const todo = images.filter(isWsq);
  if (!todo.length) { toast("Seules les images .wsquashfs se désinstallent", "alert", true); return; }
  instDlg = { mode: "uninstall", images: todo, skipped: images.length - todo.length, picked: null, edit: null, busy: false };
  paintInstDialog();
  $(DLG_INST.uninstall).showModal();
  loadInstall();
}
for (const id of Object.values(DLG_INST)) $(id).addEventListener("close", () => { instDlg = null; });

// Une ligne par machine ; le dialogue entier est refait à chaque changement (peu d'éléments)
function paintInstDialog() {
  if (!instDlg) return;
  const d = instDlg, install = d.mode === "install", p = install ? "in" : "un";
  const dlg = $(DLG_INST[d.mode]);
  const files = d.images.map(imageFile);
  const n = d.images.length;
  const label = (f) => f.replace(/\.wsquashfs$/i, "");      // nom affiché, sans l'extension
  const shown = files.slice(0, 3).map((f) => `« ${label(f)} »`).join(", ") + (n > 3 ? ` et ${n - 3} autre${n - 3 > 1 ? "s" : ""}` : "");
  const skipped = d.skipped ? ` · ${d.skipped} ignorée${d.skipped > 1 ? "s" : ""} (pas une .wsquashfs)` : "";
  dlg.querySelector("h2").textContent = n === 1 ? `${install ? "Installer" : "Désinstaller"} « ${label(files[0])} » ${install ? "sur" : "de"}…`
    : `${install ? "Installer" : "Désinstaller"} ${n} images ${install ? "sur" : "de"}…`;
  const total = d.images.reduce((sum, i) => sum + (i.size || 0), 0);
  $(`${p}-images`).textContent = `${n} image${n > 1 ? "s" : ""} : ${shown}${install ? ` · ${size(total)}` : ""}${skipped}`;

  // Machines proposées : toutes (installer), ou celles qui ont au moins une de ces images
  const rows = [];
  const candidates = install ? inst.machines.map((m) => ({ m, plan: instPlan(m, d.images) }))
    : inst.machines.filter((m) => m.ok && d.images.some((i) => gamesOf(m).includes(imageFile(i)))).map((m) => ({ m }));
  const eligible = candidates.filter((c) => install ? canInstallOn(c.m, c.plan) : true).map((c) => c.m.id);
  d.picked = new Set([...(d.picked ?? eligible)].filter((id) => eligible.includes(id)));

  const status = [];
  if (inst.error) status.push(notice("err", `États des machines indisponibles : ${inst.error}`));
  else if (!inst.loaded) status.push(el("div", { className: "notice info" }, el("span", { className: "spinner" }), el("span", { textContent: "Lecture des machines…" })));
  else if (!install) {
    const absent = d.images.filter((i) => !installedOn(imageFile(i)).length).length;
    if (absent) status.push(notice("info", `${absent} image${absent > 1 ? "s" : ""} installée${absent > 1 ? "s" : ""} nulle part : ${absent > 1 ? "elles sont" : "elle est"} ignorée${absent > 1 ? "s" : ""}.`));
  }
  $(`${p}-status`).replaceChildren(...status);
  $(`${p}-refresh`).classList.toggle("spin", inst.loading);

  for (const { m, plan } of candidates) {
    const box = el("input", { type: "checkbox", id: `${p}-${m.id.replace(/\W/g, "_")}`, checked: d.picked.has(m.id), disabled: !eligible.includes(m.id) });
    box.onchange = () => { box.checked ? d.picked.add(m.id) : d.picked.delete(m.id); paintInstDialog(); };
    const chips = [];
    let cls = "", meta = null, dirNode = null;
    if (!m.ok) {
      cls = " none";
      chips.push(withTip(badge("injoignable", "err"), "Injoignable", [m.message || "pas de réponse"]));
    } else if (install) {
      if (!plan.todo.length) { cls = " none"; chips.push(badge("déjà installé", "ok")); }
      else {
        if (plan.done) chips.push(badge(`${plan.done} sur ${n} déjà là`, "info"));
        if (!plan.fits) { cls = " short"; chips.push(badge("espace insuffisant", "err")); }
        meta = el("div", { className: "in-meta" }, `libre ${size(m.free || 0)} · à envoyer ${size(plan.bytes)} (+ 1 Go de marge) · ${etaText(plan.bytes, m.rate)}`);
      }
      dirNode = dirLine(m, d);
    } else {
      const mine = files.filter((f) => gamesOf(m).includes(f));
      meta = el("div", { className: "in-meta mono", textContent: mine.join(", ") });
    }
    rows.push(el("div", { className: `in-row${cls}` }, box, icon(machineIcon(m.id), "sm"),
      el("div", { className: "in-body" },
        el("div", { className: "in-head" }, el("label", { htmlFor: box.id, className: "in-name", textContent: m.name }), ...chips),
        meta, dirNode)));
  }
  if (!rows.length && inst.loaded && !inst.error) {
    rows.push(el("p", { className: "muted", textContent: install ? "Aucune machine associée : onglet Sauvegardes, « Associer »."
      : "Ces images ne sont installées sur aucune machine joignable." }));
  }
  $(`${p}-targets`).replaceChildren(...rows);
  const run = $(`${p}-run`);
  run.disabled = d.busy || d.picked.size === 0;
  run.textContent = d.busy ? "Lancement…" : install ? "Installer" : "Désinstaller";
}

// Dossier d'installation : affichage, ou champ de saisie (« Modifier »)
function dirLine(m, d) {
  if (d.edit?.id === m.id) return dirEditor(m, d);
  return el("div", { className: "in-dir" },
    el("code", { textContent: m.dir || "dossier non défini" }),
    m.set ? null : el("em", { textContent: "(deviné)" }),
    el("button", { type: "button", className: "linklike", textContent: "Modifier",
      onclick: () => { d.edit = { id: m.id, value: m.dir || "", error: "", busy: false }; paintInstDialog(); } }));
}
function dirEditor(m, d) {
  const edit = d.edit;
  const input = el("input", { type: "text", className: "code", value: edit.value, disabled: edit.busy,
    placeholder: "/chemin/absolu, ex. /home/abc/games" });
  input.oninput = () => { edit.value = input.value; };
  input.onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); saveDir(m, input.value.trim()); } };
  const save = el("button", { type: "button", className: "btn primary sm", disabled: edit.busy, onclick: () => saveDir(m, input.value.trim()) },
    icon("check", "sm"), "Enregistrer");
  const guessed = el("button", { type: "button", className: "btn secondary sm", disabled: edit.busy, onclick: () => saveDir(m, "") },
    icon("undo", "sm"), "Revenir au dossier deviné");
  const cancel = el("button", { type: "button", className: "btn bare sm", onclick: () => { d.edit = null; paintInstDialog(); } }, "Annuler");
  setTimeout(() => input.focus(), 0);
  return el("div", { className: "in-edit" }, input, edit.error ? notice("err", edit.error) : null,
    el("div", { className: "row" }, save, guessed, cancel));
}
async function saveDir(m, dir) {
  const d = instDlg;
  if (!d) return;
  d.edit.busy = true;
  d.edit.error = "";
  paintInstDialog();
  try {
    await api(`/api/install/machines/${encodeURIComponent(m.id)}/dir`, { method: "POST", body: { dir } });
    d.edit = null;
    toast(dir ? "Dossier d'installation enregistré" : "Dossier deviné rétabli", "check");
    await loadInstall();          // un autre dossier, c'est une autre liste de jeux : état relu
  } catch (error) {
    if (instDlg !== d || !d.edit) return;
    d.edit.busy = false;
    d.edit.error = error.message;
    paintInstDialog();
  }
}

// Envoi ou retrait : POST, puis le suivi passe par la file de tâches (refreshTasks relit l'état)
async function runInstallDialog(install) {
  const d = instDlg;
  if (!d || !d.picked.size) return;
  const machines = [...d.picked];
  d.busy = true;
  paintInstDialog();
  try {
    let data;
    if (install) {
      data = await api("/api/install", { method: "POST", body: { ids: d.images.map((i) => i.id), machines } });
    } else {
      const names = [...new Set(d.images.map(imageFile))].filter((f) => machines.some((id) => gamesOf(machineOf(id) || {}).includes(f)));
      data = await api("/api/uninstall", { method: "POST", body: { names, machines } });
    }
    $(DLG_INST[d.mode]).close();
    const count = `${data.tasks.length} tâche${data.tasks.length > 1 ? "s" : ""}`;
    toast(install ? `Installation lancée (${count})` : `Désinstallation lancée (${count})`, install ? "drive" : "trash");
    refreshTasks();
  } catch (error) {
    d.busy = false;
    toast(error.message, "alert", true);
    paintInstDialog();
  }
}

// Résultat par machine d'une tâche install / uninstall (dans le détail de la tâche)
function installResultNode(r) {
  const items = r.results || [];
  const failed = items.filter((x) => !x.ok).length;
  return el("div", { className: `sync-res${failed ? " err" : ""}` },
    el("strong", { textContent: failed ? `${failed} échec(s) sur ${items.length}` : `${items.length} machine(s) sans erreur` }),
    el("ul", { className: "sync-list" }, ...items.map((x) => el("li", { className: x.ok ? "ok" : "err",
      textContent: `${x.ok ? "✓" : "✗"} ${x.machine} : ${x.message}` }))));
}

$("in-run").onclick = () => runInstallDialog(true);
$("un-run").onclick = () => runInstallDialog(false);
$("in-refresh").onclick = () => loadInstall();
$("un-refresh").onclick = () => loadInstall();
$("d-inst-install").onclick = () => openInstall([state.current]);
$("d-inst-uninstall").onclick = () => openUninstall([state.current]);
$("btn-inst-install").onclick = () => openInstall(selectedImages());
$("btn-inst-uninstall").onclick = () => openUninstall(selectedImages());

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
  loadSaves();                 // compteur de l'onglet et icônes de la liste (sans bloquer)
  loadInstall();               // images installées par machine : filtre, pastilles, fiche
  refreshTasks();
  if (["folders", "saves", "help"].includes(storage.get("wsfs-view"))) showView(storage.get("wsfs-view"));
})();
