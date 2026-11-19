// tasks.js — File de tâches : panneau, rafraîchissement et actions sur les tâches.

import { api } from "./api.js";
import { $, el, hideAnimated, icon, isShown, showAnimated, state, storage, toast, when } from "./core.js";
import { playIcons } from "./aicons.js";
import { loadAutorun } from "./detail/autorun.js";
import { renderHistory } from "./detail/files.js";
import { loadFolders } from "./folders.js";
import { setTopMenu } from "./header.js";
import { loadInstall } from "./install.js";
import { installResultNode } from "./install-dialog.js";
import { loadLibrary, renderTodo } from "./library.js";
import { loadSaves } from "./saves.js";
import { whenOf } from "./when.js";

export const TASK_KINDS = { autorun: ["hammer", "Reconstruction"], update: ["upload", "Montée de version"], files: ["file", "Fichiers et registre"],
  verify: ["shield", "Vérification"], cover: ["image", "Illustration"], pack: ["package", "Compression"], runner: ["clip", "Runner intégré"], trial: ["flask", "Lancement d'essai"],
  saves: ["save", "Sauvegardes entre clients"], install: ["drive", "Installation sur les clients"] };

export const TASK_STATES = { pending: ["clock", "en attente", "t-neutral"], running: ["refresh", "en cours", "t-accent"], done: ["check", "terminée", "t-ok"],
  failed: ["alert", "échec", "t-err"], cancelled: ["x", "annulée", "t-neutral"] };

function taskNode(t, opened = new Set()) {
  const [kindIcon, kindLabel] = TASK_KINDS[t.kind] || ["tasks", t.kind];
  let [stIcon, stLabel, tone] = TASK_STATES[t.status] || ["info", t.status, "t-neutral"];
  if (t.status === "pending" && t.night) { stIcon = "moon"; stLabel = state.config ? `nuit ${state.config.night_start}` : "cette nuit"; }
  if (t.status === "running") stLabel = `${Math.round((t.progress || 0) * 100)} %`;
  const progress = t.status === "done" ? 100 : t.status === "running" || t.status === "failed" ? Math.round((t.progress || 0) * 100) : 0;
  const node = el("div", { className: `task ${t.status}${opened.has(t.id) ? " open" : ""}` },
    el("span", { className: `disc ${tone}` }, icon(kindIcon)),
    el("div", { className: "mid" },
      el("div", { className: "line", title: `${t.image || ""} — ${t.title}` }, el("strong", { textContent: t.image || kindLabel }), " ", el("span", { textContent: t.title || kindLabel })),
      el("div", { className: "bar" }, el("div", { style: `width:${progress}%` })),
      el("div", { className: "muted phase", style: "font-size:11.5px", hidden: !(t.status === "running" && t.phase), textContent: t.phase || "" }),
      t.error ? el("div", { className: "err-line", textContent: t.error }) : null),
    el("span", { className: "st", title: TASK_STATES[t.status]?.[1] || t.status }, icon(stIcon, `sm${t.status === "running" ? " spinning" : ""}`),
      el("span", { className: "st-label", textContent: stLabel })));
  node.dataset.id = t.id;
  node.dataset.key = taskKey(t);
  const more = el("div", { className: "more" });
  const actions = el("div", { className: "row" }, el("span", { className: "when-at spacer", textContent: `créée le ${when(t.created_at)}` }));
  if (t.status === "pending" && t.night) {
    const now = el("button", { className: "btn secondary sm" }, icon("zap", "sm"), "Lancer maintenant");
    now.onclick = async (event) => {
      event.stopPropagation();
      try { await api(`/api/tasks/${t.id}/now`, { method: "POST" }); } catch (error) { alert(error.message); }
      refreshTasks();
    };
    actions.append(now);
  }
  if (t.status === "pending" || t.status === "running") {
    const cancel = el("button", { className: "btn danger sm" }, icon("x", "sm"), "Annuler");
    cancel.onclick = async (event) => {
      event.stopPropagation();
      try { await api(`/api/tasks/${t.id}/cancel`, { method: "POST" }); } catch (error) { alert(error.message); }
      refreshTasks();
    };
    actions.append(cancel);
  }
  more.append(actions);
  const result = t.kind === "trial" ? t.params?.result : null;
  if (result) more.append(trialReport(t, result));
  if (t.kind === "install" && t.params?.result) more.append(installResultNode(t.params.result));
  if ((t.log || []).length) more.append(el("pre", { className: "log", textContent: t.log.join("\n") }));
  node.append(more);
  node.onclick = (event) => { if (!event.target.closest(".more")) node.classList.toggle("open"); };
  return node;
}

