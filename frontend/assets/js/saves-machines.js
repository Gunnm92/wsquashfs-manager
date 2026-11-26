// saves-machines.js — Clients de sauvegarde : menus, icônes, dossiers des jeux, oubli.

import { api } from "./api.js";
import { $, el, icon, toast } from "./core.js";
import { closeMenu } from "./menu.js";
import { updateLauncher } from "./activity.js";
import { isTrial, openDoctor, openMaint, useForTrials } from "./mode.js";
import { OS_ICONS, OS_LABELS, PRESENT_STATES, loadSaves, stateOf } from "./saves.js";

// Menu d'un client : client d'essai (choix, diagnostic, maintenance), icône (détectée ou
// choisie), dossiers synchronisés, puis oubli (sauf le client d'essai du lanceur local ou agent)
export function machineMenu(m) {
  const choice = (label, key, checked, onpick) => menuItem(label, key, onpick, { checked, role: "menuitemradio" });
  const trial = isTrial(m);
  const nodes = [];
  if (m.kind === "saved") nodes.push(choice("Utiliser pour les essais", "flask", trial, () => useForTrials(m)));
  if (trial) nodes.push(menuItem("Diagnostic du lanceur…", "info", openDoctor), menuItem("Maintenance…", "settings", () => openMaint(m)));
  nodes.push(
    el("div", { className: "dd-sub", textContent: "Icône" }),
    choice(`Détectée (${({ linux: "Linux", steamos: "SteamOS", batocera: "Batocera" })[m.os] || m.os || "Linux"})`, m.icon || "linux", !m.icon_set, () => setMachineIcon(m, null)),
    ...OS_ICONS.map((key) => choice(OS_LABELS[key], key, m.icon_set && m.icon === key, () => setMachineIcon(m, key))));
  if (m.ok) nodes.push(menuItem("Dossiers synchronisés…", "folder", openDirs));
  // Agent 11 : mise à jour du lanceur à distance (binaire vérifié par son empreinte, agent relancé)
  if (m.ok && Number(m.agent) >= 11) nodes.push(menuItem("Mettre à jour le lanceur…", "refresh", () => updateLauncher(m)));
  if (m.removable) nodes.push(menuItem("Oublier ce client", "trash", () => forgetMachine(m), { danger: true }));
  return nodes;
}

// Dossiers synchronisés : section des réglages (tous les clients à la fois)
function openDirs() {
  $("btn-config").click();
  requestAnimationFrame(() => $("st-syncdirs").scrollIntoView({ block: "start" }));
}

// Entrée de menu : bouton (cochée, danger) avec icône optionnelle
export function menuItem(label, iconName, action, { checked = null, danger = false, role = "menuitem", tag = "" } = {}) {
  // La place de la coche n'existe que dans un menu de choix (durée, icône, source) : les libellés restent alignés
  const b = el("button", { type: "button", className: `menu-item${danger ? " danger" : ""}`, role, "aria-checked": checked === null ? undefined : String(checked) },
    checked === null ? null : el("span", { className: "nocheck" }, checked ? icon("check", "sm") : null),
    icon(iconName, "sm"), el("span", { textContent: label }, tag ? el("em", { className: "dd-tag", textContent: tag }) : null));
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
    toast("Client oublié");
    loadSaves();
  } catch (error) { toast(error.message, "alert", true); }
}

// Présence d'un jeu sur un client (utilisée par le bandeau et les cibles)
export const onMachine = (g, id) => PRESENT_STATES.has(stateOf(g, id)?.state);

export function init() {}
