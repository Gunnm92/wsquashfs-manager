// detail/autorun.js — Autorun : éditeur texte ⇄ formulaire, analyse et propositions, enregistrement.

import { reportSkipped } from "../actions.js";
import { api } from "../api.js";
import { $, el, helpLink, icon, state } from "../core.js";
import { KEY_LABELS, leaveEdit, markDirty, showTab } from "./detail.js";
import { loadLibrary } from "../library.js";
import { queued } from "../tasks.js";
import { whenOf } from "../when.js";

// Mêmes règles que le serveur (services/autorun.py) : clé en début de ligne,
// première occurrence retenue ; une clé modifiée garde sa place, une clé
// ajoutée va en fin de fichier ; vider un champ retire la clé.

export const KEY_RE = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/;

const KEY_ICONS = { CMD: "terminal", DIR: "folder", GAME_VERSION: "tag", PRE: "play", ATTACH: "link", ATTACH_PROCESS: "cpu",
  WINE: "wine", PROTON: "atom", RUNNER: "drive", ARCH: "cpu", LANG: "book", ENV: "code", HIDRAW: "usb",
  SAVEDIR: "folder", SAVEFILES: "file", DXVK: "monitor", VKD3D: "monitor", D7VK: "monitor", VIRTUAL_DESKTOP: "grid",
  NVAPI: "cpu", VKREFLEX: "zap", FSR: "monitor", FSR4: "monitor", ESYNC: "zap", FSYNC: "zap" };

export function editorLines() {
  const raw = $("a-editor").value.replace(/\r\n/g, "\n");
  const trailing = raw.endsWith("\n");
  const content = trailing ? raw.slice(0, -1) : raw;
  return { lines: content ? content.split("\n") : [], trailing };
}

function setEditorLines(lines, trailing) {
  $("a-editor").value = lines.join("\n") + (trailing && lines.length ? "\n" : "");
}

function getKey(lines, key) {
  for (const line of lines) {
    const m = KEY_RE.exec(line);
    if (m && m[1] === key) return m[2];
  }
  return null;
}

function setKey(key, value) {
  let { lines, trailing } = editorLines();
  const index = lines.findIndex((line) => KEY_RE.exec(line)?.[1] === key);
  if (value === "") {
    lines = lines.filter((line) => KEY_RE.exec(line)?.[1] !== key);
  } else if (index >= 0) {
    lines[index] = `${key}=${value}`;
  } else {
    lines.push(`${key}=${value}`);
    if (lines.length === 1) trailing = true;
  }
  if (value !== "") state.pendingKeys.delete(key);
  setEditorLines(lines, trailing);
  syncForm();
}

// Valeurs proposées par clé (datalists), y compris les runners du client d'essai
export function buildForm() {
  $("a-lists").replaceChildren(...state.keys.map(({ key, values }) =>
    el("datalist", { id: `v-${key}` }, ...values.map((v) => new Option(v, v)))));
  for (const key of ["PROTON", "WINE"]) if (!$(`v-${key}`)) $("a-lists").append(el("datalist", { id: `v-${key}` }));
}

const keyInfo = (key) => state.keys.find((k) => k.key === key);

function kvRow(key, value) {
  const info = keyInfo(key);
  const input = el("input", { id: `k-${key}`, value: value ?? "", placeholder: info ? "valeur" : "", autocomplete: "off", spellcheck: false });
  if ($(`v-${key}`)) input.setAttribute("list", `v-${key}`);
  input.addEventListener("change", () => setKey(key, input.value.trim()));
  const remove = el("button", { className: "iconbtn", title: "Retirer la clé" }, icon("x", "sm"));
  remove.onclick = () => { state.pendingKeys.delete(key); setKey(key, ""); };
  const row = el("div", { className: "kv-pill", title: info?.help || "Clé non reconnue : conservée, ignorée par Batocera" },
    el("span", { className: "disc" }, icon(KEY_ICONS[key] || "terminal")),
    el("span", { className: "k" }, info ? helpLink(`hk-${key}`, key, `${key} dans l'aide`) : key),
    input, remove);
  row.dataset.key = key;
  return row;
}

