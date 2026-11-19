// install-dialog.js — Dialogues d'installation et de désinstallation.

import { api } from "./api.js";
import { $, badge, el, icon, size, state, toast, withBusy, withTip } from "./core.js";
import { notice } from "./detail/autorun.js";
import { canInstallOn, etaText, gamesOf, imageFile, inst, instDialog, instPlan, installedOn, isWsq, loadInstall, machineIcon, machineOf, selectedImages } from "./install.js";
import { refreshTasks } from "./tasks.js";

const DLG_INST = { install: "dlg-install", uninstall: "dlg-uninstall" };

function openInstall(images) {
  const todo = images.filter(isWsq);
  if (!todo.length) { toast("Seules les images .wsquashfs s'installent", "alert", true); return; }
  instDialog.current = { mode: "install", images: todo, skipped: images.length - todo.length, picked: null, edit: null, busy: false };
  paintInstDialog();
  $(DLG_INST.install).showModal();
  loadInstall();                 // l'état affiché est relu à l'ouverture
}

function openUninstall(images) {
  const todo = images.filter(isWsq);
  if (!todo.length) { toast("Seules les images .wsquashfs se désinstallent", "alert", true); return; }
  instDialog.current = { mode: "uninstall", images: todo, skipped: images.length - todo.length, picked: null, edit: null, busy: false };
  paintInstDialog();
  $(DLG_INST.uninstall).showModal();
  loadInstall();
}

// Une ligne par client ; le dialogue entier est refait à chaque changement (peu d'éléments)
export function paintInstDialog() {
  if (!instDialog.current) return;
  const d = instDialog.current, install = d.mode === "install", p = install ? "in" : "un";
  const dlg = $(DLG_INST[d.mode]);
  const files = d.images.map(imageFile);
  const n = d.images.length;
  const label = (f) => f.replace(/\.wsquashfs$/i, "");      // nom affiché, sans l'extension
  const shown = files.slice(0, 3).map((f) => `« ${label(f)} »`).join(", ") + (n > 3 ? ` et ${n - 3} autre${n - 3 > 1 ? "s" : ""}` : "");
  const skipped = d.skipped ? ` · ${d.skipped} ignorée${d.skipped > 1 ? "s" : ""} (pas une .wsquashfs)` : "";
  dlg.querySelector("h2").textContent = n === 1 ? `${install ? "Installer" : "Désinstaller"} « ${label(files[0])} » ${install ? "sur" : "de"}…`
    : `${install ? "Installer" : "Désinstaller"} ${n} images ${install ? "sur" : "de"}…`;
  const total = d.images.reduce((sum, i) => sum + (i.size || 0), 0);
  $(`${p}-images`).textContent = `${n} image${n > 1 ? "s" : ""} : ${shown}${install ? ` · ${size(total)}` : ""}${skipped}`;

  // Clients proposés : tous (installer), ou ceux qui ont au moins une de ces images
  const rows = [];
  const candidates = install ? inst.machines.map((m) => ({ m, plan: instPlan(m, d.images) }))
    : inst.machines.filter((m) => m.ok && d.images.some((i) => gamesOf(m).includes(imageFile(i)))).map((m) => ({ m }));
  const eligible = candidates.filter((c) => install ? canInstallOn(c.m, c.plan) : true).map((c) => c.m.id);
  d.picked = new Set([...(d.picked ?? eligible)].filter((id) => eligible.includes(id)));

  const status = [];
  if (inst.error) status.push(notice("err", `États des clients indisponibles : ${inst.error}`));
  else if (!inst.loaded) status.push(el("div", { className: "notice info" }, el("span", { className: "spinner" }), el("span", { textContent: "Lecture des clients…" })));
  else if (!install) {
    const absent = d.images.filter((i) => !installedOn(imageFile(i)).length).length;
    if (absent) status.push(notice("info", `${absent} image${absent > 1 ? "s" : ""} installée${absent > 1 ? "s" : ""} nulle part : ${absent > 1 ? "elles sont" : "elle est"} ignorée${absent > 1 ? "s" : ""}.`));
  }
  $(`${p}-status`).replaceChildren(...status);
  $(`${p}-refresh`).classList.toggle("spin", inst.loading);

  for (const { m, plan } of candidates) {
    const box = el("input", { type: "checkbox", id: `${p}-${m.id.replace(/\W/g, "_")}`, checked: d.picked.has(m.id), disabled: !eligible.includes(m.id) });
    box.onchange = () => { box.checked ? d.picked.add(m.id) : d.picked.delete(m.id); paintInstDialog(); };
    const chips = [];
    let cls = "", meta = null, dirNode = null;
    if (!m.ok) {
      cls = " none";
      chips.push(withTip(badge("injoignable", "err"), "Injoignable", [m.message || "pas de réponse"]));
    } else if (install) {
      if (!plan.todo.length) { cls = " none"; chips.push(badge("déjà installé", "ok")); }
      else {
        if (plan.done) chips.push(badge(`${plan.done} sur ${n} déjà là`, "info"));
        if (!plan.fits) { cls = " short"; chips.push(badge("espace insuffisant", "err")); }
        meta = el("div", { className: "in-meta" }, `libre ${size(m.free || 0)} · à envoyer ${size(plan.bytes)} (+ 1 Go de marge) · ${etaText(plan.bytes, m.rate)}`);
      }
      dirNode = dirLine(m, d);
    } else {
      const mine = files.filter((f) => gamesOf(m).includes(f));
      meta = el("div", { className: "in-meta mono", textContent: mine.join(", ") });
    }
    rows.push(el("div", { className: `in-row${cls}` }, box, icon(machineIcon(m.id), "sm"),
      el("div", { className: "in-body" },
        el("div", { className: "in-head" }, el("label", { htmlFor: box.id, className: "in-name", textContent: m.name }), ...chips),
        meta, dirNode)));
  }
  if (!rows.length && inst.loaded && !inst.error) {
    rows.push(el("p", { className: "muted", textContent: install ? "Aucun client associé : onglet Sauvegardes, « Associer »."
      : "Ces images ne sont installées sur aucun client joignable." }));
  }
  $(`${p}-targets`).replaceChildren(...rows);
  const run = $(`${p}-run`);
  run.disabled = d.busy || d.picked.size === 0;
  run.textContent = d.busy ? "Lancement…" : install ? "Installer" : "Désinstaller";
}

