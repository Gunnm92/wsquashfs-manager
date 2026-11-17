// art.js — Illustrations : chargement à la demande, cache, vignettes ; bouton Actualiser.

import { $, el, state, storage } from "./core.js";

// Illustration intégrée à l'image (.wsquashfs/cover) ou au dossier à empaqueter
// (.wsquashfs/cover du dossier) : seule source affichée. Chargée à la demande, 4 à la
// fois, gardée pour la session. Repli : initiales sur un dégradé.

// epoch : incrémentée à chaque Actualiser (illustrations Steam relues) ; purge : requête de purge serveur
const art = { cache: new Map(), done: new Map(), waiting: [], active: 0, epoch: 0, purge: null };

const hueOf = (name) => [...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);

const initials = (name) => name.split(/[\s\-:]+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();

export function paintInitials(node, name) {
  const hue = hueOf(name);
  node.textContent = initials(name);
  node.style.background = `linear-gradient(145deg, oklch(0.56 0.12 ${hue}), oklch(0.3 0.07 ${(hue + 40) % 360}))`;
  node.classList.remove("icon", "es", "steam");
}

// Version de l'illustration d'une image : change avec son mtime et avec l'exécutable de
// CMD (adresse différente, donc jamais l'ancienne servie par le cache HTTP). Un dossier n'a
// pas de version : sa route n'est pas mise en cache durablement (Actualiser la redemande).
const artVersion = (item) => `${item.mtime ?? ""}|${item.dir || ""}|${item.cmd || ""}`;

// source : « image » (/api/image/art) ou « folder » (/api/folders/art, dossier à empaqueter)
const artKey = (item, source) => `${source}:${item.id}:${artVersion(item)}:${art.epoch}`;

const artUrl = (item, source) => source === "folder"
  ? `/api/folders/art?id=${encodeURIComponent(item.id)}&e=${art.epoch}`
  : `/api/image/art?id=${encodeURIComponent(item.id)}&v=${encodeURIComponent(artVersion(item))}&e=${art.epoch}`;

// Vignette → { item, source } : ce que l'observateur doit charger quand elle apparaît
const artRefs = new WeakMap();

function loadArt(item, source) {
  const key = artKey(item, source);
  if (!art.cache.has(key)) {
    art.cache.set(key, new Promise((resolve) => { art.waiting.push({ item, source, resolve }); pumpArt(); })
      .then((result) => { art.done.set(key, result); return result; }));
  }
  return art.cache.get(key);
}

function pumpArt() {
  while (art.active < 4 && art.waiting.length) {
    const { item, source, resolve } = art.waiting.shift();
    art.active++;
    const url = artUrl(item, source);
    // Après une purge (Actualiser), les images ne sont demandées qu'une fois le serveur purgé
    (art.purge || Promise.resolve())
      .then(() => fetch(url, { headers: { "x-wsfs-token": storage.get("wsfs-token") || "" } }))
      .then(async (r) => r.ok ? { url: URL.createObjectURL(await r.blob()), kind: r.headers.get("X-Art-Kind") || "embedded" } : null)
      .catch(() => null)
      .then((result) => { art.active--; resolve(result); pumpArt(); });
  }
}

export function putArt(node, a, animate) {
  node.textContent = "";
  node.style.background = "";
  node.classList.add(a.kind);
  node.append(el("img", { src: a.url, alt: "", className: animate ? "fade-in" : "" }));
}

// Vrai si l'illustration était déjà connue (posée sans attendre)
function artReady(node, item, source = "image") {
  const key = artKey(item, source);
  if (!art.done.has(key)) return false;
  const a = art.done.get(key);
  if (a) putArt(node, a, false); else paintInitials(node, item.name);
  return true;
}

export async function showArt(node, item, source = "image") {
  if (artReady(node, item, source)) return;
  paintInitials(node, item.name);
  const a = await loadArt(item, source);
  if (!a || node.dataset.id !== item.id) return;
  putArt(node, a, true);
}

const artObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    artObserver.unobserve(entry.target);
    const ref = artRefs.get(entry.target);
    // Une image est relue dans la bibliothèque (elle peut avoir changé depuis le rendu)
    const item = ref && (ref.source === "folder" ? ref.item : state.byId.get(ref.item.id));
    if (item) showArt(entry.target, item, ref.source);
  }
}, { rootMargin: "200px" });

// source : « image » (bibliothèque) ou « folder » (dossiers à empaqueter)
export function thumb(item, className = "thumb", source = "image") {
  const node = el("span", { className });
  node.dataset.id = item.id;
  artRefs.set(node, { item, source });
  if (!artReady(node, item, source)) {
    paintInitials(node, item.name);
    artObserver.observe(node);
  }
  return node;
}

// Actualiser (bouton de l'en-tête, déjà relié à loadLibrary) : nouvelle époque, le serveur
// revalide l'illustration Steam de chaque jeu, les images affichées sont redemandées
let refreshTurns = 0;

export function init() {
  $("btn-refresh").addEventListener("click", () => {
    // Un tour complet à chaque clic (rotation cumulée, animée par la transition de .ico)
    refreshTurns += 360;
    $("btn-refresh").querySelector(".ico").style.transform = `rotate(${refreshTurns}deg)`;
    art.epoch++;
    art.cache.clear();
    art.done.clear();
    art.purge = fetch("/api/image/art/forget", { method: "POST", headers: { "x-wsfs-token": storage.get("wsfs-token") || "" } })
      .catch(() => null);
  });
}
