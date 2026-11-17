// settings.js — Page Réglages : mode, systèmes, bibliothèque, tâches de nuit, clients, enregistrement.

import { api } from "./api.js";
import { $, bigSize, el, icon, state, storage, toast } from "./core.js";
import { TRIAL_DURS, paintTrial, trialPrefs } from "./detail/detail.js";
import { loadDoctor } from "./doctor.js";
import { placeInd, showView } from "./header.js";
import { inst, loadInstall } from "./install.js";
import { SCAN_TIMEOUT, libBusy, loadLibrary } from "./library.js";
import { openMenuNode } from "./menu.js";
import { applyMode, connected, hostName, hostOn, isTrial, useForTrials } from "./mode.js";
import { loadSaves, osIcon, sv } from "./saves.js";
import { machineMenu } from "./saves-machines.js";
import { openPair } from "./saves-ops.js";

// Vue « settings » (menu ⚙). Chaque valeur vient de state.config ou de /api/config/systems ;
// « Enregistrer » n'envoie que ce qui a changé, par les routes existantes ; « Annuler » relit
// les valeurs enregistrées. Le mode choisi (segment) pilote les sections connectées.

const trimSlash = (d) => d.replace(/\/+$/, "") || "/";

const dirsKey = (list) => [...new Set(list.map(trimSlash))].sort().join("\n");

const sgdbAuthors = (text) => [...new Set(text.split(/[\n,]+/).map((a) => a.trim()).filter(Boolean))];

const countText = (images, folders) => [images && `${images} image${images > 1 ? "s" : ""}`,
  folders && `${folders} dossier${folders > 1 ? "s" : ""}`].filter(Boolean).join(" · ") || "vide";

const SYNC_CHOICES = [[0, "Jamais"], [600, "10 min"], [3600, "1 h"], [21600, "6 h"]];

const syncLabel = (s) => s % 3600 === 0 ? `${s / 3600} h` : s % 60 === 0 ? `${s / 60} min` : `${s} s`;

const MODE_HINTS = {
  autonome: "Construire et maintenir les images, sans client ni sauvegardes.",
  connecte: "Sauvegardes entre clients, installation et lancement d'essai.",
};

const CHANGE_LABELS = { mode: "mode", dirs: "bibliothèque", night: "plage de nuit", sgdb: "illustrations",
  machine: "adresse du manager", autopush: "synchro automatique", trial: "durée d'essai" };

let settingsSystems = [];       // /api/config/systems, relu à chaque ouverture

let settingsBase = null;        // valeurs affichées à l'ouverture : tout écart avec elles est une modification

export let settingsBusy = false;       // enregistrement en cours

// Segments en pilule (.seg) : un bouton par choix (data-value) ; le choix courant est .on
function buildSeg(seg, choices) {
  seg.replaceChildren(el("span", { className: "ind", ariaHidden: "true" }),
    ...choices.map(([value, text]) => {
      const b = el("button", { type: "button", role: "radio", textContent: text });
      b.dataset.value = String(value);
      return b;
    }));
}

const segValue = (seg) => seg.querySelector("button.on")?.dataset.value ?? null;

function setSeg(seg, value) {
  for (const b of seg.querySelectorAll("button[data-value]")) {
    const on = b.dataset.value === String(value);
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", String(on));
  }
  placeInd(seg);
}

function bindSeg(seg, onPick) {
  seg.addEventListener("click", (event) => {
    const b = event.target.closest("button[data-value]");
    if (!b || b.classList.contains("on")) return;
    setSeg(seg, b.dataset.value);
    onPick(b.dataset.value);
  });
  new ResizeObserver(() => placeInd(seg)).observe(seg);   // page masquée à l'origine : largeur connue à l'affichage
}

const settingsMode = () => segValue($("st-mode-seg")) || "autonome";

function onModeChoice() {
  paintPendingMode();
  loadSettingsConnected();
  paintSettingsBar();
}

// Le mode choisi (pas encore enregistré) montre ou cache les sections connectées
function paintPendingMode() {
  const mode = settingsMode();
  $("settings-main").dataset.pending = mode;
  $("st-mode-hint").textContent = MODE_HINTS[mode];
}

// Bibliothèque : un interrupteur par système (liste), la sélection dossier par dossier est dans « Avancé »
function rootInput(path) {
  return [...$("st-roots").querySelectorAll("input[data-path]")].find((i) => i.dataset.path === path);
}

