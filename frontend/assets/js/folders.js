// folders.js — Dossiers à empaqueter : liste, sélection, analyse et empaquetage.

import { reportSkipped } from "./actions.js";
import { api } from "./api.js";
import { thumb } from "./art.js";
import { $, badge, cap, dotIc, el, emptyState, helpLink, icon, size, storage } from "./core.js";
import { notice, proposalItems } from "./detail/autorun.js";
import { todoChip } from "./library.js";
import { queued } from "./tasks.js";
import { whenOf } from "./when.js";

export const folders = { items: [], selected: new Set(), state: "", system: "",
  layout: "list", sort: { key: "folder", dir: 1 } };

export async function loadFolders() {
  try {
    folders.items = await api("/api/folders");
  } catch (error) {
    $("g-rows").replaceChildren(notice("err", error.message));
    return;
  }
  $("count-folders").textContent = folders.items.filter((f) => !f.image_exists).length || "";
  const ids = new Set(folders.items.map((f) => f.id));
  for (const id of [...folders.selected]) if (!ids.has(id)) folders.selected.delete(id);
  renderFolders();
}

function visibleFolders() {
  const q = $("g-search").value.trim().toLowerCase(), { system, state: st } = folders;
  return folders.items.filter((f) => (!q || f.folder.toLowerCase().includes(q)) && (!system || f.system === system)
    && (st !== "todo" || !f.image_exists) && (st !== "done" || f.image_exists)
    && (st !== "noautorun" || !f.has_autorun))
    .sort((a, b) => a.folder.localeCompare(b.folder, "fr", { numeric: true, sensitivity: "base" }));
}

// État d'un dossier : libellé et classe de pastille (.state), rang pour le tri
function folderStatus(f) {
  if (f.task) return f.task === "running" ? ["empaquetage en cours", "in_use", 0] : ["en file", "in_use", 1];
  return f.image_exists ? ["déjà empaqueté", "ok", 3] : ["à traiter", "modified", 2];
}

function folderProfile(f) {
  return el("div", { className: "profile" },
    f.task ? dotIc("clock", "t-accent", f.task === "running" ? "Empaquetage en cours" : "Empaquetage en file") : null,
    dotIc("terminal", f.has_autorun ? "t-sage" : "t-accent", f.has_autorun ? "autorun.cmd présent" : "Sans autorun : il sera proposé"),
    dotIc("layers", f.image_exists ? "t-sage" : "t-off", f.image_exists ? "Image .wsquashfs déjà présente" : "Pas encore d'image"));
}

function toggleFolder(f) {
  folders.selected.has(f.id) ? folders.selected.delete(f.id) : folders.selected.add(f.id);
  renderFolders();
}

// Case de sélection d'un dossier (liste et tuiles), comme selectBox pour les images
function folderBox(f) {
  const box = el("input", { type: "checkbox", checked: folders.selected.has(f.id), title: "Sélectionner" });
  box.onclick = (event) => event.stopPropagation();
  box.onchange = () => toggleFolder(f);
  return box;
}

// Pastilles « À traiter » des dossiers (filtres) : [état, libellé, icône, ton, test]
const FOLDER_TODO = [
  ["todo", "à traiter", "package", "accent", (f) => !f.image_exists],
  ["noautorun", "sans autorun", "terminal", "accent", (f) => !f.has_autorun],
  ["done", "déjà empaquetés", "check", "", (f) => f.image_exists],
];

function renderFolderTodo() {
  const chips = FOLDER_TODO.map(([id, label, iconName, tone, test]) => ({ id, label, iconName, tone, n: folders.items.filter(test).length }))
    .filter((c) => c.n)
    .map((c) => todoChip(c.n, c.label, c.iconName, c.tone, folders.state === c.id, () => {
      folders.state = folders.state === c.id ? "" : c.id;
      renderFolders();
    }));
  $("g-todo").replaceChildren(...chips);
  $("g-todo-label").hidden = !chips.length;
  const active = FOLDER_TODO.find(([id]) => id === folders.state);
  $("g-clear-filter").hidden = !active;
  $("g-clear-filter-label").textContent = active ? cap(active[1]) : "";
}

