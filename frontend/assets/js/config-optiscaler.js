// config-optiscaler.js — Modèle partiel d'OptiScaler.ini d'une machine ou d'une plateforme (page Configuration).
// Les réglages essentiels en clair, par thème ; les ~275 autres clés dans « Toutes les options », repliée, avec
// recherche et « modifiées seulement » ; texte brut en repli. Seules les clés changées sont enregistrées :
// une clé laissée vide n'est pas écrite (la machine ou le modèle s'applique). Le reste se règle en jeu (menu Inser), gardé par jeu.

import { el, icon } from "./core.js";
import { notice } from "./detail/autorun.js";

// Réglages essentiels : [section, clé, libellé, valeurs possibles (vide : champ libre), aide courte].
// Une clé absente du modèle installé n'est pas montrée.
const TF = ["true", "false"];
const ESSENTIAL = [
  { title: "Upscaler", keys: [
    ["Upscalers", "Dx12Upscaler", "Jeux DirectX 12", ["xess", "fsr21", "fsr22", "fsr31", "dlss"], "fsr31 sert aussi pour FSR 4 ; dlss : carte NVIDIA."],
    ["Upscalers", "Dx11Upscaler", "Jeux DirectX 11", ["fsr22", "fsr31", "xess", "fsr21_12", "fsr22_12", "fsr31_12", "xess_12", "dlss"], "_12 : passe par DirectX 12 (fsr31_12 pour FSR 4)."],
    ["Upscalers", "VulkanUpscaler", "Jeux Vulkan", ["fsr21", "fsr22", "fsr31", "xess", "fsr21_12", "fsr31_12", "dlss"], "fsr31_12 pour FSR 4."]] },
  { title: "FSR 4 (AMD)", keys: [
    ["FSR", "Fsr4Update", "Passer FSR 3.1 à FSR 4", TF, "Par défaut : oui sur RDNA 2, 3 et 4."],
    ["FSR", "Fsr4Preset", "Préréglage FSR 4", ["0", "1", "2", "3", "4", "5"], "0 AA natif, 1 ultra qualité/qualité, 2 équilibré, 3 performance, 4 DRS, 5 ultra performance."]] },
  { title: "Génération d'images", keys: [
    ["FrameGen", "Enabled", "Activée", TF, "Désactivée par défaut."],
    ["FrameGen", "FGInput", "Source", ["nofg", "dlssg", "nukems", "fsrfg", "upscaler", "fsrfg30"], "fsrfg : avec toute sortie ; dlssg : jeux qui ont DLSS-G."],
    ["FrameGen", "FGOutput", "Sortie", ["nofg", "fsrfg", "xefg", "nukems"], "fsrfg : FSR 3 FG (AMD et NVIDIA)."]] },
  { title: "DLSS et carte graphique", keys: [
    ["DLSS", "Enabled", "DLSS", TF, "Appels au vrai NVNGX (carte NVIDIA)."],
    ["Spoofing", "Dxgi", "Se présenter comme une carte NVIDIA", TF, "Débloque les options DLSS des jeux sur AMD et Intel (défaut : oui sur AMD/Intel)."]] },
  { title: "Menu et images par seconde", keys: [
    ["Menu", "ShortcutKey", "Touche du menu", [], "Code de touche : 0x2D = Inser (défaut), 0x24 = Origine."],
    ["Menu", "ShowFps", "Compteur d'images/s", TF, ""],
    ["Framerate", "FramerateLimit", "Limite d'images/s", [], "Nombre, ex. 60 ; 0 : aucune."]] },
];
// Valeur enregistrée dans CE fichier ("" : non réglée ici, la couche plus faible ou le modèle s'applique)
const saved = (k) => (k.override ? k.value ?? "" : "");

const isEssential = (section, key) => ESSENTIAL.some((g) => g.keys.some(([s, k]) => s === section && k === key));

