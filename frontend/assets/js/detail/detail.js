// detail/detail.js — Fiche d'une image : ouverture, en-tête, onglets (aperçu, sauvegardes, lancement, fichiers, journal).

import { paintSaves } from "../actions.js";
import { api } from "../api.js";
import { showArt } from "../art.js";
import { $, cap, el, hideAnimated, icon, setUnavailable, showAnimated, size, state, storage, toast, when } from "../core.js";
import { loadAutorun, syncForm } from "./autorun.js";
import { loadFiles, renderHistory } from "./files.js";
import { loadUpdateDefaults } from "./version.js";
import { placeInd } from "../header.js";
import { renderInstBox } from "../install.js";
import { STATES, TYPES, loadLibrary } from "../library.js";
import { connected, hostOn } from "../mode.js";
import { gameOfItem, presentIn, sv } from "../saves.js";
import { setTasksOpen } from "../tasks.js";

// Ligne d'état de l'en-tête : « Prêt » en vert, les exceptions en couleur
const LINE_STATE = { ok: ["Prêt", "st"], in_use: ["En cours", "st accent"], modified: ["Tâche en file", "st accent"], has_old: ["Ancienne version à supprimer ou restaurer", "st warn"] };

export const TRIAL_DURS = [[30, "30 s"], [60, "60 s"], [120, "2 min"]];

export const trialPrefs = { seconds: 60 };   // réglé dans la page Réglages

// Clés de l'autorun, en français (le nom technique reste en dessous)
export const KEY_LABELS = { DIR: "Dossier du jeu", CMD: "Exécutable", GAME_VERSION: "Version du jeu", PRE: "Commande avant le jeu",
  ATTACH: "Commande pendant le jeu", ATTACH_PROCESS: "Processus attaché", WINE: "Version de Wine", PROTON: "Version de Proton",
  RUNNER: "Runner", ARCH: "Architecture", LANG: "Langue", ENV: "Variables d'environnement", HIDRAW: "Manettes en accès direct",
  SAVEDIR: "Dossier des sauvegardes", SAVEFILES: "Fichiers de sauvegarde", DXVK: "DXVK", VKD3D: "VKD3D", D7VK: "D7VK",
  VIRTUAL_DESKTOP: "Bureau virtuel", NVAPI: "NVAPI", VKREFLEX: "VKReflex", FSR: "FSR", FSR4: "FSR 4", ESYNC: "ESYNC", FSYNC: "FSYNC" };

export async function openDetail(id) {
  if (state.current?.id === id && !$("detail").hidden) return;
  if (!leaveEdit()) return;
  state.tab = null;
  state.autorunEdit = false;
  state.current = state.byId.get(id);
  delete $("u-anchor").dataset.edited;
  $("u-source").value = ""; $("u-version").value = "";
  state.pendingKeys.clear();
  $("main").classList.add("with-detail");
  showAnimated($("detail"), "is-leaving");
  markCurrent();
  renderDetailHeader();
  showTab("apercu");
  await loadAutorun();
}

function closeDetail() {
  if (!leaveEdit()) return;
  // Sortie en glissement ; la colonne de la liste ne reprend sa place qu'à la fin
  hideAnimated($("detail"), "is-leaving", 200, () => $("main").classList.remove("with-detail"));
  state.current = null;
  state.tab = null;
  markCurrent();
}

export function renderDetailHeader() {
  const i = state.current;
  $("d-title").textContent = i.name;
  $("d-cover").dataset.id = i.id;
  showArt($("d-cover"), i);
  paintStatusLine(i);
  paintTrial();
  renderInstBox();
  if ($("d-links-result").dataset.id !== i.id) {      // autre image : constat à refaire
    $("d-links-result").dataset.id = i.id;
    $("d-links-result").className = "msg";
    $("d-links-result").textContent = "Vérifie que l'image ne dépend pas d'un runner installé sur le client.";
  }
  paintOld(i);
  paintApercu();
  if (state.tab === "saves") paintSaves();
}

// En-tête : une ligne d'état (Prêt · Prefix · version · taille) au lieu de pastilles
function paintStatusLine(i) {
  const [label, cls] = (i.override || []).length && i.state === "ok" ? ["Surcharge à inscrire", "st warn"]
    : LINE_STATE[i.state] || [STATES[i.state]?.[0] || i.state, "st idle"];
  const sep = () => el("span", { className: "sep", textContent: "·" });
  $("d-status").replaceChildren(
    el("span", { className: cls, textContent: label }), sep(),
    el("span", { textContent: `Prefix ${TYPES[i.type] || i.type}` }), sep(),
    el("span", { className: "mono", textContent: i.version ? `v${i.version.replace(/^v/i, "")}` : "sans version" }), sep(),
    el("span", { textContent: size(i.size) }));
}

export function paintTrial() {
  $("d-trial-dur").textContent = (TRIAL_DURS.find(([s]) => s === trialPrefs.seconds) || TRIAL_DURS[1])[1];
  // Essai sans client d'essai : grisé, la raison en infobulle
  const reason = hostOn() ? "" : "Aucun client d'essai : choisir un client (menu d'un client, « Utiliser pour les essais »)";
  setUnavailable($("d-trial"), reason);
  setUnavailable($("d-trial-pick"), reason);
}