function renderFolderList(visible) {
  const { key, dir } = folders.sort;
  const text = (f) => key === "system" ? `${f.system || ""} ${f.folder}` : f.folder;
  const rows = [...visible].sort((a, b) => (key === "state" ? folderStatus(a)[2] - folderStatus(b)[2] : 0) * dir
    || text(a).localeCompare(text(b), "fr", { numeric: true, sensitivity: "base" }) * (key === "state" ? 1 : dir));
  const all = el("input", { type: "checkbox", checked: rows.length > 0 && rows.every((f) => folders.selected.has(f.id)),
    title: "Tout sélectionner (filtre actuel)" });
  all.onchange = () => { rows.forEach((f) => all.checked ? folders.selected.add(f.id) : folders.selected.delete(f.id)); renderFolders(); };
  const sortBtn = (k, label, cls = "") => {
    const b = el("button", { className: cls, textContent: label + (key === k ? (dir > 0 ? " ↑" : " ↓") : "") });
    b.onclick = () => { folders.sort = { key: k, dir: key === k ? -dir : 1 }; renderFolders(); };
    return b;
  };
  $("g-thead").replaceChildren(all, sortBtn("folder", "Dossier"), sortBtn("system", "Système", "c-system"),
    el("span", { className: "c-profile", textContent: "Contenu", title: "autorun.cmd, image .wsquashfs" }), sortBtn("state", "État", "c-state"));
  $("g-list-rows").replaceChildren(...rows.map((f) => {
    const selected = folders.selected.has(f.id), [label, cls] = folderStatus(f);
    const row = el("div", { className: `grow item${selected ? " selected" : ""}` },
      folderBox(f),
      el("div", { className: "iname" }, thumb(f, "thumb", "folder"),
        el("div", { className: "t" }, el("span", { className: "n", textContent: f.folder, title: f.folder }),
          el("span", { className: "sub", textContent: `.${f.kind}` }))),
      el("span", { className: "c-system", textContent: cap(f.system) || "—" }),
      folderProfile(f),
      el("span", { className: `state ${cls} c-state`, textContent: label, title: label }));
    row.onclick = () => toggleFolder(f);
    return row;
  }));
  if (!rows.length) $("g-list-rows").append(folders.items.length
    ? emptyState("search", "Aucun dossier pour ces filtres", null)
    : emptyState("folder", "Aucun dossier à traiter", "Les dossiers *.pc et *.wine des systèmes choisis dans « Réglages » apparaissent ici."));
}

function renderFolders() {
  const systems = [...new Set(folders.items.map((f) => f.system).filter(Boolean))].sort();
  if (folders.system && !systems.includes(folders.system)) folders.system = "";
  $("g-system").hidden = systems.length < 2;
  $("g-system").replaceChildren(...[["", "Tous"], ...systems.map((s) => [s, cap(s)])].map(([value, label]) => {
    const b = el("button", { className: folders.system === value ? "on" : "", textContent: label });
    b.onclick = () => { folders.system = value; renderFolders(); };
    return b;
  }));
  renderFolderTodo();
  const visible = visibleFolders(), list = folders.layout === "list", total = folders.items.length;
  $("g-title").textContent = `${total} dossier${total > 1 ? "s" : ""}`;
  $("g-visible").textContent = `${visible.length} sur ${total}`;
  for (const b of $("g-layout").children) b.classList.toggle("on", b.dataset.layout === folders.layout);
  $("g-list").hidden = !list;
  $("g-rows").hidden = list;
  $("g-all-label").hidden = list;              // en liste : case de l'en-tête
  if (list) {
    $("g-rows").replaceChildren();
    renderFolderList(visible);
    renderFolderSelection();
    return;
  }
  $("g-list-rows").replaceChildren();
  $("g-all").checked = visible.length > 0 && visible.every((f) => folders.selected.has(f.id));
  // Tuiles comme la bibliothèque (jaquette = illustration du dossier, sinon initiales)
  $("g-rows").replaceChildren(...visible.map((f) => {
    const selected = folders.selected.has(f.id), [label, cls] = folderStatus(f);
    const card = el("div", { className: `gcard${selected ? " selected" : ""}`, tabIndex: 0, role: "checkbox" },
      el("div", { className: "gart" }, thumb(f, "gcover", "folder"), folderBox(f),
        el("span", { className: `state ${cls}`, textContent: label, title: label }), folderProfile(f)),
      el("div", { className: "meta" }, el("div", { className: "n", textContent: f.folder, title: f.folder }),
        el("div", { className: "sub" }, el("span", { textContent: cap(f.system) || "—" }), el("span", { textContent: `.${f.kind}` }))));
    card.dataset.id = f.id;
    card.setAttribute("aria-checked", selected);
    const toggle = () => toggleFolder(f);
    card.onclick = toggle;
    card.onkeydown = (event) => { if (event.key === " " || event.key === "Enter") { event.preventDefault(); toggle(); } };
    return card;
  }));
  if (!visible.length) $("g-rows").append(el("div", { className: "fcard-empty" }, folders.items.length
    ? emptyState("search", "Aucun dossier pour ces filtres", null)
    : emptyState("folder", "Aucun dossier à traiter", "Les dossiers *.pc et *.wine des systèmes choisis dans « Réglages » apparaissent ici.")));
  renderFolderSelection();
}

