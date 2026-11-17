// mass.js — Actions en masse sur les images : autorun, fichiers et registre.

import { api } from "./api.js";
import { $, badge, el, icon, size, state, storage, toast } from "./core.js";
import { commitOverrides, notice } from "./detail/autorun.js";
import { loadLibrary } from "./library.js";
import { queued } from "./tasks.js";
import { whenOf } from "./when.js";

function opRow(op = { op: "set", key: "", value: "" }) {
  const kind = el("select", {}, new Option("Poser / modifier", "set"), new Option("Retirer", "remove"), new Option("Remplacer tout", "replace"));
  const key = el("input", { placeholder: "CLÉ", value: op.key || "", className: "code" });
  key.setAttribute("list", "known-keys");
  const value = el("input", { placeholder: "valeur", value: op.value || "", className: "code" });
  const template = el("textarea", { placeholder: "CMD=…\nDIR=…\nGAME_VERSION=…", value: op.template || "" });
  const remove = el("button", { className: "iconbtn", title: "Retirer l'opération" }, icon("x"));
  const row = el("div", { className: "op" }, kind, key, value, remove);
  const update = () => {
    const replace = kind.value === "replace";
    key.hidden = replace;
    value.hidden = replace;
    value.style.visibility = kind.value === "remove" ? "hidden" : "";   // garde la grille alignée
    if (replace && !template.isConnected) row.insertBefore(template, remove);
    if (!replace) template.remove();
    row.style.gridTemplateColumns = replace ? "11rem 1fr auto" : "";
    $("m-run").disabled = true;
  };
  kind.value = op.op;
  kind.onchange = update;
  for (const input of [key, value, template]) input.addEventListener("input", () => { $("m-run").disabled = true; });
  remove.onclick = () => { row.remove(); $("m-run").disabled = true; };
  row.read = () => kind.value === "replace" ? { op: "replace", template: template.value }
    : kind.value === "remove" ? { op: "remove", key: key.value.trim() }
    : { op: "set", key: key.value.trim(), value: value.value };
  update();
  return row;
}

const massBody = () => ({ ids: [...state.selected], ops: [...$("m-ops").children].map((row) => row.read()), when: whenOf(), target: $("m-target").value });

function lineDiff(before, after) {
  const a = (before || "").replace(/\r\n/g, "\n").split("\n").filter(Boolean);
  const b = after.replace(/\r\n/g, "\n").split("\n").filter(Boolean);
  const out = el("div", { className: "diff" });
  for (const line of a) if (!b.includes(line)) out.append(el("div", { className: "del", textContent: `- ${line}` }));
  for (const line of b) if (!a.includes(line)) out.append(el("div", { className: "add", textContent: `+ ${line}` }));
  return out;
}

const spaceNotices = (space) => space.map((s) => notice(s.needed > s.free ? "err" : "info",
  `${s.path} : ${size(s.needed)} nécessaires (anciennes images gardées en .old), ${size(s.free)} libres`, "drive"));

const waiting = (text) => el("div", { className: "notice info" }, el("span", { className: "spinner" }), el("span", { textContent: text }));

function fileOpRow(kind, values = {}) {
  const input = (placeholder, value = "", style = "") => el("input", { placeholder, value, style, className: "code" });
  const remove = el("button", { className: "iconbtn", title: "Retirer l'opération" }, icon("x"));
  let row, read;
  if (kind === "copy") {
    const dest = input("destination dans l'image, ex. {exe_dir}/dinput8.dll", values.dest);
    const source = input("fichier source sur le serveur, ex. /config/tools/dinput8.dll", values.source);
    row = el("div", { className: "op", style: "grid-template-columns:7rem 1fr 1fr auto" }, el("div", {}, badge("ajouter", "ok")), dest, source, remove);
    read = () => ({ copy: { dest: dest.value.trim(), source: source.value.trim() } });
  } else if (kind === "delete") {
    const path = input("chemin dans l'image", values.path);
    row = el("div", { className: "op", style: "grid-template-columns:7rem 1fr auto" }, el("div", {}, badge("supprimer", "err")), path, remove);
    read = () => ({ delete: path.value.trim() });
  } else {
    const file = el("select", {}, new Option("user.reg", "user.reg"), new Option("system.reg", "system.reg"));
    file.value = values.file || "user.reg";
    const key = input("clé, ex. Software\\Wine\\DllOverrides", values.key);
    const name = input("nom", values.name, "max-width:9rem");
    const value = input("valeur", values.value, "max-width:11rem");
    row = el("div", { className: "op", style: "grid-template-columns:7rem 1fr auto auto auto" }, file, key, name, value, remove);
    read = () => ({ reg: { file: file.value, key: key.value.trim(), name: name.value.trim(), value: value.value } });
  }
  remove.onclick = () => { row.remove(); $("f-run").disabled = true; };
  row.addEventListener("input", () => { $("f-run").disabled = true; });
  row.read = read;
  return row;
}