// Bandeaux d'état : jeu en cours, ancienne version (.old) à valider ou à remettre
function paintOld(i) {
  const old = $("d-old");
  old.replaceChildren();
  if (i.state === "in_use") old.append(el("div", { className: "fbanner" }, icon("play"), el("span", { textContent: "Jeu en cours : aucune reconstruction possible." })));
  if (i.has_old) {
    const keep = el("button", { className: "btn primary sm" }, icon("archive-x", "sm"), "Supprimer l'ancienne version");
    const back = el("button", { className: "btn secondary sm on-accent" }, icon("undo", "sm"), "Restaurer l'ancienne version");
    keep.title = "Supprimer définitivement l'ancienne version (.old)";
    back.title = "Remettre l'ancienne version à la place de l'image actuelle";
    keep.onclick = () => oldAction("validate", `Supprimer définitivement l'ancienne version de « ${i.name} » (.old) ?`);
    back.onclick = () => oldAction("restore", "Remettre l'ancienne version à la place de l'image actuelle (.old) ? La reconstruction sera perdue.");
    old.append(el("div", { className: "fbanner tall" }, icon("history"), el("div", { className: "body" },
      el("span", { textContent: "Une ancienne version (.old) est conservée : vérifier le jeu, puis la supprimer ou la restaurer." }),
      el("div", { className: "row" }, keep, back))));
  }
}

// Aperçu : trois cartes (Sauvegardes, Lancement, Image). Un champ vide est masqué et listé en note.
function paintApercu() {
  const i = state.current, hiddenFields = [];
  const link = (text, action) => { const b = el("button", { type: "button", className: "linklike", textContent: text }); b.onclick = action; return b; };
  const row = (label, value, { mono = false, action = null } = {}) => {
    const v = value instanceof Node ? value : document.createTextNode(String(value));
    return el("div", { className: "frow" }, el("span", { className: "k", textContent: label }),
      el("span", { className: mono ? "v mono" : "v" }, v), action || el("span"));
  };
  const used = i.proton || i.wine;
  const exe = i.cmd ? [(i.dir || "").replace(/[\\/]+$/, ""), i.cmd].filter(Boolean).join("\\") : "";
  const gfx = [i.dxvk && "DXVK", i.vkd3d && "VKD3D"].filter(Boolean);
  const gfxNode = gfx.length ? el("span", {}, ...gfx.map((t) => el("span", { className: "chip plain t-accent", textContent: t }))) : null;
  const runner = i.runner === "default" ? "Aucun (choisi par le lanceur)"
    : i.runner_joined ? `Intégré${used ? ` (${used})` : ""}` : `Custom${used || i.runner_path ? ` (${used || i.runner_path})` : ""}`;
  const pad = i.hidraw ? el("span", {}, icon("dualsense", "sm"), " DualSense") : el("span", {}, icon("xbox", "sm"), " Xbox");
  const override = i.override || [];
  const g = connected() ? sv.games.find((x) => x.game === gameOfItem(i)) : null;
  const sync = g ? syncLine(g) : null;

  const saveRows = [];
  if (i.saves_present) saveRows.push(row("Taille", size(i.saves_size)));
  if (sync) saveRows.push(row("Synchro", sync));
  if (!saveRows.length) saveRows.push(row("Taille", "aucune"));

  const launchRows = [];
  if (exe) launchRows.push(row("Exécutable", exe, { mono: true })); else hiddenFields.push("Exécutable");
  launchRows.push(row("Prefix", cap(TYPES[i.type] || i.type)));
  launchRows.push(row("Runner", runner));
  if (gfxNode) launchRows.push(row("Graphismes", gfxNode)); else hiddenFields.push("Graphismes");
  launchRows.push(row("Manettes", pad));
  if ((i.extras || []).length) launchRows.push(row("Extras", i.extras.join(", ")));
  else hiddenFields.push("Extras");
  if (i.attach) launchRows.push(row("Commande pendant le jeu", i.attach, { mono: true }));
  else hiddenFields.push("Commande pendant le jeu");
  if (override.length) launchRows.push(row("Surcharge", `${override.join(", ")} (à inscrire)`, { action: link("Lancement", () => showTab("autorun")) }));
  else hiddenFields.push("Surcharge");

  const versionText = i.version ? i.version.replace(/^v/i, "") : "sans version";
  const imageRows = [
    row("Emplacement", i.path, { mono: true }),
    row("Version", versionText, { mono: true, action: link("Mettre à jour", () => showTab("version")) }),
    row("Modifiée", when(i.mtime)),
  ];

  const card = (title, linkText, action, rows) => el("div", {},
    el("div", { className: "dsec-row" }, el("p", { className: "dsec", textContent: title }), linkText ? link(linkText, action) : null),
    el("div", { className: "dcard" }, ...rows));
  // « Installé sur » (mode connecté) fait partie de la carte Image. Le nœud est pris avant
  // replaceChildren : la carte précédente le contient et le retire (sinon il serait introuvable)
  const inst = $("d-inst");
  $("d-apercu").replaceChildren(
    card("Sauvegardes", "Gérer", () => showTab("saves"), saveRows),
    card("Lancement", "Détails", () => showTab("autorun"), launchRows),
    card("Image", null, null, imageRows));
  $("d-apercu").querySelectorAll(".dcard")[2].append(inst);
  $("d-apercu-note").hidden = !hiddenFields.length;
  $("d-apercu-note").replaceChildren(icon("info"), document.createTextNode(`Champs vides masqués : ${hiddenFields.join(", ")}.`));
}