// Brouillon, à partir de la réponse de l'API (sections avec valeur effective et surcharge)
export function optiscalerDraft(view) {
  const sections = view.sections || [];
  const values = {};
  for (const s of sections) {
    values[s.name] = {};
    for (const k of s.keys) values[s.name][k.key] = saved(k);
  }
  return { sections, values, text: view.text ?? "", raw: null, filter: "", changedOnly: false, open: false };
}

// Modifié par rapport à ce qui est enregistré (valeur effective) ?
export function iniDirty(d) {
  if (d.raw !== null) return d.raw !== d.text;
  return d.sections.some((s) => s.keys.some((k) => (d.values[s.name]?.[k.key] ?? "") !== saved(k)));
}

// Corps de l'écriture : texte brut, ou valeurs de toutes les clés (vide : clé retirée du fichier)
export function iniBody(d, baseSha) {
  return d.raw !== null ? { text: d.raw, base_sha: baseSha } : { values: d.values, base_sha: baseSha };
}

// Préréglage : fixe une clé du formulaire (le fichier n'est pas écrit)
export function iniPreset(d, section, key, value) {
  if (d.values[section] && key in d.values[section]) d.values[section][key] = value;
}

// `onChange()` après chaque modification (le parent met à jour Enregistrer) ; `onMode()` au passage au
// texte brut (le parent redessine). `ctx.inherited` : { section: { clé: valeur } } de la machine, montré
// en regard d'une plateforme ; `ctx.from` : nom de la machine.
export function optiscalerNode(d, onChange, onMode, ctx = { inherited: {}, from: "" }) {
  if (d.raw !== null || !d.sections.length) return rawArea(d, onChange, onMode);
  const find = (section, key) => {
    const s = d.sections.find((x) => x.name === section);
    const k = s?.keys.find((x) => x.key === key);
    return k ? { s, k } : null;
  };

  // Essentiel
  const essential = ESSENTIAL.map((group) => {
    const rows = group.keys.map(([section, key, label, choices, help]) => find(section, key) && { ...find(section, key), meta: { label, choices, help } }).filter(Boolean);
    if (!rows.length) return null;
    return el("div", { className: "cfg-group" }, el("p", { className: "dsec", textContent: group.title }),
      ...rows.map(({ s, k, meta }) => keyRow(d, s, k, onChange, ctx, meta).node));
  }).filter(Boolean);

  // Toutes les autres options, repliées
  const search = el("input", { type: "search", value: d.filter, placeholder: "Clé, section, aide…", spellcheck: false });
  const only = el("input", { type: "checkbox", checked: d.changedOnly });
  const count = el("span", { className: "muted fine" });
  const rows = [];
  const groups = d.sections.map((s) => {
    const keys = s.keys.filter((k) => !isEssential(s.name, k.key));
    if (!keys.length) return null;
    const body = el("div", { className: "ini-body" });
    const badge = el("span", { className: "chip plain t-neutral" });
    const group = el("details", { className: "ini-sec" }, el("summary", {}, el("b", { textContent: s.name }), badge), body);
    for (const k of keys) {
      const row = keyRow(d, s, k, () => { refresh(); onChange(); }, ctx);
      rows.push(row);
      body.append(row.node);
    }
    group._badge = badge;
    group._body = body;
    group._keys = keys;
    group._name = s.name;
    return group;
  }).filter(Boolean);

  const refresh = () => {
    const q = search.value.trim().toLowerCase();
    d.filter = search.value;
    d.changedOnly = only.checked;
    let shown = 0;
    for (const r of rows) {
      const visible = (!q || r.text.includes(q)) && (!d.changedOnly || r.changed() || r.override);
      r.node.hidden = !visible;
      if (visible) shown += 1;
    }
    for (const g of groups) {
      const n = g._keys.filter((k) => d.values[g._name][k.key]).length;
      g._badge.textContent = n ? `${n} réglée${n > 1 ? "s" : ""}` : `${g._keys.length}`;
      g.hidden = !g._body.querySelector(":scope > .ini-row:not([hidden])");
      if (q || d.changedOnly) g.open = !g.hidden;
    }
    raw.disabled = iniDirty(d);
    count.textContent = `${shown} clé${shown > 1 ? "s" : ""}`;
  };
  search.oninput = refresh;
  only.onchange = refresh;

  const raw = el("button", { type: "button", className: "btn bare sm", title: "Éditer le fichier partiel tel quel (après enregistrement ou annulation)" },
    icon("code", "sm"), el("span", { textContent: "Texte brut" }));
  raw.onclick = () => { d.raw = d.text; onMode(); };
  const all = el("details", { className: "cfg-more", open: d.open },
    el("summary", {}, el("b", { textContent: "Toutes les options" }), el("span", { className: "muted fine", textContent: `${rows.length} clés, pour les cas particuliers` })),
    el("div", { className: "ini-form" },
      el("div", { className: "ini-tools" }, search,
        el("label", { className: "row muted" }, only, el("span", { textContent: "Réglées seulement" })), count, raw),
      ...groups));
  all.ontoggle = () => { d.open = all.open; };
  refresh();
  return el("div", { className: "ini-form" }, ...essential, all);
}

