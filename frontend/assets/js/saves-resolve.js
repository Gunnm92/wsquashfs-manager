// saves-resolve.js — Sauvegardes : fenêtres « Départager » (copie du manager et versions de conflit)
// et « Restaurer » (versions précédentes gardées par le manager).

import { api } from "./api.js";
import { $, ago, el, icon, size, when } from "./core.js";
import { busy, dayText, gameLabel, machineName } from "./saves.js";
import { runSaveOp } from "./saves-ops.js";

// Candidats d'un jeu : la copie du manager (si elle existe), puis chaque version de conflit rangée,
// puis chaque client en conflit dont la copie n'est pas déjà rangée (même empreinte)
function candidates(g) {
  const list = [];
  if (g.reserve) {
    list.push({ keep: "manager", origin: `Manager${g.reserve.source ? ` (venue de ${g.reserve.source})` : ""}`, newest: g.reserve.newest, size: g.reserve.size, iconName: "server" });
  }
  for (const c of g.conflicts || []) {
    list.push({ keep: c.name, origin: (c.machine_id && machineName(c.machine_id)) || c.source || c.name, newest: c.newest, size: c.size, iconName: "save" });
  }
  const stored = new Set((g.conflicts || []).map((c) => `${c.machine_id}:${String(c.fingerprint || "").toLowerCase()}`));
  for (const m of g.machines || []) {
    if (m.action !== "conflit" || stored.has(`${m.id}:${String(m.fingerprint || "").toLowerCase()}`)) continue;
    list.push({ keep: `client:${m.id}`, origin: m.name || machineName(m.id), newest: m.newest, size: m.size, iconName: "save" });
  }
  const top = Math.max(0, ...list.map((c) => c.newest || 0));
  return list.map((c) => ({ ...c, latest: list.length > 1 && top > 0 && c.newest === top }));
}

// Ouvre la fenêtre : les données sont déjà dans sv.games, aucune lecture réseau
export function openResolve(g) {
  $("rs-title").textContent = `Départager · ${gameLabel(g)}`;
  $("rs-push").checked = true;
  const cards = candidates(g).map((c) => {
    const keep = el("button", { type: "button", className: "btn secondary sm", disabled: busy(), textContent: "Garder celle-ci" });
    keep.onclick = () => {
      const push = $("rs-push").checked;
      $("dlg-resolve").close();
      runSaveOp("/api/saves/resolve", { game: g.game, keep: c.keep, push });
    };
    return el("div", { className: `rs-card${c.latest ? " latest" : ""}` },
      el("div", { className: "rs-origin" }, icon(c.iconName, "sm"), el("b", { textContent: c.origin }),
        c.latest ? el("em", { className: "rs-badge", textContent: "la plus récente" }) : null),
      el("div", { className: "rs-meta" },
        el("span", { textContent: c.newest ? `Dernière partie : ${dayText(c.newest)}` : "Date inconnue", title: c.newest ? when(c.newest) : "" }),
        el("span", { textContent: c.size ? size(c.size) : "Taille inconnue" })),
      keep);
  });
  $("rs-cands").replaceChildren(...cards);
  $("dlg-resolve").showModal();
}

// Fenêtre « Restaurer » : versions précédentes du manager, lues à l'ouverture (/api/saves/versions)
export async function openRestore(g) {
  $("rv-title").textContent = `Restaurer · ${gameLabel(g)}`;
  $("rv-push").checked = true;
  $("rv-cands").replaceChildren(el("p", { className: "muted", textContent: "Lecture des versions…" }));
  $("dlg-restore").showModal();
  let versions = [];
  try {
    versions = (await api(`/api/saves/versions?game=${encodeURIComponent(g.game)}`)).versions || [];
  } catch (err) {
    $("rv-cands").replaceChildren(el("p", { className: "notice err", textContent: String(err.message || err) }));
    return;
  }
  if (!versions.length) {
    $("rv-cands").replaceChildren(el("p", { className: "muted", textContent: "Aucune version précédente sur le manager." }));
    return;
  }
  const cards = versions.map((v) => {
    const go = el("button", { type: "button", className: "btn secondary sm", disabled: busy(), textContent: "Restaurer celle-ci" });
    go.onclick = () => {
      const push = $("rv-push").checked;
      $("dlg-restore").close();
      runSaveOp("/api/saves/restore", { game: g.game, version: v.name, push });
    };
    return el("div", { className: "rs-card" },
      el("div", { className: "rs-origin" }, icon("history", "sm"),
        el("b", { textContent: v.archived_at ? `Remplacée ${ago(v.archived_at)}` : v.name, title: v.archived_at ? when(v.archived_at) : "" })),
      el("div", { className: "rs-meta" },
        el("span", { textContent: v.newest ? `Dernière partie : ${dayText(v.newest)}` : "Date de partie inconnue", title: v.newest ? when(v.newest) : "" }),
        el("span", { textContent: v.size ? size(v.size) : "Taille inconnue" }),
        v.source ? el("span", { textContent: `Venue de ${v.source}` }) : null),
      go);
  });
  $("rv-cands").replaceChildren(...cards);
}
