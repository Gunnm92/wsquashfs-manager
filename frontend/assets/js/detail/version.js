// detail/version.js — Montée de version : mise à jour d'une image depuis un dossier source.

import { api } from "../api.js";
import { $, el, size, state } from "../core.js";
import { notice } from "./autorun.js";
import { markDirty, resetVersionForm } from "./detail.js";
import { queued } from "../tasks.js";
import { whenOf } from "../when.js";

export async function loadUpdateDefaults() {
  const id = state.current.id;
  $("u-result").replaceChildren();
  $("u-run").disabled = true;
  try {
    const d = await api(`/api/image/update?id=${encodeURIComponent(id)}`);
    if (state.current?.id !== id) return;
    if (!$("u-anchor").dataset.edited) $("u-anchor").value = d.anchor;
    $("u-version").placeholder = d.version ? `actuelle : ${d.version} — proposée d'après l'exécutable` : "proposée d'après l'exécutable";
  } catch (error) {
    $("u-result").replaceChildren(notice("err", error.message));
  }
}

const updateBody = () => ({ source: $("u-source").value.trim(), anchor: $("u-anchor").value.trim(),
  delete_missing: $("u-delete").checked, clean_saves: $("u-saves").checked,
  version: $("u-version").value.trim() || null, when: whenOf() });

function fileList(title, items, render) {
  if (!items.length) return null;
  return el("details", {}, el("summary", { textContent: title }),
    el("div", { className: "filelist", textContent: items.map(render).join("\n") }));
}

export function init() {
  for (const id of ["u-source", "u-anchor", "u-version"]) $(id).addEventListener("input", () => { $("u-run").disabled = true; });
  $("u-delete").addEventListener("change", () => { $("u-run").disabled = true; });
  $("u-anchor").addEventListener("input", () => { $("u-anchor").dataset.edited = "1"; });
  $("u-preview").onclick = async () => {
    $("u-result").replaceChildren(el("div", { className: "notice info" }, el("span", { className: "spinner" }), el("span", { textContent: "Comparaison de la source et de l'image…" })));
    try {
      const r = await api(`/api/image/update/preview?id=${encodeURIComponent(state.current.id)}`, { method: "POST", body: updateBody() });
      if (r.version && !$("u-version").value) $("u-version").value = r.version;
      const more = (n, shown) => n > shown ? ` (${shown} affichés)` : "";
      $("u-result").replaceChildren(
        el("h3", { textContent: r.changed ? `Dans ${r.anchor || "la racine"} : ${r.added_count} ajouté(s), ${r.replaced_count} remplacé(s), ${r.deleted_count} supprimé(s), ${r.same} identique(s)` : "Rien à mettre à jour" }),
        el("dl", {}, el("dt", { textContent: "Version" }), el("dd", { textContent: `${r.version_before || "—"} → ${r.version || "inchangée"}${r.version_hint ? ` (${r.version_hint})` : ""}` }),
          el("dt", { textContent: "À copier" }), el("dd", { textContent: size(r.copy_size) }),
          el("dt", { textContent: "Disque" }), el("dd", { textContent: `${size(r.space.needed)} nécessaires (ancienne image gardée en .old), ${size(r.space.free)} libres` })),
        ...[fileList(`Ajoutés${more(r.added_count, r.added.length)}`, r.added, (a) => `+ ${a.path}  (${size(a.size)})`),
          fileList(`Remplacés${more(r.replaced_count, r.replaced.length)}`, r.replaced, (x) => `~ ${x.path}  (${size(x.before)} → ${size(x.after)})`),
          fileList(`Supprimés${more(r.deleted_count, r.deleted.length)}`, r.deleted, (d) => `- ${d}`),
          fileList(`Masqués par les sauvegardes${more(r.masked_count, r.masked.length)}`, r.masked, (m) => `! ${m}`)].filter(Boolean),
        ...(r.warnings.length ? [el("ul", { className: "issues" }, ...r.warnings.map((w) => el("li", { textContent: w })))] : []),
        ...(r.blocked ? [notice("", r.blocked, "alert")] : []));
      $("u-run").disabled = !r.changed || Boolean(r.blocked);
    } catch (error) {
      $("u-result").replaceChildren(notice("err", error.message));
    }
  };
  $("u-run").onclick = async () => {
    if (!confirm("Mettre à jour l'image ? L'original sera conservé en .old jusqu'à validation.")) return;
    try {
      await api(`/api/image/update?id=${encodeURIComponent(state.current.id)}`, { method: "POST", body: updateBody() });
      $("u-run").disabled = true;
      resetVersionForm();
      markDirty();
      queued("Mise à jour ajoutée à la file de tâches");
    } catch (error) {
      $("u-result").replaceChildren(notice("err", error.message));
    }
  };
}