const systemIsOn = (input) => input.closest(".root-block").dataset.mode === "all" || input.checked;

function setSystem(path, on) {
  const input = rootInput(path);
  if (!input) return;
  const block = input.closest(".root-block");
  if (block.dataset.mode === "all" && !on) {
    // « Tout le dossier » : tout cocher d'abord, pour garder la sélection effective, puis passer en choix
    for (const i of block.querySelectorAll("input[data-path]")) i.checked = true;
    block.setMode("pick");
  }
  input.checked = on;
  paintLibrary();
}

function renderSystemRows(systems) {
  $("st-libsys").replaceChildren(...systems.map((x) => {
    const rootName = x.root.split("/").filter(Boolean).pop() || x.root;
    const rel = x.path.startsWith(`${x.root}/`) ? x.path.slice(x.root.length + 1) : x.name;
    const sw = el("input", { type: "checkbox", className: "switch" });
    sw.dataset.path = x.path;
    sw.setAttribute("aria-label", `${x.name} : lu par le gestionnaire`);
    sw.onchange = () => setSystem(x.path, sw.checked);
    const count = [`${x.images} image${x.images > 1 ? "s" : ""}`, x.folders && `${x.folders} dossier${x.folders > 1 ? "s" : ""}`]
      .filter(Boolean).join(" · ");
    return el("div", { className: "st-row st-sys" },
      el("div", { className: "st-txt" }, el("b", { textContent: x.name }),
        el("span", { className: "st-sub mono", title: x.path, textContent: `${rootName}/${rel} · ${count}` })),
      sw);
  }));
}

// Interrupteurs et total d'après la sélection dossier par dossier (source de vérité)
function paintLibrary() {
  for (const sw of $("st-libsys").querySelectorAll("input[data-path]")) {
    const input = rootInput(sw.dataset.path);
    sw.checked = Boolean(input) && systemIsOn(input);
  }
  paintRootsTotal();
}

function paintRootsTotal() {
  let images = 0, folders = 0;
  for (const block of $("st-roots").querySelectorAll(".root-block")) {
    for (const i of block.querySelectorAll("input[data-path]")) {
      if (systemIsOn(i)) {
        images += Number(i.dataset.images) || 0;
        folders += Number(i.dataset.folders) || 0;
      }
    }
  }
  const extra = extraDirs().length;
  const total = $("st-total");
  const none = !images && !folders && !extra;
  total.classList.toggle("warn", none);
  total.textContent = none ? "Rien de sélectionné : activer au moins un système."
    : `Sélection : ${countText(images, folders)}${extra ? ` + ${extra} autre(s) dossier(s)` : ""}`;
}

// Dossiers de base (Avancé) : « Tout le dossier » (les systèmes ajoutés plus tard aussi) ou choix
function renderRoots(systems, chosen) {
  const roots = [...new Set(systems.map((x) => x.root))];
  const blocks = roots.map((root) => {
    const list = systems.filter((x) => x.root === root);
    const block = el("div", { className: "root-block" });
    block.dataset.root = root;
    const modes = el("div", { className: "pills on-s2 root-modes", role: "radiogroup" },
      el("button", { type: "button", role: "radio", textContent: "Tout le dossier" }),
      el("button", { type: "button", role: "radio", textContent: "Choisir les systèmes" }));
    modes.children[0].dataset.mode = "all";
    modes.children[1].dataset.mode = "pick";
    const chips = el("div", { className: "sys-toggles" }, ...list.map((x) => {
      const input = el("input", { type: "checkbox", checked: chosen.has(trimSlash(x.path)) });
      input.dataset.path = x.path;
      input.dataset.images = x.images;
      input.dataset.folders = x.folders;
      input.onchange = paintLibrary;
      return el("label", { title: x.path }, input, el("span", { textContent: x.name }),
        el("span", { className: "n", textContent: [x.images || "", x.folders ? `${x.folders} dossier(s)` : ""].filter(Boolean).join(" · ") }));
    }));
    const hint = el("p", { className: "st-hint" });
    const setMode = (mode) => {
      block.dataset.mode = mode;
      for (const b of modes.children) {
        b.classList.toggle("on", b.dataset.mode === mode);
        b.setAttribute("aria-checked", String(b.dataset.mode === mode));
      }
      const boxes = [...chips.querySelectorAll("input")];
      // Passer en choix sans rien de coché : tout cocher, on décoche ce qu'on ne veut pas
      if (mode === "pick" && !boxes.some((i) => i.checked)) boxes.forEach((i) => { i.checked = true; });
      chips.classList.toggle("all", mode === "all");
      boxes.forEach((i) => { i.disabled = mode === "all"; });
      hint.textContent = mode === "all"
        ? "Tous les systèmes ci-dessous sont lus, et ceux ajoutés plus tard aussi."
        : "Seuls les systèmes cochés sont lus.";
      paintLibrary();
    };
    modes.onclick = (event) => {
      const b = event.target.closest("button[data-mode]");
      if (b) setMode(b.dataset.mode);
    };
    block.append(
      el("div", { className: "root-head" },
        el("div", { className: "root-name" }, el("b", { textContent: root.split("/").filter(Boolean).pop() || root }),
          el("span", { className: "mono muted", textContent: root })),
        modes),
      hint, chips);
    block.setMode = setMode;
    return block;
  });
  $("st-roots").replaceChildren(...blocks);
  for (const block of blocks) block.setMode(chosen.has(trimSlash(block.dataset.root)) ? "all" : "pick");
  if (!blocks.length) {
    $("st-roots").replaceChildren(el("p", { className: "muted st-hint", textContent: "Aucun système trouvé dans les dossiers de base : saisir les dossiers ci-dessous." }));
  }
  return new Set([...roots, ...systems.map((x) => x.path)].map(trimSlash));
}