function renderFolderSelection() {
  $("g-selection").hidden = folders.selected.size === 0;
  $("g-count").textContent = folders.selected.size > 1 ? `${folders.selected.size} dossiers` : "1 dossier";
}

// Revue : une carte par dossier, analysée l'une après l'autre
const review = { cards: [] };

function renderCard(card) {
  const r = card.report, node = card.node;
  const include = el("input", { type: "checkbox", checked: card.include, disabled: card.sent, title: "Inclure ce dossier" });
  include.onchange = () => { card.include = include.checked; node.classList.toggle("off", !card.include); updatePackButton(); };
  const head = el("div", { className: "head" }, include, el("strong", { textContent: card.folder.folder }),
    badge(r.prefix === "none" ? "jeu seul" : `prefix ${r.prefix}${r.arch ? " " + r.arch : ""}`, r.prefix === "none" ? "" : "violet"),
    r.engine ? badge(r.engine, "info") : null,
    el("span", { className: "muted", style: "font-size:12.5px", textContent: `${size(r.size)} · ${r.file_count} fichiers` }),
    card.folder.image_exists ? badge("image déjà présente", "err") : null,
    card.sent ? badge("envoyé dans la file", "ok") : null);
  node.replaceChildren(head);
  node.classList.toggle("off", !card.include);

  const choice = el("div", { className: "row" });
  const radio = (value, label, disabled = false) => {
    const input = el("input", { type: "radio", name: `src-${card.folder.id}`, value, checked: card.source === value, disabled });
    input.onchange = () => { card.source = value; renderCard(card); updatePackButton(); };
    return el("label", {}, input, label);
  };
  choice.append(radio("existing", "Autorun existant", !r.existing), radio("generated", "Autorun proposé"),
    el("span", { className: "muted", style: "font-size:12.5px", textContent: `recommandé : ${r.recommended === "existing" ? "existant" : "proposé"}` }));
  node.append(choice);

  const left = el("div"), right = el("div");
  if (card.source === "existing" && r.existing) {
    left.append(el("pre", { className: "bat", textContent: r.existing.text }));
    if (r.existing.issues.length) left.append(el("ul", { className: "issues" }, ...r.existing.issues.map((i) => el("li", { className: i.level, textContent: i.message }))));
  } else {
    const select = el("select", {}, ...r.candidates.map((c) => new Option(`${c.path}  (score ${c.score}${c.bits ? `, ${c.bits} bits` : ""}${c.version ? `, v${c.version}` : ""})`, c.path)));
    select.value = r.chosen || "";
    select.onchange = () => analyzeCard(card, select.value);
    const text = el("textarea", { value: card.text, spellcheck: false });
    text.oninput = () => { card.text = text.value; updatePackButton(); };
    left.append(r.candidates.length ? el("label", { className: "muted" }, "Exécutable ", select) : notice("err", "Aucun exécutable Windows : jeu absent ou version Linux ?"), text);
    const issues = r.generated.issues;     // validation de la proposition d'origine
    if (issues.length) left.append(el("ul", { className: "issues" }, ...issues.map((i) => el("li", { className: i.level, textContent: i.message }))));
  }
  right.append(proposalItems(r.generated.items));
  if (r.generated.warnings.length) right.append(el("ul", { className: "issues" }, ...r.generated.warnings.map((w) => el("li", { textContent: w }))));
  for (const bat of r.bats) {
    right.append(el("div", { className: "muted mono", style: "font-size:12px", textContent: bat.path }), el("pre", { className: "bat", textContent: bat.content }));
    if (bat.suspicious.length) right.append(el("ul", { className: "issues" }, ...bat.suspicious.map((x) => el("li", { textContent: x }))));
  }
  if (r.excluded.length) right.append(el("details", {}, el("summary", { textContent: `${r.excluded.length} exécutable(s) écarté(s)` }),
    el("ul", { className: "items", style: "margin-top:6px" }, ...r.excluded.map((e) => el("li", { textContent: `${e.path} — ${e.reason}` })))));
  node.append(el("div", { className: "cols" }, left, right));
  if (r.link_issues?.length) node.append(el("ul", { className: "issues" },
    ...r.link_issues.map((i) => el("li", { className: i.level }, i.message, " ", helpLink("h-runner")))));
}

