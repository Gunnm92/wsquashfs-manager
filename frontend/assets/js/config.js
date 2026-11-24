// config.js — Configuration : réglages communs du lanceur, en couches comme le lanceur les lit.
// Le client choisi en haut de page donne la couche « machine » (fichiers chez son agent) ; le menu
// Plateforme donne la couche « plateforme » (_defaults.autorun et _optiscaler.ini du dossier du système),
// avec en regard la valeur héritée de la machine. « Toutes les plateformes » : on règle la machine.
// Enregistrer : aperçu des changements, puis confirmation.

import { api } from "./api.js";
import { $, el, emptyState, state, storage, toast } from "./core.js";
import { notice } from "./detail/autorun.js";
import { filterDropdown } from "./menu.js";
import { connected } from "./mode.js";
import { iniBody, iniDirty, iniPreset, optiscalerDraft, optiscalerNode } from "./config-optiscaler.js";

const PLATFORM_KEY = "wsfs-config-platform";

const cfg = {
  keys: null,            // /api/config/keys : groupes, aide, préréglages
  machines: [],          // clients associés (GPU, OptiScaler)
  folders: [],           // plateformes
  platform: "",          // plateforme choisie ("" : toutes → couche machine)
  scopeSig: "",          // portée de la fiche affichée
  scope: null,           // { kind: "machine" | "folder", id, machine }
  view: null,            // fiche lue
  inherit: null,         // fiche de la machine, pour les valeurs héritées d'une plateforme
  values: {},            // réglages du lanceur : { CLÉ: valeur }
  raw: null,             // texte brut du defaults.autorun, null : formulaire
  ini: null,             // brouillon OptiScaler
  loaded: false,
};

const GROUP_KEYS = () => cfg.keys.groups.flatMap((g) => g.keys.map((k) => k.key));

// Client de l'en-tête retenu pour la machine (null : « Tous », mode autonome ou client inconnu)
function chosenMachine() {
  if (!connected() || state.client === "all") return null;
  return cfg.machines.find((m) => m.id === state.client) ?? null;
}

function scopeOf() {
  const machine = chosenMachine();
  if (cfg.platform) return { kind: "folder", id: cfg.platform, machine };
  return machine ? { kind: "machine", id: machine.id, machine } : null;
}

const sigOf = (scope) => (scope ? `${scope.kind}:${scope.id}:${scope.machine?.id ?? ""}` : "");

function base(scope = cfg.scope) {
  return scope.kind === "machine" ? `/api/config/machines/${encodeURIComponent(scope.id)}`
    : `/api/config/folders/${encodeURIComponent(scope.id)}`;
}
const machineQuery = () => (cfg.scope?.kind === "folder" && cfg.scope.machine ? `machine=${encodeURIComponent(cfg.scope.machine.id)}` : "");
const withQuery = (path, ...parts) => {
  const q = parts.filter(Boolean).join("&");
  return q ? `${path}?${q}` : path;
};

const gpuText = (gpu) => (gpu ? [gpu.nvidia && "NVIDIA", gpu.amd && "AMD"].filter(Boolean).join(" + ") || "GPU non reconnu" : "");

// ------------------------------------------------------------------ chargement

export async function loadConfig() {
  if (!cfg.loaded) $("c-body").replaceChildren(busy("Lecture des clients et des plateformes…"));
  try {
    cfg.keys ??= await api("/api/config/keys");
    const [machines, folders] = await Promise.all([api("/api/config/machines"), api("/api/config/folders")]);
    cfg.machines = machines.machines;
    cfg.folders = folders.folders;
    cfg.loaded = true;
  } catch (error) {
    $("c-body").replaceChildren(notice("err", error.message));
    return;
  }
  if (!cfg.platform) cfg.platform = storage.get(PLATFORM_KEY) || "";
  if (cfg.platform && !cfg.folders.some((f) => f.id === cfg.platform)) cfg.platform = "";
  cfg.scopeSig = "";            // relue : un autre onglet a pu écrire
  paintConfig();
}

// Redessine la page : appelée au chargement, au changement de plateforme et de client (en-tête)
export function paintConfig() {
  if (!cfg.loaded || document.body.dataset.view !== "config") return;
  paintFilters();
  const scope = scopeOf();
  const sig = sigOf(scope);
  if (sig === cfg.scopeSig) return;
  if (cfg.view && dirty()) toast("Modifications non enregistrées abandonnées", "alert");
  cfg.scopeSig = sig;
  cfg.scope = scope;
  cfg.view = null;
  if (!scope) { paintEmpty(); return; }
  openScope();
}

