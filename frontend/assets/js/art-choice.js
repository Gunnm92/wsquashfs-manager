// art-choice.js — Aperçu avant de remplacer ou d'ajouter une illustration : galerie des
// propositions, dialogue d'une image (ou choix d'une proposition), dialogue en lot.
// Aucune reconstruction sans validation de cet aperçu.

import { api } from "./api.js";
import { artImages } from "./art.js";
import { $, el, icon, state, storage } from "./core.js";
import { notice } from "./detail/autorun.js";
import { showView } from "./header.js";
import { queued } from "./tasks.js";
import { fillWhen, whenButton, whenOf } from "./when.js";

const enc = encodeURIComponent;

// Images protégées par le jeton : chargées une fois (blob), jusqu'à 4 à la fois
const blobs = new Map();
let pending = 0;
const waitQueue = [];
const slot = (job) => new Promise((resolve, reject) => {
  const run = () => { pending++; job().then(resolve, reject).finally(() => { pending--; waitQueue.shift()?.(); }); };
  pending < 4 ? run() : waitQueue.push(run);
});

export function authUrl(url) {
  if (!blobs.has(url)) {
    const job = slot(() => fetch(url, { headers: { "x-wsfs-token": storage.get("wsfs-token") || "" } })
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `erreur ${r.status}`);
        return URL.createObjectURL(await r.blob());
      }));
    blobs.set(url, job);
    job.catch(() => blobs.delete(url));
  }
  return blobs.get(url);
}

// Cadre portrait rempli quand il apparaît à l'écran (galerie) ou tout de suite (aperçu)
const lazy = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    lazy.unobserve(entry.target);
    fillFrame(entry.target, frameUrls.get(entry.target));
  }
}, { rootMargin: "200px" });
const frameUrls = new WeakMap();

function fillFrame(frame, url) {
  frame.replaceChildren(el("span", { className: "spinner" }));
  authUrl(url).then((src) => frame.replaceChildren(...artImages(frame, src)),
    (error) => frame.replaceChildren(el("span", { className: "art-none", textContent: "illustration indisponible", title: error.message })));
}

function lazyFrame(url, className = "frame") {
  const frame = el("span", { className });
  frameUrls.set(frame, url);
  lazy.observe(frame);
  return frame;
}

const attrs = (node, values) => { for (const [k, v] of Object.entries(values)) node.setAttribute(k, v); return node; };

const SOURCE_LABEL = { es: "Jaquette EmulationStation", icon: "Icône de l'exécutable" };
const caption = (c) => c.source === "sgdb" ? `SteamGridDB · ${c.author || "auteur inconnu"} · ${c.score ?? "—"}` : (SOURCE_LABEL[c.source] || c.label);
const candidateOf = (data, key) => data.candidates.find((c) => c.key === key) || null;

// Dialogue sans présentation fixe : ouvert, retiré à la fermeture
function newDialog(className, onClose = () => {}) {
  const dlg = el("dialog", { className });
  dlg.addEventListener("close", () => { onClose(); dlg.remove(); });
  document.body.append(dlg);
  return dlg;
}

const dialogHead = (title, dlg) => {
  const close = el("button", { type: "button", className: "iconbtn", title: "Fermer" }, icon("x"));
  close.onclick = () => dlg.close();
  return el("div", { className: "dlg-head" }, el("h2", { textContent: title }), el("span", { className: "spacer" }), close);
};

// Image en grand (double-clic ou loupe)
function openZoom(loadUrl, title) {
  const dlg = newDialog("art-zoom");
  const img = el("img", { alt: title, className: "art-zoom-img" });
  dlg.append(dialogHead(title, dlg), el("div", { className: "art-zoom-box" }, el("span", { className: "spinner" })));
  dlg.querySelector(".art-zoom-box").replaceChildren(img);
  authUrl(loadUrl).then((src) => { img.src = src; }, (error) => {
    dlg.querySelector(".art-zoom-box").replaceChildren(notice("err", error.message));
  });
  dlg.showModal();
}

