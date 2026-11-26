// settings-dirs.js — Réglages › Dossiers synchronisés : dossiers déclarés sur les clients (agent v8),
// avec le chemin de chacun ; déclaration ou modification sur les clients cochés, retrait.

import { api } from "./api.js";
import { $, el, icon, toast } from "./core.js";
import { connected } from "./mode.js";
import { loadSaves, osIcon, sv } from "./saves.js";

let clients = [];             // {id, name, ok, dirs: [{name, path}], message} (GET /api/saves/dirs)
let loading = false;
let editing = null;           // nom du dossier modifié ; "" : nouveau ; null : formulaire fermé

const homePath = (p) => (p.startsWith("/") ? p : `~/${p}`);
const relPath = (p) => p.trim().replace(/^~\//, "");
const iconOf = (id) => sv.machines.find((m) => m.id === id)?.icon;

// Noms déclarés sur au moins un client, triés
function folderNames() {
  return [...new Set(clients.flatMap((c) => c.dirs.map((d) => d.name)))].sort((a, b) => a.localeCompare(b, "fr"));
}

export async function loadSettingsDirs() {
  const list = $("st-sd-list");
  if (!connected()) {
    list.replaceChildren(el("p", { className: "st-empty muted", textContent: "Disponible une fois le mode connecté enregistré." }));
    $("st-sd-new").hidden = true;
    closeForm();
    return;
  }
  $("st-sd-new").hidden = false;
  if (loading) return;
  loading = true;
  if (!clients.length) list.replaceChildren(el("p", { className: "st-empty muted", textContent: "Lecture des clients…" }));
  try {
    clients = (await api("/api/saves/dirs", { timeout: 60000 })).machines;
  } catch (error) {
    list.replaceChildren(el("p", { className: "st-empty muted", textContent: `Lecture impossible : ${error.message}` }));
    return;
  } finally {
    loading = false;
  }
  paintList();
  if (editing !== null) paintTargets();
}

function paintList() {
  const names = folderNames();
  if (!names.length) {
    $("st-sd-list").replaceChildren(el("p", { className: "st-empty muted", textContent: "Aucun dossier déclaré." }));
    return;
  }
  $("st-sd-list").replaceChildren(...names.map(folderRow));
}

// Un dossier : son chemin sur chaque client ; un chemin différent des autres est signalé
function folderRow(name) {
  const paths = clients.filter((c) => c.ok).map((c) => [c, c.dirs.find((d) => d.name === name)?.path ?? null]);
  const declared = paths.filter(([, p]) => p !== null).map(([, p]) => p);
  const counts = new Map();
  for (const p of declared) counts.set(p, (counts.get(p) || 0) + 1);
  const usual = [...counts].sort((a, b) => b[1] - a[1])[0]?.[0];
  const lines = paths.map(([c, p]) => el("li", { className: p === null ? "none" : p !== usual ? "diff" : "" },
    osIcon(iconOf(c.id), "sm"), el("b", { textContent: c.name }),
    el("code", { textContent: p === null ? "non déclaré" : homePath(p) })));
  for (const c of clients.filter((x) => !x.ok)) {
    lines.push(el("li", { className: "none" }, osIcon(iconOf(c.id), "sm"), el("b", { textContent: c.name }),
      el("span", { textContent: "injoignable" })));
  }
  const edit = el("button", { type: "button", className: "btn secondary sm" }, icon("pen", "sm"), "Modifier");
  edit.onclick = () => openForm(name, usual ?? "");
  const del = el("button", { type: "button", className: "iconbtn bare sm", title: "Retirer la déclaration sur tous les clients (les fichiers restent)",
    "aria-label": `Retirer ${name}` }, icon("trash", "sm"));
  del.onclick = () => removeFolder(name);
  return el("div", { className: "st-row st-sd-row" },
    el("div", { className: "st-txt" }, el("b", { textContent: name }), el("ul", { className: "st-sd-paths" }, ...lines)),
    el("div", { className: "st-sd-actions" }, edit, del));
}

function openForm(name, path) {
  editing = name;
  $("st-sd-name").value = name;
  $("st-sd-name").disabled = Boolean(name);      // renommer = retirer puis déclarer sous un autre nom
  $("st-sd-path").value = path;
  $("st-sd-err").hidden = true;
  $("st-sd-form-title").textContent = name ? `Modifier « ${name} »` : "Nouveau dossier";
  $("st-sd-form").hidden = false;
  paintTargets(true);
  (name ? $("st-sd-path") : $("st-sd-name")).focus();
}

function closeForm() {
  editing = null;
  $("st-sd-form").hidden = true;
}

// Clients à cocher. Modification : ceux qui déclarent ce dossier sont cochés ; nouveau dossier :
// tous les clients joignables. Les coches changées à la main restent quand le chemin change.
function paintTargets(reset = false) {
  const name = $("st-sd-name").value.trim();
  const path = relPath($("st-sd-path").value);
  const kept = reset ? new Map() : new Map([...$("st-sd-targets").querySelectorAll("input")].map((b) => [b.dataset.id, b.checked]));
  $("st-sd-targets").replaceChildren(...clients.map((c) => {
    const known = c.dirs.find((d) => d.name === name)?.path;
    const detail = !c.ok ? (c.message || "injoignable")
      : known === undefined ? "non déclaré" : known === path ? "déjà ce chemin" : `actuellement : ${homePath(known)}`;
    const box = el("input", { type: "checkbox", disabled: !c.ok,
      checked: c.ok && (kept.get(c.id) ?? (editing ? known !== undefined : true)) });
    box.dataset.id = c.id;
    return el("label", { className: "sd-row" }, box, osIcon(iconOf(c.id), "sm"),
      el("span", {}, el("b", { textContent: c.name }),
        el("small", { className: known !== undefined && known !== path && c.ok ? "diff" : "", textContent: detail })));
  }));
}

async function saveForm() {
  const name = $("st-sd-name").value.trim(), path = $("st-sd-path").value.trim();
  const ids = [...$("st-sd-targets").querySelectorAll("input:checked")].map((b) => b.dataset.id);
  const fail = (text) => { $("st-sd-err").textContent = text; $("st-sd-err").hidden = false; };
  $("st-sd-err").hidden = true;
  if (!name || !path) return fail("Nom et chemin requis.");
  if (!ids.length) return fail("Cocher au moins un client.");
  const button = $("st-sd-save");
  button.disabled = true;
  try {
    // Le premier client coché retient le chemin ; les autres le reçoivent à partir de leur HOME
    const [first, ...also] = ids;
    const reply = await api(`/api/saves/machines/${encodeURIComponent(first)}/dirs`, {
      method: "PUT", body: { name, path, also }, timeout: 120000 });
    const failed = (reply.others || []).filter((o) => !o.ok);
    await loadSettingsDirs();
    loadSaves();
    if (failed.length) {
      paintTargets(true);
      return fail(`Non déclaré sur : ${failed.map((o) => `${o.name} (${o.message})`).join(" ; ")}`);
    }
    toast(`« ${name} » déclaré sur ${ids.length} client${ids.length > 1 ? "s" : ""}`);
    closeForm();
  } catch (error) {
    fail(error.message);
  } finally {
    button.disabled = false;
  }
}

async function removeFolder(name) {
  const on = clients.filter((c) => c.ok && c.dirs.some((d) => d.name === name));
  if (!on.length) return;
  if (!confirm(`Retirer « ${name} » de ${on.map((c) => c.name).join(", ")} ? Les fichiers restent sur les clients et sur le manager.`)) return;
  const errors = [];
  for (const c of on) {
    try {
      await api(`/api/saves/machines/${encodeURIComponent(c.id)}/dirs?name=${encodeURIComponent(name)}`, { method: "DELETE" });
    } catch (error) { errors.push(`${c.name} : ${error.message}`); }
  }
  if (errors.length) toast(errors.join(" ; "), "alert", true);
  else toast(`« ${name} » retiré`);
  if (editing === name) closeForm();
  await loadSettingsDirs();
  loadSaves();
}

export function init() {
  $("st-sd-new").onclick = () => openForm("", "");
  $("st-sd-cancel").onclick = closeForm;
  $("st-sd-save").onclick = saveForm;
  $("st-sd-name").addEventListener("input", () => paintTargets());
  $("st-sd-path").addEventListener("input", () => paintTargets());
  $("st-sd-path").addEventListener("keydown", (event) => { if (event.key === "Enter") saveForm(); });
}