export function syncForm() {
  const { lines } = editorLines();
  const inFile = [...new Set(lines.map((l) => KEY_RE.exec(l)?.[1]).filter(Boolean))];
  const shown = [...inFile, ...[...state.pendingKeys].filter((k) => !inFile.includes(k))];
  const form = $("a-form");
  const current = [...form.children].map((r) => r.dataset.key);
  if (current.join("\n") === shown.join("\n")) {
    // Mêmes clés : seules les valeurs changent (le champ en cours de saisie est laissé tel quel)
    for (const key of shown) {
      const input = $(`k-${key}`);
      if (input && document.activeElement !== input) input.value = getKey(lines, key) ?? "";
    }
  } else {
    const focused = document.activeElement?.id;
    form.replaceChildren(...shown.map((key) => kvRow(key, getKey(lines, key))));
    if (focused && $(focused) && form.contains($(focused))) $(focused).focus();
  }
  const unset = state.keys.filter(({ key }) => !shown.includes(key));
  $("a-missing").replaceChildren(...unset.map(({ key, help }) => {
    const b = el("button", { title: help }, icon(KEY_ICONS[key] || "terminal", "xs"), key);
    b.onclick = () => { state.pendingKeys.add(key); syncForm(); $(`k-${key}`)?.focus(); };
    return b;
  }));
  // Lecture de l'autorun : libellé clair, clé technique en dessous
  const readRows = inFile.map((key) => el("div", { className: "frow kv" },
    el("span", {}, el("span", { textContent: KEY_LABELS[key] || key }), el("small", { textContent: key })),
    el("span", { className: "v mono", textContent: getKey(lines, key) ?? "" })));
  $("a-read").replaceChildren(...(readRows.length ? readRows : [el("div", { className: "frow" },
    el("span", { className: "k", textContent: "Autorun" }), el("span", { className: "v", textContent: "Aucune clé : l'image n'a pas d'autorun." }))]));
  markDirty();
  const unknown = inFile.filter((k) => !keyInfo(k));
  $("a-note").textContent = unknown.length
    ? `Clés non reconnues (conservées, ignorées par Batocera) : ${unknown.join(", ")}`
    : !state.autorun ? "" : state.autorun.found ? `${inFile.length} clé(s) définie(s)` : "Pas d'autorun dans l'image : il sera créé.";
}

export async function loadAutorun() {
  const id = state.current.id;
  $("a-issues").replaceChildren();
  $("a-result").replaceChildren();
  $("a-proposal").replaceChildren();
  try {
    const data = await api(`/api/image/autorun?id=${encodeURIComponent(id)}`);
    if (state.current?.id !== id) return;
    state.autorun = data;
    // L'éditeur montre l'autorun effectif : image + surcharge, ce que le lanceur applique
    $("a-editor").value = (data.effective ?? data.text).replace(/\r\n/g, "\n");
    renderOverrideBox(data);
    $("a-eol").value = data.eol === "\n" ? "lf" : "crlf";
  } catch (error) {
    state.autorun = null;
    $("a-editor").value = "";
    $("a-result").append(el("div", { className: "notice err" }, icon("alert"), el("span", { textContent: `Lecture impossible : ${error.message}` })));
  }
  state.baseline = $("a-editor").value;     // ce qui est affiché = état à partir duquel on détecte les changements
  syncForm();
}

function autorunBody() {
  const { lines, trailing } = editorLines();
  return {
    lines, trailing_eol: trailing || !state.autorun?.found,
    eol: $("a-eol").value === "crlf" ? "\r\n" : "\n",
    encoding: state.autorun?.encoding || "utf-8",
    base_sha: state.autorun?.sha ?? null,
    when: whenOf(),
  };
}

export const notice = (kind, text, iconName = kind === "err" ? "alert" : "info") =>
  el("div", { className: `notice ${kind}` }, icon(iconName), el("span", { textContent: text }));

function showIssues(list) {
  $("a-issues").replaceChildren(...list.map((x) => el("li", { className: x.level, textContent: x.message })));
}

function confidence(level) {
  return el("span", { className: `conf-${level}`, textContent: { high: "sûr", medium: "probable", low: "incertain" }[level] || level });
}

