// config.js — Configuration : réglages communs du lanceur, par machine (fichier chez l'agent du client) et par
// plateforme (_defaults.autorun et _optiscaler.ini dans le dossier du système). Colonne des machines et des
// plateformes ; fiche avec le formulaire des clés (onglet « Réglages du lanceur ») et le modèle OptiScaler
// (onglet « OptiScaler.ini », config-optiscaler.js). Enregistrer : aperçu des changements, puis confirmation.

import { api } from "./api.js";
import { detailPanel, markCurrent } from "./collection.js";
import { $, el, emptyState, icon, toast } from "./core.js";
import { notice } from "./detail/autorun.js";
import { iniBody, iniDirty, iniPreset, optiscalerDraft, optiscalerNode } from "./config-optiscaler.js";

const cfg = {
  keys: null,            // /api/config/keys : groupes, aide, préréglages
  machines: [],
  folders: [],
  current: null,         // { kind: "machine" | "folder", id }
  view: null,            // fiche lue
  tab: "lanceur",
  values: {},            // formulaire des clés du lanceur : { CLÉ: valeur }
  raw: null,             // texte brut du defaults.autorun, null : formulaire
  ini: null,             // brouillon de l'onglet OptiScaler.ini
  loaded: false,
};

const panel = () => detailPanel($("config-main"), $("cdetail"));
const GROUP_KEYS = () => cfg.keys.groups.flatMap((g) => g.keys.map((k) => k.key));
const base = () => `/api/config/${cfg.current.kind === "machine" ? "machines" : "folders"}/${encodeURIComponent(cfg.current.id)}`;

// ------------------------------------------------------------------ lecture et listes

export async function loadConfig() {
  $("c-machines").replaceChildren(el("div", { className: "notice info", role: "status" }, el("span", { className: "spinner" }), el("span", { textContent: "Lecture des clients…" })));
  try {
    cfg.keys ??= await api("/api/config/keys");
    const [machines, folders] = await Promise.all([api("/api/config/machines"), api("/api/config/folders")]);
    cfg.machines = machines.machines;
    cfg.folders = folders.folders;
    cfg.loaded = true;
  } catch (error) {
    $("c-machines").replaceChildren(notice("err", error.message));
    return;
  }
  paintLists();
  if (cfg.current) markCurrent(".cfg-row", `${cfg.current.kind}:${cfg.current.id}`);
}

function listRow(key, title, lines, extra = []) {
  const row = el("button", { type: "button", className: "cfg-row" },
    el("span", { className: "cfg-t" }, el("b", { textContent: title }), ...lines.map((l) => el("small", { className: "muted", textContent: l }))),
    el("span", { className: "cfg-x" }, ...extra));
  row.dataset.id = key;
  row.onclick = () => openFiche(key);
  return row;
}

function paintLists() {
  const machines = cfg.machines.map((m) => {
    const gpu = m.gpu ? [m.gpu.nvidia && "NVIDIA", m.gpu.amd && "AMD"].filter(Boolean).join(" + ") || "GPU inconnu" : "GPU non lu";
    const opti = m.optiscaler ? (m.optiscaler.installed ? `OptiScaler ${m.optiscaler.version}`.trim() : "OptiScaler non installé") : "";
    const lines = [gpu, opti || m.error].filter(Boolean);
    return listRow(`machine:${m.id}`, m.name, lines, m.error ? [el("span", { className: "chip plain t-err", textContent: "injoignable" })] : []);
  });
  $("c-machines").replaceChildren(...(machines.length ? machines : [emptyState("monitor", "Aucune machine associée", "Associer un client dans Sauvegardes.")]));
  const folders = cfg.folders.map((f) => listRow(`folder:${f.id}`, f.system, [f.path], [
    f.defaults ? el("span", { className: "chip plain t-accent", textContent: "réglages" }) : null,
    f.optiscaler ? el("span", { className: "chip plain t-violet", textContent: "OptiScaler" }) : null]));
  $("c-folders").replaceChildren(...(folders.length ? folders : [emptyState("folder", "Aucun système avec des images", "Les plateformes apparaissent dès qu'une image est dans roms/<système>/.")]));
  $("c-summary").textContent = `${cfg.machines.length} machine${cfg.machines.length > 1 ? "s" : ""} · ${cfg.folders.length} plateforme${cfg.folders.length > 1 ? "s" : ""}`;
}

// ------------------------------------------------------------------ fiche

