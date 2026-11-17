// install.js — Installation sur les clients : état, pastilles, filtres.

import { api } from "./api.js";
import { $, el, icon, state } from "./core.js";
import { paintClients } from "./header.js";
import { paintInstDialog } from "./install-dialog.js";
import { renderLibrary } from "./library.js";
import { connected } from "./mode.js";
import { OS_ICONS, sv } from "./saves.js";
import { paintSettingsClients } from "./settings.js";

// Une image .wsquashfs est copiée dans le dossier des jeux d'un client associé (les clients de
// l'onglet Sauvegardes) : une tâche « install » par image, puis l'état des clients est relu à la fin.
// Désinstaller retire l'image du client ; elle reste dans la bibliothèque.

export const inst = { machines: [], loaded: false, error: "", loading: false, seq: 0 };

export const instDialog = { current: null };             // dialogue ouvert : { mode, images, skipped, picked, edit, busy }

const GIB = 1024 ** 3;          // marge d'espace libre demandée au-delà de la taille à envoyer

const LINK_RATE = 100e6;        // débit supposé sans mesure (filaire), en octets/s

export const isWsq = (i) => /\.wsquashfs$/i.test(i.path || i.id);

export const imageFile = (i) => (i.path || i.id).split("/").pop();

const iconKey = (key) => OS_ICONS.includes(key) || key === "server" ? key : "linux";

export const machineIcon = (id) => iconKey(sv.machines.find((m) => m.id === id)?.icon);

export const machineOf = (id) => inst.machines.find((m) => m.id === id);

const machineNameOf = (id) => machineOf(id)?.name ?? id;

export const gamesOf = (m) => m.games || [];

export const selectedImages = () => [...state.selected].map((id) => state.byId.get(id)).filter(Boolean);

// Durée estimée : dernier débit réellement mesuré vers ce client (m.rate), sinon le filaire supposé
export function etaText(bytes, rate) {
  const min = Math.round(bytes / (rate || LINK_RATE) / 60);
  const span = min < 1 ? "moins d'1 min" : min < 60 ? `~${min} min` : `~${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")}`;
  const mo = (rate || LINK_RATE) / 1e6;
  return `${span} (${rate ? `débit mesuré ${mo < 10 ? mo.toFixed(1).replace(".", ",") : Math.round(mo)} Mo/s` : "débit supposé 100 Mo/s"})`;
}

// Identifiants des clients joignables où ce fichier est installé
export const installedOn = (file) => inst.machines.filter((m) => m.ok && gamesOf(m).includes(file)).map((m) => m.id);

// Ce qui reste à envoyer sur un client pour ces images (un envoi coupé est compté à sa reprise)
export function instPlan(m, images) {
  const have = new Set(gamesOf(m));
  const parts = new Map((m.parts || []).map((p) => [p.name, p.size]));
  const todo = images.filter((i) => !have.has(imageFile(i)));
  const bytes = todo.reduce((n, i) => n + Math.max(0, (i.size || 0) - (parts.get(imageFile(i)) || 0)), 0);
  return { todo, bytes, done: images.length - todo.length, fits: (m.free || 0) >= bytes + GIB };
}

export const canInstallOn = (m, plan) => m.ok && plan.todo.length > 0 && plan.fits;

export async function loadInstall() {
  if (!connected()) return;
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
  paintSettingsClients();
}

export function paintInstall() {
  if (state.items.length && !$("main").hidden) renderLibrary();   // filtre, pastilles
  renderInstBox();
  paintClients();
  paintInstDialog();
}

// Pastilles discrètes : icône de chaque client où l'image est installée (liste et jaquettes)
export function instPins(i) {
  const ids = isWsq(i) ? installedOn(imageFile(i)) : [];
  if (!ids.length) return null;
  return el("span", { className: "inst-pins", title: `Installée sur : ${ids.map(machineNameOf).join(", ")}` },
    ...ids.map((id) => icon(machineIcon(id), "sm")));
}

// Filtre « Installé sur… » : une entrée par client joignable, et « nulle part »
export function instFilterOptions() {
  if (!inst.loaded) return [];
  return [...inst.machines.filter((m) => m.ok).map((m) => [m.id, machineIcon(m.id), "t-sage", `Installé sur ${m.name}`]),
    ["none", null, "", "Installé nulle part"]];
}

// Fiche : état d'une image .wsquashfs sur les clients, et boutons
export function renderInstBox() {
  const i = state.current, box = $("d-inst");
  box.hidden = !i || !isWsq(i);
  if (box.hidden) return;
  const ids = installedOn(imageFile(i));
  const down = inst.machines.filter((m) => !m.ok).length;
  let text = ids.length ? ids.map(machineNameOf).join(", ") : "nulle part";
  if (!inst.loaded) text = inst.error ? "État des clients indisponible" : "Lecture des clients…";
  else if (down) text += ` · ${down} client${down > 1 ? "s" : ""} injoignable${down > 1 ? "s" : ""} (non vérifié${down > 1 ? "s" : ""})`;
  $("d-inst-where").textContent = text;
  $("d-inst-uninstall").hidden = !ids.length;
}
