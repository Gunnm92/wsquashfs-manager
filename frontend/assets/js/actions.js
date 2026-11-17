// actions.js — Vérification, essai et sauvegardes de la fiche, info-bulle.

import { openCover, openCoverLot } from "./art-choice.js";
import { api } from "./api.js";
import { $, el, helpLink, icon, size, state, storage, toast } from "./core.js";
import { notice } from "./detail/autorun.js";
import { TRIAL_DURS, oldAction, paintTrial, setAutorunEdit, showTab, syncLine, trialPrefs } from "./detail/detail.js";
import { loadLibrary } from "./library.js";
import { openMenuNode } from "./menu.js";
import { connected, hostName, hostOn, stripAnsi } from "./mode.js";
import { gameOfItem, presentIn, sv } from "./saves.js";
import { menuItem } from "./saves-machines.js";
import { openSavesFor } from "./saves-ops.js";
import { queued } from "./tasks.js";
import { whenButton, whenOf } from "./when.js";

export function reportSkipped(skipped) {
  if (skipped.length) alert(skipped.map((s) => `${s.id} : ${s.reason}`).join("\n"));
}

async function verify(ids) {
  const data = await api("/api/mass/verify", { method: "POST", body: { ids } });
  reportSkipped(data.skipped);
  queued("Vérification ajoutée à la file");
}

async function resetSaves(ids) {
  const names = ids.filter((id) => state.byId.get(id)?.saves_present);
  if (!names.length) { alert("Aucune image sélectionnée n'a de sauvegardes."); return; }
  if (!confirm(`Réinitialiser les sauvegardes de ${names.length} jeu(x) ? Elles sont renommées en <jeu>.avant-<date>, pas supprimées.`)) return;
  const data = await api("/api/mass/saves/reset", { method: "POST", body: { ids: names } });
  reportSkipped(data.skipped);
  toast("Sauvegardes mises de côté", "save");
  await loadLibrary();
}

async function runTrial(duration) {
  if (!confirm(`Lancer « ${state.current.name} » sur ${hostName()} pendant ${duration} s ? Le jeu s'affiche sur l'écran de la session : ne rien lancer pendant ce temps. Il est ensuite arrêté.`)) return;
  try {
    await api(`/api/image/trial?id=${encodeURIComponent(state.current.id)}`, { method: "POST", body: { duration, when: "now" } });
    queued("Lancement d'essai ajouté à la file");
  } catch (error) { alert(error.message); }
}

const hasCover = () => $("d-cover").classList.contains("embedded");

// Illustration de la fiche : l'aperçu (art-choice.js) décide, rien n'est lancé directement
function coverItem() {
  const i = state.current;
  return menuItem(hasCover() ? "Remplacer l'illustration" : "Ajouter l'illustration", "image", () => openCover(i));
}

// Action sur un jeu de la fiche (save, sync) : résultat par jeu dans un toast.
async function gameSaveAction(button, body, label, doneText) {
  button.disabled = true;
  try {
    const r = await api("/api/game_host/maintenance", { method: "POST", timeout: 900000, body });
    // Lignes de résultat par jeu : « ␣␣✓ Jeu  message » (– ignoré, ✗ échec, → état).
    const plain = stripAnsi(r.output || "");
    const out = plain.split("\n").filter((l) => /^  [✓–✗→] /.test(l)).map((l) => l.trim()).join("\n");
    if (r.code !== 0) alert(`${label} : ${plain.trim() || `code ${r.code}`}`);
    else toast(out || doneText, "save");
  } catch (error) { alert(error.message); } finally { button.disabled = false; }
}

// Sauvegardes de la fiche : synchroniser, copier (clients d'essai), réinitialiser isolé
function copySaves() {
  gameSaveAction($("s-copy"), { action: "save", id: state.current.id }, "Copie des sauvegardes", "Sauvegardes copiées");
}

function syncSaves() {
  const i = state.current;
  if (!confirm(`Synchroniser les sauvegardes de « ${i.name} » avec les autres clients associés du réseau local ? En cas de conflit, la plus lourde gagne (à taille égale, la plus récente) et l'autre est archivée.`)) return;
  gameSaveAction($("s-sync-btn"), { action: "sync", id: i.id }, "Synchronisation", "Sauvegardes synchronisées");
}

