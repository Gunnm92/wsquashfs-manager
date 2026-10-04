# Reste à faire

## Fait

- [x] **Base de connaissances** (04/10) : repérage par nom du jeu (`game`),
  exécutable imposé (`prefer`, repêché même s'il était écarté) ; base locale
  dans l'appdata, prioritaire sur `rules/games.yaml` ; fiche → Autorun →
  « Ajouter à la base… ». Choix du calibrage ajoutés : Xenoverse 2
  (`START.exe`), FlatOut, RDR2, PAC-MAN CE DX (lanceurs) — Horizon Chase
  retiré, son autorun ayant été corrigé depuis.

- [x] **Autorun temporaire sans reconstruction** (04/10) :
  `<jeu>.wsquashfs.autorun` à côté de l'image, fusion clé par clé (`CLÉ=`
  vide : défaut du lanceur). Lanceur : lecture au lancement et par `--info`
  (commit f2892b8 du lanceur). Manager : « Appliquer tout de suite » dans la
  fiche (seules les différences avec l'image sont écrites), badge et filtre
  « surcharge », valeurs effectives dans la bibliothèque, « Inscrire dans
  l'image » (fiche ou sélection), actions en masse « en surcharge ».
  Batocera ne lit pas ce fichier : penser à inscrire les surcharges validées.

- [x] **Tâches de nuit** (04/10) : « Maintenant » ou « Cette nuit » sur chaque
  lancement (empaquetage, autorun, fichiers) ; plage horaire dans Réglages
  (01:00–07:00 par défaut, peut passer minuit) ; une tâche démarrée va au
  bout, aucune ne démarre hors plage ; « Lancer maintenant » dans la file.

## Jeux à tester

- [ ] **`HIDRAW=1` proposé** pour 9 jeux qui gèrent la DualSense eux-mêmes
  (règle vérifiée sur les 394 images le 02/10) : Beyond Two Souls, F1 22,
  Hot Wheels Unleashed 1 et 2, The Devil in Me, The King of Fighters XV,
  Pinball FX, Banishers, Kena — Steam Input désactivé.
- [ ] **God of War Ragnarök** : empaqueter (190 Go) puis tester lancement et
  DualSense (`HIDRAW=1`, `libScePad`).
- [ ] **Aces of the Luftwaffe** : manette non reconnue ; d'après ProtonDB, le
  jeu choisit l'appareil au menu principal (appuyer sur un bouton de la
  manette à l'écran titre). À confirmer.

## Génération et base de connaissances (SPEC § 2.5, étape 7)

- [ ] Lancement d'essai chronométré (le jeu tient-il 60 s, erreurs connues
  dans le journal).

## Fonctions de la SPEC non faites

- [ ] Étape 4 — montée de version : dossier source, aperçu des différences,
  `GAME_VERSION`, fichiers du jeu masqués par les sauvegardes.
- [ ] Historique de la fiche : afficher aussi les opérations antérieures à
  l'effacement de l'historique des tâches.

## Déploiement

- [ ] Pousser les commits (manager, lanceur `04bd003`, SteamBox `92cc329`)
  et reconstruire l'image SteamBox.
- [ ] `make push` puis installation du template Unraid
  (`unraid/wsquashfs-manager.xml`).
- [ ] Base de l'image : Debian trixie retenue, Alpine possible (image plus
  petite) — à confirmer.

## SteamBox (à reporter dans son dépôt)

- [ ] Fenêtre de jeu minimisée au lancement : un jeu D3D9 plein écran qui
  s'ouvre en fenêtre derrière ES perd le focus et se minimise (écran noir,
  le jeu attend). Vu sur Astebreed le 01/10, restauré à la main
  (`_NET_ACTIVE_WINDOW` + `XMapRaised`). Correctif possible : surveillance
  de la fenêtre au lancement, ou règle labwc.