function paintFilters() {
  const options = cfg.folders.map((f) => [f.id, f.defaults || f.optiscaler ? "check" : null, "t-accent", f.system, null]);
  $("c-filters").replaceChildren(...(cfg.folders.length ? [filterDropdown({
    label: "Plateforme", all: "Toutes les plateformes", total: null, options, value: cfg.platform,
    onChoose: (value) => {
      if (value !== cfg.platform && dirty() && !confirm("Changer de plateforme sans enregistrer les modifications ?")) return;
      cfg.platform = value;
      storage.set(PLATFORM_KEY, value);
      cfg.view = null;          // l'abandon vient d'être accepté
      paintConfig();
    } })] : []));
}

function paintEmpty() {
  $("c-actions").hidden = true;
  const hasClients = connected() && cfg.machines.length;
  $("c-summary").textContent = "";
  $("c-body").replaceChildren(hasClients
    ? emptyState("monitor", "Sélectionne un client", "Choisis un client en haut de page pour régler sa machine, ou une plateforme dans le menu.")
    : emptyState("folder", "Choisis une plateforme", "Les réglages d'une plateforme s'écrivent à côté de ses images (_defaults.autorun)."));
}

const busy = (text) => el("div", { className: "notice info", role: "status" }, el("span", { className: "spinner" }), el("span", { textContent: text }));

async function openScope() {
  const scope = cfg.scope, sig = cfg.scopeSig;
  $("c-actions").hidden = true;
  $("c-body").replaceChildren(busy(scope.kind === "machine" ? `Lecture de ${scope.machine.name}…` : "Lecture…"));
  let view, inherit = null;
  try {
    [view, inherit] = await Promise.all([
      api(withQuery(base(scope), scope.kind === "folder" && scope.machine ? `machine=${encodeURIComponent(scope.machine.id)}` : "")),
      scope.kind === "folder" && scope.machine ? api(base({ kind: "machine", id: scope.machine.id })).catch(() => null) : null,
    ]);
  } catch (error) {
    if (sig === cfg.scopeSig) $("c-body").replaceChildren(notice("err", error.message));
    return;
  }
  if (sig !== cfg.scopeSig) return;        // un autre choix entre-temps
  cfg.view = view;
  cfg.inherit = inherit;
  cfg.values = { ...view.defaults.values };
  cfg.raw = null;
  cfg.ini = optiscalerDraft(view.optiscaler);
  paint();
}

// ------------------------------------------------------------------ page

function dirtyDefaults() {
  const v = cfg.view;
  if (!v) return false;
  if (cfg.raw !== null) return cfg.raw !== v.defaults.text;
  return GROUP_KEYS().some((k) => (cfg.values[k] ?? "") !== (v.defaults.values[k] ?? ""));
}

const dirty = () => dirtyDefaults() || Boolean(cfg.ini && iniDirty(cfg.ini));

function paint() {
  const v = cfg.view, scope = cfg.scope;
  if (scope.kind === "machine") {
    const opti = v.optiscaler.installed ? `OptiScaler ${v.optiscaler.version}`.trim() : "OptiScaler non installé";
    $("c-summary").textContent = `${v.machine.name} · ${[gpuText(v.gpu), opti].filter(Boolean).join(" · ")}`;
  } else {
    $("c-summary").textContent = `Plateforme ${v.folder.system}${scope.machine ? ` · valeurs héritées de ${scope.machine.name}` : ""}`;
  }
  const nodes = [];
  const intro = scope.kind === "machine"
    ? `Réglages de ${v.machine.name} pour toutes les plateformes. Une plateforme ou un jeu peut les remplacer.`
    : `Réglages de la plateforme ${v.folder.system}, écrits à côté de ses images : ils remplacent ceux de la machine.`;
  nodes.push(el("p", { className: "muted fine", textContent: intro }));
  for (const text of new Set([...v.warnings.lanceur])) nodes.push(notice("info", text));
  nodes.push(presetBar());
  nodes.push(el("section", { className: "cfg-sec" }, el("h2", { className: "cfg-h", textContent: "Lanceur" }), ...lanceurNodes()));
  nodes.push(el("section", { className: "cfg-sec" }, el("h2", { className: "cfg-h", textContent: "OptiScaler" }), ...optiscalerNodes()));
  $("c-body").replaceChildren(...nodes);
  $("c-actions").hidden = false;
  refreshSave();
}

function refreshSave() {
  const d = dirty();
  $("c-save").disabled = !d;
  $("c-reset").disabled = !d;
  $("c-dirty").textContent = d ? "Modifications non enregistrées" : "";
}

// ------------------------------------------------------------------ lanceur

function lanceurNodes() {
  if (cfg.raw !== null) {
    const area = el("textarea", { rows: 12, spellcheck: false, value: cfg.raw });
    area.oninput = () => { cfg.raw = area.value; refreshSave(); };
    return [area, el("div", { className: "cfg-tools" }, rawToggle())];
  }
  return [...cfg.keys.groups.map((group) => el("div", { className: "cfg-group" },
    el("p", { className: "dsec", textContent: group.title }), ...group.keys.map((item) => keyRow(item)))),
  el("div", { className: "cfg-tools" }, rawToggle())];
}

