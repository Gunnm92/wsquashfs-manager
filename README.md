# wsquashfs-manager

Application web de gestion des images `.wsquashfs` (jeux Windows empaquetés au
format Batocera) : ludothèque, édition de l'`autorun.cmd`, montée de version,
génération automatique de l'autorun, traitement en masse.

Le manager s'appuie sur les conventions de
[wsquashfs-launcher](https://github.com/Gunnm92/wsquashfs-launcher) (qui reste
tel quel) et réutilise ses outils (`--info`, `--pack`) quand c'est possible.

Voir [SPEC.md](SPEC.md) pour la spécification complète.

## Avancement (SPEC § 6)

| Étape | État |
|---|---|
| 1. Socle : scan + cache, bibliothèque, fiche | fait |
| 2. Autorun : formulaire + texte, validations, reconstruction, vérification, `.old` | fait |
| 3. Masse : sélection, autorun, fichiers et registre (relais fakeping), sauvegardes, intégrité, file de tâches, aperçu, espace disque | fait |
| 4. Version | fait : fiche → Version (build ou patch, aperçu, sauvegardes masquantes mises de côté) |
| 5. Génération | fait : dossiers et images existantes (fiche → Autorun → « Proposer un autorun »), analyse PE, choix de l'exécutable, règles YAML, base de connaissances |
| 6. Création | fait : dossiers `*.pc`/`*.wine` → `.wsquashfs` en batch (fakeping : étape 3) |
| 7. Connaissances | fait : base par jeu (nom, empreinte, exécutable imposé), ajout en un clic, lancement d'essai dans SteamBox |

Aussi : tâches programmées la nuit (plage dans Réglages), autorun temporaire
`<jeu>.wsquashfs.autorun` sans reconstruction (lu par le lanceur, à inscrire
ensuite dans l'image), historique permanent par image. Ce qui reste à faire :
[TODO.md](TODO.md).

## Lancer

```sh
./run.sh            # service : http://127.0.0.1:8765
./run.sh --dev      # rechargement automatique du code
```

Au premier lancement, `run.sh` crée l'environnement Python (`backend/.venv`)
et installe les dépendances ; ensuite seulement si `pyproject.toml` change.
Une seule instance à la fois : une seconde refuse de démarrer (elle prendrait
les mêmes tâches). Tests : `backend/.venv/bin/python -m pytest` (dans
`backend/`, les reconstructions réelles exigent squashfs-tools).

Les systèmes se choisissent dans l'interface (Réglages → Bibliothèque),
parmi ceux du dossier de base `WSQUASHFS_MGR_ROMS_DIRS` (`/roms` dans le
conteneur) : « Tout le dossier » (les systèmes ajoutés plus tard compris) ou
une sélection de systèmes ; ce choix l'emporte sur la variable. D'autres
chemins peuvent s'ajouter : le système est déduit comme le fait le lanceur. Un serveur resté sur une ancienne version répond
« API introuvable ».

## Docker (déploiement)

Conteneur séparé de SteamBox (SPEC § 7, point 2), image
`registry.elfenn.eu/wsquashfs-manager` (Debian trixie : squashfs-tools 4.6,
squashfuse, fuse-overlayfs), déployé par un **template Unraid** :
[`unraid/wsquashfs-manager.xml`](unraid/wsquashfs-manager.xml).

```sh
make build                 # image locale (via docker-socket-proxy)
make push                  # publication sur registry.elfenn.eu
```

Installation : copier le template dans
`/boot/config/plugins/dockerMan/templates-user/my-wsquashfs-manager.xml`,
puis Docker → Add Container → Template : `wsquashfs-manager`. Champs :

| Champ | Conteneur | Rôle |
|---|---|---|
| Port de l'interface | 8765 | interface web |
| Roms | `/roms` | dossier des roms Batocera ; les systèmes se cochent dans Réglages |
| Sauvegardes du lanceur | `/saves` | sauvegardes de wsquashfs-launcher dans SteamBox |
| Appdata | `/data` | file de tâches, caches, réglages |
| Mot de passe | `WSQUASHFS_MGR_PASSWORD` | obligatoire : sans lui, le conteneur refuse de démarrer |
| FUSE (avancé) | `/dev/fuse` | reconstruction sans extraction (mode overlay) |
| PUID / PGID (avancé) | 99 / 100 | propriétaire des images créées (nobody:users = `arcade` de SteamBox) |

**Machine de jeu** (Réglages → Machine de jeu) : là où tourne
`wsquashfs-launcher`, pour le **lancement d'essai** (le jeu tient-il,
fenêtre minimisée, erreurs connues, capture ; refusé si un jeu .wsquashfs
tourne déjà), l'état du lanceur (`--doctor`) et les runners installés
(réparation des liens du runner). Facultative : tout le reste fonctionne
sans. Le diagnostic et la maintenance du lanceur sont dans Réglages → Outils,
utilisables une fois la machine de jeu enregistrée et joignable : lister,
démonter, nettoyer, copier les sauvegardes (tous les jeux), synchroniser les
sauvegardes avec les autres machines associées, voir l'état de la synchro.
La fiche d'un jeu a « Copier les sauvegardes » et « Synchroniser » (ce jeu), et
« Sauvegardes entre machines », qui ouvre l'onglet Sauvegardes filtré sur le jeu.
L'association d'une machine se fait une fois, en ligne de commande, sur les
deux machines : `wsquashfs-launcher --pair` sur l'autre, puis
`wsquashfs-launcher --sync wsq://…` sur celle-ci (le manager ne l'expose pas ;
le code ne sert qu'une fois, voir ci-dessous).

| Mode | Usage |
|---|---|
| Désactivée | défaut d'une installation neuve |
| Cette machine | lanceur installé sur la machine du gestionnaire, avec un écran ; le service tourne sous l'utilisateur de la session graphique |
| Agent réseau | **Une machine déjà associée** (onglet Sauvegardes : SteamBox, AIBox, Deck…) se choisit dans la liste « Machine associée » : son adresse, son jeton et son certificat sont repris, sans nouveau code (`PUT /api/config/game_host/machine {"id"}`). **Autre machine** : sur la machine de jeu, ou dans son conteneur (ex. SteamBox) : `wsquashfs-launcher --pair` affiche le code `wsq://…` et attend qu'il soit collé dans **Associer** (le certificat est vérifié contre le code, puis le code est échangé contre un jeton propre à ce manager ; le code ne sert qu'une fois, pendant que `--pair` attend ; une machine se révoque côté machine par `wsquashfs-launcher --pair forget <machine>` ou `--pair reset`) ; ensuite `wsquashfs-launcher --agent` lance l'agent en arrière-plan (`--agent stop` l'arrête ; rien ne le démarre tout seul, aucun port ouvert en dehors de `--agent`). Voir `docs/agent.md` du lanceur. |