// Synchro d'un jeu entre clients (mode connecté) : partagé si présent sur plusieurs clients
export function syncLine(g) {
  const n = presentIn(g).length;
  return n > 1 ? `Partagées entre ${n} clients` : "Sur ce client seulement";
}

export async function oldAction(action, question) {
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

// Lancement (autorun) : lecture par défaut ; « Modifier » ouvre l'édition. Nouvelle version
// est une sous-vue (sans onglet) ouverte par « Mettre à jour » ou le menu « … ».
// Quitter avec des changements non enregistrés demande confirmation.
const EDIT_TABS = new Set(["autorun", "version"]);

export function showTab(name) {
  if (name !== state.tab && !leaveEdit()) return;
  const entering = name !== state.tab;
  state.tab = name;
  for (const b of $("d-tabs").querySelectorAll("button[data-tab]")) {
    const on = b.dataset.tab === name;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", String(on));
  }
  for (const pane of document.querySelectorAll(".dpane")) pane.hidden = pane.dataset.pane !== name;
  placeInd($("d-tabs"));
  if (entering && EDIT_TABS.has(name)) enterEdit(name);
  markDirty();
  if (name === "files") loadFiles();
  if (name === "journal") renderHistory();
  if (name === "saves") paintSaves();
}

function enterEdit(kind) {
  if (kind === "autorun") {
    $("a-issues").replaceChildren(); $("a-result").replaceChildren(); $("a-proposal").replaceChildren();
    $("a-missing").hidden = true;
  } else {
    resetVersionForm();
    loadUpdateDefaults();
  }
  markDirty();
}

// Mode modification du lancement : l'éditeur remplace la lecture
export function setAutorunEdit(on) {
  state.autorunEdit = on;
  $("a-edit").hidden = on;
  $("a-edit-box").hidden = !on;
  $("a-read").hidden = on;
  $("a-title").textContent = on ? "Autorun · modification" : "Autorun";
  syncForm();
}

export function resetVersionForm() {
  delete $("u-anchor").dataset.edited;
  $("u-source").value = ""; $("u-version").value = "";
}

function isDirty() {
  if (state.tab === "autorun") return state.autorunEdit && $("a-editor").value !== state.baseline;
  if (state.tab === "version") return Boolean($("u-source").value.trim() || $("u-version").value.trim() || $("u-anchor").dataset.edited);
  return false;
}

export function markDirty() { $("tab-dirty").hidden = !isDirty(); }

// Quitte l'onglet d'édition courant (confirmation si des changements seraient perdus) ; faux si refusé.
// force : après une écriture réussie, rien à confirmer ni à restaurer.
export function leaveEdit(force = false) {
  if (!EDIT_TABS.has(state.tab)) return true;
  if (!force && isDirty() && !confirm("Abandonner les modifications non enregistrées ?")) return false;
  if (state.tab === "autorun" && state.autorunEdit) {
    if (!force) { $("a-editor").value = state.baseline; state.pendingKeys.clear(); }
    setAutorunEdit(false);
  }
  return true;
}

// Surligne la fiche ouverte sans redessiner la liste
export function markCurrent() {
  for (const node of document.querySelectorAll("#rows [data-id], #grid [data-id]")) {
    node.classList.toggle("current", node.dataset.id === state.current?.id);
    node.classList.toggle("selected", state.selected.has(node.dataset.id));
  }
}

export function init() {
  trialPrefs.seconds = Number(storage.get("wsfs-trial-duration")) || 60;
  $("d-tabs").onclick = (event) => { const b = event.target.closest("button[data-tab]"); if (b) showTab(b.dataset.tab); };
  new ResizeObserver(() => placeInd($("d-tabs"))).observe($("d-tabs"));
  $("d-close").onclick = closeDetail;
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
    if (!$("tasks-panel").hidden) { setTasksOpen(false); return; }
    if ($("detail").hidden || event.target.closest?.("input, textarea, select")) return;
    closeDetail();
  });
  $("a-edit").onclick = () => setAutorunEdit(true);
  $("a-cancel").onclick = () => {
    if (isDirty() && !confirm("Abandonner les modifications non enregistrées ?")) return;
    $("a-editor").value = state.baseline; state.pendingKeys.clear();
    $("a-issues").replaceChildren(); $("a-result").replaceChildren(); $("a-proposal").replaceChildren();
    setAutorunEdit(false);
  };
  for (const id of ["u-source", "u-anchor", "u-version"]) $(id).addEventListener("input", markDirty);
  $("v-back").onclick = () => showTab("apercu");
}