function extraDirs() {
  return $("st-dirs").value.split("\n").map((d) => d.trim()).filter(Boolean);
}

function chosenDirs() {
  const dirs = [];
  for (const block of $("st-roots").querySelectorAll(".root-block")) {
    if (block.dataset.mode === "all") dirs.push(block.dataset.root);
    else dirs.push(...[...block.querySelectorAll("input:checked")].map((i) => i.dataset.path));
  }
  return [...new Set([...dirs, ...extraDirs()])];
}

// Valeurs telles qu'affichées, et liste des réglages modifiés par rapport à l'ouverture
function settingsValues() {
  return {
    mode: settingsMode(),
    dirs: dirsKey(chosenDirs()),
    night: [$("st-night-start").value, $("st-night-end").value].join(),
    sgdb: JSON.stringify([sgdbAuthors($("st-sgdb-authors").value), Number($("st-sgdb-threshold").value)]),
    sgdbKey: $("st-sgdb-key").value.trim(),
    machine: $("st-machine-url").value.trim(),
    autopush: segValue($("st-autopush")),
    trial: segValue($("st-trial-dur")),
  };
}

export function settingsChanges() {
  if (!settingsBase) return [];
  const now = settingsValues(), b = settingsBase, online = now.mode === "connecte";
  const out = [];
  if (now.mode !== b.mode) out.push("mode");
  if (now.dirs !== b.dirs) out.push("dirs");
  if (now.night !== b.night) out.push("night");
  if (now.sgdb !== b.sgdb || now.sgdbKey) out.push("sgdb");
  if (online && now.machine !== b.machine) out.push("machine");
  if (online && now.autopush !== b.autopush) out.push("autopush");
  if (now.trial !== b.trial) out.push("trial");
  return out;
}

function paintSettingsBar() {
  const changes = settingsChanges(), bar = $("st-savebar"), shown = changes.length > 0;
  bar.classList.toggle("show", shown);
  bar.inert = !shown;
  $("st-count").textContent = shown
    ? `${changes.length} modification${changes.length > 1 ? "s" : ""} : ${changes.map((c) => CHANGE_LABELS[c]).join(", ")}`
    : "";
  $("st-save").disabled = settingsBusy || !shown;
  $("st-cancel").disabled = settingsBusy;
}

// Clients et diagnostic : seulement en mode connecté (enregistré)
const ONLY_CONNECTED_TEXT = "Disponible une fois le mode connecté enregistré.";

function loadSettingsConnected() {
  renderClients();
  paintSettingsDoctor();
  if (!connected()) return;
  loadSaves();
  loadInstall();
}

function paintSettingsDoctor() {
  const body = $("st-doctor"), trial = hostOn();
  $("st-doctor-refresh").hidden = !(connected() && trial);
  $("st-doctor-age").textContent = "";
  $("st-doctor-intro").textContent = `Vérifie ce dont le lanceur a besoin sur ${trial ? hostName() : "le client d'essai"}.`;
  if (settingsMode() !== "connecte") return;
  if (!connected()) body.replaceChildren(el("p", { className: "muted", textContent: ONLY_CONNECTED_TEXT }));
  else if (!trial) body.replaceChildren(el("p", { className: "muted", textContent: "Aucun client d'essai : dans Clients, menu d'un client, « Utiliser pour les essais »." }));
  else loadDoctor(false, true);
}