Le mode Conteneur Docker (API Docker, `docker-socket-proxy`) a été retiré : un
conteneur se joint par l'agent. Une configuration qui le mentionne repasse en
« Désactivée ». Le template Unraid ne doit plus poser
`WSQUASHFS_MGR_STEAMBOX_CONTAINER`, qui n'a plus d'effet.

Paramètres supplémentaires du template : `--pid=host` (voir les jeux lancés
dans SteamBox et ne pas reconstruire leur image pendant une partie),
`--cap-add=SYS_ADMIN --security-opt apparmor=unconfined` (montages FUSE).
Le conteneur démarre en root, prépare `/data`, puis passe à PUID/PGID.
Le travail sur une image se fait à côté d'elle (`.wsquashfs-manager/`, même
disque), jamais dans l'appdata.

## Sauvegardes entre machines

Le manager tourne en permanence : il garde une copie des sauvegardes de partie
(`wsquashfs-launcher`, `/v1/saves/*` de l'agent) et synchronise celles d'une
machine **source** (celle qui est la plus avancée) vers les autres machines.
À l'écran, cette copie est la colonne et la carte **Manager**.

- **Machines** : `POST /api/saves/machines {code}` associe une machine par son
  code `wsq://…` : le certificat est contrôlé contre l'empreinte du code, puis
  le secret du code est échangé une seule fois (`POST /v1/pair`, agent version 7)
  contre le jeton durable de ce manager ; le nom est le `host` de l'agent. Une
  même empreinte ou une même adresse remplace l'entrée. Le secret, le jeton et
  le certificat ne sont jamais renvoyés. La machine de
  jeu en mode agent apparaît d'office (identifiant `game-host`, non
  supprimable) sauf si une machine de la liste a la même adresse.
