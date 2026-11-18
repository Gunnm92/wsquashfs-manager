// folder-detail.js — Panneau d'un dossier à compresser : autorun.cmd lu, modifié (même
// formulaire que la fiche image), proposé par l'analyse, enregistré dans le dossier.

import { api } from "./api.js";
import { thumb } from "./art.js";
import { $, cap, el, hideAnimated, showAnimated, toast } from "./core.js";
import { notice, proposalItems } from "./detail/autorun.js";
import { KEY_LABELS } from "./detail/detail.js";
import { KEY_RE, autorunEditor, getKey, textLines } from "./detail/kv-form.js";

const panel = { folder: null, data: null, editor: null, editing: false, baseline: "" };
let hooks = { reload: async () => {}, cover: () => {}, pack: () => {} };

export const currentFolder = () => panel.folder?.id ?? null;

// État d'un dossier, commun à la liste et au panneau :
// key (filtres), libellé, classe de pastille (liste), classe de la ligne d'état (panneau), rang de tri
export function folderState(f) {
  if (f.task === "running") return { key: "running", label: "En cours", cls: "in_use", st: "st accent", rank: 0 };
  if (f.task) return { key: "queued", label: "En attente", cls: "in_use", st: "st accent", rank: 1 };
  if (f.image_exists) return { key: "done", label: "Fait", cls: "ok", st: "st", rank: 4 };
  return f.has_autorun ? { key: "todo", label: "À traiter", cls: "modified", st: "st accent", rank: 3 }
    : { key: "noautorun", label: "À faire", cls: "has_old", st: "st warn", rank: 2 };
}

const dirty = () => panel.editing && panel.editor && panel.editor.text() !== panel.baseline;

function confirmLeave() {
  return !dirty() || confirm("Abandonner les modifications de l'autorun non enregistrées ?");
}

export async function openFolder(folder) {
  if (panel.folder?.id === folder.id && !$("fdetail").hidden) return;
  if (!confirmLeave()) return;
  panel.folder = folder;
  panel.editing = false;
  $("folders-main").classList.add("with-detail");
  showAnimated($("fdetail"), "is-leaving");
  paintHead();
  await loadFolderAutorun();
}

export function closeFolder() {
  if (!confirmLeave()) return;
  panel.folder = null;
  panel.editing = false;
  hideAnimated($("fdetail"), "is-leaving", 200, () => $("folders-main").classList.remove("with-detail"));
  hooks.reload(false);
}

// Liste rechargée : le dossier ouvert reprend ses nouvelles données (ou le panneau se ferme)
export function refreshFolder(items) {
  if (!panel.folder || $("fdetail").hidden) return;
  const fresh = items.find((f) => f.id === panel.folder.id);
  if (!fresh) { panel.editing = false; closeFolder(); return; }
  panel.folder = fresh;
  paintHead();
}

function paintHead() {
  const f = panel.folder;
  $("fd-title").textContent = f.folder;
  // Jaquette + pastille d'illustration (comme la fiche image)
  const art = thumb(f, "cover", "folder");
  art.id = "fd-cover-art";
  $("fd-cover-art").replaceWith(art);
  const sep = () => el("span", { className: "sep", textContent: "·" });
  const { label, st: cls } = folderState(f);
  $("fd-status").replaceChildren(el("span", { className: cls, textContent: label }), sep(),
    el("span", { textContent: cap(f.system) || "—" }), sep(), el("span", { textContent: `.${f.kind}` }));
  $("fd-pack").disabled = Boolean(f.task || f.image_exists);
  $("fd-edit").hidden = panel.editing || Boolean(f.task);
}

async function loadFolderAutorun() {
  const id = panel.folder.id;
  $("fd-msg").replaceChildren();
  $("fd-read-issues").replaceChildren();
  try {
    const data = await api(`/api/folders/autorun?id=${encodeURIComponent(id)}`);
    if (panel.folder?.id !== id) return;
    panel.data = data;
  } catch (error) {
    panel.data = null;
    $("fd-read").replaceChildren(notice("err", `Lecture impossible : ${error.message}`));
    return;
  }
  paintRead();
  setEditing(panel.editing);
}

function paintRead() {
  const { lines } = textLines(panel.data?.text || "");
  const keys = [...new Set(lines.map((l) => KEY_RE.exec(l)?.[1]).filter(Boolean))];
  $("fd-read").replaceChildren(...(keys.length ? keys.map((key) => el("div", { className: "frow kv" },
    el("span", {}, el("span", { textContent: KEY_LABELS[key] || key }), el("small", { textContent: key })),
    el("span", { className: "v mono", textContent: getKey(lines, key) ?? "" })))
    : [el("div", { className: "frow" }, el("span", { className: "k", textContent: "Autorun" }),
      el("span", { className: "v", textContent: "Pas d'autorun.cmd dans le dossier : « Modifier » pour en écrire un, ou « Proposer »." }))]));
}

function setEditing(on) {
  panel.editing = on;
  $("fd-edit-box").hidden = !on;
  $("fd-read").hidden = on;
  $("fd-edit").hidden = on || Boolean(panel.folder?.task);
  if (!on) { $("fd-save").disabled = true; return; }
  panel.baseline = (panel.data?.text || "").replace(/\r\n/g, "\n");
  panel.editor = autorunEditor(panel.baseline, { onChange: paintSaveState });
  $("fd-editor").replaceChildren(panel.editor.node);
  for (const id of ["fd-proposal", "fd-issues", "fd-result"]) $(id).replaceChildren();
  paintSaveState();
}