// Liste des clients associés (sv.machines), état et espace libre (inst.machines)
function renderClients() {
  const list = $("st-clients-list");
  if (!connected()) { list.replaceChildren(el("p", { className: "st-empty muted", textContent: ONLY_CONNECTED_TEXT })); return; }
  if (!sv.machines.length) {
    list.replaceChildren(el("p", { className: "st-empty muted", textContent: sv.loaded ? "Aucun client associé." : "Lecture des clients…" }));
    return;
  }
  list.replaceChildren(...sv.machines.map(clientRow));
}

function clientRow(m) {
  const free = inst.machines.find((x) => x.id === m.id)?.free;
  const ok = Boolean(m.ok);
  const menu = el("button", { type: "button", className: "mcard-menu", title: "Actions sur ce client", "aria-label": `Actions sur ${m.name}`, "aria-haspopup": "menu" },
    icon("chevd", "sm"));
  menu.onclick = () => openMenuNode(menu.parentElement, machineMenu(m));
  return el("div", { className: "st-row st-client" },
    el("span", { className: "mcard-ico" }, osIcon(m.icon, "lg")),
    el("div", { className: "st-txt" },
      el("span", { className: "st-cname" }, el("b", { textContent: m.name }),
        isTrial(m) ? el("span", { className: "chip plain t-accent", title: "Client d'essai", textContent: "essais" }) : null),
      el("span", { className: "st-sub mono", textContent: m.url || "" })),
    el("span", { className: `st-state${ok ? " ok" : ""}` }, el("span", { className: "st-led" }),
      el("span", { textContent: ok ? (free ? `En ligne · ${bigSize(free)} libres` : "En ligne") : "Hors ligne" })),
    el("div", { className: "dd" }, menu));
}

// Appelé quand les clients sont relus (sauvegardes, installation) : la page suit
export function paintSettingsClients() {
  if (document.body.dataset.view !== "settings") return;
  renderClients();
  paintSettingsTrial();
}

// Client d'essai : le nom, et le client choisi en haut de la page (s'il n'est pas l'essai, proposé)
function paintSettingsTrial() {
  const cfg = state.config || {};
  $("st-trial-name").textContent = hostOn() ? hostName() : "Aucun";
  const pick = connected() && state.client !== "all" ? sv.machines.find((m) => m.id === state.client) : null;
  const use = $("st-trial-use");
  use.hidden = !pick || pick.kind !== "saved" || pick.id === cfg.game_host_machine;
  use.textContent = pick ? `Utiliser ${pick.name} pour les essais` : "";
  use.onclick = async () => { await useForTrials(pick); paintSettingsTrial(); };
}

// Réglages lus de la configuration : les sections, puis la référence « modifié »
function renderSettings(cfg, systems, systemsError = "") {
  const chosen = new Set(cfg.roms_dirs.map(trimSlash));
  const known = renderRoots(systems, chosen);
  $("st-dirs").value = [...chosen].filter((d) => !known.has(d)).join("\n");
  renderSystemRows(systems);
  $("st-systems-err").textContent = systemsError;
  $("st-systems-err").hidden = !systemsError;
  paintLibrary();

  setSeg($("st-mode-seg"), cfg.mode);
  // Intervalle actuel hors des choix proposés : ajouté comme choix
  const auto = cfg.auto_push_interval ?? 120;
  buildSeg($("st-autopush"), SYNC_CHOICES.some(([v]) => v === auto) ? SYNC_CHOICES
    : [...SYNC_CHOICES, [auto, syncLabel(auto)]].sort((a, b) => a[0] - b[0]));
  setSeg($("st-autopush"), auto);
  $("st-machine-url").value = cfg.machine_url || (cfg.machine_port ? `https://${location.hostname}:${cfg.machine_port}` : "");
  $("st-machine-fp").textContent = !cfg.machine_port ? "Port des clients désactivé (WSQUASHFS_MGR_MACHINE_PORT=0)."
    : cfg.machine_fingerprint ? `Empreinte du certificat du manager : ${cfg.machine_fingerprint}` : "Certificat du manager créé au prochain démarrage.";

  $("st-night-start").value = cfg.night_start || "01:00";
  $("st-night-end").value = cfg.night_end || "07:00";
  $("st-concurrency").textContent = String(cfg.concurrency ?? "—");
  $("st-retention").textContent = cfg.old_retention_days == null ? "manuelle" : `${cfg.old_retention_days} jours`;

  $("st-sgdb-key").value = "";
  $("st-sgdb-key").placeholder = cfg.sgdb_configured ? "enregistrée" : "clé API";
  $("st-sgdb-msg").hidden = true;
  $("st-sgdb-authors").value = (cfg.sgdb_authors || []).join("\n");
  $("st-sgdb-threshold").value = cfg.sgdb_round_threshold ?? 0.85;
  paintThreshold();

  const trial = Number(storage.get("wsfs-trial-duration"));
  setSeg($("st-trial-dur"), TRIAL_DURS.some(([s]) => s === trial) ? trial : 60);
  $("st-info").replaceChildren(...[
    ["Dossiers de base", (cfg.roms_roots || []).join(", ") || "—"],
    ["Sauvegardes", cfg.saves_dir], ["Dossier de travail", cfg.tmp_dir], ["Reconstruction", cfg.rebuild_mode],
    ["Tâches simultanées", cfg.concurrency],
    ["Rétention .old", cfg.old_retention_days == null ? "manuelle" : `${cfg.old_retention_days} jours`],
  ].flatMap(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: v ?? "—" })]));

  paintPendingMode();
  paintSettingsTrial();
  renderClients();
  settingsBase = settingsValues();
  paintSettingsBar();
}

