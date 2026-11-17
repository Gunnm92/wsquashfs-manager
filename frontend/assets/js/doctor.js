// doctor.js — État du lanceur (--doctor) : page et réparation des runners.

import { api } from "./api.js";
import { $, el, icon, state } from "./core.js";
import { notice } from "./detail/autorun.js";
import { openHelp } from "./help.js";
import { hostName } from "./mode.js";

// Diagnostic du client d'essai : fenêtre (#dlg-doctor) ou page Réglages (#st-doctor, inPage)
const doctorAge = (d) => d.age ? `il y a ${Math.round(d.age / 60)} min` : "à l'instant";

const DOCTOR_MARKS = { ok: "check", missing: "x", optional: "minus" };
const DOCTOR_LABELS = { ok: "OK", missing: "À corriger", optional: "Facultatif" };

// Rapport tolérant : un champ absent (section sans items, item sans details) ne fait pas planter l'affichage
const sectionItems = (section) => (Array.isArray(section?.items) ? section.items : []).filter((i) => i && typeof i === "object");
const itemsOf = (d) => (d?.sections || []).flatMap(sectionItems);
const detailsOf = (item) => (Array.isArray(item.details) ? item.details : [item.details]).filter(Boolean);

function doctorRow(item) {
  const details = detailsOf(item);
  return el("div", { className: `item ${item.status}` },
    el("span", { className: "disc" }, icon(DOCTOR_MARKS[item.status] || "info")),
    el("div", {}, el("span", { textContent: item.text || "" }),
      details.length ? el("span", { className: "details", textContent: details.join("\n") }) : null));
}

export async function loadDoctor(refresh = false, inPage = false) {
  const body = inPage ? $("st-doctor") : $("doc-body");
  const button = inPage ? $("st-doctor-refresh") : $("doc-refresh");
  if (refresh || !state.doctor) body.replaceChildren(el("div", { className: "row muted" }, el("span", { className: "spinner" }), `vérification sur ${hostName()}…`));
  button.disabled = true;
  try {
    state.doctor = await api(`/api/launcher/doctor${refresh ? "?refresh=1" : ""}`);
    if (inPage) renderDoctorPage(); else renderDoctor();
    runnerSuggestions();
  } catch (error) {
    const failure = notice("", `État du lanceur indisponible : ${error.message}`, "alert");
    body.replaceChildren(inPage ? el("div", { className: "st-card" }, el("div", { className: "st-empty" }, failure)) : failure);
  } finally {
    button.disabled = false;
  }
}

function renderDoctor() {
  const d = state.doctor;
  $("doc-age").textContent = doctorAge(d);
  const ok = !itemsOf(d).some((i) => i.status === "missing");
  $("doc-body").replaceChildren(
    ...(d.sections || []).flatMap((section) => [
      section?.title ? el("h4", { textContent: section.title }) : null,
      ...sectionItems(section).map((item) => doctorRow(item))]),
    d.verdict ? (ok ? el("div", { className: "verdict", textContent: d.verdict }) : notice("err", d.verdict)) : null);
}

// Page Réglages, même format que les autres sections : une carte de lignes « libellé + explication »
// à gauche, valeur à droite. Les problèmes d'abord (avec leur action), les facultatifs, puis les
// vérifications OK repliées en une ligne.
function doctorPageRow(item) {
  const details = detailsOf(item), missing = item.status === "missing";
  return el("div", { className: `st-row st-doc ${missing ? "missing" : item.status || ""}` },
    el("span", { className: `st-disc ${item.status || ""}` }, icon(DOCTOR_MARKS[item.status] || "info", "sm")),
    el("div", { className: "st-txt" }, el("b", { textContent: item.text || "Vérification" }),
      details.length ? el("span", { className: "st-sub", textContent: details.join("\n") }) : null),
    missing ? el("button", { type: "button", className: "btn secondary sm", textContent: "Comment l'installer", onclick: () => openHelp("h-install") })
      : el("span", { className: "st-fixed", textContent: DOCTOR_LABELS[item.status] || "Information" }));
}

function renderDoctorPage() {
  const d = state.doctor, items = itemsOf(d);
  const problems = items.filter((i) => i.status === "missing");
  const oks = items.filter((i) => i.status === "ok");
  const others = items.filter((i) => i.status !== "missing" && i.status !== "ok");
  $("st-doctor-age").textContent = `${hostName()} · ${doctorAge(d)}`;
  $("st-toc-warn").hidden = !problems.length;
  const rows = items.length
    ? [...problems, ...others].map((item) => doctorPageRow(item))
    : [el("div", { className: "st-row" }, el("div", { className: "st-txt" },
        el("b", { textContent: "Aucune vérification" }), el("span", { textContent: "Le lanceur n'a rien renvoyé à vérifier." })))];
  const fold = oks.length ? el("details", { className: "st-okfold" },
    el("summary", { className: "st-row" }, el("span", { className: "st-disc ok" }, icon("check", "sm")),
      el("div", { className: "st-txt" }, el("b", { textContent: `${oks.length} vérification${oks.length > 1 ? "s" : ""} OK` })),
      icon("chevd", "sm")),
    ...oks.map((item) => doctorPageRow(item))) : null;
  $("st-doctor").replaceChildren(el("div", { className: "st-card" }, ...rows, ...(fold ? [fold] : [])),
    ...(d.verdict && !problems.length ? [el("p", { className: "st-verdict", textContent: d.verdict })] : []));
}

// Runners installés sur le client d'essai : proposés pour PROTON= et WINE=
function runnerSuggestions() {
  const names = [...new Set((state.doctor?.runners || []).map((r) => r.name))];
  for (const key of ["PROTON", "WINE"]) {
    const list = $(`v-${key}`);
    if (!list) continue;
    const have = new Set([...list.options].map((o) => o.value));
    for (const name of names) {
      const proton = /proton/i.test(name);
      if (!have.has(name) && (key === "PROTON") === proton) list.append(new Option(`${name} (${hostName()})`, name));
    }
  }
}

export function init() {
  $("doc-refresh").onclick = () => loadDoctor(true);
  $("st-doctor-refresh").onclick = () => loadDoctor(true, true);
}
