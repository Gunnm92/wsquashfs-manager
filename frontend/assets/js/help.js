// help.js — Aide : chargement du fragment, affichage, recherche.

import { $, storage } from "./core.js";
import { notice } from "./detail/autorun.js";
import { showView } from "./header.js";

// Fragment assets/help.html : une page à la fois (menu de gauche) ; la
// recherche parcourt toutes les pages et ne garde que les blocs [data-f].

let helpLoading = null;

export function loadHelp() {
  if (!helpLoading) {
    helpLoading = fetch("assets/help.html", { cache: "no-cache" })
      .then((r) => { if (!r.ok) throw new Error(`erreur ${r.status}`); return r.text(); })
      .then((html) => { $("help-main").innerHTML = html; setupHelp(); })
      .catch((error) => {
        helpLoading = null;
        $("help-main").replaceChildren(notice("err", `Aide indisponible : ${error.message}`));
      });
  }
  return helpLoading;
}

function showHelpPage(page) {
  for (const b of document.querySelectorAll(".help-nav button")) b.classList.toggle("on", b.dataset.page === page);
  for (const p of document.querySelectorAll(".hpage")) p.hidden = p.dataset.page !== page;
  storage.set("wsfs-help-page", page);
}

export async function openHelp(anchor) {
  await showView("help");
  if ($("help-q")?.value) { $("help-q").value = ""; filterHelp(""); }
  const target = $(anchor) || (anchor.startsWith("hk-") ? $("h-keys") : null);
  if (!target) return;
  const page = target.closest(".hpage");
  if (page) showHelpPage(page.dataset.page);
  if (target.tagName === "DETAILS") target.open = true;
  target.scrollIntoView({ block: target.matches("details.key") ? "center" : "start" });
  target.classList.remove("flash");
  void target.offsetWidth;          // relance l'animation
  target.classList.add("flash");
}

function setupHelp() {
  showHelpPage(storage.get("wsfs-help-page") || "start");
  for (const b of document.querySelectorAll(".help-nav button")) {
    b.onclick = () => {
      if ($("help-q").value) { $("help-q").value = ""; filterHelp(""); }
      showHelpPage(b.dataset.page);
      window.scrollTo(0, 0);
    };
  }
  // Liens internes : ouvrent la bonne page, sans toucher à l'adresse
  $("help-main").addEventListener("click", (event) => {
    const a = event.target.closest('a[href^="#"]');
    if (!a) return;
    event.preventDefault();
    openHelp(a.getAttribute("href").slice(1));
  });
  // Un seul guide ouvert à la fois
  $("help-main").addEventListener("toggle", (event) => {
    const d = event.target;
    if (!d.open || !d.matches?.("details.guide") || $("help-q").value) return;
    for (const other of d.parentElement.querySelectorAll(":scope > details.guide[open]")) if (other !== d) other.open = false;
  }, true);
  let timer = null;
  $("help-q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => filterHelp($("help-q").value), 150); });
}

function filterHelp(raw) {
  const q = raw.trim().toLowerCase();
  const body = $("help-body");
  const items = [...body.querySelectorAll("[data-f]")];
  const pages = [...body.querySelectorAll(".hpage")];
  body.classList.toggle("searching", Boolean(q));
  if (!q) {
    for (const item of items) item.hidden = false;
    for (const h of body.querySelectorAll(".hpage > h2, .hpage > p:not([data-f]), .hpage > .lead")) h.hidden = false;
    showHelpPage(storage.get("wsfs-help-page") || "start");
    $("help-nores").hidden = true;
    return;
  }
  let any = false;
  for (const item of items) {
    const match = item.textContent.toLowerCase().includes(q);
    item.hidden = !match;
    if (match && item.tagName === "DETAILS") item.open = true;
  }
  // Titres et introductions masqués pendant la recherche ; une page sans résultat disparaît
  for (const h of body.querySelectorAll(".hpage > h2, .hpage > p:not([data-f]), .hpage > .lead")) h.hidden = true;
  for (const page of pages) {
    const shown = page.querySelector("[data-f]:not([hidden])");
    page.hidden = !shown;
    any ||= Boolean(shown);
  }
  $("help-nores").hidden = any;
}