export function paintSaves() {
  const i = state.current;
  if (!i) return;
  const g = connected() ? sv.games.find((x) => x.game === gameOfItem(i)) : null;
  $("s-size").textContent = i.saves_present ? size(i.saves_size) : "Aucune";
  $("s-sync").textContent = g ? syncLine(g) : "";
  $("s-share").setAttribute("aria-checked", String(Boolean(g && presentIn(g).length > 1)));
  $("s-share").title = "Le partage se règle par client, dans Sauvegardes";
  $("s-sync-btn").hidden = $("s-copy").hidden = !hostOn();
  $("s-reset").disabled = !i.saves_present;
}

async function applyRelink(id, sug, button) {
  if (!confirm(`Reconstruire « ${state.current.name} » : réécrire ${sug.rewritten} lien(s) en relatif. L'original est gardé en .old.`)) return;
  button.disabled = true;
  try {
    await api(`/api/image/relink?id=${encodeURIComponent(id)}`, { method: "POST", body: { when: whenOf() } });
    button.replaceChildren(icon("check", "sm"), "Envoyé dans la file");
    queued();
  } catch (error) {
    button.disabled = false;
    alert(error.message);
  }
}

async function applyRunner(id, sug, button) {
  const what = sug.action === "join"
    ? `copier ${sug.runner} (${size(sug.size)}) dans runners/${sug.dest}/ et poser ${sug.key}=${sug.dest}`
    : `copier ${sug.sub} de ${sug.runner} (${size(sug.size)}) dans runners/${sug.dest}/`;
  if (!confirm(`Reconstruire « ${state.current.name} » : ${what}, ${sug.rewritten} lien(s) réécrit(s). L'original est gardé en .old.`)) return;
  button.disabled = true;
  try {
    await api(`/api/image/runner?id=${encodeURIComponent(id)}`, { method: "POST", body: {
      source: sug.source, sub: sug.sub, dest: sug.dest, set_key: sug.set_key, when: whenOf() } });
    button.replaceChildren(icon("check", "sm"), "Envoyé dans la file");
    queued();
  } catch (error) {
    button.disabled = false;
    alert(error.message);
  }
}