export const proposalItems = (items) => el("ul", { className: "items" }, ...items.map((i) => el("li", { className: i.written ? "" : "default" },
  el("code", { textContent: `${i.key}=${i.value}` }), " — ", i.justification, " (", confidence(i.confidence), ")",
  i.written ? "" : el("div", { className: "muted", textContent: "→ déjà le comportement par défaut du lanceur : rien à ajouter à l'autorun" }))));

function renderOverrideBox(data) {
  const box = $("a-override-box");
  box.replaceChildren();
  if (!data?.override) return;
  const commit = el("button", { className: "btn primary sm" }, icon("hammer", "sm"), "Inscrire dans l'image");
  const drop = el("button", { className: "btn secondary sm" }, icon("trash", "sm"), "Supprimer la surcharge");
  commit.onclick = () => commitOverrides([state.current.id]);
  drop.onclick = async () => {
    if (!confirm("Supprimer la surcharge ? Le jeu reprendra l'autorun de l'image.")) return;
    try {
      await api(`/api/image/override?id=${encodeURIComponent(state.current.id)}`, { method: "DELETE" });
      await loadLibrary();
      await loadAutorun();
    } catch (error) { alert(error.message); }
  };
  box.append(el("div", { className: "fbanner tall" }, icon("filepen"), el("div", { className: "body" },
    el("span", { textContent: `Surcharge active (${data.override.keys.join(", ")}) : appliquée par le lanceur, mais pas par Batocera tant qu'elle n'est pas inscrite dans l'image.` }),
    el("pre", { className: "bat", textContent: data.override.text }),
    el("div", { className: "row" }, commit, drop))));
}

export async function commitOverrides(ids) {
  const withOverride = ids.filter((id) => (state.byId.get(id)?.override || []).length);
  if (!withOverride.length) { alert("Aucune image sélectionnée n'a de surcharge."); return; }
  if (!confirm(`Inscrire la surcharge dans ${withOverride.length} image(s) ? Chaque image est reconstruite (original gardé en .old), puis sa surcharge supprimée.`)) return;
  try {
    const data = await api("/api/mass/override/commit", { method: "POST", body: { ids: withOverride, when: whenOf() } });
    reportSkipped(data.skipped);
    queued();
  } catch (error) { alert(error.message); }
}

