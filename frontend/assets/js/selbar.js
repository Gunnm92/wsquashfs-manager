// selbar.js — Barre de sélection des images : une action impossible pour la sélection reste visible,
// grisée, avec la raison en infobulle (recalculé à chaque changement de sélection).

import { $, setUnavailable } from "./core.js";
import { inst, installedOn, imageFile, isWsq, selectedImages } from "./install.js";

export function paintSelectionActions() {
  const images = selectedImages();
  setUnavailable($("btn-mass-validate"), images.some((i) => i.has_old) ? "" : "Aucune image sélectionnée n'a d'ancienne version (.old)");
  setUnavailable($("btn-mass-commit"), images.some((i) => (i.override || []).length) ? "" : "Aucune image sélectionnée n'a de surcharge à inscrire");
  setUnavailable($("btn-saves-reset"), images.some((i) => i.saves_present) ? "" : "Aucune image sélectionnée n'a de sauvegardes");

  const wsq = images.filter(isWsq);
  const online = inst.machines.filter((m) => m.ok);
  const installed = (i) => installedOn(imageFile(i)).length;
  let install = "";
  if (!wsq.length) install = "Aucune image .wsquashfs dans la sélection";
  else if (!inst.loaded) install = inst.error ? "Clients indisponibles" : "Lecture des clients…";
  else if (!online.length) install = "Aucun client joignable";
  else if (!wsq.some((i) => installed(i) < online.length)) install = "Déjà installées sur tous les clients joignables";
  setUnavailable($("btn-inst-install"), install);

  const uninstall = !wsq.length ? "Aucune image .wsquashfs dans la sélection"
    : !wsq.some((i) => installed(i) > 0) ? "Aucune image sélectionnée n'est installée sur un client joignable" : "";
  setUnavailable($("btn-inst-uninstall"), uninstall);
}
