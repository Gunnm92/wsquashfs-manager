// detail/files.js — Fichiers internes de l'image et historique (journal).

import { api } from "../api.js";
import { $, el, icon, state, when } from "../core.js";
import { notice } from "./autorun.js";
import { TASK_KINDS, TASK_STATES } from "../tasks.js";

let filesTimer = null;

const isInternal = (path) => path.split("/").some((part) => part.startsWith(".wsquashfs"));

export async function loadFiles() {
  const id = state.current.id, q = $("files-q").value.trim();
  $("files-count").textContent = "lecture…";
  try {
    const data = await api(`/api/image/files?id=${encodeURIComponent(id)}&q=${encodeURIComponent(q)}`);
    if (state.current?.id !== id) return;
    state.files = data;
    paintFiles();
  } catch (error) {
    $("files-count").textContent = error.message;
  }
}

// Arborescence : un niveau d'indentation par dossier ; les fichiers internes (.wsquashfs/…) sont masqués
function paintFiles() {
  const data = state.files;
  if (!data) return;
  const q = $("files-q").value.trim(), internal = Boolean(state.filesInternal);
  const shown = data.files.filter((path) => internal || !isInternal(path));
  $("tab-files-count").textContent = data.total;
  $("files-count").textContent = q ? `${data.matches} sur ${data.total} entrées` : `${data.total} entrées`;
  $("files-internal").textContent = internal ? "Masquer les fichiers internes" : "Afficher les fichiers internes";
  $("files-list").replaceChildren(...shown.map((path) => {
    const dir = path.endsWith("/"), parts = (dir ? path.slice(0, -1) : path).split("/");
    const name = parts.pop();
    const row = el("div", { className: `${dir ? "dir" : ""}${isInternal(path) ? " internal" : ""}`, title: path },
      icon(dir ? "folder" : "file"), el("span", { textContent: name }));
    row.dataset.depth = String(Math.min(parts.length, 6));
    return row;
  }), ...(data.matches > data.files.length ? [el("div", { className: "more", textContent: `… ${data.matches - data.files.length} de plus : affiner la recherche` })] : []));
}

export async function renderHistory() {
  if (!state.current) return;
  const id = state.current.id;
  let tasks;
  try {
    // File en cours + journal permanent (conservé après « Effacer l'historique »)
    tasks = await api(`/api/tasks?image=${encodeURIComponent(id)}`);
  } catch (error) {
    $("d-journal").replaceChildren(notice("err", error.message));
    return;
  }
  if (state.current?.id !== id) return;
  paintHistory(tasks);
}

// Journal : les tâches de l'image, répétitions regroupées (« 4 × Illustration »), date, sans barre de progression
function paintHistory(tasks) {
  if (!tasks.length) {
    $("d-journal").replaceChildren(el("p", { className: "muted", textContent: "Aucune opération enregistrée." }));
    return;
  }
  const groups = new Map();
  for (const t of tasks) {
    const key = [t.kind, t.title || "", t.status].join("|");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(t);
  }
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  $("d-journal").replaceChildren(...[...groups.values()].map((group) => {
    const t = group[0];
    const [kindIcon, kindLabel] = TASK_KINDS[t.kind] || ["tasks", t.kind];
    const counts = {};
    for (const x of group) counts[x.status] = (counts[x.status] || 0) + 1;
    const summary = Object.entries(counts).map(([status, n]) => `${TASK_STATES[status]?.[1] || status}${n > 1 ? ` (${n})` : ""}`).join(" · ");
    const tone = counts.failed ? "t-err" : counts.running || counts.pending ? "t-accent" : counts.done ? "t-violet" : "t-neutral";
    const cls = counts.failed ? "err" : counts.running || counts.pending ? "accent" : counts.done ? "" : "idle";
    const stIcon = counts.failed ? "alert" : counts.running ? "refresh" : counts.pending ? "clock" : counts.done ? "check" : "x";
    const times = group.map((x) => x.created_at || 0);
    const first = Math.min(...times), last = Math.max(...times);
    const sameDay = new Date(first * 1000).toDateString() === new Date(last * 1000).toDateString();
    const dateText = group.length === 1 ? when(t.created_at)
      : sameDay ? `${when(first)} à ${clock(last)}` : `${when(first)} → ${when(last)}`;
    const titleText = t.title && t.title !== kindLabel ? `${kindLabel} — ${t.title}` : kindLabel;
    const err = group.find((x) => x.error)?.error;
    return el("div", { className: "jrow" },
      el("span", { className: `disc ${tone}` }, icon(kindIcon)),
      el("div", { className: "jbody" },
        el("strong", { textContent: `${group.length > 1 ? `${group.length} × ` : ""}${titleText}` }),
        el("span", { textContent: dateText }),
        err ? el("span", { className: "jerr", textContent: err }) : null),
      el("span", { className: `jst ${cls}` }, icon(stIcon, `sm${counts.running ? " spinning" : ""}`), el("span", { textContent: summary })));
  }));
}

export function init() {
  $("files-q").addEventListener("input", () => { clearTimeout(filesTimer); filesTimer = setTimeout(loadFiles, 250); });
  $("files-internal").onclick = () => { state.filesInternal = !state.filesInternal; paintFiles(); };
}