async function openFiche(key) {
  const [kind, id] = [key.slice(0, key.indexOf(":")), key.slice(key.indexOf(":") + 1)];
  if (cfg.current && dirty() && !confirm("Quitter sans enregistrer les modifications ?")) return;
  cfg.current = { kind, id };
  cfg.view = null;
  markCurrent(".cfg-row", key);
  panel().open();
  $("c-dtitle").textContent = id;
  $("c-body").replaceChildren(el("div", { className: "notice info" }, el("span", { className: "spinner" }), el("span", { textContent: "Lecture…" })));
  await reopen();
}

// Relit la fiche (après un enregistrement, ou pour annuler les modifications)
async function reopen() {
  if (!cfg.current) return;
  try {
    cfg.view = await api(base());
  } catch (error) {
    $("c-body").replaceChildren(notice("err", error.message));
    return;
  }
  const v = cfg.view;
  cfg.values = { ...v.defaults.values };
  cfg.raw = null;
  cfg.ini = optiscalerDraft(v.optiscaler);
  if (cfg.current.kind === "machine") {
    $("c-dtitle").textContent = v.machine.name;
    $("c-dsub").textContent = v.gpu ? `GPU : ${[v.gpu.nvidia && "NVIDIA", v.gpu.amd && "AMD"].filter(Boolean).join(" + ") || "non détecté"}` : "";
  } else {
    $("c-dtitle").textContent = v.folder.system;
    $("c-dsub").textContent = v.folder.path;
  }
  paint();
}

// Modifications non enregistrées : réglages du lanceur ou OptiScaler.ini
function dirtyDefaults() {
  const v = cfg.view;
  if (!v) return false;
  if (cfg.raw !== null) return cfg.raw !== v.defaults.text;
  return GROUP_KEYS().some((k) => (cfg.values[k] ?? "") !== (v.defaults.values[k] ?? ""));
}

const dirty = () => dirtyDefaults() || Boolean(cfg.ini && iniDirty(cfg.ini));

function setTab(tab) {
  cfg.tab = tab;
  paint();
}

function paint() {
  for (const b of $("c-tabs").querySelectorAll("button[data-tab]")) b.classList.toggle("on", b.dataset.tab === cfg.tab);
  const body = cfg.tab === "lanceur" ? lanceurBody() : [optiscalerNode(cfg.ini, refreshSave, paint)];
  $("c-body").replaceChildren(...body);
  refreshSave();
}

function refreshSave() {
  $("c-save").disabled = !dirty();
  $("c-reset").disabled = !dirty();
}

// ------------------------------------------------------------------ onglet « Réglages du lanceur »

function lanceurBody() {
  const v = cfg.view, nodes = [];
  for (const text of v.warnings.lanceur) nodes.push(notice("info", text));
  if (cfg.current.kind === "folder") {
    nodes.push(el("p", { className: "muted fine", textContent: "Valeur effective : défaut du lanceur, puis les machines (fichier, variables WSQUASHFS_*), puis ce dossier." }));
  }
  nodes.push(presetBar());
  if (cfg.raw !== null) {
    const area = el("textarea", { rows: 12, spellcheck: false, value: cfg.raw });
    area.oninput = () => { cfg.raw = area.value; refreshSave(); };
    nodes.push(area);
  } else {
    nodes.push(...cfg.keys.groups.map((group) => el("section", { className: "cfg-group" },
      el("p", { className: "dsec", textContent: group.title }), ...group.keys.map((item) => keyRow(item)))));
  }
  nodes.push(el("div", { className: "dactions" }, rawToggle()));
  return nodes;
}

function keyRow(item) {
  const current = cfg.values[item.key] ?? "";
  const envVar = cfg.view.env?.find((e) => e.key === item.key);
  let control;
  if (item.choices.length) {
    const options = [...new Set([...item.choices, ...(current && !item.choices.includes(current) ? [current] : [])])];
    control = el("select", {}, new Option("défaut du lanceur", ""), ...options.map((o) => new Option(o, o)));
    control.value = current;
    control.onchange = () => { cfg.values[item.key] = control.value; refreshSave(); };
  } else {
    control = el("input", { value: current, placeholder: "défaut du lanceur", spellcheck: false, autocomplete: "off" });
    control.oninput = () => { cfg.values[item.key] = control.value.trim(); refreshSave(); };
  }
  return el("div", { className: "cfg-kv" },
    el("label", { className: "cfg-k", title: item.help },
      el("b", { textContent: item.key }), el("small", { className: "muted", textContent: item.help || "" })),
    control,
    envVar ? el("span", { className: "chip plain t-err", title: `La variable ${envVar.var} posée sur le client l'emporte sur ce fichier`,
      textContent: envVar.var }) : null);
}