function paintSaveState() {
  if (!panel.editor) return;
  const text = panel.editor.text();
  $("fd-save").disabled = text === panel.baseline || !/^CMD=/m.test(text);
  $("fd-save").title = !/^CMD=/m.test(text) ? "CMD est obligatoire" : "Écrit autorun.cmd dans le dossier, sans compresser";
}

function body(check = false) {
  return { text: panel.editor.text(), eol: panel.data?.eol || "\r\n", encoding: panel.data?.encoding || "utf-8",
    base_sha: panel.data?.sha ?? null, check };
}

const issuesList = (list) => list.map((x) => el("li", { className: x.level, textContent: x.message }));

async function propose() {
  const id = panel.folder.id, box = $("fd-proposal");
  box.replaceChildren(el("div", { className: "notice info" }, el("span", { className: "spinner" }), el("span", { textContent: "Analyse du dossier (lecture des exécutables)…" })));
  try {
    const r = await api(`/api/folders/analyze?id=${encodeURIComponent(id)}`);
    if (panel.folder?.id !== id || !panel.editor) return;
    const norm = (key, v) => {
      let x = (v || "").trim();
      if (["CMD", "DIR"].includes(key)) x = x.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
      return x;
    };
    const { lines } = textLines(panel.editor.text());
    const changes = [];
    for (const line of r.generated.text.replace(/\r\n/g, "\n").split("\n")) {
      const m = KEY_RE.exec(line);
      if (!m) continue;
      const before = getKey(lines, m[1]);
      if (before === null || norm(m[1], before) !== norm(m[1], m[2])) changes.push({ key: m[1], value: m[2], before });
    }
    const analysis = el("details", {}, el("summary", { textContent: "Voir l'analyse" }), el("div", { style: "margin-top:8px" }, proposalItems(r.generated.items)));
    const warnings = r.generated.warnings.length ? [el("ul", { className: "issues" }, ...r.generated.warnings.map((w) => el("li", { textContent: w })))] : [];
    if (!r.generated.text.trim()) {
      box.replaceChildren(notice("", "Rien à proposer : aucun exécutable Windows trouvé dans le dossier.", "alert"), ...warnings);
      return;
    }
    if (!changes.length) {
      box.replaceChildren(notice("ok", "Rien à proposer : l'autorun correspond déjà à l'analyse.", "check"), ...warnings, analysis);
      return;
    }
    const use = el("button", { type: "button", className: "btn primary sm" }, changes.length > 1 ? `Appliquer ces ${changes.length} changements` : "Appliquer ce changement");
    use.onclick = () => {
      for (const c of changes) panel.editor.setKey(c.key, c.value);
      box.replaceChildren(notice("ok", "Changements placés dans le formulaire : vérifier, puis enregistrer.", "check"));
    };
    box.replaceChildren(el("div", { className: "proposal" },
      el("div", { className: "head" }, el("strong", { textContent: changes.length > 1 ? `${changes.length} changements proposés` : "1 changement proposé" }),
        r.engine ? el("span", { className: "chip", textContent: r.engine }) : null),
      el("div", { className: "diff" }, ...changes.map((c) => el("div", { className: "add",
        textContent: c.before === null ? `+ ${c.key}=${c.value}` : `~ ${c.key} : ${c.before} → ${c.value}` }))),
      ...warnings, analysis, el("div", { className: "row" }, use)));
  } catch (error) {
    box.replaceChildren(notice("err", error.message));
  }
}

async function check() {
  try {
    const r = await api(`/api/folders/autorun?id=${encodeURIComponent(panel.folder.id)}`, { method: "PUT", body: body(true) });
    $("fd-issues").replaceChildren(...issuesList(r.issues));
    $("fd-result").replaceChildren(notice(r.issues.length ? "" : "ok", r.issues.length ? "Avertissements ci-dessus." : "Aucun problème détecté.", r.issues.length ? "alert" : "check"));
  } catch (error) {
    $("fd-result").replaceChildren(notice("err", error.message));
  }
}

async function save() {
  try {
    const r = await api(`/api/folders/autorun?id=${encodeURIComponent(panel.folder.id)}`, { method: "PUT", body: body() });
    panel.editing = false;
    await loadFolderAutorun();
    await hooks.reload(true);
    $("fd-read-issues").replaceChildren(...issuesList(r.issues));
    $("fd-msg").replaceChildren(notice("ok", r.written ? "autorun.cmd enregistré dans le dossier : la compression le reprendra." : "Aucun changement.", "check"));
    if (r.written) toast("autorun.cmd enregistré");
  } catch (error) {
    $("fd-result").replaceChildren(notice("err", error.message));
  }
}

export function init({ reload, cover, pack }) {
  hooks = { reload, cover, pack };
  $("fd-close").onclick = closeFolder;
  $("fd-edit").onclick = () => setEditing(true);
  $("fd-cancel").onclick = () => { if (confirmLeave()) setEditing(false); paintHead(); };
  $("fd-propose").onclick = propose;
  $("fd-check").onclick = check;
  $("fd-save").onclick = save;
  $("fd-cover-btn").onclick = () => hooks.cover(panel.folder);
  $("fd-pack").onclick = () => { if (confirmLeave()) hooks.pack(panel.folder); };
}