export function init() {
  $("a-editor").addEventListener("input", syncForm);
  $("a-pick").onclick = () => { $("a-missing").hidden = !$("a-missing").hidden; };
  $("a-tabs").onclick = (event) => {
    const tab = event.target.closest("button[data-atab]")?.dataset.atab;
    if (!tab) return;
    for (const b of $("a-tabs").children) b.classList.toggle("on", b.dataset.atab === tab);
    for (const pane of document.querySelectorAll("[data-apane]")) pane.hidden = pane.dataset.apane !== tab;
  };
  $("a-propose").onclick = async () => {
    const id = state.current.id, box = $("a-proposal");
    box.replaceChildren(el("div", { className: "notice info" }, el("span", { className: "spinner" }), el("span", { textContent: "Analyse de l'image (lecture des exécutables)…" })));
    try {
      const r = await api(`/api/image/analyze?id=${encodeURIComponent(id)}`);
      if (state.current?.id !== id) return;
      // Ce que la proposition change par rapport à l'autorun en cours d'édition
      const norm = (key, v) => {
        let x = (v || "").trim();
        if (["CMD", "DIR"].includes(key)) x = x.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
        return x;
      };
      const { lines } = editorLines();
      const changes = [];
      for (const line of r.generated.text.replace(/\r\n/g, "\n").split("\n")) {
        const m = KEY_RE.exec(line);
        if (!m) continue;
        const before = getKey(lines, m[1]);
        if (before === null || norm(m[1], before) !== norm(m[1], m[2])) changes.push({ key: m[1], value: m[2], before });
      }
      const analysis = el("details", {}, el("summary", { textContent: "Voir l'analyse" }),
        el("div", { style: "margin-top:8px" }, proposalItems(r.generated.items)));
      const warnings = r.generated.warnings.length ? [el("ul", { className: "issues" }, ...r.generated.warnings.map((w) => el("li", { textContent: w })))] : [];
      if (!r.generated.text.trim()) {
        box.replaceChildren(notice("", "Rien à proposer : aucun exécutable Windows trouvé dans l'image.", "alert"), ...warnings);
        return;
      }
      if (!changes.length) {
        box.replaceChildren(el("div", { className: "proposal" },
          el("div", { className: "head" }, icon("check"), el("strong", { textContent: "Rien à proposer : l'autorun actuel correspond déjà à l'analyse." })),
          ...warnings, analysis));
        return;
      }
      const use = el("button", { className: "btn primary sm" }, icon("check", "sm"), changes.length > 1 ? `Appliquer ces ${changes.length} changements` : "Appliquer ce changement");
      use.onclick = () => {
        for (const c of changes) setKey(c.key, c.value);
        box.replaceChildren(notice("ok", "Changements placés dans le formulaire : vérifier, puis essayer ou reconstruire.", "check"));
      };
      box.replaceChildren(el("div", { className: "proposal" },
        el("div", { className: "head" }, icon("sparkles"), el("strong", { textContent: changes.length > 1 ? `${changes.length} changements proposés` : "1 changement proposé" }),
          r.engine ? el("span", { className: "chip", textContent: r.engine }) : null),
        el("div", { className: "diff" }, ...changes.map((c) => el("div", { className: "add",
          textContent: c.before === null ? `+ ${c.key}=${c.value}` : `~ ${c.key} : ${c.before} → ${c.value}` }))),
        ...warnings, analysis,
        el("div", { className: "row" }, use)));
    } catch (error) {
      box.replaceChildren(notice("err", error.message));
    }
  };
  $("a-check").onclick = async () => {
    try {
      const data = await api(`/api/image/autorun/preview?id=${encodeURIComponent(state.current.id)}`, { method: "POST", body: autorunBody() });
      showIssues(data.issues);
      $("a-result").replaceChildren(notice(data.issues.length ? "" : "ok",
        !data.changed ? "Aucun changement." : data.issues.length ? "Avertissements ci-dessus (non bloquants)." : "Aucun problème détecté.",
        data.issues.length ? "alert" : "check"));
    } catch (error) {
      $("a-result").replaceChildren(notice("err", error.message));
    }
  };
  $("a-save").onclick = async () => {
    if (!confirm("Reconstruire l'image avec cet autorun ? L'original sera conservé en .old jusqu'à validation.")) return;
    try {
      const data = await api(`/api/image/autorun?id=${encodeURIComponent(state.current.id)}`, { method: "POST", body: autorunBody() });
      const hadOverride = Boolean(state.autorun?.override);
      state.baseline = $("a-editor").value;    // enregistré : plus de changement en attente
      leaveEdit(true);
      showTab("autorun");
      const issues = data.issues.length ? [el("ul", { className: "issues" }, ...data.issues.map((x) => el("li", { className: x.level, textContent: x.message })))] : [];
      if (data.override_removed) {
        $("ar-result").replaceChildren(notice("ok", "Autorun identique à l'image : surcharge supprimée, pas de reconstruction.", "check"), ...issues);
        await loadLibrary();
        await loadAutorun();
        return;
      }
      $("ar-result").replaceChildren(notice("ok", hadOverride
        ? "Reconstruction ajoutée à la file : l'autorun effectif sera inscrit dans l'image, puis la surcharge supprimée."
        : "Reconstruction ajoutée à la file : l'image sera reconstruite avec cet autorun.", "check"), ...issues);
      queued();
    } catch (error) {
      $("a-result").replaceChildren(notice("err", error.message));
    }
  };
  $("a-override").onclick = async () => {
    try {
      const data = await api(`/api/image/override?id=${encodeURIComponent(state.current.id)}`, { method: "PUT", body: autorunBody() });
      leaveEdit(true);
      await loadLibrary();
      await loadAutorun();          // recharge l'autorun effectif
      showTab("autorun");
      $("ar-result").replaceChildren(notice("ok", data.keys.length
        ? `Surcharge écrite (${data.keys.join(", ")}) : effet au prochain lancement, sans reconstruction. Une fois validée, « Inscrire dans l'image ».`
        : "Identique à l'image : surcharge supprimée.", "zap"),
        ...(data.issues.length ? [el("ul", { className: "issues" }, ...data.issues.map((x) => el("li", { className: x.level, textContent: x.message })))] : []));
    } catch (error) {
      $("a-result").replaceChildren(notice("err", error.message));
    }
  };
}
