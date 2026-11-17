// doctor.js — État du lanceur (--doctor) : page et réparation des runners.

import { api } from "./api.js";
import { $, el, icon, state } from "./core.js";
import { notice } from "./detail/autorun.js";
import { openHelp } from "./help.js";
import { hostName } from "./mode.js";

// Diagnostic du client d'essai : fenêtre (#dlg-doctor) ou page Réglages (#st-doctor, inPage)
const doctorAge = (d) => d.age ? `il y a ${Math.round(d.age / 60)} min` : "à l'instant";

const DOCTOR_MARKS = { ok: "check", missing: "x", optional: "minus" };

function doctorRow(item) {
  return el("div", { className: `item ${item.status}` },
    el("span", { className: "disc" }, icon(DOCTOR_MARKS[item.status] || "info")),
    el("div", {}, el("span", { textContent: item.text }),
      item.details.length ? el("span", { className: "details", textContent: item.details.join("\n") }) : null));
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
    body.replaceChildren(notice("", `État du lanceur indisponible : ${error.message}`, "alert"));
  } finally {
    button.disabled = false;
  }
}

function renderDoctor() {
  const d = state.doctor;
  $("doc-age").textContent = doctorAge(d);
  const ok = !d.sections.some((s) => s.items.some((i) => i.status === "missing"));
  $("doc-body").replaceChildren(
    ...d.sections.flatMap((section) => [
      section.title ? el("h4", { textContent: section.title }) : null,
      ...section.items.map((item) => doctorRow(item))]),
    d.verdict ? (ok ? el("div", { className: "verdict", textContent: d.verdict }) : notice("err", d.verdict)) : null);
}

// Page Réglages : les problèmes d'abord (encart avec l'aide), les vérifications OK repliées
function renderDoctorPage() {
  const d = state.doctor, items = d.sections.flatMap((s) => s.items);
  const problems = items.filter((i) => i.status === "missing");
  const rest = items.filter((i) => i.status !== "missing");
  const oks = rest.filter((i) => i.status === "ok").length;
  $("st-doctor-age").textContent = `${hostName()} · ${doctorAge(d)}`;
  $("st-toc-warn").hidden = !problems.length;
  $("st-doctor").replaceChildren(
    ...problems.map((item) => el("div", { className: "st-alert" }, icon("alert"),
      el("div", { className: "st-alert-txt" }, el("b", { textContent: item.text }),
        item.details.length ? el("span", { className: "mono", textContent: item.details.join("\n") }) : null),
      el("button", { type: "button", className: "btn secondary sm", textContent: "Comment l'installer", onclick: () => openHelp("h-install") }))),
    el("details", { className: "st-ok" },
      el("summary", {}, icon("check", "sm"), el("span", { textContent: `${oks} vérification${oks > 1 ? "s" : ""} OK` })),
      el("div", { className: "doctor" }, ...rest.map((item) => doctorRow(item)))),
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
