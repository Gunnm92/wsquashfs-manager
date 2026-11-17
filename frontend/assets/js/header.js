// header.js — En-tête : vues, clients, menu ⚙, actualisation.

import { $, bigSize, el, state, storage } from "./core.js";
import { loadFolders } from "./folders.js";
import { loadHelp } from "./help.js";
import { inst } from "./install.js";
import { renderLibrary } from "./library.js";
import { connected } from "./mode.js";
import { loadSaves, sv } from "./saves.js";
import { openSettings, settingsBusy, settingsChanges } from "./settings.js";
import { setTasksOpen } from "./tasks.js";

export function showView(name) {
  if (name === "saves" && !connected()) name = "images";     // autonome : pas d'onglet Sauvegardes
  // Quitter la page Réglages avec des modifications non enregistrées : on demande d'abord
  if (document.body.dataset.view === "settings" && name !== "settings" && !settingsBusy
      && settingsChanges().length && !confirm("Quitter les réglages sans enregistrer les modifications ?")) {
    return Promise.resolve();
  }
  document.body.dataset.view = name;
  paintViews(name);
  $("btn-menu").classList.toggle("on", name === "settings");   // le ⚙ de l'en-tête montre la page ouverte
  $("main").hidden = name !== "images";
  $("folders-main").hidden = name !== "folders";
  $("saves-main").hidden = name !== "saves";
  $("help-main").hidden = name !== "help";
  $("settings-main").hidden = name !== "settings";
  storage.set("wsfs-view", name);
  if (name === "folders") loadFolders();
  if (name === "saves") loadSaves();
  if (name === "images" && state.items.length) renderLibrary();   // icônes « Sauveg. » à jour
  if (name === "help") return loadHelp();
  if (name === "settings") return openSettings();
  return Promise.resolve();
}

// Indicateur qui glisse derrière le choix actif d'un groupe à pastille (.seg > .ind)
export function placeInd(seg) {
  const ind = seg.querySelector(":scope > .ind"), on = seg.querySelector(":scope > button.on");
  if (!ind) return;
  ind.hidden = !on;
  if (!on) return;
  ind.style.left = `${on.offsetLeft}px`;
  ind.style.width = `${on.offsetWidth}px`;
  requestAnimationFrame(() => seg.classList.add("ready"));   // pas d'animation à la première pose
}

function paintViews(name) {
  for (const b of $("views").querySelectorAll("button[data-view]")) {
    const on = b.dataset.view === name;
    b.classList.toggle("on", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
  placeInd($("views"));
}

// Clients associés : un bouton par client, puis « Toutes » ; le choix reste d'une visite à l'autre
const CLIENT_KEY = "wsfs-client";

let clientsSig = "";

function clientList() {
  return (inst.loaded ? inst.machines : sv.machines).map((m) => ({ id: m.id, name: m.name, ok: Boolean(m.ok),
    free: inst.machines.find((x) => x.id === m.id)?.free ?? null }));
}

export function paintClients() {
  const list = connected() ? clientList() : [];
  $("clients").hidden = !list.length;
  if (!list.length) return;
  const seg = $("client-seg");
  const known = (id) => id === "all" || list.some((c) => c.id === id);
  if (!known(state.client)) state.client = known(storage.get(CLIENT_KEY)) ? storage.get(CLIENT_KEY) : list[0].id;
  const sig = JSON.stringify(list.map((c) => [c.id, c.name, c.ok]));
  if (sig !== clientsSig) {
    clientsSig = sig;
    const choices = [...list, { id: "all", name: "Toutes", ok: null }];
    seg.replaceChildren(el("span", { className: "ind", ariaHidden: "true" }), ...choices.map((c) => {
      const b = el("button", { type: "button", role: "radio",
        title: c.ok === null ? `${list.length} clients` : `${c.name} : ${c.ok ? "en ligne" : "hors ligne"}` },
        c.ok === null ? null : el("span", { className: `dot${c.ok ? " ok" : ""}` }), el("span", { textContent: c.name }));
      b.dataset.client = c.id;
      return b;
    }));
  }
  for (const b of seg.querySelectorAll("button[data-client]")) {
    const on = b.dataset.client === state.client;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", String(on));
  }
  placeInd(seg);
  const c = state.client === "all" ? null : list.find((x) => x.id === state.client);
  const text = !c ? `${list.length} client${list.length > 1 ? "s" : ""}` : !c.ok ? "Hors ligne" : c.free ? `${bigSize(c.free)} libres` : "En ligne";
  const info = $("client-info");
  if (info.textContent !== text) {
    info.textContent = text;
    info.classList.remove("fade");
    void info.offsetWidth;
    info.classList.add("fade");
  }
}

// Menu ⚙ : Réglages, Actualiser (avec l'âge du dernier chargement), Aide
export function paintRefreshAgo() {
  const min = state.loadedAt ? Math.floor((Date.now() - state.loadedAt) / 60000) : null;
  $("refresh-ago").textContent = min === null ? "" : min < 1 ? "à l'instant" : `il y a ${min} min`;
}

export function setTopMenu(open) {
  $("top-menu").hidden = !open;
  $("btn-menu").setAttribute("aria-expanded", String(open));
  if (open) { paintRefreshAgo(); setTasksOpen(false); }
}

export function init() {
  $("views").onclick = (event) => {
    const button = event.target.closest("button[data-view]");
    if (button) { showView(button.dataset.view); window.scrollTo(0, 0); }
  };
  new ResizeObserver(() => placeInd($("views"))).observe($("views"));
  new ResizeObserver(() => placeInd($("client-seg"))).observe($("client-seg"));
  $("client-seg").onclick = (event) => {
    const b = event.target.closest("button[data-client]");
    if (!b) return;
    state.client = b.dataset.client;
    storage.set(CLIENT_KEY, state.client);
    paintClients();
  };
  $("btn-menu").onclick = (event) => { event.stopPropagation(); setTopMenu($("top-menu").hidden); };
  $("top-menu").addEventListener("click", (event) => { if (event.target.closest("button")) setTopMenu(false); });
  document.addEventListener("click", (event) => {
    if (!$("top-menu").hidden && !event.target.closest("#top-menu, #btn-menu")) setTopMenu(false);
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("top-menu").hidden) setTopMenu(false); });
  $("menu-help").onclick = () => { showView("help"); window.scrollTo(0, 0); };
}
