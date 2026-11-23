// header.js — En-tête : vues, clients, menu ⚙, actualisation.

import { $, bigSize, el, hideAnimated, icon, isShown, motionOff, replay, showAnimated, state, storage } from "./core.js";
import { loadConfig } from "./config.js";
import { loadFolders } from "./folders.js";
import { loadHelp } from "./help.js";
import { inst } from "./install.js";
import { renderLibrary } from "./library.js";
import { playingOn } from "./activity.js";
import { connected } from "./mode.js";
import { loadSaves, osIcon, paintSavesView, sv } from "./saves.js";
import { openSettings, paintSettingsClients, settingsBusy, settingsChanges } from "./settings.js";
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
  swapView(name);
  storage.set("wsfs-view", name);
  if (name === "folders") loadFolders();
  if (name === "config") loadConfig();
  if (name === "saves") loadSaves();
  if (name === "images" && state.items.length) renderLibrary();   // icônes « Sauveg. » à jour
  if (name === "help") return loadHelp();
  if (name === "settings") return openSettings();
  return Promise.resolve();
}

// Changement de vue animé : la vue quittée s'efface (VIEW_OUT ms), puis la nouvelle arrive du
// côté de l'onglet choisi (ordre de la navigation). Un clic pendant la transition l'emporte.
const VIEW_IDS = { saves: "saves-main", images: "main", folders: "folders-main", config: "config-main", help: "help-main", settings: "settings-main" };
const VIEW_ORDER = ["saves", "images", "folders", "config", "help", "settings"];
const VIEW_OUT = 150;
let viewTurn = 0;
let shownView = null;

function swapView(name) {
  const turn = ++viewTurn;
  const prev = shownView;
  shownView = name;
  const target = $(VIEW_IDS[name]);
  const others = Object.entries(VIEW_IDS).filter(([v]) => v !== name).map(([, id]) => $(id));
  const leaving = others.filter((n) => !n.hidden);
  const reveal = () => {
    if (turn !== viewTurn) return;
    for (const n of others) { n.hidden = true; n.classList.remove("view-out", "view-in-r", "view-in-l"); }
    target.classList.remove("view-out", "view-in-r", "view-in-l");
    target.hidden = false;
    if (motionOff() || !prev || prev === name) return;
    const dir = VIEW_ORDER.indexOf(name) >= VIEW_ORDER.indexOf(prev) ? "view-in-r" : "view-in-l";
    replay(target, dir);
    // Classe retirée à la fin : une animation de transformation encore attachée à la vue servirait de
    // repère aux éléments fixes qu'elle contient (barre « Enregistrer », barres de sélection, menus)
    const done = (event) => {
      if (event.target !== target) return;              // fin d'une animation d'un enfant (lignes…)
      target.classList.remove("view-in-r", "view-in-l");
      target.removeEventListener("animationend", done);
    };
    target.addEventListener("animationend", done);
  };
  if (motionOff() || !leaving.length || leaving.includes(target)) { reveal(); return; }
  for (const n of leaving) n.classList.add("view-out");
  setTimeout(reveal, VIEW_OUT);
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

// Clients associés : un bouton par client, puis « Toutes » ; le choix reste d'une visite à l'autre.
// Le client choisi filtre la liste des images et la vue Sauvegardes (voir library.js, saves.js).
const CLIENT_KEY = "wsfs-client";

let clientsSig = "";

function clientList() {
  return (inst.loaded ? inst.machines : sv.machines).map((m) => ({ id: m.id, name: m.name, ok: Boolean(m.ok),
    free: inst.machines.find((x) => x.id === m.id)?.free ?? null, icon: sv.machines.find((x) => x.id === m.id)?.icon ?? null,
    playing: playingOn(m.id).map((p) => p.game) }));
}

// Client choisi dans l'en-tête ({ id, name, ok, free }), ou null : « Toutes » ou mode autonome
export function chosenClient() {
  if (!connected() || state.client === "all") return null;
  return clientList().find((c) => c.id === state.client) ?? null;
}

// Nouveau choix : le mémorise, puis remet à jour les vues qui le suivent
function chooseClient(id) {
  if (id !== state.client) {
    // Une sélection faite sur un autre client ne doit pas agir sur des lignes devenues invisibles
    state.selected.clear();
    sv.selected.clear();
  }
  state.client = id;
  storage.set(CLIENT_KEY, id);
  paintClients();
  if (!$("main").hidden) renderLibrary();
  if (sv.loaded) paintSavesView();     // aussi le compteur de l'onglet, qui suit le client
  paintSettingsClients();
}

export function paintClients() {
  const list = connected() ? clientList() : [];
  $("clients").hidden = !list.length;
  if (!list.length) return;
  const seg = $("client-seg");
  const known = (id) => id === "all" || list.some((c) => c.id === id);
  if (!known(state.client)) state.client = known(storage.get(CLIENT_KEY)) ? storage.get(CLIENT_KEY) : list[0].id;
  const sig = JSON.stringify(list.map((c) => [c.id, c.name, c.ok, c.icon, c.playing]));
  if (sig !== clientsSig) {
    clientsSig = sig;
    const choices = [...list, { id: "all", name: "Tous", ok: null }];
    // L'indicateur est gardé : recréé, il repartirait de zéro au lieu de glisser jusqu'au choix
    const ind = seg.querySelector(":scope > .ind") ?? el("span", { className: "ind", ariaHidden: "true" });
    seg.replaceChildren(ind, ...choices.map((c) => {
      const playing = c.ok !== null && c.playing.length > 0;
      const b = el("button", { type: "button", role: "radio",
        title: c.ok === null ? `${list.length} clients` : playing ? `${c.name} : en jeu (${c.playing.join(", ")})`
          : `${c.name} : ${c.ok ? "en ligne" : "hors ligne"}` },
        c.ok === null ? null : el("span", { className: `dot${c.ok ? " ok" : ""}${playing ? " playing" : ""}` }),
        c.ok === null ? null : osIcon(c.icon, "sm"), el("span", { textContent: c.name }),
        playing ? el("span", { className: "cplay", title: c.playing.join(", ") }, icon("play", "xs")) : null);
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
  const text = !c ? `${list.length} client${list.length > 1 ? "s" : ""}` : c.playing.length ? `En jeu · ${c.playing.join(", ")}`
    : !c.ok ? "Hors ligne" : c.free ? `${bigSize(c.free)} libres` : "En ligne";
  const info = $("client-info");
  if (info.textContent !== text) {
    info.textContent = text;
    replay(info, "fade");
  }
}

// Menu ⚙ : Réglages, Actualiser (avec l'âge du dernier chargement), Aide
export function paintRefreshAgo() {
  const min = state.loadedAt ? Math.floor((Date.now() - state.loadedAt) / 60000) : null;
  $("refresh-ago").textContent = min === null ? "" : min < 1 ? "à l'instant" : `il y a ${min} min`;
}

export function setTopMenu(open) {
  if (open) showAnimated($("top-menu"), "is-leaving"); else hideAnimated($("top-menu"), "is-leaving", 200);
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
    chooseClient(b.dataset.client);
  };
  $("btn-menu").onclick = (event) => { event.stopPropagation(); setTopMenu(!isShown($("top-menu"))); };
  $("top-menu").addEventListener("click", (event) => { if (event.target.closest("button")) setTopMenu(false); });
  document.addEventListener("click", (event) => {
    if (!$("top-menu").hidden && !event.target.closest("#top-menu, #btn-menu")) setTopMenu(false);
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("top-menu").hidden) setTopMenu(false); });
  $("menu-help").onclick = () => { showView("help"); window.scrollTo(0, 0); };
}
