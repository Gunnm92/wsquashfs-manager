// saves-ops.js — Sauvegardes : appairage, suppression, synchronisation et copies.

import { api } from "./api.js";
import { $, cap, el, icon, toast } from "./core.js";
import { notice } from "./detail/autorun.js";
import { showView } from "./header.js";
import { AUTO, PRESENT_STATES, RESERVE, busy, gameLabel, lateIn, loadSaves, machineName, names, osIcon, paintSavesView, paintStatus, pushTargets, reachableIds, stateOf, suggestedOf, sv } from "./saves.js";
import { onMachine } from "./saves-machines.js";
import { TASK_STATES, refreshTasks } from "./tasks.js";

// Appairage : même code wsq:// que l'agent ; l'erreur reste dans le dialogue
function setPairError(text) {
  $("pair-err").textContent = text || "";
  $("pair-err").hidden = !text;
}

export function openPair() {
  $("pair-code").value = "";
  setPairError("");
  $("dlg-pair").showModal();
  $("pair-code").focus();
}

// Suppression des sauvegardes (#dlg-save-delete) : une case par client joignable et une pour le
// manager ; pré-cochée si un des jeux y a une sauvegarde, sinon grisée. Valider : tâche « saves ».
let sdGames = [];

// `only` : client (ou « manager ») seul coché au départ, depuis la fiche d'un jeu ; sinon tout ce qui a une copie
export function openSaveDelete(games, only = null) {
  sdGames = games;
  const gameNames = games.map(gameLabel);
  const total = gameNames.length;
  const shownNames = gameNames.slice(0, 3).map((n) => `« ${n} »`).join(", ");
  const rest = total - 3;
  $("sd-games").textContent = `${total} jeu${total > 1 ? "x" : ""} : ${shownNames}${rest > 0 ? ` et ${rest} autre${rest > 1 ? "s" : ""}` : ""}`;
  const row = (id, label, count, iconNode, detail) => {
    const box = el("input", { type: "checkbox", checked: count > 0 && (!only || id === only), disabled: count === 0 });
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

// Résultat : ✓ / ✗ par jeu et client ; failuresOnly : les échecs détaillés, les réussites en compte
export function syncResultNode(task, failuresOnly = false) {
  const items = syncItems(task);
  if (items === null) return notice("err", task?.error || `Tâche ${TASK_STATES[task?.status]?.[1] || "sans résultat"}.`);
  if (!items.length) return notice("info", "Rien à faire.");
  const failed = syncFailed(items);
  const shown = failuresOnly ? failed : items;
  // Suppression : le message dit déjà où (« supprimée sur <client> ») et si c'est archivé
  const removal = task?.params?.op === "delete";
  const rows = shown.slice(0, 40).map((item) => {
    // « envoyée vers <client> » : le nom du client est déjà affiché à côté
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
export async function runSaveOp(path, body) {
  sv.result = null;
  sv.op = { text: "Démarrage…" };
  paintSavesView();
  let taskId;
  try {
    ({ task: taskId } = await api(path, { method: "POST", body }));
  } catch (error) {
    sv.op = null;
    sv.result = { task: null, error: error.message };
    paintSavesView();
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

const ARCHIVED = "La version écrasée sur chaque client est archivée.";

// Cibles d'une copie du manager vers les clients : joignables, qui ont le jeu, pas déjà à sa version
export const reachManagerTargets = (g) => reachableIds().filter((id) => onMachine(g, id) && (!g.reserve || stateOf(g, id)?.fingerprint !== g.reserve.fingerprint));

// Copie vers le manager : depuis `source` (un client, ou AUTO : la suggérée, jeu par jeu)
export function pullGames(games, source) {
  const names = games.map((g) => g.game);
  if (!names.length || source === RESERVE) return;
  runSaveOp("/api/saves/pull", { source, games: names });
}

export function pushFrom(g, sourceId, targets) {
  if (!targets.length) return;
  if (!confirm(`Sync « ${gameLabel(g)} » : ${machineName(sourceId)} → ${names(targets)}.\n\n${ARCHIVED}`)) return;
  runSaveOp("/api/saves/push", { source: sourceId, targets, games: [g.game] });
}

// Copie du manager (sans source) vers des clients, après confirmation
export function pushManager(g, targets) {
  if (!targets.length || !confirm(`Sync « ${gameLabel(g)} » : Manager → ${names(targets)}.\n\n${ARCHIVED}`)) return;
  runSaveOp("/api/saves/push", { targets, games: [g.game] });
}

export function pushReserve(g) { pushManager(g, reachManagerTargets(g)); }

export function pushSelection() {
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
    if (!moves.length) { toast("Rien à synchroniser : aucun client coché n'a ces jeux dans une version en retard.", "info"); return; }
    body = { source: AUTO, targets, games: idsOf(games) };
    lines = moves.map(({ g, from, to }) => `Sync « ${gameLabel(g)} » : ${from.name} → ${names(to)}.`);
    if (moves.length < games.length) lines.push(`${games.length - moves.length} jeu(x) déjà à jour, ignoré(s).`);
    lines.push("Les clients qui n'ont pas le jeu ne sont pas touchés.");
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
export function openSavesFor(game) {
  sv.todo = "";
  $("s-search").value = game;
  showView("saves");
  window.scrollTo(0, 0);
}

export function init() {
  $("s-empty-settings").onclick = () => { showView("settings").then(() => $("st-clients").scrollIntoView({ block: "start" })); };
  $("pair-save").onclick = async () => {
    const code = $("pair-code").value.trim(), button = $("pair-save");
    if (!code) { setPairError("Colle le code wsq:// affiché par wsquashfs-launcher --pair."); return; }
    button.disabled = true;
    setPairError("");
    try {
      const r = await api("/api/saves/machines", { method: "POST", body: { code } });
      $("dlg-pair").close();
      toast(r.replaced ? `Client mis à jour : ${r.name}` : `Client associé : ${r.name}`);
      loadSaves();
    } catch (error) {
      setPairError(error.message);
    } finally {
      button.disabled = false;
    }
  };
  $("pair-code").addEventListener("keydown", (event) => { if (event.key === "Enter") $("pair-save").click(); });
  $("sd-run").onclick = () => {
    const checked = [...$("sd-targets").querySelectorAll("input:checked")].map((box) => box.dataset.id);
    const manager = checked.includes("manager");
    const machines = checked.filter((id) => id !== "manager");
    if (!machines.length && !manager) return;
    $("dlg-save-delete").close();
    runSaveOp("/api/saves/delete", { games: sdGames.map((g) => g.game), machines, manager });
  };
}
