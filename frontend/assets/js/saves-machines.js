// saves-machines.js — Clients de sauvegarde : menus, icônes, dossiers des jeux, oubli.

import { api } from "./api.js";
import { $, el, icon, toast } from "./core.js";
import { closeMenu } from "./menu.js";
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
  if (m.ok) nodes.push(menuItem("Dossiers synchronisés…", "folder", () => openDirs(m)));
  if (m.removable) nodes.push(menuItem("Oublier ce client", "trash", () => forgetMachine(m), { danger: true }));
  return nodes;
}

// Dossiers synchronisés d'un client (#dlg-dirs, agent v8) : liste, déclaration, retrait.
let dirsMachine = null;

function paintDirs(data) {
  const rows = data.dirs.map((d) => {
    const del = el("button", { type: "button", className: "iconbtn bare sm", title: "Retirer la déclaration (les fichiers restent)",
      "aria-label": `Retirer ${d.name}` }, icon("trash", "sm"));
    del.onclick = () => dirsCall("DELETE", `?name=${encodeURIComponent(d.name)}`);
    return el("div", { className: "dirs-row" }, el("b", { textContent: d.name }),
      el("code", { textContent: d.path.startsWith("/") ? d.path : `~/${d.path}` }), del);
  });
  $("dirs-list").replaceChildren(...(rows.length ? rows : [el("p", { className: "muted", textContent: "Aucun dossier déclaré sur ce client." })]));
  $("dirs-eden").textContent = data.eden
    ? `Switch : Eden est installé (${data.eden.startsWith("/") ? data.eden : `~/${data.eden}`}) ; ses sauvegardes sont synchronisées jeu par jeu, sans rien déclarer.`
    : "Switch : Eden n'est pas installé sur ce client.";
}

async function dirsCall(method, query = "", body = undefined) {
  $("dirs-err").hidden = true;
  try {
    paintDirs(await api(`/api/saves/machines/${encodeURIComponent(dirsMachine.id)}/dirs${query}`, { method, body }));
    if (method !== "GET") loadSaves();
    return true;
  } catch (error) {
    $("dirs-err").textContent = error.message;
    $("dirs-err").hidden = false;
    return false;
  }
}

function openDirs(m) {
  dirsMachine = m;
  $("dirs-title").textContent = `Dossiers synchronisés · ${m.name}`;
  $("dirs-list").replaceChildren(el("p", { className: "muted", textContent: "Lecture…" }));
  $("dirs-eden").textContent = "";
  $("dirs-name").value = "";
  $("dirs-path").value = "";
  $("dirs-err").hidden = true;
  $("dlg-dirs").showModal();
  dirsCall("GET");
}

// Entrée de menu : bouton (cochée, danger) avec icône optionnelle
export function menuItem(label, iconName, action, { checked = null, danger = false, role = "menuitem" } = {}) {
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
    toast("Client oublié");
    loadSaves();
  } catch (error) { toast(error.message, "alert", true); }
}

// Présence d'un jeu sur un client (utilisée par le bandeau et les cibles)
export const onMachine = (g, id) => PRESENT_STATES.has(stateOf(g, id)?.state);

export function init() {
  $("dirs-add").onclick = async () => {
    const name = $("dirs-name").value.trim(), path = $("dirs-path").value.trim();
    if (!name || !path) {
      $("dirs-err").textContent = "Nom et chemin requis.";
      $("dirs-err").hidden = false;
      return;
    }
    if (await dirsCall("PUT", "", { name, path })) {
      $("dirs-name").value = "";
      $("dirs-path").value = "";
    }
  };
  $("dirs-path").addEventListener("keydown", (event) => { if (event.key === "Enter") $("dirs-add").click(); });
}
