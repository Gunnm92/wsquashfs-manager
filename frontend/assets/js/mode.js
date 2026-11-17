// mode.js — Mode autonome ou connecté, client d'essai, maintenance du client.

import { api } from "./api.js";
import { $, state, toast } from "./core.js";
import { loadDoctor } from "./doctor.js";
import { paintClients, showView } from "./header.js";
import { inst, loadInstall, paintInstall } from "./install.js";
import { loadSaves, paintSavesView, sv } from "./saves.js";

// Mode autonome : le manager ne fait que construire (Images, Dossiers), rien n'est connecté.
// Mode connecté : sauvegardes entre clients, installation, lancement d'essai. Le client
// d'essai se choisit dans le bandeau Clients de Sauvegardes (menu d'un client).

export const connected = () => state.config?.mode !== "autonome";

export const hostOn = () => Boolean(state.config && state.config.game_host && state.config.game_host !== "off");

export const hostName = () => state.config?.game_host_name || "le client d'essai";

export const isTrial = (m) => m.id === state.config?.game_host_machine || m.kind === "game_host";

let appliedMode = null;         // mode affiché : le passage d'un mode à l'autre relance les lectures

// Applique le mode sans recharger la page : ce qui touche un client disparaît (attribut
// data-connected, voir app.css) ; en autonome, les clients lus sont oubliés.
export function applyMode() {
  const mode = connected() ? "connecte" : "autonome";
  const previous = appliedMode;
  appliedMode = mode;
  document.body.dataset.mode = mode;
  $("d-trial-wrap").hidden = !(connected() && hostOn());
  $("d-links").title = hostOn() ? `Liens du prefix vers un runner hors de l'image ou absent, et réparation avec les runners de ${hostName()}`
    : "Liens du prefix vers un runner hors de l'image ou absent";
  paintClients();
  if (previous === null) return;                  // premier affichage : le démarrage charge tout
  if (mode === "autonome") {
    clearConnected();
    if (document.body.dataset.view === "saves") showView("images");
  } else if (previous === "autonome") {
    loadInstall();
    if (document.body.dataset.view === "saves") loadSaves();
  }
}

// Mode autonome : plus rien de connecté à montrer (clients, installation, diagnostic)
function clearConnected() {
  inst.machines = []; inst.loaded = false; inst.error = "";
  sv.machines = []; sv.games = []; sv.loaded = false; sv.error = "";
  state.doctor = null;
  paintInstall();
  paintSavesView();
}

// Client associé choisi comme client d'essai (menu d'un client du bandeau)
export async function useForTrials(m) {
  try {
    state.config = await api("/api/config/game_host/machine", { method: "PUT", body: { id: m.id } });
    state.doctor = null;
    applyMode();
    paintSavesView();
    toast(`${m.name} : client d'essai`);
  } catch (error) { toast(error.message, "alert", true); }
}

// Sortie colorée du lanceur (codes ANSI, couleurs forcées pour la maintenance) → HTML.
// Gras, atténué et les 16 couleurs de base ; le reste des codes est ignoré.
const ANSI_RE = /\x1b\[([0-9;]*)m/g;

export function stripAnsi(text) { return text.replace(ANSI_RE, ""); }

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
  sync: ["Synchroniser les sauvegardes", "Envoie et reçoit les sauvegardes de partie avec les autres clients associés du réseau local ; en cas de conflit, la plus lourde gagne (à taille égale, la plus récente) et l'autre est archivée. Les jeux en cours sont ignorés."],
  "sync-status": ["État de la synchro", ""],
};

// Sans confirmation : les actions qui ne modifient rien
const MAINT_QUIET = new Set(["list", "sync-status"]);

// Maintenance du client d'essai (menu d'un client, #dlg-maint)
function paintMaint() {
  for (const b of $("tools-list").querySelectorAll("button")) b.disabled = Boolean(state.maintBusy) || !hostOn();
}

export function openMaint(m) {
  $("maint-intro").textContent = `Actions immédiates sur ${m.name} : elles ne passent pas par « Enregistrer ».`;
  $("maint-out").hidden = true;
  paintMaint();
  $("dlg-maint").showModal();
}

// Diagnostic du lanceur du client d'essai (menu d'un client, #dlg-doctor)
export function openDoctor() {
  $("doc-title").textContent = `wsquashfs-launcher --doctor sur ${hostName()}`;
  $("dlg-doctor").showModal();
  loadDoctor(true);
}

export function init() {
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
        paintMaint();
      }
    };
  }
}