// Préréglages : remplissent le formulaire sans enregistrer
function presetBar() {
  const presets = cfg.view.presets || [];
  if (!presets.length) return el("span");
  const bar = el("div", { className: "cfg-presets" }, el("span", { className: "muted fine", textContent: "Préréglages :" }));
  for (const preset of presets) {
    const b = el("button", { type: "button", className: "btn secondary sm", disabled: cfg.raw !== null,
      title: `${Object.entries(preset.values).map(([k, v]) => `${k}=${v}`).concat(preset.ini.map((i) => `[${i.section}] ${i.key}=${i.value}`)).join(" ; ") || "aucun changement"}` },
      el("span", { textContent: preset.label }));
    b.onclick = () => {
      for (const [key, value] of Object.entries(preset.values)) cfg.values[key] = value;
      for (const i of preset.ini) iniPreset(cfg.ini, i.section, i.key, i.value);
      paint();
    };
    bar.append(b);
  }
  return bar;
}

// Bascule formulaire / texte brut : pas au milieu d'une modification (elle serait perdue)
function rawToggle() {
  const on = cfg.raw !== null;
  const b = el("button", { type: "button", className: "btn bare sm", disabled: !on && dirtyDefaults(),
    title: "Éditer le defaults.autorun tel quel (après enregistrement ou annulation)" },
    icon(on ? "list" : "code", "sm"), el("span", { textContent: on ? "Formulaire" : "Texte brut" }));
  b.onclick = () => {
    cfg.raw = on ? null : cfg.view.defaults.text;
    paint();
  };
  return b;
}

// ------------------------------------------------------------------ enregistrement

// Aperçu de chaque fichier modifié, confirmation, puis écriture (409 : la fiche est relue)
async function save() {
  const v = cfg.view, jobs = [];
  if (dirtyDefaults()) {
    const body = cfg.raw !== null ? { text: cfg.raw, base_sha: v.defaults.sha }
      : { values: Object.fromEntries(GROUP_KEYS().map((k) => [k, cfg.values[k] ?? ""])), base_sha: v.defaults.sha };
    jobs.push({ label: "Réglages du lanceur", path: `${base()}/defaults`, body });
  }
  if (cfg.ini && iniDirty(cfg.ini)) {
    jobs.push({ label: "OptiScaler.ini", path: `${base()}/optiscaler`, body: iniBody(cfg.ini, v.optiscaler.sha) });
  }
  if (!jobs.length) return;
  let previews;
  try {
    previews = await Promise.all(jobs.map((j) => api(`${j.path}?preview=1`, { method: "PUT", body: j.body })));
  } catch (error) {
    toast(error.message, "alert", true);
    return;
  }
  const changes = jobs.map((job, i) => ({ ...job, diff: previews[i].diff })).filter((c) => c.diff);
  if (!changes.length) { toast("Aucun changement à enregistrer"); return; }
  if (!(await confirmSave(changes))) return;
  try {
    for (const change of changes) await api(change.path, { method: "PUT", body: change.body });
    toast("Réglages enregistrés");
  } catch (error) {
    toast(error.message, "alert", true);
  }
  await reopen();
  loadConfig();
}

// Fenêtre d'aperçu : diff de chaque fichier ; résout true si l'utilisateur confirme
function confirmSave(changes) {
  const dlg = $("dlg-config");
  $("dc-body").replaceChildren(...changes.map((c) => el("div", { className: "cfg-diff" },
    el("p", { className: "dsec", textContent: c.label }), el("pre", { className: "cfg-pre", textContent: c.diff }))));
  let answer = false;
  $("dc-ok").onclick = () => { answer = true; dlg.close(); };
  $("dc-cancel").onclick = () => dlg.close();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(answer), { once: true });
    dlg.showModal();
  });
}

export function init() {
  $("c-close").onclick = () => {
    if (dirty() && !confirm("Fermer sans enregistrer les modifications ?")) return;
    cfg.current = null;
    markCurrent(".cfg-row", null);
    panel().close();
  };
  for (const b of $("c-tabs").querySelectorAll("button[data-tab]")) b.onclick = () => setTab(b.dataset.tab);
  $("c-reset").onclick = () => { if (!dirty() || confirm("Abandonner les modifications ?")) reopen(); };
  $("c-save").onclick = save;
}