// Galerie des propositions : clic = sélectionner, double-clic ou loupe = en grand
function gallery(data, selected, onSelect) {
  const node = el("div", { className: "art-gal", role: "listbox", "aria-label": "Propositions d'illustration" });
  const tiles = new Map();
  const paint = (key) => {
    for (const [k, tile] of tiles) {
      const on = k === key;
      tile.classList.toggle("selected", on);
      tile.setAttribute("aria-selected", String(on));
    }
  };
  for (const c of data.candidates) {
    const check = el("span", { className: "art-check" }, icon("check", "sm"));
    const zoomBtn = attrs(el("button", { type: "button", className: "art-zoom-btn" }, icon("search", "sm")),
      { title: "Voir en grand", "aria-label": "Voir en grand" });
    zoomBtn.onclick = (event) => { event.stopPropagation(); openZoom(c.full, caption(c)); };
    const tags = [c.key === data.preferred ? "automatique" : null, c.same_as_current ? "déjà intégrée" : null].filter(Boolean);
    const tile = attrs(el("div", { className: `art-cand${c.same_as_current ? " same" : ""}` },
      lazyFrame(c.thumb), check, zoomBtn,
      el("span", { className: "art-cap", textContent: caption(c), title: `${caption(c)} · ${c.width}×${c.height}` }),
      tags.length ? el("span", { className: "art-tag", textContent: tags.join(" · ") }) : null),
    { role: "option", tabindex: "0", "aria-selected": "false" });
    tile.onclick = () => onSelect(c.key);
    tile.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(c.key); } };
    tile.ondblclick = () => openZoom(c.full, caption(c));
    tiles.set(c.key, tile);
    node.append(tile);
  }
  paint(selected);
  return { node, paint };
}

// Dialogue d'une image. choose : renvoie la clé choisie (sans rien envoyer) ; sinon envoie la tâche.
// data : aperçu déjà chargé (dialogue en lot) ; sinon chargé ici.
export function openCover(item, { choose = false, data = null, selected = undefined } = {}) {
  return new Promise((resolve) => {
    let result;
    const dlg = newDialog("art-dlg", () => resolve(result));
    const msg = el("div", { className: "art-msg" });
    const cur = el("div", { className: "art-cur" }, el("span", { className: "art-cur-label", textContent: "Actuelle" }));
    const galBox = el("div", { className: "art-galbox" }, el("div", { className: "notice info" },
      el("span", { className: "spinner" }), el("span", { textContent: "Recherche des illustrations…" })));
    const hint = el("span", { className: "muted art-hint" });
    const apply = el("button", { type: "button", className: "btn primary", disabled: true }, choose ? "Choisir" : "Remplacer");
    const cancel = el("button", { type: "button", className: "btn secondary" }, "Annuler");
    cancel.onclick = () => dlg.close();
    const when = whenButton();
    const foot = el("div", { className: "dlg-foot" }, hint, el("span", { className: "spacer" }), cancel,
      choose ? apply : el("div", { className: "split" }, apply, when));
    dlg.append(dialogHead(choose ? `Choisir l'illustration de ${item.name}` : `Illustration de ${item.name}`, dlg),
      msg, el("div", { className: "art-body" }, cur, galBox), foot);
    dlg.showModal();

    const show = (d) => {
      const present = d.current.present;
      const frame = present ? el("span", { className: "frame" }) : el("span", { className: "art-none", textContent: "aucune" });
      cur.replaceChildren(el("span", { className: "art-cur-label", textContent: "Actuelle" }), frame);
      if (present) fillFrame(frame, `${d.current.url}&v=${d.current.sha1}`);
      if (!d.sgdb_configured) {
        const link = el("button", { type: "button", className: "linklike", textContent: "Réglages › Illustrations" });
        link.onclick = () => { dlg.close(); showView("settings"); };
        msg.append(notice("info", "SteamGridDB n'est pas configuré : seules les propositions locales sont offertes. ", "info"));
        msg.lastChild.append(link);
      }
      if (d.sgdb_error) msg.append(notice("err", `SteamGridDB : ${d.sgdb_error}`, "alert"));
      apply.textContent = choose ? "Choisir" : present ? "Remplacer" : "Ajouter";
      hint.textContent = choose ? "" : present ? "Remplacer réécrit l'image (reconstruction, l'ancienne est gardée en .old)."
        : "Ajout instantané, sans reconstruction.";
      if (!d.candidates.length) {
        galBox.replaceChildren(el("div", { className: "notice info", textContent: "Aucune proposition pour cette image." }));
        return;
      }
      let key = candidateOf(d, selected ?? d.preferred) ? (selected ?? d.preferred) : null;
      const g = gallery(d, key, (k) => { key = k; g.paint(k); refresh(); });
      galBox.replaceChildren(g.node);
      const refresh = () => {
        const c = key && candidateOf(d, key);
        apply.disabled = !c || c.same_as_current;
        apply.textContent = !c ? "Choisir une illustration"
          : c.same_as_current ? "Déjà intégrée : rien à changer"
          : choose ? "Choisir" : present ? "Remplacer" : "Ajouter";
      };
      refresh();
      apply.onclick = async () => {
        if (choose) { result = key; dlg.close(); return; }
        apply.disabled = true;
        try {
          const body = { ids: [item.id], replace: present, choices: { [item.id]: key }, when: whenOf() };
          const r = await api("/api/mass/cover", { method: "POST", body });
          if (r.skipped.length) {
            msg.replaceChildren(notice("err", r.skipped.map((s) => s.reason).join(" ; "), "alert"));
            apply.disabled = false;
            return;
          }
          queued(present ? "Remplacement de l'illustration ajouté à la file" : "Illustration ajoutée à la file");
          dlg.close();
        } catch (error) {
          msg.replaceChildren(notice("err", error.message));
          apply.disabled = false;
        }
      };
    };

    if (data) show(data);
    else api(`/api/image/art/candidates?id=${enc(item.id)}`).then(show, (error) => {
      galBox.replaceChildren(notice("err", error.message));
    });
  });
}