function filesBody() {
  const body = { ids: [...state.selected], copy: [], delete: [], reg: [], when: whenOf() };
  for (const row of $("f-ops").children) {
    const [kind, value] = Object.entries(row.read())[0];
    body[kind].push(value);
  }
  return body;
}

const ACTIONS = { add: ["ajouté", "ok"], replace: ["remplacé", "warn"], delete: ["supprimé", "err"], reg: ["registre", "info"], same: ["identique", ""] };

export function init() {
  $("btn-mass").onclick = () => {
    $("m-count").textContent = state.selected.size;
    if (!$("m-ops").children.length) $("m-ops").append(opRow());
    $("m-result").replaceChildren();
    $("m-run").disabled = true;
    $("dlg-mass").showModal();
  };
  $("m-add").onclick = () => $("m-ops").append(opRow());
  $("m-target").onchange = () => { $("m-run").disabled = true; };
  $("m-preview").onclick = async () => {
    $("m-result").replaceChildren(waiting("Calcul de l'aperçu…"));
    try {
      const data = await api("/api/mass/autorun/preview", { method: "POST", body: massBody() });
      const items = data.items.map((item) => {
        const head = el("div", { className: "row" }, el("strong", { textContent: item.name || item.id }));
        if (item.error) head.append(badge(item.error, "err"));
        else if (!item.changed) head.append(badge("inchangé"));
        else if (item.blocked) head.append(badge(item.blocked, "warn"));
        else head.append(badge($("m-target").value === "override" ? "surcharge" : "à reconstruire", "ok"));
        const node = el("div", { className: "preview-item" }, head);
        if (item.changed) node.append(lineDiff(item.before, item.after));
        if (item.issues?.length) node.append(el("ul", { className: "issues" }, ...item.issues.map((x) => el("li", { textContent: x.message }))));
        return node;
      });
      const overrideMode = $("m-target").value === "override";
      $("m-result").replaceChildren(el("h3", { textContent: overrideMode
        ? `${data.ready} surcharge(s) à écrire — effet au prochain lancement, sans reconstruction`
        : `${data.ready} image(s) à reconstruire` }), ...spaceNotices(data.space), ...items);
      $("m-run").disabled = data.ready === 0;
      $("m-run").textContent = `Lancer ${data.ready} ${overrideMode ? "surcharge(s)" : "reconstruction(s)"}`;
    } catch (error) {
      $("m-result").replaceChildren(notice("err", error.message));
    }
  };
  $("m-run").onclick = async () => {
    try {
      const data = await api("/api/mass/autorun", { method: "POST", body: massBody() });
      const skipped = data.skipped.map((s) => el("li", { textContent: `${s.id} : ${s.reason}` }));
      const done = data.overrides ? `${data.overrides.length} surcharge(s) écrite(s) : effet au prochain lancement.` : `${data.tasks.length} tâche(s) créée(s).`;
      if (data.overrides) loadLibrary();
      $("m-result").replaceChildren(notice("ok", done, "check"), ...(skipped.length ? [el("ul", { className: "issues" }, ...skipped)] : []));
      $("m-run").disabled = true;
      if (!data.overrides) queued();
    } catch (error) {
      $("m-result").replaceChildren(notice("err", error.message));
    }
  };
  $("btn-mass-commit").onclick = () => commitOverrides([...state.selected]);
  $("btn-mass-validate").onclick = async () => {
    const ids = [...state.selected].filter((id) => state.byId.get(id)?.has_old);
    if (!ids.length) { alert("Aucune image sélectionnée n'a de .old."); return; }
    const libres = ids.reduce((total, id) => total + (state.byId.get(id).old_size || 0), 0);
    if (!confirm(`Supprimer les anciennes versions (.old) de ${ids.length} image(s) (${size(libres)} libérés) ? Elles sont effacées définitivement ; les images actuelles ne changent pas.`)) return;
    try {
      const data = await api("/api/mass/old/validate", { method: "POST", body: { ids } });
      if (data.skipped.length) alert(data.skipped.map((s) => `${s.id} : ${s.reason}`).join("\n"));
      toast("Anciennes versions (.old) supprimées");
    } catch (error) { alert(error.message); }
    loadLibrary();
  };
  $("btn-files").onclick = () => {
    $("f-count").textContent = state.selected.size;
    $("f-result").replaceChildren();
    $("f-run").disabled = true;
    $("dlg-files").showModal();
  };
  for (const button of document.querySelectorAll("[data-add]")) {
    button.onclick = () => { $("f-ops").append(fileOpRow(button.dataset.add)); $("f-run").disabled = true; };
  }
  $("f-preset-fakeping").onclick = () => {
    const source = prompt("Chemin de dinput8.dll (relais fakeping compilé) sur le serveur :", storage.get("wsfs-fakeping") || "");
    if (source === null) return;
    storage.set("wsfs-fakeping", source.trim());
    $("f-ops").replaceChildren(
      fileOpRow("copy", { dest: "{exe_dir}/dinput8.dll", source: source.trim() }),
      fileOpRow("reg", { file: "user.reg", key: "Software\\Wine\\AppDefaults\\{exe_name}\\DllOverrides", name: "dinput8", value: "native,builtin" }));
    $("f-run").disabled = true;
  };
  $("f-preview").onclick = async () => {
    $("f-result").replaceChildren(waiting("Calcul de l'aperçu…"));
    try {
      const data = await api("/api/mass/files/preview", { method: "POST", body: filesBody() });
      const items = data.items.map((item) => {
        const head = el("div", { className: "row" }, el("strong", { textContent: item.name || item.id }));
        if (item.error) head.append(badge(item.error, "err"));
        else if (!item.changed) head.append(badge("inchangé"));
        else if (item.blocked) head.append(badge(item.blocked, "warn"));
        else head.append(badge("à appliquer", "ok"));
        const node = el("div", { className: "preview-item" }, head);
        for (const a of item.actions || []) {
          const [label, kind] = ACTIONS[a.action] || [a.action, ""];
          node.append(el("div", { className: "row mono" }, badge(label, kind), a.path, el("span", { className: "muted", textContent: a.detail })));
        }
        if (item.warnings?.length) node.append(el("ul", { className: "issues" }, ...item.warnings.map((w) => el("li", { textContent: w }))));
        return node;
      });
      $("f-result").replaceChildren(el("h3", { textContent: `${data.ready} image(s) à modifier` }), ...spaceNotices(data.space), ...items);
      $("f-run").disabled = data.ready === 0;
      $("f-run").textContent = `Lancer ${data.ready} tâche(s)`;
    } catch (error) {
      $("f-result").replaceChildren(notice("err", error.message));
    }
  };
  $("f-run").onclick = async () => {
    try {
      const data = await api("/api/mass/files", { method: "POST", body: filesBody() });
      const skipped = data.skipped.map((s) => el("li", { textContent: `${s.id} : ${s.reason}` }));
      $("f-result").replaceChildren(notice("ok", `${data.tasks.length} tâche(s) créée(s).`, "check"),
        ...(skipped.length ? [el("ul", { className: "issues" }, ...skipped)] : []));
      $("f-run").disabled = true;
      queued();
    } catch (error) {
      $("f-result").replaceChildren(notice("err", error.message));
    }
  };
}
