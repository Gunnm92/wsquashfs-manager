// config-optiscaler.js — Onglet « OptiScaler.ini » de la page Configuration : le modèle partiel d'une machine
// ou d'une plateforme. Sections repliables, valeurs possibles en liste, recherche, « modifiés seulement »,
// texte brut. Seules les clés changées sont enregistrées : le serveur ne garde que ce qui diffère du modèle.

import { el, icon } from "./core.js";
import { notice } from "./detail/autorun.js";

// Brouillon de l'onglet, à partir de la réponse de l'API (sections avec valeur effective et surcharge)
export function optiscalerDraft(view) {
  const sections = view.sections || [];
  const values = {};
  for (const s of sections) {
    values[s.name] = {};
    for (const k of s.keys) values[s.name][k.key] = k.value ?? "";
  }
  return { sections, values, text: view.text ?? "", raw: null, filter: "", changedOnly: false };
}

// Modifié par rapport à ce qui est enregistré (valeur effective) ?
export function iniDirty(d) {
  if (d.raw !== null) return d.raw !== d.text;
  return d.sections.some((s) => s.keys.some((k) => (d.values[s.name]?.[k.key] ?? "") !== (k.value ?? "")));
}

// Corps de l'écriture : texte brut, ou valeurs de toutes les clés (le serveur retire ce qui égale le modèle)
export function iniBody(d, baseSha) {
  return d.raw !== null ? { text: d.raw, base_sha: baseSha } : { values: d.values, base_sha: baseSha };
}

// Préréglage : fixe une clé du formulaire (le fichier n'est pas écrit)
export function iniPreset(d, section, key, value) {
  if (d.values[section] && key in d.values[section]) d.values[section][key] = value;
}

// Onglet OptiScaler.ini : `onChange()` après chaque modification (le parent met à jour Enregistrer) ;
// `onMode()` quand on passe du formulaire au texte brut (le parent redessine l'onglet)
export function optiscalerNode(d, onChange, onMode) {
  if (d.raw !== null || !d.sections.length) return rawArea(d, onChange, onMode);
  const search = el("input", { type: "search", value: d.filter, placeholder: "Clé, section, aide…", spellcheck: false });
  const only = el("input", { type: "checkbox", checked: d.changedOnly });
  const count = el("span", { className: "muted fine" });
  const rows = [];
  const groups = d.sections.map((s) => {
    const body = el("div", { className: "ini-body" });
    const badge = el("span", { className: "chip plain t-neutral" });
    const group = el("details", { className: "ini-sec", open: true }, el("summary", {}, el("b", { textContent: s.name }), badge), body);
    for (const k of s.keys) {
      const row = keyRow(d, s, k, () => { refresh(); onChange(); });
      rows.push(row);
      body.append(row.node);
    }
    group._badge = badge;
    group._body = body;
    return group;
  });

  // Filtre et « modifiés seulement » : cache les lignes, puis les sections vides
  const refresh = () => {
    const q = search.value.trim().toLowerCase();
    d.filter = search.value;
    d.changedOnly = only.checked;
    let shown = 0, changed = 0;
    for (const r of rows) {
      const isChanged = r.changed();
      if (isChanged) changed += 1;
      const match = !q || r.text.includes(q);
      const visible = match && (!d.changedOnly || isChanged || r.override);
      r.node.hidden = !visible;
      if (visible) shown += 1;
    }
    groups.forEach((g, i) => {
      const s = d.sections[i];
      const n = s.keys.filter((k) => d.values[s.name][k.key] !== (k.value ?? "")).length;
      g._badge.textContent = n ? `${n} modifiée${n > 1 ? "s" : ""}` : `${s.keys.length} clé${s.keys.length > 1 ? "s" : ""}`;
      g.hidden = !g._body.querySelector(":scope > .ini-row:not([hidden])");
    });
    raw.disabled = iniDirty(d);
    count.textContent = `${shown} clé${shown > 1 ? "s" : ""} affichée${shown > 1 ? "s" : ""} · ${changed} modifiée${changed > 1 ? "s" : ""}`;
  };
  search.oninput = refresh;
  only.onchange = refresh;

  const raw = el("button", { type: "button", className: "btn bare sm", title: "Éditer le fichier partiel tel quel (après enregistrement ou annulation)" },
    icon("code", "sm"), el("span", { textContent: "Texte brut" }));
  raw.onclick = () => { d.raw = d.text; onMode(); };
  const tools = el("div", { className: "ini-tools" }, search,
    el("label", { className: "row muted" }, only, el("span", { textContent: "Modifiées seulement" })), count, raw);
  refresh();
  return el("div", { className: "ini-form" }, tools, ...groups);
}

// Une clé : libellé et aide, liste des valeurs possibles (ou champ libre)
function keyRow(d, s, k, onChange) {
  const current = d.values[s.name][k.key] ?? "";
  const options = [...new Set([...(k.choices || []), ...(current && !(k.choices || []).includes(current) ? [current] : [])])];
  let control;
  if (options.length) {
    control = el("select", {}, new Option("valeur du modèle", ""), ...options.map((v) => new Option(v, v)));
    control.value = current;
    control.onchange = () => { d.values[s.name][k.key] = control.value; onChange(); };
  } else {
    control = el("input", { value: current, placeholder: k.default || "vide", spellcheck: false, autocomplete: "off" });
    control.oninput = () => { d.values[s.name][k.key] = control.value; onChange(); };
  }
  const changed = () => (d.values[s.name][k.key] ?? "") !== (k.value ?? "");
  const badges = el("span", { className: "ini-flags" },
    k.override ? el("span", { className: "chip plain t-accent", title: "Surcharge déjà enregistrée" }, "surcharge") : null,
    k.extra ? el("span", { className: "chip plain t-neutral", title: "Clé absente du modèle installé" }, "hors modèle") : null);
  const node = el("div", { className: "ini-row" },
    el("div", { className: "ini-k", title: k.help || "" },
      el("b", { textContent: k.key }), badges,
      el("small", { className: "muted", textContent: k.help ? k.help : `valeur du modèle : ${k.default || "vide"}` })),
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
    parts.push(notice("info", "Aucune machine n'a OptiScaler installé : le modèle n'est pas connu, le fichier partiel s'écrit en texte brut."));
  } else {
    const back = el("button", { type: "button", className: "btn bare sm" }, icon("list", "sm"), el("span", { textContent: "Formulaire" }));
    back.onclick = () => { d.raw = null; onMode(); };
    parts.push(el("div", { className: "ini-tools" }, back));
  }
  parts.push(el("p", { className: "muted fine", textContent: "[Section] puis Clé=valeur ; une ligne ; est un commentaire." }), area);
  return el("div", { className: "ini-form" }, ...parts);
}