// Ce qui oblige à reconstruire le nœud d'une tâche ; le reste (progression, phase) est mis à
// jour en place, sans recréer le nœud : sinon l'icône animée redémarre et la liste clignote
// à chaque relecture (toutes les 1,5 s pendant un transfert).
function taskKey(t) {
  return JSON.stringify([t.status, t.night, t.image, t.title, t.error, (t.log || []).length,
    Boolean(t.params?.result), state.config?.night_start]);
}

function updateTaskNode(node, t) {
  const progress = t.status === "done" ? 100 : Math.round((t.progress || 0) * 100);
  node.querySelector(".bar > div").style.width = `${progress}%`;
  if (t.status === "running") node.querySelector(".st-label").textContent = `${progress} %`;
  const phase = node.querySelector(".phase");
  phase.hidden = !(t.status === "running" && t.phase);
  if (phase.textContent !== (t.phase || "")) phase.textContent = t.phase || "";
}

// Liste de tâches réconciliée : nœuds gardés quand c'est la même tâche dans le même état ;
// la liste n'est réordonnée que si l'ordre ou le contenu change.
function syncTaskList(box, tasks, empty) {
  if (!tasks.length) {
    if (!box.querySelector(":scope > p.muted")) box.replaceChildren(empty);
    return;
  }
  const opened = new Set([...box.querySelectorAll(".task.open")].map((n) => n.dataset.id));
  const old = new Map([...box.querySelectorAll(":scope > .task")].map((n) => [n.dataset.id, n]));
  const nodes = tasks.map((t) => {
    const node = old.get(t.id);
    if (node && node.dataset.key === taskKey(t)) {
      updateTaskNode(node, t);
      return node;
    }
    return taskNode(t, opened);
  });
  const same = nodes.length === box.children.length && nodes.every((n, i) => box.children[i] === n);
  if (!same) box.replaceChildren(...nodes);
}

function trialReport(t, r) {
  const box = el("div", { className: `notice ${r.ok ? "ok" : "err"}`, style: "margin:0" }, icon(r.ok ? "check" : "alert"),
    el("div", { className: "body" }, el("strong", { textContent: r.verdict })));
  const body = box.lastChild;
  for (const w of r.windows || []) body.append(el("div", { className: "mono", style: "font-size:12px", textContent: `fenêtre : ${w.name} [${w.state || "normale"}]` }));
  if ((r.errors || []).length) body.append(el("ul", { className: "issues" }, ...r.errors.map((e) => el("li", { textContent: `${e.meaning} — ${e.line}` }))));
  if (r.screenshot) {
    const show = el("button", { className: "btn secondary sm" }, icon("image", "sm"), "Voir la capture");
    show.onclick = async (event) => {
      event.stopPropagation();
      try {
        const response = await fetch(`/api/tasks/${t.id}/screenshot`, { headers: { "x-wsfs-token": storage.get("wsfs-token") || "" } });
        if (!response.ok) throw new Error(`erreur ${response.status}`);
        show.replaceWith(el("img", { src: URL.createObjectURL(await response.blob()), alt: "Capture de l'essai" }));
      } catch (error) { alert(error.message); }
    };
    body.append(el("div", {}, show));
  }
  return box;
}

let tasksTimer = null;

let activeBefore = new Set();

let installActive = new Set();     // tâches « install » en cours (leur fin recharge l'état des clients)

