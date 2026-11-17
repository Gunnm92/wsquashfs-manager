// when.js — Moment d'exécution : maintenant ou cette nuit.

import { el, icon, state, storage, toast } from "./core.js";

// Un seul choix pour toute la page (mémorisé) : chaque bouton .when le bascule.

export const whenOf = () => storage.get("wsfs-when") === "night" ? "night" : "now";

function paintWhen(button) {
  const night = whenOf() === "night";
  const range = state.config ? ` (${state.config.night_start}–${state.config.night_end})` : "";
  button.replaceChildren(icon(night ? "moon" : "zap", "sm"));
  button.title = night ? `Programmé cette nuit${range} — cliquer pour lancer maintenant` : `Lancé maintenant — cliquer pour programmer cette nuit${range}`;
  button.setAttribute("aria-label", night ? `Quand : cette nuit${range}` : "Quand : maintenant");
}

export function whenButton() {
  const button = el("button", { className: "when", type: "button" });
  paintWhen(button);
  return button;
}

export function fillWhen() {
  for (const button of document.querySelectorAll("button.when")) paintWhen(button);
}

export function init() {
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button.when");
    if (!button) return;
    event.stopPropagation();
    storage.set("wsfs-when", whenOf() === "night" ? "now" : "night");
    fillWhen();
    toast(whenOf() === "night" ? "Les prochaines actions seront programmées cette nuit" : "Les prochaines actions partent tout de suite",
      whenOf() === "night" ? "moon" : "zap");
  });
}