- **Icône** : `os` de `/v1/info` (ID de `/etc/os-release` : `steamos`,
  `batocera`, sinon `linux`), remplaçable à la main (`PATCH`, champ `icon` de
  la machine ; `saves_game_host_icon` pour la machine de jeu).
- **Copie du manager** : `state_dir/saves-reserve/<jeu>/` : `current.tar.gz`,
  `meta.json` (empreinte, newest, taille, profil, source, date de réception) ;
  avant chaque remplacement, l'ancienne version devient
  `AAAAMMJJ-HHMMSS.tar.gz` (3 gardées). Écritures atomiques (durable.py).
- **Pull** (machine → manager), **push** (manager → cibles), **push_from**
  (pull puis push des seuls jeux récupérés). Une machine occupée (jeu en
  cours, 409) n'est pas touchée : le résultat le dit, par jeu et par machine.
- **États** (`GET /api/saves/status`) : par jeu et par machine, avec la taille
  et la date. une machine sans le jeu est « absente » et ce
  n'est pas une différence (AIBox ou le Deck n'ont qu'une partie des jeux).
  « présente » : une seule machine l'a. « identique » : les machines qui l'ont
  (deux au moins) ont la même version. Sinon : « plus avancée » (la plus
  lourde, puis la plus récente : la source suggérée) ou « en retard ». Les
  machines non jointes sont « injoignable » (elles ne comptent pas). `check` : la plus lourde n'est
  pas la plus récente (`check_by` = la plus récente), à vérifier avant de
  synchroniser. Chaque machine n'est interrogée qu'une fois.
- **Suppression** (`POST /api/saves/delete {games, machines, manager}`) : sur
  les machines cochées, l'agent archive la sauvegarde dans son dossier des
  backups puis l'efface (`DELETE /v1/saves/tar?game=`, route de l'agent v5) ;
  sur le manager, la version courante devient une version datée (3 gardées) et
  le jeu n'est plus dans la copie. Une machine occupée (409), injoignable, ou
  un agent plus ancien (404/405 : `--update` demandé) échoue seule. Le jeu doit
  être arrêté. Rien à supprimer sur une machine : réussite sans effet.
- **Dossiers déclarés et jeux Switch** (agent v8) : en plus des jeux, les agents
  listent des dossiers déclarés (`dossier:<nom>`) et les sauvegardes Eden, jeu par
  jeu (`switch:<TitleID>`) ; `status` leur donne `label` (nom lisible venu de la
  machine, sinon déduit) et `kind` (`dossier`, `switch` ou `jeu`). Pull, push,
  suppression et copie du manager les traitent comme des jeux. Les dossiers se
  déclarent par machine (menu de la carte → « Dossiers synchronisés… ») :
  `GET|PUT|DELETE /api/saves/machines/{id}/dirs` relaient `/v1/saves/dirs` de
  l'agent (422 : son refus, ex. chemin hors de son HOME, ou agent trop ancien ;
  502 : injoignable).
- **Tâches** : pull, push et suppression passent par la file (tâche `saves`,
  sans image) ; le détail par jeu et par cible est dans `params.result` de la tâche.