async function analyzeCard(card, exe = null) {
  card.node.replaceChildren(el("div", { className: "head" }, el("span", { className: "spinner" }), el("strong", { textContent: card.folder.folder }), el("span", { className: "muted", textContent: "analyse…" })));
  try {
    const r = await api(`/api/folders/analyze?id=${encodeURIComponent(card.folder.id)}${exe ? `&exe=${encodeURIComponent(exe)}` : ""}`);
    card.report = r;
    card.text = r.generated.text;
    if (!exe) card.source = r.recommended;
    card.include = !card.folder.image_exists && !card.folder.task && (card.source === "existing" || /^CMD=/m.test(card.text));
    renderCard(card);
  } catch (error) {
    card.include = false;
    card.node.replaceChildren(notice("err", `${card.folder.folder} : ${error.message}`));
  }
  updatePackButton();
}

function updatePackButton() {
  const ready = review.cards.filter((c) => c.include && !c.sent && c.report && (c.source === "existing" || /^CMD=/m.test(c.text)));
  $("p-run").disabled = ready.length === 0;
  $("p-run").replaceChildren(icon("package", "sm"), `Empaqueter ${ready.length} dossier(s)`);
  return ready;
}

export function init() {
  folders.layout = storage.get("wsfs-folder-layout") === "grid" ? "grid" : "list";
  $("g-search").addEventListener("input", renderFolders);
  $("g-layout").onclick = (event) => {
    const b = event.target.closest("button[data-layout]");
    if (!b) return;
    folders.layout = b.dataset.layout;
    storage.set("wsfs-folder-layout", folders.layout);
    renderFolders();
  };
  $("g-clear-filter").onclick = () => { folders.state = ""; renderFolders(); };
  $("g-all").onchange = () => { visibleFolders().forEach((f) => $("g-all").checked ? folders.selected.add(f.id) : folders.selected.delete(f.id)); renderFolders(); };
  $("g-clear").onclick = () => { folders.selected.clear(); renderFolders(); };
  $("g-delete").onclick = async () => {
    const ids = folders.items.filter((f) => folders.selected.has(f.id) && f.image_exists).map((f) => f.id);
    if (!ids.length) { alert("Aucun dossier sélectionné n'a encore son image .wsquashfs."); return; }
    if (!confirm(`Supprimer DÉFINITIVEMENT ${ids.length} dossier(s) déjà empaqueté(s) ? L'image de chacun est vérifiée avant.`)) return;
    try {
      const data = await api("/api/folders/delete", { method: "POST", body: { ids } });
      reportSkipped(data.skipped);
    } catch (error) { alert(error.message); }
    loadFolders();
  };
  $("g-review").onclick = async () => {
    const selected = folders.items.filter((f) => folders.selected.has(f.id));
    $("p-count").textContent = selected.length;
    $("p-result").replaceChildren();
    review.cards = selected.map((folder) => ({ folder, node: el("div", { className: "pcard" }), include: false, source: "generated", text: "" }));
    $("p-cards").replaceChildren(...review.cards.map((c) => c.node));
    updatePackButton();
    $("dlg-pack").showModal();
    for (const [index, card] of review.cards.entries()) {
      if (!$("dlg-pack").open) break;
      $("p-progress").textContent = `analyse ${index + 1}/${review.cards.length}…`;
      await analyzeCard(card);
    }
    $("p-progress").textContent = "";
  };
  $("p-delete").onchange = () => {
    if ($("p-delete").checked && !confirm("Supprimer chaque dossier une fois son image créée et vérifiée ? Sans retour possible.")) $("p-delete").checked = false;
  };
  $("p-run").onclick = async () => {
    // autorun null : celui du dossier est gardé tel quel
    const cards = updatePackButton();
    const items = cards.map((c) => ({ id: c.folder.id, autorun: c.source === "existing" ? null : c.text }));
    try {
      const data = await api("/api/folders/pack", { method: "POST", body: { items, rename_wine: $("p-rename").checked, delete_source: $("p-delete").checked, when: whenOf() } });
      const skipped = data.skipped.map((s) => el("li", { textContent: `${s.id} : ${s.reason}` }));
      // Un dossier envoyé est renommé (.pc → .wine) : ne plus le renvoyer
      const refused = new Set(data.skipped.map((s) => s.id));
      for (const card of cards) {
        if (!refused.has(card.folder.id)) { card.sent = true; card.include = false; renderCard(card); }
      }
      updatePackButton();
      $("p-result").replaceChildren(notice("ok", `${data.tasks.length} empaquetage(s) ajouté(s) à la file.`, "check"),
        ...(skipped.length ? [el("ul", { className: "issues" }, ...skipped)] : []));
      queued();
      loadFolders();
    } catch (error) {
      $("p-result").replaceChildren(notice("err", error.message));
    }
  };
}