// Dialogue en lot : une ligne par image, choix proposé par image, validation groupée.
export function openCoverLot(ids) {
  const n = ids.length;
  // Plus de trois images : cette nuit est présélectionné (bouton .when, mémorisé comme les autres)
  if (n > 3) { storage.set("wsfs-when", "night"); fillWhen(); }
  const rows = new Map(ids.map((id) => [id, { id, item: state.byId.get(id) || { id, name: id }, data: null, error: null, key: null, checked: false }]));
  const dlg = newDialog("art-dlg");
  const count = el("p", { className: "art-count muted" });
  const msg = el("div", { className: "art-msg" });
  const list = el("div", { className: "art-lot" });
  const apply = el("button", { type: "button", className: "btn primary", disabled: true });
  const cancel = el("button", { type: "button", className: "btn secondary" }, "Annuler");
  cancel.onclick = () => dlg.close();
  dlg.append(dialogHead(`Illustrations de ${n} image${n > 1 ? "s" : ""}`, dlg), msg,
    el("p", { className: "dlg-intro", textContent: "Les images déjà illustrées sont reconstruites (ancienne gardée en .old) ; les autres reçoivent l'illustration sans reconstruction. Clique une proposition pour en choisir une autre." }),
    count, list,
    el("div", { className: "dlg-foot" }, el("span", { className: "spacer" }), cancel, el("div", { className: "split" }, apply, whenButton())));
  dlg.showModal();

  const selected = () => [...rows.values()].filter((r) => r.checked);
  const tally = () => {
    const all = [...rows.values()];
    const loaded = all.filter((r) => r.data || r.error);
    const rep = selected().filter((r) => r.data?.current.present).length;
    const add = selected().filter((r) => r.data && !r.data.current.present).length;
    count.textContent = loaded.length < n ? `Recherche des illustrations… ${loaded.length} sur ${n}`
      : `${rep} à remplacer, ${add} à ajouter, ${n - rep - add} inchangée${n - rep - add > 1 ? "s" : ""}`;
    apply.disabled = loaded.length < n || selected().length === 0;
    apply.textContent = `Valider (${selected().length})`;
  };

  const rowNode = (r) => {
    const box = el("input", { type: "checkbox", checked: r.checked });
    box.onchange = () => { r.checked = box.checked; tally(); };
    const cur = el("span", { className: "art-mini-wrap" });
    const prop = el("button", { type: "button", className: "art-prop", title: "Choisir une autre illustration" });
    const reason = el("span", { className: "art-reason muted" });
    const capText = el("span", { className: "art-cap muted" });
    prop.onclick = async () => {
      if (!r.data || !r.key) return;
      const key = await openCover(r.item, { choose: true, data: r.data, selected: r.key });
      if (!key) return;
      r.key = key;
      r.checked = !candidateOf(r.data, key).same_as_current;
      paint();
      tally();
    };
    const paint = () => {
      const d = r.data, c = d && r.key ? candidateOf(d, r.key) : null;
      const same = Boolean(c?.same_as_current);
      box.checked = r.checked;
      box.disabled = !c || same || Boolean(r.error);
      if (d?.current.present) cur.replaceChildren(lazyFrame(`${d.current.url}&v=${d.current.sha1}`, "frame art-mini"));
      else cur.replaceChildren(el("span", { className: "art-none", textContent: d ? "aucune" : "" }));
      if (c) { prop.replaceChildren(lazyFrame(c.thumb, "frame art-mini")); capText.textContent = caption(c); }
      else { prop.replaceChildren(el("span", { className: "art-none", textContent: r.error ? "erreur" : d ? "—" : "" })); capText.textContent = ""; }
      prop.disabled = !c;
      reason.textContent = r.error ? r.error : !d ? "recherche…" : !c ? "aucune proposition" : same ? "déjà intégrée"
        : d.sgdb_error ? "SteamGridDB en erreur : proposition locale" : "";
      node.classList.toggle("off", !r.checked);
    };
    const node = el("div", { className: "art-row" }, box,
      el("div", { className: "art-name" }, el("strong", { textContent: r.item.name || r.id }), reason),
      cur, el("span", { className: "art-arrow", "aria-hidden": "true", textContent: "→" }), prop, capText);
    r.paint = paint;
    paint();
    return node;
  };

  for (const r of rows.values()) list.append(rowNode(r));
  tally();

  // Chargement des aperçus : 4 requêtes à la fois, une ligne après l'autre
  const queue = [...ids];
  const worker = async () => {
    while (queue.length) {
      const r = rows.get(queue.shift());
      try {
        r.data = await api(`/api/image/art/candidates?id=${enc(r.id)}`);
        r.key = r.data.candidates.some((c) => c.key === r.data.preferred) ? r.data.preferred : null;
        const c = r.key && candidateOf(r.data, r.key);
        r.checked = Boolean(c) && !c.same_as_current;
      } catch (error) {
        r.error = error.message;
      }
      r.paint();
      tally();
    }
  };
  Promise.all(Array.from({ length: Math.min(4, n) }, worker));

  apply.onclick = async () => {
    const picked = selected();
    const groups = [picked.filter((r) => r.data.current.present), picked.filter((r) => !r.data.current.present)].filter((g) => g.length);
    apply.disabled = true;
    const skipped = [];
    let tasks = 0;
    try {
      for (const group of groups) {
        const r = await api("/api/mass/cover", { method: "POST", body: {
          ids: group.map((x) => x.id), replace: group[0].data.current.present,
          choices: Object.fromEntries(group.map((x) => [x.id, x.key])), when: whenOf() } });
        tasks += r.tasks.length;
        skipped.push(...r.skipped);
      }
    } catch (error) {
      msg.replaceChildren(notice("err", error.message));
    }
    if (tasks) queued(`${tasks} illustration(s) ajoutée(s) à la file`);
    if (skipped.length) {
      msg.replaceChildren(notice("err", skipped.map((s) => `${s.id} : ${s.reason}`).join("\n"), "alert"));
      apply.disabled = false;
    } else if (!msg.childElementCount) dlg.close();
    else apply.disabled = false;
  };
}
