# Reste à faire

## Autorun temporaire, sans reconstruire l'image (SPEC § 7, point 4)

Pouvoir changer un réglage **tout de suite** (ajouter `GAME_VERSION`, essayer
`HIDRAW=1`, `DXVK=0`…) sans attendre une reconstruction de l'image, puis
l'inscrire dans l'image plus tard, en une fois.

- [ ] **Fichier de surcharge** `<jeu>.wsquashfs.autorun`, à côté de l'image
  (même logique de nommage que le `.keys` de Batocera). Contenu au format
  `autorun.cmd`. Fusion **clé par clé** : une clé de la surcharge remplace
  celle de l'image ou s'ajoute ; `CLÉ=` vide la retire. Le reste de
  l'autorun de l'image reste valable. *À trancher : fusion clé par clé ou
  remplacement complet de l'autorun.*
- [ ] **Lanceur** (`wsquashfs-launcher`) : lire la surcharge après
  l'autorun de l'image, l'annoncer dans sa sortie (« Surcharge : … »), et
  l'appliquer aussi à `--info` (version affichée).
- [ ] **Manager** :
  - fiche → Autorun : choix « Appliquer tout de suite (surcharge) » ou
    « Reconstruire l'image » ;
  - bibliothèque : badge « surcharge » ; version, runner, manettes affichés
    avec la valeur effective (surcharge comprise) ;
  - « Inscrire dans l'image » (une image ou une sélection) : reconstruction
    avec l'autorun fusionné, vérification, puis suppression de la surcharge ;
  - actions en masse (poser `GAME_VERSION`, `HIDRAW`…) : option « en
    surcharge » pour un effet immédiat ;
  - filtre « avec surcharge en attente ».
- [ ] **Batocera ignore ce fichier** : un jeu lancé sous Batocera garde
  l'autorun de l'image. Le signaler dans l'interface, et ne pas oublier
  d'« inscrire dans l'image » les surcharges validées.

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

- [ ] « Ajouter à la base » en un clic depuis une correction validée
  (`rules/games.yaml`), comme Astebreed (`d3dx9_43` native).
- [ ] Choix propres au jeu repérés au calibrage, à mettre dans la base :
  `START.exe` (Dragon Ball Xenoverse 2), `Launcher.exe` (FlatOut, RDR2,
  PAC-MAN CE DX), `-Shipping.exe` (Horizon Chase).
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