function paintThreshold() {
  $("st-sgdb-threshold-out").textContent = Number($("st-sgdb-threshold").value).toFixed(2).replace(".", ",");
}

// Ouverture de la page : configuration et systèmes relus, sections connectées chargées
export async function openSettings() {
  let cfg;
  try {
    cfg = await api("/api/config");
  } catch (error) {
    toast(`Réglages indisponibles : ${error.message}`, "alert", true);
    return;
  }
  state.config = cfg;
  applyMode();
  let systemsError = "";
  try {
    settingsSystems = await api("/api/config/systems");
  } catch (error) {
    // Serveur plus ancien que la page : la page s'ouvre quand même, chemins à saisir
    settingsSystems = [];
    systemsError = `Liste des systèmes indisponible (${error.message}) — dossiers à saisir dans « Avancé ».`;
  }
  renderSettings(cfg, settingsSystems, systemsError);
  $("st-adv").open = !settingsSystems.length || Boolean($("st-dirs").value);
  loadSettingsConnected();
  paintSettingsToc();
}

function paintSettingsToc() {
  const secs = [...$("settings-main").querySelectorAll(".st-sec")].filter((s) => s.offsetParent !== null);
  let current = secs[0];
  for (const s of secs) if (s.getBoundingClientRect().top <= 160) current = s;
  for (const a of document.querySelectorAll(".settings-toc a")) a.classList.toggle("on", a.dataset.st === current?.dataset.st);
}