- **Écran** : l'onglet **Sauvegardes**, premier onglet, ouvert au démarrage
  (Dossiers à empaqueter et Aide restent mémorisés). Aide intégrée : page
  « Sauvegardes ».
  - Bandeau : une carte par machine (icône, pastille verte joignable ou rouge
    injoignable, nombre de jeux) et une carte Manager. Un clic sur une machine
    n'affiche que ses sauvegardes, sa colonne en premier (second clic : tout) ;
    le chevron à droite de la carte propose son icône et « Oublier » (pas pour
    la machine de jeu). « Associer
    une machine » prend le code `wsq://` ; sur la machine :
    `wsquashfs-launcher --pair`, puis `wsquashfs-launcher --agent` pour
    qu'elle reste joignable. Sans machine, un encadré explique ces étapes.
  - Tableau en sections (Jeux PC, Switch, Dossiers ; titres seulement s'il y en
    a plusieurs), lignes triées par nom lisible. Un clic sur le titre replie ou
    rouvre la section (mémorisé dans le navigateur) ; « Tout sélectionner » ne
    prend que les lignes visibles.
  - Tableau jeux × machines : ★ plus avancée, identique, en retard, — absente,
    seule copie, ? injoignable, avec taille · date ; colonne Manager ;
    ⚠ à vérifier. Filtres : Tous, Différences, Identiques, Sur le manager ; le
    filtre par machine passe par le bandeau (ci-dessus).
  - Une ligne : « Sync X → Y » (la plus avancée vers les machines en retard)
    et son chevron : sync depuis une machine précise, « Installer sur Z » (machine
    qui n'a pas le jeu, choix explicite), copier vers le manager, sync depuis
    le manager (machines qui ont le jeu), et « Supprimer… ».
  - « Supprimer… » (menu de la ligne, ou bouton de la barre de sélection) ouvre
    un dialogue : une case par machine joignable et une pour le manager, cochées
    si le jeu (ou un des jeux) y a une sauvegarde, sinon grisées ; « Supprimer »
    reste désactivé tant que rien n'est coché.
  - Sélection : source (la plus avancée, jeu par jeu ; une machine ; le
    manager), cibles, « Sync » (récapitulatif avant envoi), « Copier vers le
    manager ». Une opération à la fois ; le résultat liste les échecs par jeu et
    par machine.
  - Liste des images : une icône dans « Sauveg. » ouvre l'onglet filtré sur le
    jeu, dès qu'une machine est associée.

Routes (toutes sous `/api/saves/`) :

| Méthode | Chemin | Corps / réponse |
|---|---|---|
| GET | `machines` | `{"machines":[{id,name,url,fingerprint,kind,removable,icon}]}` |
| POST | `machines` | `{"code":"wsq://…"}` → `{id,name,url,fingerprint,replaced}` ; 422 code illisible, déjà utilisé ou expiré, empreinte différente, agent trop ancien (version < 7) ; 502 injoignable |
| GET | `machines/{id}/dirs` | `{dirs:[{name,path}], eden}` : dossiers déclarés sur la machine, dossier d'Eden (`""` sinon) |
| PUT | `machines/{id}/dirs` | `{"name","path"}` (chemin à partir du dossier personnel de la machine, ex. `.local/share/hookshot/save`) → même réponse ; 422 refus de l'agent (chemin hors HOME, nom invalide, agent < 8) ; 502 injoignable |
| DELETE | `machines/{id}/dirs?name=` | retire la déclaration (fichiers gardés) → même réponse ; 422 nom inconnu |
| PATCH | `machines/{id}` | `{"icon":"linux"\|"steamos"\|"batocera"\|null}` → `{id,icon,icon_set}` ; 400 icône ; 404 inconnue |
| DELETE | `machines/{id}` | `{"removed":id}` ; 404 inconnue ; 422 `game-host` |
| GET | `status[?id=<image>]` | `{machines:[{…,os,icon,icon_set}], games:[{game, reserve, check, check_by, machines:[{id,name,state,fingerprint,newest,size,message}]}]}` |
| POST | `pull` | `{"source":id, "games":[…]}` ou `{"source":id, "all":true}` → `{"task":id}` |
| POST | `push` | `{"source"?:id, "targets":[id…], "games":[…]}` ou `"all":true` → `{"task":id}` ; sans source : depuis le manager |
| POST | `delete` | `{"games":[…], "machines":[id…], "manager":bool}` → `{"task":id}` ; 422 : jeu invalide ou en double, machine en double, ni machine ni manager ; 404 machine inconnue |

`"source":"auto"` (pull et push) : chaque jeu vient de sa machine « plus
avancée ». Push : seules les cibles « en retard » reçoivent le jeu ; une cible
« absente » ou déjà à la même version est sautée ; avec une source explicite,
toutes les cibles reçoivent (c'est ainsi qu'on installe sur une machine) ; un jeu
identique, ou seulement sur le manager, n'est pas transféré.

`games` (noms de jeux, sans image requise : un jeu installé sur une seule machine peut ne pas être dans la bibliothèque) et `all` s'excluent (422 sinon ; nom invalide : 422) ; source inconnue ou cible inconnue : 404 ;
source parmi les cibles, cible en double, jeu absent du manager ou manager
vide : 422. Le jeu d'une image est son nom sans `.wsquashfs` ni `.pc`.

## Installer un jeu sur une machine

Une image `.wsquashfs` de la bibliothèque s'envoie sur les machines associées
(les mêmes que l'onglet Sauvegardes), via leur agent (`/v1/games/*`, agent v6),
et s'en désinstalle. Rien n'est créé sur la machine au-delà de l'image : le
raccourci de lancement reste à ajouter à la main.

- **Routes** : `GET /api/install/status` (par machine : joignable, dossier des
  jeux, espace libre, jeux, envois partiels) ; `POST /api/install {ids, machines}`
  (une tâche par image) ; `POST /api/uninstall {names, machines}` (une tâche par
  nom de jeu) ; `POST /api/install/machines/{id}/dir {dir}`. Les tâches sont
  de type `install` ; le résultat par machine est dans `params.result`.
- **Dossier des jeux** : deviné par l'agent, modifiable (`dir` vide : retour au
  dossier deviné). L'agent n'accepte qu'un dossier dans son HOME : sinon 400, avec
  son message. `set` indique si le dossier a été choisi à la main.
- **Envoi** : par blocs de 256 Mio (PUT à partir d'un décalage). Avant l'envoi,
  l'espace libre doit couvrir le reste de l'image plus 1 Gio, sinon rien n'est
  envoyé. L'empreinte SHA-256 est vérifiée par la machine au commit.
- **Reprise** : une coupure ou un décalage différent relance l'envoi après 1, 2,
  4, 8, puis 16 s (5 reprises au plus). La machine dit ce qu'elle a déjà reçu ;
  l'empreinte est recalculée depuis le début de l'image jusqu'à cet endroit. Une
  empreinte refusée au commit recommence un envoi complet, une fois.
- **Déjà installé** : même nom et même taille → réussite sans envoi. Une autre
  version → échec (la désinstaller d'abord).
- **Désinstallation** : jeu monté ou en cours (409) → échec pour cette machine
  seulement ; pas installé → réussite sans effet.
- **Agent trop ancien** (404/405 sans détail) : `wsquashfs-launcher --update`
  sur la machine, puis relancer son agent.

## Configuration

Variables `WSQUASHFS_MGR_*` (ou fichier `backend/.env`) :

| Variable | Défaut | Rôle |
|---|---|---|
| `ROMS_DIRS` | — | dossiers de roms de base (ex. `/roms`) ; le choix fait dans « Réglages » l'emporte |
| `SAVES_DIR` | `~/.local/share/wsquashfs/saves` | sauvegardes du lanceur |
| `LAUNCHER_CACHE` | `$WSQUASHFS_CACHE`, sinon `~/.cache/wsquashfs` | cache du lanceur : verrous par jeu (`locks/`) et montages, pour reconnaître un jeu en cours, même en préparation |
| `TMP_DIR` | — (à côté de l'image) | dossier de travail imposé ; par défaut `.wsquashfs-manager/` à côté de chaque image, même disque |
| `STATE_DIR` | `~/.local/state/wsquashfs-manager` | état de la file de tâches |
| `REBUILD_MODE` | `auto` | `overlay` (FUSE), `extract` (repli) ou `auto` |
| `TASK_CONCURRENCY` | `1` | reconstructions simultanées |
| `OLD_RETENTION_DAYS` | — (manuelle) | suppression automatique des `.old` |
| `HOST`, `PORT`, `PASSWORD` | `127.0.0.1`, `8765`, — | accès (voir ci-dessous) |

## Création : dossiers → images

Vue « Dossiers à empaqueter » : les dossiers `*.pc` et `*.wine` des dossiers
`roms/`. Pour une sélection, « Analyser et empaqueter… » :

1. **analyse** de chaque dossier (lecture seule) : prefix (`system.reg`,
   `config_info`), moteur (Unreal, Unity, TeknoParrot), exécutables classés
   par score — installeurs, redistribuables, rapporteurs de plantage, dossiers
   `_crack`/`_original files`, bandes-son, serveurs écartés —, en-têtes PE
   (32/64 bits, imports, version), règles `rules/rules.yaml` ;
2. **proposition d'autorun** justifiée valeur par valeur, avec niveau de
   confiance ; l'autorun existant est validé et recommandé s'il est correct.
   Exécutable modifiable (liste des candidats), texte modifiable. Pour
   Unreal, le lanceur racine est préféré au `-Shipping.exe`, comme dans les
   autoruns existants ;
3. **empaquetage** dans la file : `<jeu>.pc` → `<jeu>.wine` (option),
   `mksquashfs` vers `<jeu>.wsquashfs.part` avec l'autorun choisi **injecté**
   (le dossier n'est pas modifié), vérification (arborescence identique au
   dossier, autorun relu), puis `<jeu>.wsquashfs`. Une image existante n'est
   jamais écrasée. Le dossier est gardé, ou supprimé après vérification si
   l'option est cochée ; « Supprimer les dossiers déjà empaquetés » le fait
   plus tard, après contrôle de l'image.

Calibrage sur les 76 autoruns écrits à la main de `roms/win` : même
exécutable dans 70 cas (dont 9 où l'autorun existant l'écrit entre guillemets
simples). Les 6 autres : un dossier sans jeu (Baldur's Gate 3) et des choix
propres au jeu (`START.exe` de Xenoverse 2, `Launcher.exe` de FlatOut, RDR2,
PAC-MAN, `-Shipping` de Horizon Chase), pour la future base de connaissances.

`CMD` : seuls les guillemets **doubles** sont compris par le lanceur
(`CMD="Mon Jeu.exe"`) ; `CMD='Mon Jeu.exe'` est signalé.

## Reconstruction d'une image

Une image squashfs ne se modifie pas en place (SPEC § 3.1) :

1. **overlay** si `/dev/fuse`, `squashfuse` et `fuse-overlayfs` sont là : l'image
   est montée en lecture seule, les changements vont dans une couche
   `fuse-overlayfs`, `mksquashfs` relit la vue fusionnée — rien n'est extrait ;
   sinon **extraction** complète (≈ 2 × la taille décompressée) ; le dossier
   de travail est `.wsquashfs-manager/` à côté de l'image (même disque),
   supprimé ensuite ;
2. la nouvelle image est construite en `<jeu>.wsquashfs.part` à côté de
   l'original, après contrôle de l'espace disque ;
3. vérification : table lisible, arborescence identique à celle attendue,
   fichiers écrits relus à l'octet près ;
4. échange : l'original devient `<jeu>.wsquashfs.old`, refusé si le jeu a été
   lancé ou l'image modifiée entre-temps.

L'`.old` n'est supprimé que sur « Valider » (ou après `OLD_RETENTION_DAYS`) ;
« Revenir à l'ancienne » le remet en place. Une image qui a déjà un `.old`
n'est pas reconstruite.

Les reconstructions passent par une file persistante (`STATE_DIR/tasks.json`) :
une tâche par image à la fois, priorité basse (`nice`/`ionice`), progression,
annulation ; une tâche interrompue par un redémarrage est remise en attente.

### Fichiers et registre

« Fichiers / registre… » ajoute, remplace ou supprime des fichiers et pose des
valeurs de registre (`user.reg`/`system.reg` du prefix, à la racine ou dans
`pfx/`). Variables : `{exe_dir}` et `{exe_name}` (exécutable de `CMD`, résolu
comme le lanceur, casse de l'image), `{name}`, `{system}`. Le préréglage
**relais fakeping** pose `{exe_dir}/dinput8.dll` et
`[Software\Wine\AppDefaults\{exe_name}\DllOverrides] "dinput8"="native,builtin"`.

Wine réécrit `user.reg` à chaque partie dans la couche de sauvegardes
(`SAVES_DIR/<jeu>`), qui masque celui de l'image : la valeur y est donc posée
aussi. Un fichier ajouté qui existe déjà dans les sauvegardes est signalé (il
masquerait le nouveau). Une opération rejouée ne reconstruit pas une image déjà
à jour.

L'autorun est relu et réécrit octet pour octet : fins de ligne CRLF/LF,
commentaires, ordre des lignes et encodage (UTF-8, sinon latin-1) conservés.

## Accès

Par défaut l'API n'écoute que sur `127.0.0.1`. Pour l'ouvrir au réseau, poser
**à la fois** `WSQUASHFS_MGR_HOST=0.0.0.0` et `WSQUASHFS_MGR_PASSWORD=…` (le
lancement est refusé sans mot de passe) : l'application lit et réécrit les
images. L'interface demande le mot de passe et envoie ensuite un jeton dérivé
dans l'en-tête `x-wsfs-token`.

Sur un réseau domestique de confiance, on peut renoncer au mot de passe en
posant `WSQUASHFS_MGR_ALLOW_NO_PASSWORD=1` : toute machine du réseau peut
alors modifier et supprimer des images. Le template Unraid ne pose pas ce
réglage : le mot de passe y reste obligatoire.

Le service doit tourner sous l'utilisateur propriétaire des images et des
sauvegardes (`arcade` sur SteamBox) : il n'élève pas ses privilèges.

## API

Les images sont désignées par leur identifiant `<système>/<nom>` en paramètre
(`?id=arcade/gticlub`) : deux systèmes peuvent contenir une image du même nom.

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/api/library[?refresh=true]` | ludothèque |
| GET | `/api/image?id=` · `/api/image/files?id=&q=` | fiche, arborescence |
| GET | `/api/image/autorun?id=` · `/api/autorun/keys` | autorun, aide des clés |
| POST | `/api/image/autorun/preview?id=` · `/api/image/autorun?id=` | vérifier, reconstruire |
| POST | `/api/image/old/validate?id=` · `/api/image/old/restore?id=` | `.old` |
| POST | `/api/image/saves/reset?id=` | sauvegardes → `<jeu>.avant-<date>` |
| POST | `/api/game_host/maintenance` | lanceur sur la machine de jeu : corps `{"action", "id"?}` ; `list`, `umount`, `clean`, `saves` (sans jeu) ; `save` (jeu requis) ; `sync` (jeu facultatif) ; `sync-status` (sans jeu) |
| POST | `/api/mass/autorun[/preview]` · `/api/mass/files[/preview]` | masse : autorun, fichiers/registre |
| POST | `/api/mass/verify` · `/api/mass/old/validate` · `/api/mass/saves/reset` | masse : intégrité, `.old`, sauvegardes |
| GET | `/api/folders` · `/api/folders/analyze?id=&exe=` | dossiers à empaqueter, analyse |
| POST | `/api/folders/pack` · `/api/folders/delete` | empaquetage en batch, suppression des dossiers empaquetés |
| GET/POST | `/api/tasks[?image=]` · `/api/tasks/{id}` · `/api/tasks/{id}/cancel` | file |
| GET/POST/PATCH/DELETE | `/api/saves/machines` · `/api/saves/status[?id=]` · `/api/saves/pull` · `/api/saves/push` · `/api/saves/delete` | sauvegardes entre machines (voir ci-dessus) |
| GET/POST | `/api/install/status` · `/api/install` · `/api/uninstall` · `/api/install/machines/{id}/dir` | installer ou désinstaller un jeu sur les machines (voir ci-dessus) |

## Structure

```
backend/app/
├── main.py              # API FastAPI, démarrage de la file
├── config.py            # réglages (WSQUASHFS_MGR_*)
├── models.py
└── services/
    ├── scan.py          # scan + cache, lecture sans montage (unsquashfs)
    ├── autorun.py       # lecture/écriture ligne à ligne, validations, opérations
    ├── rebuild.py       # reconstruction overlay / extraction, vérification, .old
    ├── tasks.py         # file de tâches persistante
    ├── operations.py    # tâches : autorun, fichiers/registre, vérification (aperçu = exécution)
    ├── registry.py      # édition des .reg Wine (user.reg, system.reg)
    ├── analyze.py       # analyse d'un dossier, PE, choix de l'exécutable, proposition
    └── rules.py         # moteur de règles YAML (génération, étape 5)
backend/rules/rules.yaml # règles « fichier → réglage », éditables
frontend/index.html      # interface (sans build)
```

## Décisions en attente (SPEC § 7)

1. Web ou bureau — **web** retenu de fait.
2. Où elle tourne — **conteneur Docker séparé** retenu (voir « Docker »),
   avec `/dev/fuse` : mode overlay.
3. Rétention des `.old` : manuelle par défaut, `OLD_RETENTION_DAYS` sinon.
4. (Plus tard) fichier `<jeu>.wsquashfs.autorun` à côté de l'image, lu par le
   lanceur, ignoré par Batocera.

## Dépendances système

`squashfs-tools` (mksquashfs/unsquashfs), `squashfuse`, `fuse-overlayfs`.
Wine n'est pas nécessaire : l'analyse des exécutables (`pefile`) et du
registre se fait en Python.
