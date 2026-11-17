// detail/kb.js — Base de connaissances : réglages mémorisés par jeu.

import { api } from "../api.js";
import { $, el, state } from "../core.js";
import { KEY_RE, editorLines, notice } from "./autorun.js";

export function init() {
  $("a-kb").onclick = () => {
    const { lines } = editorLines();
    const autorun = Object.fromEntries(lines.map((l) => KEY_RE.exec(l)).filter(Boolean).map((m) => [m[1], m[2]]));
    const exe = (autorun.CMD || "").startsWith('"') ? autorun.CMD.split('"')[1] : (autorun.CMD || "").split(" ")[0];
    const dir = (autorun.DIR || "").replace(/^["']|["']$/g, "").replace(/\\/g, "/").replace(/\/+$/, "");
    $("kb-game").value = state.current.name;
    $("kb-prefer").textContent = exe ? [dir, exe].filter(Boolean).join("/") : "—";
    $("kb-prefer-on").checked = false;
    $("kb-prefer-on").disabled = !exe;
    const keys = Object.entries(autorun).filter(([k]) => !["CMD", "DIR", "GAME_VERSION"].includes(k));
    $("kb-keys").replaceChildren(...(keys.length ? keys.map(([k, v]) => {
      const box = el("input", { type: "checkbox", checked: true });
      box.dataset.key = k; box.dataset.value = v;
      return el("label", { className: "row" }, box, `${k}=${v}`);
    }) : [el("span", { className: "muted", textContent: "aucun réglage hors CMD/DIR/GAME_VERSION dans l'autorun" })]));
    $("kb-note").value = "";
    $("kb-err").hidden = true;
    $("dlg-kb").showModal();
  };
  $("kb-save").onclick = async () => {
    const keys = Object.fromEntries([...$("kb-keys").querySelectorAll("input:checked")].map((b) => [b.dataset.key, b.dataset.value]));
    const body = { game: $("kb-game").value.trim(), keys, note: $("kb-note").value.trim(),
      prefer: $("kb-prefer-on").checked ? $("kb-prefer").textContent : null };
    try {
      if (!body.game) throw new Error("Nom du jeu requis.");
      if (!Object.keys(keys).length && !body.prefer) throw new Error("Rien à mémoriser : cocher un réglage ou l'exécutable.");
      await api("/api/knowledge", { method: "POST", body });
      $("dlg-kb").close();
      $("a-result").replaceChildren(notice("ok", "Ajouté à la base : appliqué aux prochaines propositions pour ce jeu.", "bookmark"));
    } catch (error) {
      $("kb-err").textContent = error.message;
      $("kb-err").hidden = false;
    }
  };
}