// Enregistrer : seuls les réglages modifiés sont envoyés, par les routes existantes
async function saveSettings() {
  const changes = settingsChanges();
  if (!changes.length || settingsBusy) return;
  const errBox = $("st-err"), save = $("st-save"), v = settingsValues();
  const fail = (text) => { errBox.textContent = text; errBox.hidden = false; };
  errBox.hidden = true;
  if (changes.includes("dirs") && !chosenDirs().length) { fail("Bibliothèque : activer au moins un système."); return; }
  settingsBusy = true;
  paintSettingsBar();
  try {
    if (changes.includes("mode")) state.config = await api("/api/config/mode", { method: "PUT", body: { mode: v.mode } });
    if (changes.includes("machine")) state.config = await api("/api/config/machine_url", { method: "PUT", body: { url: v.machine } });
    if (changes.includes("autopush")) state.config = await api("/api/config/auto_push", { method: "PUT", body: { seconds: Number(v.autopush) } });
    if (changes.includes("night")) {
      state.config = await api("/api/config/night", { method: "PUT", body: { start: $("st-night-start").value, end: $("st-night-end").value } });
    }
    if (changes.includes("sgdb")) {
      state.config = await api("/api/config/sgdb", { method: "PUT",
        body: { ...(v.sgdbKey ? { api_key: v.sgdbKey } : {}), authors: sgdbAuthors($("st-sgdb-authors").value), threshold: Number($("st-sgdb-threshold").value) } });
    }
  } catch (error) {
    fail(error.message);
    settingsBusy = false;
    paintSettingsBar();
    return;
  }
  if (changes.includes("trial")) {
    storage.set("wsfs-trial-duration", v.trial);
    trialPrefs.seconds = Number(v.trial);
    paintTrial();
  }
  applyMode();
  let libraryMessage = "Réglages enregistrés";
  if (changes.includes("dirs")) {
    // Le serveur lit les images des dossiers choisis avant de répondre : long pour des dossiers
    // nouveaux. Passé un instant, la page cède la place à la bibliothèque, qui montre la lecture.
    save.replaceChildren(el("span", { className: "spinner" }), "Lecture des dossiers…");
    const dirs = chosenDirs();
    let done = null;
    const handOver = setTimeout(() => {
      showView("images");
      done = libBusy("Lecture des dossiers choisis",
        "Chaque nouvelle image est ouverte une fois pour lire son autorun et sa version : compter quelques secondes par image. Les prochaines ouvertures viendront du cache.");
    }, 1500);
    try {
      state.config = await api("/api/config/roms_dirs", { method: "PUT", body: { roms_dirs: dirs }, timeout: SCAN_TIMEOUT });
      clearTimeout(handOver);
      done?.();
      done = null;
      await loadLibrary();
      libraryMessage = `Réglages enregistrés : ${state.items.length} image(s)`;
    } catch (error) {
      clearTimeout(handOver);
      done?.();
      done = null;
      settingsBusy = false;
      save.textContent = "Enregistrer";
      if (document.body.dataset.view === "settings") fail(error.message);
      else toast(`Réglages : ${error.message}`, "alert", true);
      paintSettingsBar();
      return;
    }
    save.textContent = "Enregistrer";
  }
  settingsBusy = false;
  toast(libraryMessage);
  if (document.body.dataset.view === "settings") {
    renderSettings(state.config, settingsSystems);
    if (changes.includes("mode")) loadSettingsConnected();
  }
  paintSettingsBar();
}

export function init() {
  buildSeg($("st-trial-dur"), TRIAL_DURS);
  bindSeg($("st-mode-seg"), onModeChoice);
  bindSeg($("st-autopush"), () => paintSettingsBar());
  bindSeg($("st-trial-dur"), () => paintSettingsBar());
  $("st-dirs").addEventListener("input", paintLibrary);
  $("settings-main").addEventListener("input", paintSettingsBar);
  $("settings-main").addEventListener("change", paintSettingsBar);
  $("st-pair").onclick = openPair;
  $("st-sgdb-threshold").addEventListener("input", paintThreshold);
  // Essai de la clé saisie (corps {api_key}) ou, champ vide, de la clé enregistrée (POST /api/config/sgdb/test)
  $("st-sgdb-test").onclick = async () => {
    const msg = $("st-sgdb-msg"), button = $("st-sgdb-test");
    const typed = $("st-sgdb-key").value.trim();
    button.disabled = true;
    msg.hidden = false;
    msg.className = "notice";
    msg.textContent = typed ? "Essai de la clé saisie…" : "Essai de la clé enregistrée…";
    try {
      const r = await api("/api/config/sgdb/test", { method: "POST", timeout: 60000,
        ...(typed ? { body: { api_key: typed } } : {}) });
      msg.className = r.ok ? "notice ok" : "notice err";
      msg.textContent = r.message;
    } catch (error) {
      msg.className = "notice err";
      msg.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  // Sommaire : liens ancrés ; l'entrée de la section visible est active
  for (const a of document.querySelectorAll(".settings-toc a")) {
    a.addEventListener("click", (event) => {
      event.preventDefault();
      $(a.getAttribute("href").slice(1)).scrollIntoView({ block: "start" });
    });
  }
  window.addEventListener("scroll", () => { if (!$("settings-main").hidden) paintSettingsToc(); }, { passive: true });
  $("st-save").onclick = saveSettings;
  // Annuler : relit les valeurs enregistrées
  $("st-cancel").onclick = () => {
    if (state.config) renderSettings(state.config, settingsSystems);
    loadSettingsConnected();
  };
  // Le menu ⚙ ouvre la page (une seule fois : un second clic ne vide pas les modifications)
  $("btn-config").onclick = () => {
    if (document.body.dataset.view !== "settings") { showView("settings"); window.scrollTo(0, 0); }
  };
  for (const button of document.querySelectorAll("[data-close]")) button.onclick = () => button.closest("dialog").close();
}