// Dossier d'installation : affichage, ou champ de saisie (« Modifier »)
function dirLine(m, d) {
  if (d.edit?.id === m.id) return dirEditor(m, d);
  return el("div", { className: "in-dir" },
    el("code", { textContent: m.dir || "dossier non défini" }),
    m.set ? null : el("em", { textContent: "(deviné)" }),
    el("button", { type: "button", className: "linklike", textContent: "Modifier",
      onclick: () => { d.edit = { id: m.id, value: m.dir || "", error: "", busy: false }; paintInstDialog(); } }));
}

function dirEditor(m, d) {
  const edit = d.edit;
  const input = el("input", { type: "text", className: "code", value: edit.value, disabled: edit.busy,
    placeholder: "/chemin/absolu, ex. /home/abc/games" });
  input.oninput = () => { edit.value = input.value; };
  input.onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); saveDir(m, input.value.trim()); } };
  const save = el("button", { type: "button", className: "btn primary sm", disabled: edit.busy, onclick: () => saveDir(m, input.value.trim()) },
    icon("check", "sm"), "Enregistrer");
  const guessed = el("button", { type: "button", className: "btn secondary sm", disabled: edit.busy, onclick: () => saveDir(m, "") },
    icon("undo", "sm"), "Revenir au dossier deviné");
  const cancel = el("button", { type: "button", className: "btn bare sm", onclick: () => { d.edit = null; paintInstDialog(); } }, "Annuler");
  setTimeout(() => input.focus(), 0);
  return el("div", { className: "in-edit" }, input, edit.error ? notice("err", edit.error) : null,
    el("div", { className: "row" }, save, guessed, cancel));
}

async function saveDir(m, dir) {
  const d = instDialog.current;
  if (!d) return;
  d.edit.busy = true;
  d.edit.error = "";
  paintInstDialog();
  try {
    await api(`/api/install/machines/${encodeURIComponent(m.id)}/dir`, { method: "POST", body: { dir } });
    d.edit = null;
    toast(dir ? "Dossier d'installation enregistré" : "Dossier deviné rétabli", "check");
    await loadInstall();          // un autre dossier, c'est une autre liste de jeux : état relu
  } catch (error) {
    if (instDialog.current !== d || !d.edit) return;
    d.edit.busy = false;
    d.edit.error = error.message;
    paintInstDialog();
  }
}

// Envoi ou retrait : POST, puis le suivi passe par la file de tâches (refreshTasks relit l'état)
async function runInstallDialog(install) {
  const d = instDialog.current;
  if (!d || !d.picked.size) return;
  const machines = [...d.picked];
  d.busy = true;
  paintInstDialog();
  try {
    const data = await withBusy($(install ? "in-run" : "un-run"), () => {
      if (install) return api("/api/install", { method: "POST", body: { ids: d.images.map((i) => i.id), machines } });
      const names = [...new Set(d.images.map(imageFile))].filter((f) => machines.some((id) => gamesOf(machineOf(id) || {}).includes(f)));
      return api("/api/uninstall", { method: "POST", body: { names, machines } });
    });
    $(DLG_INST[d.mode]).close();
    const count = `${data.tasks.length} tâche${data.tasks.length > 1 ? "s" : ""}`;
    toast(install ? `Installation lancée (${count})` : `Désinstallation lancée (${count})`, install ? "drive" : "trash");
    refreshTasks();
  } catch (error) {
    d.busy = false;
    toast(error.message, "alert", true);
    paintInstDialog();
  }
}

// Résultat par client d'une tâche install / uninstall (dans le détail de la tâche)
export function installResultNode(r) {
  const items = r.results || [];
  const failed = items.filter((x) => !x.ok).length;
  return el("div", { className: `sync-res${failed ? " err" : ""}` },
    el("strong", { textContent: failed ? `${failed} échec(s) sur ${items.length}` : `${items.length} client(s) sans erreur` }),
    el("ul", { className: "sync-list" }, ...items.map((x) => el("li", { className: x.ok ? "ok" : "err",
      textContent: `${x.ok ? "✓" : "✗"} ${x.machine} : ${x.message}` }))));
}

export function init() {
  for (const id of Object.values(DLG_INST)) $(id).addEventListener("close", () => { instDialog.current = null; });
  $("in-run").onclick = () => runInstallDialog(true);
  $("un-run").onclick = () => runInstallDialog(false);
  $("in-refresh").onclick = () => loadInstall();
  $("un-refresh").onclick = () => loadInstall();
  $("d-inst-install").onclick = () => openInstall([state.current]);
  $("d-inst-uninstall").onclick = () => openUninstall([state.current]);
  $("btn-inst-install").onclick = () => openInstall(selectedImages());
  $("btn-inst-uninstall").onclick = () => openUninstall(selectedImages());
}
