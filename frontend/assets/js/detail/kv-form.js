// detail/kv-form.js — Formulaire clé=valeur d'un autorun, adossé à un <textarea>.
// Utilisé par la fiche image (Lancement) et par la revue d'empaquetage des dossiers.
//
// Mêmes règles que le serveur (services/autorun.py) : clé en début de ligne,
// première occurrence retenue ; une clé modifiée garde sa place, une clé
// ajoutée va en fin de fichier ; vider un champ retire la clé.

import { el, helpLink, icon, state } from "../core.js";

export const KEY_RE = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/;

export const KEY_ICONS = { CMD: "terminal", DIR: "folder", GAME_VERSION: "tag", PRE: "play", ATTACH: "link", ATTACH_PROCESS: "cpu",
  WINE: "wine", PROTON: "atom", RUNNER: "drive", ARCH: "cpu", LANG: "book", ENV: "code", HIDRAW: "usb",
  SAVEDIR: "folder", SAVEFILES: "file", DXVK: "monitor", VKD3D: "monitor", D7VK: "monitor", VIRTUAL_DESKTOP: "grid",
  NVAPI: "cpu", VKREFLEX: "zap", FSR: "monitor", FSR4: "monitor", ESYNC: "zap", FSYNC: "zap" };

export const keyInfo = (key) => state.keys.find((k) => k.key === key);

export function textLines(text) {
  const raw = text.replace(/\r\n/g, "\n");
  const trailing = raw.endsWith("\n");
  const content = trailing ? raw.slice(0, -1) : raw;
  return { lines: content ? content.split("\n") : [], trailing };
}

export function getKey(lines, key) {
  for (const line of lines) {
    const m = KEY_RE.exec(line);
    if (m && m[1] === key) return m[2];
  }
  return null;
}

let seq = 0;

/**
 * Relie un formulaire au texte d'un <textarea> (source de vérité).
 * - form : conteneur des lignes clé/valeur ; missing : boutons des clés absentes ;
 * - pending : clés ajoutées au formulaire, pas encore renseignées ;
 * - onSync(lines) : appelé après chaque mise à jour (texte ou formulaire).
 * Les champs ont des id uniques par instance (plusieurs formulaires dans la page).
 */
export function kvForm({ textarea, form, missing, pending = new Set(), onSync = () => {} }) {
  const prefix = `k${seq++}-`;
  const inputOf = (key) => form.querySelector(`#${CSS.escape(prefix + key)}`);

  function setKey(key, value) {
    let { lines, trailing } = textLines(textarea.value);
    const index = lines.findIndex((line) => KEY_RE.exec(line)?.[1] === key);
    if (value === "") {
      lines = lines.filter((line) => KEY_RE.exec(line)?.[1] !== key);
    } else if (index >= 0) {
      lines[index] = `${key}=${value}`;
    } else {
      lines.push(`${key}=${value}`);
      if (lines.length === 1) trailing = true;
    }
    if (value !== "") pending.delete(key);
    textarea.value = lines.join("\n") + (trailing && lines.length ? "\n" : "");
    sync();
  }

  function row(key, value) {
    const info = keyInfo(key);
    const input = el("input", { id: prefix + key, value: value ?? "", placeholder: info ? "valeur" : "", autocomplete: "off", spellcheck: false });
    if (document.getElementById(`v-${key}`)) input.setAttribute("list", `v-${key}`);
    input.addEventListener("change", () => setKey(key, input.value.trim()));
    const remove = el("button", { type: "button", className: "iconbtn", title: "Retirer la clé", "aria-label": "Retirer la clé" }, icon("x", "sm"));
    remove.onclick = () => { pending.delete(key); setKey(key, ""); };
    const node = el("div", { className: "kv-pill", title: info?.help || "Clé non reconnue : conservée, ignorée par Batocera" },
      el("span", { className: "disc" }, icon(KEY_ICONS[key] || "terminal")),
      el("span", { className: "k" }, info ? helpLink(`hk-${key}`, key, `${key} dans l'aide`) : key),
      input, remove);
    node.dataset.key = key;
    return node;
  }

  function sync() {
    const { lines } = textLines(textarea.value);
    const inFile = [...new Set(lines.map((l) => KEY_RE.exec(l)?.[1]).filter(Boolean))];
    const shown = [...inFile, ...[...pending].filter((k) => !inFile.includes(k))];
    const current = [...form.children].map((r) => r.dataset.key);
    if (current.join("\n") === shown.join("\n")) {
      // Mêmes clés : seules les valeurs changent (le champ en cours de saisie est laissé tel quel)
      for (const key of shown) {
        const input = inputOf(key);
        if (input && document.activeElement !== input) input.value = getKey(lines, key) ?? "";
      }
    } else {
      const focused = document.activeElement;
      form.replaceChildren(...shown.map((key) => row(key, getKey(lines, key))));
      if (focused?.id?.startsWith(prefix)) inputOf(focused.id.slice(prefix.length))?.focus();
    }
    const unset = state.keys.filter(({ key }) => !shown.includes(key));
    missing.replaceChildren(...unset.map(({ key, help }) => {
      const b = el("button", { type: "button", title: help }, icon(KEY_ICONS[key] || "terminal", "xs"), key);
      b.onclick = () => { pending.add(key); sync(); inputOf(key)?.focus(); };
      return b;
    }));
    onSync(lines, inFile);
  }

  textarea.addEventListener("input", sync);
  return { setKey, sync, pending };
}

/**
 * Éditeur autonome (formulaire ⇄ texte brut) pour un texte donné :
 * renvoie { node, text(), setKey(), setText() } ; onChange(text) à chaque modification.
 */
export function autorunEditor(text, { onChange = () => {}, rows = 10 } = {}) {
  const textarea = el("textarea", { value: text.replace(/\r\n/g, "\n"), rows, spellcheck: false });
  const form = el("div", { className: "kv-list" });
  const missing = el("div", { className: "unset", hidden: true });
  const pick = el("button", { type: "button", className: "addkey" }, icon("plus", "sm"), el("span", { textContent: "Ajouter une clé" }));
  pick.onclick = () => { missing.hidden = !missing.hidden; };
  const formPane = el("div", {}, form, pick, missing);
  const rawPane = el("div", { hidden: true }, textarea);
  const tab = (name, iconName, title) => {
    const b = el("button", { type: "button", className: name === "form" ? "on" : "", title, ariaLabel: title }, icon(iconName, "sm"));
    b.onclick = () => {
      for (const x of tabs.children) x.classList.toggle("on", x === b);
      formPane.hidden = name !== "form";
      rawPane.hidden = name === "form";
    };
    return b;
  };
  const tabs = el("div", { className: "pills icons on-s2" });
  tabs.append(tab("form", "list", "Formulaire"), tab("raw", "code", "Texte brut"));
  const note = el("p", { className: "muted fine" });
  const editor = kvForm({ textarea, form, missing, onSync: (lines, inFile) => {
    const unknown = inFile.filter((k) => !keyInfo(k));
    note.textContent = unknown.length ? `Clés non reconnues (conservées, ignorées par Batocera) : ${unknown.join(", ")}`
      : `${inFile.length} clé(s) définie(s)`;
    onChange(textarea.value);
  } });
  const node = el("div", { className: "ar-editor" }, el("div", { className: "ar-top" }, note, tabs), formPane, rawPane);
  editor.sync();
  return {
    node,
    text: () => textarea.value,
    setKey: editor.setKey,
    setText(value) { textarea.value = value.replace(/\r\n/g, "\n"); editor.pending.clear(); editor.sync(); },
  };
}