export async function refreshTasks() {
  clearTimeout(tasksTimer);
  try {
    state.tasks = await api("/api/tasks");
  } catch {
    tasksTimer = setTimeout(refreshTasks, 10000);
    return;
  }
  const active = state.tasks.filter((t) => t.status === "pending" || t.status === "running");
  syncTaskList($("tasks-list"), state.tasks.slice(0, 100),
    el("p", { className: "muted", style: "margin:4px", textContent: "Aucune tâche." }));
  const failed = state.tasks.filter((t) => t.status === "failed").length;
  const nightly = active.filter((t) => t.status === "pending" && t.night).length;
  paintTasks(active, failed, nightly);
  renderTodo();
  $("tasks-summary").textContent = active.length ? `${active.length} active(s)` : state.tasks.length ? "aucune active" : "";
  // Une tâche vient de se terminer : l'image a changé
  const nowActive = new Set(active.map((t) => t.id));
  const installNow = new Set(active.filter((t) => t.kind === "install").map((t) => t.id));
  const installEnded = [...installActive].some((id) => !nowActive.has(id));
  installActive = installNow;
  if (installEnded) loadInstall();
  if ([...activeBefore].some((id) => !nowActive.has(id))) {
    await loadLibrary();
    if (state.current) await loadAutorun();
    if (!$("folders-main").hidden) loadFolders();
    if (!$("saves-main").hidden) loadSaves();
  }
  activeBefore = nowActive;
  if (state.current && !$("detail").hidden && !document.querySelector('[data-pane="journal"]').hidden) renderHistory();
  tasksTimer = setTimeout(refreshTasks, active.length ? 1500 : 10000);
}

export function setTasksOpen(open) {
  if (open) showAnimated($("tasks-panel"), "is-leaving"); else hideAnimated($("tasks-panel"), "is-leaving", 200);
  $("btn-tasks").setAttribute("aria-expanded", String(open));
  if (open) setTopMenu(false);
}

// Pastille des tâches (en-tête) : échecs en rouge ; sinon en cours ou cette nuit, avec le spinner ; sinon l'icône seule
let lastActive = 0, lastFailed = 0, doneTimer = 0;
function paintTasks(active, failed, nightly) {
  const pill = $("btn-tasks"), running = active.some((t) => t.status === "running");
  // Une tâche vient de finir sans nouvel échec : la coche de l'icône se dessine un instant
  if (active.length < lastActive && failed <= lastFailed) {
    pill.classList.add("just-done");
    playIcons(pill);
    clearTimeout(doneTimer);
    doneTimer = setTimeout(() => pill.classList.remove("just-done"), 1500);
  }
  lastActive = active.length; lastFailed = failed;
  pill.classList.toggle("busy", active.length > 0);
  pill.classList.toggle("failed", failed > 0);
  $("tasks-fail").hidden = !failed;
  $("tasks-fail").textContent = `${failed} en échec`;
  $("tasks-label").textContent = running ? `${active.length} en cours`
    : nightly && nightly === active.length ? `${nightly} cette nuit` : active.length ? `${active.length} en attente` : "";
  pill.title = `File de tâches : ${active.length ? `${active.length} active${active.length > 1 ? "s" : ""}` : "aucune active"}${failed ? `, ${failed} en échec` : ""}`;
  pill.setAttribute("aria-label", pill.title);
}

export const openTasks = () => setTasksOpen(true);

// Une action vient d'entrer dans la file
export function queued(message = whenOf() === "night" ? "Programmé pour cette nuit" : "Ajouté à la file de tâches") {
  toast(message, whenOf() === "night" ? "moon" : "check");
  refreshTasks();
}

export function init() {
  $("btn-tasks").onclick = (event) => { event.stopPropagation(); setTasksOpen(!isShown($("tasks-panel"))); };
  $("tasks-close").onclick = () => setTasksOpen(false);
  // Un clic ailleurs referme le panneau des tâches
  document.addEventListener("click", (event) => {
    if (!$("tasks-panel").hidden && !event.target.closest("#tasks-panel, #btn-tasks, dialog")) setTasksOpen(false);
  });
  $("tasks-clear").onclick = async () => { await api("/api/tasks/finished", { method: "DELETE" }); refreshTasks(); };
}
