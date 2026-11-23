// main.js — Démarrage : initialisation des modules dans l'ordre d'origine, puis premier affichage.
import { init as initCore } from "./core.js";
import { hydrateAicons } from "./aicons.js";
import { init as initArt } from "./art.js";
import { init as initApi } from "./api.js";
import { init as initMenu } from "./menu.js";
import { init as initLibrary } from "./library.js";
import { init as initDetailDetail } from "./detail/detail.js";
import { init as initDetailAutorun } from "./detail/autorun.js";
import { init as initDetailVersion } from "./detail/version.js";
import { init as initDetailKb } from "./detail/kb.js";
import { init as initDetailFiles } from "./detail/files.js";
import { init as initTasks } from "./tasks.js";
import { init as initMode } from "./mode.js";
import { init as initSavesMachines } from "./saves-machines.js";
import { init as initActivity } from "./activity.js";
import { init as initSaves } from "./saves.js";
import { init as initSavesOps } from "./saves-ops.js";
import { init as initDoctor } from "./doctor.js";
import { init as initSettings } from "./settings.js";
import { init as initMass } from "./mass.js";
import { init as initActions } from "./actions.js";
import { init as initHeader } from "./header.js";
import { init as initFolders, loadFolders } from "./folders.js";
import { init as initWhen } from "./when.js";
import { init as initInstallDialog } from "./install-dialog.js";
import { init as initConfig } from "./config.js";

import { api } from "./api.js";
import { $, state, storage } from "./core.js";
import { buildForm } from "./detail/autorun.js";
import { showView } from "./header.js";
import { init as initInstall, loadInstall } from "./install.js";
import { loadLibrary, renderFilters } from "./library.js";
import { applyMode, connected } from "./mode.js";
import { loadSaves, paintSavesView } from "./saves.js";
import { refreshTasks } from "./tasks.js";
import { fillWhen } from "./when.js";

initCore();
hydrateAicons();
initArt();
initApi();
initMenu();
initLibrary();
initDetailDetail();
initDetailAutorun();
initDetailVersion();
initDetailKb();
initDetailFiles();
initTasks();
initMode();
initSavesMachines();
initActivity();
initSaves();
initSavesOps();
initDoctor();
initSettings();
initMass();
initActions();
initHeader();
initFolders();
initWhen();
initInstallDialog();
initInstall();
initConfig();

(async () => {
  fillWhen();
  renderFilters();
  state.keys = await api("/api/autorun/keys");
  try { state.config = await api("/api/config"); } catch { state.config = null; }
  applyMode();
  fillWhen();
  $("known-keys").replaceChildren(...state.keys.map((k) => new Option(k.key, k.key)));
  buildForm();
  // La vue par défaut s'affiche tout de suite (Sauvegardes ne dépend pas de la bibliothèque) ;
  // folders et help restent mémorisés
  // Sauvegardes par défaut en mode connecté, Images en autonome (folders et help restent mémorisés)
  const stored = storage.get("wsfs-view");
  const view = ["folders", "config", "help", "settings"].includes(stored) ? stored : connected() ? "saves" : "images";
  showView(view);              // charge aussi les sauvegardes si c'est la vue
  if (connected() && view !== "saves") loadSaves();   // compteur de l'onglet
  if (view !== "folders") loadFolders();               // compteur de l'onglet (dossiers à empaqueter)
  await loadLibrary();
  paintSavesView();                // les liens vers les images attendent la bibliothèque
  loadInstall();               // images installées par client : filtre, pastilles, fiche (connecté)
  refreshTasks();
})();