// Valeur héritée de la machine pour une plateforme (fichier ou variable WSQUASHFS_*), sinon défaut du lanceur
function inherited(key) {
  const m = cfg.inherit, name = cfg.scope.machine?.name;
  if (cfg.scope.kind !== "folder" || !m) return "";
  const env = m.env?.find((e) => e.key === key);
  if (env) return `hérité de ${name} (${env.var})`;
  const value = m.defaults.values[key];
  return value ? `hérité : ${value} (${name})` : "";
}

function keyRow(item) {
  const current = cfg.values[item.key] ?? "";
  const envVar = cfg.scope.kind === "machine" ? cfg.view.env?.find((e) => e.key === item.key) : null;
  const fallback = inherited(item.key) || "défaut du lanceur";
  let control;
  if (item.choices.length) {
    const options = [...new Set([...item.choices, ...(current && !item.choices.includes(current) ? [current] : [])])];
    control = el("select", {}, new Option(fallback, ""), ...options.map((o) => new Option(o, o)));
    control.value = current;
    control.onchange = () => { cfg.values[item.key] = control.value; refreshSave(); };
  } else {
    control = el("input", { value: current, placeholder: fallback, spellcheck: false, autocomplete: "off" });
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
    title: "Éditer le fichier tel quel (après enregistrement ou annulation)" },
    el("span", { textContent: on ? "Formulaire" : "Texte brut" }));
  b.onclick = () => {
    cfg.raw = on ? null : cfg.view.defaults.text;
    paint();
  };
  return b;
}

// ------------------------------------------------------------------ OptiScaler

// Valeurs du modèle partiel de la machine, pour les montrer en regard d'une plateforme
function inheritedIni() {
  const sections = cfg.scope.kind === "folder" ? cfg.inherit?.optiscaler?.sections : null;
  const out = {};
  for (const s of sections || []) for (const k of s.keys) if (k.override) (out[s.name] ??= {})[k.key] = k.value;
  return out;
}

function optiscalerNodes() {
  const v = cfg.view, scope = cfg.scope, nodes = [];
  if (scope.kind === "machine" && !v.optiscaler.installed) {
    return [notice("info", `OptiScaler n'est pas installé sur ${v.machine.name} : wsquashfs-launcher --setup optiscaler.`)];
  }
  for (const text of v.warnings.optiscaler) if (!v.warnings.lanceur.includes(text)) nodes.push(notice("info", text));
  nodes.push(optiscalerNode(cfg.ini, refreshSave, paint, { inherited: inheritedIni(), from: scope.machine?.name ?? "" }));
  return nodes;
}

// ------------------------------------------------------------------ enregistrement

// Aperçu de chaque fichier modifié, confirmation, puis écriture (409 : la page est relue)
async function save() {
  const v = cfg.view, jobs = [];
  if (dirtyDefaults()) {
    const body = cfg.raw !== null ? { text: cfg.raw, base_sha: v.defaults.sha }
      : { values: Object.fromEntries(GROUP_KEYS().map((k) => [k, cfg.values[k] ?? ""])), base_sha: v.defaults.sha };
    jobs.push({ label: "Réglages du lanceur", path: `${base()}/defaults`, query: "", body });
  }
  if (cfg.ini && iniDirty(cfg.ini)) {
    jobs.push({ label: "OptiScaler.ini", path: `${base()}/optiscaler`, query: machineQuery(), body: iniBody(cfg.ini, v.optiscaler.sha) });
  }
  if (!jobs.length) return;
  let previews;
  try {
    previews = await Promise.all(jobs.map((j) => api(withQuery(j.path, "preview=1", j.query), { method: "PUT", body: j.body })));
  } catch (error) {
    toast(error.message, "alert", true);
    return;
  }
  const changes = jobs.map((job, i) => ({ ...job, diff: previews[i].diff })).filter((c) => c.diff);
  if (!changes.length) { toast("Aucun changement à enregistrer"); return; }
  if (!(await confirmSave(changes))) return;
  try {
    for (const change of changes) await api(withQuery(change.path, change.query), { method: "PUT", body: change.body });
    toast("Réglages enregistrés");
  } catch (error) {
    toast(error.message, "alert", true);
  }
  cfg.view = null;
  await loadConfig();
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
  $("c-reset").onclick = () => {
    if (!dirty() || !confirm("Abandonner les modifications ?")) return;
    cfg.scopeSig = "";
    cfg.view = null;
    paintConfig();
  };
  $("c-save").onclick = save;
}
