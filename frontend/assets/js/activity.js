// activity.js — Activité des clients (agent 11) : jeux en cours, manettes branchées, événements de
// partie (lancée, terminée, plantée) et mise à jour du lanceur d'un client.
// L'interface lit le manager (GET /api/clients/activity) toutes les POLL ms quand l'onglet est
// visible : jamais les clients eux-mêmes (ils envoient leurs événements au manager).

import { api } from "./api.js";
import { toast } from "./core.js";
import { paintClients } from "./header.js";
import { connected } from "./mode.js";
import { loadSaves } from "./saves.js";
import { paintSettingsClients } from "./settings.js";

const POLL = 20000;

export const act = { live: {}, controllers: {}, seq: null };

// Jeux en cours sur un client : [{game, since}]
export const playingOn = (id) => act.live[id] || [];

// Manettes branchées sur un client : [{kind, name, bus}]
export const controllersOf = (id) => act.controllers[id] || [];

// Icône d'une famille de manettes (icônes animées pour Xbox et DualSense)
export const PAD_ICON = { dualsense: "dualsense", playstation: "dualsense", xbox: "xbox", steam: "steamos", nintendo: "gamepad", gamepad: "gamepad" };

export const PAD_LABEL = { dualsense: "DualSense", playstation: "Manette PlayStation", xbox: "Manette Xbox", steam: "Steam Deck",
  nintendo: "Manette Nintendo", gamepad: "Manette" };

// Durée lisible : « 42 s », « 12 min », « 1 h 05 »
export function spanText(seconds) {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} s`;
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
}

function notify(ev) {
  if (ev.event === "start") {
    toast(`${ev.name} : ${ev.game} lancé`, "play");
  } else if (ev.crash) {
    toast(`${ev.name} : ${ev.game} s'est arrêté au bout de ${spanText(ev.duration)} (code ${ev.code})`, "alert", true);
  } else {
    toast(`${ev.name} : ${ev.game} terminé${ev.duration ? ` · ${spanText(ev.duration)}` : ""}`, "check");
  }
}

let busy = false;

// Relit l'activité ; les événements arrivés depuis la dernière lecture deviennent des messages
// (pas à la première lecture : ce sont des événements anciens).
export async function loadActivity() {
  if (!connected() || busy) return;
  busy = true;
  try {
    const first = act.seq === null;
    const r = await api(`/api/clients/activity?since=${first ? 0 : act.seq}`, { timeout: 15000 });
    act.live = r.live || {};
    act.controllers = r.controllers || {};
    if (!first) (r.events || []).forEach(notify);
    act.seq = r.seq;
    paintClients();
    paintSettingsClients();
  } catch { /* manager momentanément injoignable : prochaine lecture */ } finally {
    busy = false;
  }
}

const stripAnsi = (text) => String(text || "").replace(/\x1b\[[0-9;]*m/g, "");

// Mise à jour du lanceur d'un client (binaire publié, vérifié par son empreinte) ; l'agent se relance
export async function updateLauncher(m) {
  if (!confirm(`Mettre à jour le lanceur de « ${m.name} » ?\n\nLe binaire publié est vérifié par son empreinte avant de remplacer l'actuel ; l'agent redémarre ensuite (quelques secondes hors ligne).`)) return;
  toast(`${m.name} : mise à jour du lanceur…`, "refresh");
  try {
    const r = await api(`/api/saves/machines/${encodeURIComponent(m.id)}/update`, { method: "POST", timeout: 340000 });
    if (r.code !== 0) {
      const last = stripAnsi(r.output).trim().split("\n").pop() || `code ${r.code}`;
      toast(`${m.name} : mise à jour impossible : ${last}`, "alert", true);
    } else if (r.restart) {
      toast(`${m.name} : lanceur mis à jour (${r.before} → ${r.after}), agent relancé`, "check");
      setTimeout(loadSaves, 4000);
    } else {
      toast(`${m.name} : lanceur déjà à jour (${r.after || r.before})`, "check");
    }
  } catch (error) {
    toast(`${m.name} : ${error.message}`, "alert", true);
  }
}

export function init() {
  setInterval(() => { if (!document.hidden) loadActivity(); }, POLL);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadActivity(); });
}