// Une clé : libellé (ou nom de clé) et aide, liste des valeurs possibles (ou champ libre)
function keyRow(d, s, k, onChange, ctx, meta = null) {
  const current = d.values[s.name][k.key] ?? "";
  const label = meta?.label ?? "";
  const known = meta ? (meta.choices.length ? ["auto", ...meta.choices] : []) : (k.choices || []);
  const options = [...new Set([...known, ...(current && !known.includes(current) ? [current] : [])])];
  const fromMachine = ctx.inherited?.[s.name]?.[k.key];
  const fallback = fromMachine !== undefined ? `hérité : ${fromMachine} (${ctx.from})` : `modèle : ${k.default || "auto"}`;
  let control;
  if (options.length) {
    control = el("select", {}, new Option(fallback, ""), ...options.map((v) => new Option(v, v)));
    control.value = current;
    control.onchange = () => { d.values[s.name][k.key] = control.value; onChange(); };
  } else {
    control = el("input", { value: current, placeholder: fallback, spellcheck: false, autocomplete: "off" });
    control.oninput = () => { d.values[s.name][k.key] = control.value; onChange(); };
  }
  const changed = () => (d.values[s.name][k.key] ?? "") !== saved(k);
  const flags = el("span", { className: "ini-flags" },
    k.override ? el("span", { className: "chip plain t-accent", title: "Réglage déjà enregistré ici" }, "réglé") : null,
    k.extra ? el("span", { className: "chip plain t-neutral", title: "Clé absente du modèle installé" }, "hors modèle") : null);
  const node = el("div", { className: "ini-row" },
    el("div", { className: "ini-k", title: k.help || "" },
      el("span", { className: "row" }, el("b", { className: label ? "lbl" : "", textContent: label || k.key }), flags),
      el("small", { className: "muted", textContent: meta ? [meta.help, `[${s.name}] ${k.key}`].filter(Boolean).join(" · ") : (k.help || "") })),
    control);
  node.dataset.key = k.key;
  return { node, changed, override: Boolean(k.override), text: `${s.name} ${k.key} ${k.help || ""}`.toLowerCase() };
}

// Texte brut du fichier partiel (sans modèle connu, c'est le seul mode)
function rawArea(d, onChange, onMode) {
  const area = el("textarea", { rows: 12, spellcheck: false, value: d.raw ?? d.text });
  area.oninput = () => { d.raw = area.value; onChange(); };
  const parts = [];
  if (!d.sections.length) {
    parts.push(notice("info", "Le modèle d'OptiScaler n'est pas connu (aucun client joignable ne l'a installé) : le fichier partiel s'écrit en texte brut."));
  } else {
    const back = el("button", { type: "button", className: "btn bare sm" }, icon("list", "sm"), el("span", { textContent: "Formulaire" }));
    back.onclick = () => { d.raw = null; onMode(); };
    parts.push(el("div", { className: "ini-tools" }, back));
  }
  parts.push(el("p", { className: "muted fine", textContent: "[Section] puis Clé=valeur ; une ligne commençant par ; est un commentaire." }), area);
  return el("div", { className: "ini-form" }, ...parts);
}