export function init() {
  // Essai, vérification, « … » et illustration : deux actions visibles, le reste dans les menus
  $("d-trial").onclick = () => runTrial(trialPrefs.seconds);
  $("d-trial-pick").onclick = () => openMenuNode($("d-trial-wrap"), [
    el("div", { className: "dd-sub", textContent: "Durée de l'essai" }),
    ...TRIAL_DURS.map(([s, label]) => menuItem(label, "flask", () => {
      trialPrefs.seconds = s; storage.set("wsfs-trial-duration", String(s)); paintTrial();
    }, { checked: s === trialPrefs.seconds })),
  ]);
  $("d-verify").onclick = () => verify([state.current.id]).catch((error) => alert(error.message));
  $("d-cover-menu").onclick = () => openCover(state.current);
  $("d-more").onclick = () => {
    const i = state.current;
    const disabled = (node, off) => { node.disabled = off; return node; };
    openMenuNode($("d-more").parentElement, [
      menuItem("Modifier l'autorun", "pen", () => { showTab("autorun"); if (state.tab === "autorun") setAutorunEdit(true); }),
      menuItem("Nouvelle version…", "upload", () => showTab("version")),
      el("div", { className: "dd-sub", textContent: "Illustration" }),
      coverItem(),
      ...(i.has_old ? [
        menuItem("Supprimer l'ancienne version", "check", () => oldAction("validate", `Supprimer définitivement l'ancienne version de « ${i.name} » (.old) ?`)),
        menuItem("Restaurer l'ancienne version", "undo", () => oldAction("restore", "Remettre l'ancienne version à la place de l'image actuelle (.old) ? La reconstruction sera perdue.")),
      ] : []),
      ...(connected() && hostOn() ? [menuItem("Copier les sauvegardes", "save", () => copySaves())] : []),
      disabled(menuItem("Réinitialiser les sauvegardes", "trash", () => resetSaves([i.id]).catch((error) => alert(error.message)), { danger: true }), !i.saves_present),
    ]);
  };
  $("s-share").onclick = () => openSavesFor(gameOfItem(state.current));
  $("s-open").onclick = () => openSavesFor(gameOfItem(state.current));
  $("s-sync-btn").onclick = () => syncSaves();
  $("s-copy").onclick = () => copySaves();
  $("s-reset").onclick = () => resetSaves([state.current.id]).catch((error) => alert(error.message));
  $("btn-verify").onclick = () => verify([...state.selected]).catch((error) => alert(error.message));
  // Illustrations de la sélection : un aperçu par image, validé ensemble (ajout ou remplacement)
  $("btn-cover").onclick = () => openCoverLot([...state.selected]);
  $("btn-cover-replace").onclick = () => openCoverLot([...state.selected]);
  $("btn-saves-reset").onclick = () => resetSaves([...state.selected]).catch((error) => alert(error.message));
  // Info-bulle : délégation sur tout élément [data-tip], positionnée sous lui
  {
    const tip = $("tip");
    const show = (target) => {
      tip.replaceChildren(el("strong", { textContent: target.dataset.tipTitle || "" }),
        ...target.dataset.tip.split("\n").filter(Boolean).map((line) => el("span", { className: /^[A-Z_]+=/.test(line) ? "code" : "", textContent: line })));
      tip.hidden = false;
      const r = target.getBoundingClientRect(), t = tip.getBoundingClientRect();
      const left = Math.min(Math.max(8, r.left), innerWidth - t.width - 8);
      const below = r.bottom + 6 + t.height < innerHeight;
      tip.style.left = `${left}px`;
      tip.style.top = `${below ? r.bottom + 6 : r.top - t.height - 6}px`;
    };
    const hide = () => { tip.hidden = true; };
    for (const type of ["mouseover", "focusin"]) document.addEventListener(type, (event) => {
      const target = event.target.closest?.("[data-tip]");
      target ? show(target) : hide();
    });
    for (const type of ["mouseout", "focusout"]) document.addEventListener(type, (event) => {
      if (event.target.closest?.("[data-tip]") && !event.relatedTarget?.closest?.("[data-tip]")) hide();
    });
    addEventListener("scroll", hide, true);
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") hide(); });
  }
  // Liens du runner : constat et réparation proposée (runner du client d'essai joint ou complété)
  $("d-links").onclick = async () => {
    const id = state.current.id, box = $("d-links-result");
    box.className = "msg";
    box.replaceChildren(el("span", { className: "row" }, el("span", { className: "spinner" }), `analyse de l'image${hostOn() ? ` et des runners de ${hostName()}` : ""}…`));
    $("d-links").disabled = true;
    try {
      const r = await api(`/api/image/links?id=${encodeURIComponent(id)}`);
      if (state.current?.id !== id) return;
      if (!r.issues.length) {
        box.replaceChildren(notice("ok", "Aucun lien vers un runner hors de l'image ou absent : image autonome.", "check"));
        return;
      }
      box.replaceChildren(
        el("ul", { className: "issues" }, ...r.issues.map((i) => el("li", { className: i.level }, i.message, " ", helpLink("h-runner")))),
        ...r.suggestions.map((sug) => {
          const label = { join: "Joindre le runner", complete: "Compléter le runner", relink: "Réécrire les liens" }[sug.action];
          const apply = el("button", {}, icon("clip", "sm"), label);
          apply.onclick = () => (sug.action === "relink" ? applyRelink : applyRunner)(id, sug, apply);
          return el("div", { className: "suggestion" },
            el("strong", { textContent: sug.title }),
            el("p", { textContent: `${sug.detail}. ${sug.rewritten ? `${sug.rewritten} lien(s) réécrit(s) en relatif, ` : ""}${sug.repaired} réparé(s), ${sug.remaining} restant(s) ; ${size(sug.size)} ajoutés à l'image (avant compression).` }),
            ...(sug.warnings || []).map((w) => notice("", w, "alert")),
            el("div", { className: "split", style: "margin-top:8px" }, apply, whenButton()));
        }),
        ...(r.suggestions.length ? [] : [r.steambox_error
          ? (!connected() ? notice("info", "Mode autonome : la réparation par runner du client d'essai est désactivée (Réglages › Mode).")
            : hostOn() ? notice("err", `Réparation impossible, ${hostName()} injoignable : ${r.steambox_error}`)
            : notice("", "Réparation automatique indisponible : elle reprend un runner installé sur le client d'essai (menu d'un client, « Utiliser pour les essais »). Sinon, joindre le runner à la main (voir l'aide).", "info"))
          : el("p", { className: "muted", textContent: `Aucun runner de ${hostName()} ne contient les fichiers visés : joindre le runner à la main (voir l'aide).` })]));
    } catch (error) {
      box.replaceChildren(notice("err", error.message));
    } finally {
      $("d-links").disabled = false;
    }
  };
}
