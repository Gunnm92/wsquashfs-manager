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

**Mode** (Réglages, section « Mode ») : *autonome* — le manager ne fait que construire
(Images, Dossiers) : aucun client, pas d'écoute des clients, pas de sauvegardes
ni d'installation ; les routes qui touchent un client répondent 409. *connecté* —
sauvegardes entre clients, installation, lancement d'essai. Sans choix enregistré,
le mode est connecté dès qu'un client est associé ou qu'un lanceur local est
configuré, autonome sinon (`PUT /api/config/mode {"mode"}`).

Les systèmes se choisissent dans l'interface (Réglages → Systèmes à gérer),
parmi ceux du dossier de base `WSQUASHFS_MGR_ROMS_DIRS` (`/roms` dans le
conteneur) : « Tout le dossier » (les systèmes ajoutés plus tard compris) ou
une sélection de systèmes ; ce choix l'emporte sur la variable. D'autres
chemins peuvent s'ajouter : le système est déduit comme le fait le lanceur. Un serveur resté sur une ancienne version répond
« API introuvable ».

## Docker (déploiement)

Conteneur séparé de SteamBox (SPEC § 7, point 2), image
`registry.elfenn.eu/wsquashfs-manager` (Debian trixie, squashfs-tools 4.7
compilé ; ni FUSE ni privilège), déployé par un **template Unraid** :
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
| Port des clients | 8790 | HTTPS : sauvegardes envoyées par les clients (synchro automatique) |
| Roms | `/roms` | dossier des roms Batocera ; les systèmes se cochent dans Réglages |
| Sauvegardes du lanceur | `/saves` | sauvegardes de wsquashfs-launcher dans SteamBox |
| Appdata | `/data` | file de tâches, caches, réglages |
| Mot de passe | `WSQUASHFS_MGR_PASSWORD` | obligatoire : sans lui, le conteneur refuse de démarrer |
| PUID / PGID (avancé) | 99 / 100 | propriétaire des images créées (nobody:users = `arcade` de SteamBox) |

**Client d'essai** (menu d'un client du bandeau Clients de Sauvegardes, « Utiliser pour les essais ») : là où tourne
`wsquashfs-launcher`, pour le **lancement d'essai** (le jeu tient-il,
fenêtre minimisée, erreurs connues, capture ; refusé si un jeu .wsquashfs
tourne déjà), l'état du lanceur (`--doctor`) et les runners installés
(réparation des liens du runner). Facultative : tout le reste fonctionne
sans. Le diagnostic et la maintenance du lanceur sont dans ce même menu (« Diagnostic du lanceur… », « Maintenance… »),
utilisables une fois le client d'essai enregistré et joignable : lister,
démonter, nettoyer, copier les sauvegardes (tous les jeux), synchroniser les
sauvegardes avec les autres clients associés, voir l'état de la synchro.
La fiche d'un jeu a « Copier les sauvegardes » et « Synchroniser » (ce jeu), et
« Sauvegardes entre clients », qui ouvre l'onglet Sauvegardes filtré sur le jeu.
L'association d'un client se fait une fois, en ligne de commande, sur les
deux clients : `wsquashfs-launcher --pair` sur l'autre, puis
`wsquashfs-launcher --sync wsq://…` sur celle-ci (le manager ne l'expose pas ;
le code ne sert qu'une fois, voir ci-dessous).

Dans l'interface, le client d'essai est un client associé (`game_host` = `agent`).
Le lanceur local (`game_host` = `local`, variable `WSQUASHFS_MGR_GAME_HOST=local` :
lanceur installé sur le serveur du manager, avec un écran) n'y est plus proposé.

| Valeur de `game_host` | Usage |
|---|---|
| `off` | défaut d'une installation neuve |
| `local` | lanceur installé sur le serveur du manager, avec un écran ; le service tourne sous l'utilisateur de la session graphique |
| `agent` | **Un client déjà associé** (onglet Sauvegardes : SteamBox, AIBox, Deck…) se choisit dans la liste « Client associé » : son adresse, son jeton et son certificat sont repris, sans nouveau code (`PUT /api/config/game_host/machine {"id"}`). **Autre client** : sur le client d'essai, ou dans son conteneur (ex. SteamBox) : `wsquashfs-launcher --pair` affiche le code `wsq://…` et attend qu'il soit collé dans **Associer** (le certificat est vérifié contre le code, puis le code est échangé contre un jeton propre à ce manager ; le code ne sert qu'une fois, pendant que `--pair` attend ; un client se révoque côté client par `wsquashfs-launcher --pair forget <machine>` ou `--pair reset`) ; ensuite `wsquashfs-launcher --agent` lance l'agent en arrière-plan (`--agent stop` l'arrête ; rien ne le démarre tout seul, aucun port ouvert en dehors de `--agent`). Voir `docs/agent.md` du lanceur. |

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

## Sauvegardes entre clients

Le manager tourne en permanence : il garde une copie des sauvegardes de partie
(`wsquashfs-launcher`, `/v1/saves/*` de l'agent) et synchronise celles d'une
client **source** (celle qui est la plus avancée) vers les autres clients.
À l'écran, cette copie est la colonne et la carte **Manager**.

- **Clients** : `POST /api/saves/machines {code}` associe un client par son
  code `wsq://…` : le certificat est contrôlé contre l'empreinte du code, puis
  le secret du code est échangé une seule fois (`POST /v1/pair`, agent version 7)
  contre le jeton durable de ce manager ; le nom est le `host` de l'agent. Une
  même empreinte ou une même adresse remplace l'entrée. Le secret, le jeton et
  le certificat ne sont jamais renvoyés. Le client de
  jeu en mode agent apparaît d'office (identifiant `game-host`, non
  supprimable) sauf si un client de la liste a la même adresse.
- **Icône** : `os` de `/v1/info` (ID de `/etc/os-release` : `steamos`,
  `batocera`, sinon `linux`), remplaçable à la main (`PATCH`, champ `icon` de
  le client ; `saves_game_host_icon` pour le client d'essai).
- **Copie du manager** : `state_dir/saves-reserve/<jeu>/` : `current.tar.gz`,
  `meta.json` (empreinte, newest, taille, profil, source, date de réception) ;
  avant chaque remplacement, l'ancienne version devient
  `AAAAMMJJ-HHMMSS.tar.gz` (3 gardées). Écritures atomiques (durable.py).
- **Pull** (client → manager), **push** (manager → cibles), **push_from**
  (pull puis push des seuls jeux récupérés). Un client occupé (jeu en
  cours, 409) n'est pas touchée : le résultat le dit, par jeu et par client.
- **États** (`GET /api/saves/status`) : par jeu et par client, avec la taille
  et la date. un client sans le jeu est « absente » et ce
  n'est pas une différence (AIBox ou le Deck n'ont qu'une partie des jeux).
  « présente » : un seul client l'a. « identique » : les clients qui l'ont
  (deux au moins) ont la même version. Sinon : « plus avancée » (la plus
  récente parmi les tailles proches de la plus lourde, écart ≤ 10 % : la source suggérée) ou « en retard ». Les
  clients non joints sont « injoignable » (ils ne comptent pas). `check` : la suggérée n'est
  pas la plus récente (`check_by` = la plus récente), à vérifier avant de
  synchroniser. Chaque client n'est interrogée qu'une fois.
- **Suppression** (`POST /api/saves/delete {games, machines, manager}`) : sur
  les clients cochés, l'agent archive la sauvegarde dans son dossier des
  backups puis l'efface (`DELETE /v1/saves/tar?game=`, route de l'agent v5) ;
  sur le manager, la version courante devient une version datée (3 gardées) et
  le jeu n'est plus dans la copie. Un client occupé (409), injoignable, ou
  un agent plus ancien (404/405 : `--update` demandé) échoue seule. Le jeu doit
  être arrêté. Rien à supprimer sur un client : réussite sans effet.
- **Dossiers déclarés et jeux Switch** (agent v8) : en plus des jeux, les agents
  listent des dossiers déclarés (`dossier:<nom>`) et les sauvegardes Eden, jeu par
  jeu (`switch:<TitleID>`) ; `status` leur donne `label` (nom lisible venu du
  client, sinon déduit) et `kind` (`dossier`, `switch` ou `jeu`). Pull, push,
  suppression et copie du manager les traitent comme des jeux. Les dossiers se
  déclarent par client (menu de la carte → « Dossiers synchronisés… ») :
  Un dossier se propage comme une sauvegarde : le manager garde son chemin (champ
  `dir` de la liste de l'agent, ou en-tête `X-Wsq-Dir` d'un envoi) et le transmet
  en l'envoyant ; un client qui ne l'a pas le déclare à la réception.
  `GET|PUT|DELETE /api/saves/machines/{id}/dirs` relaient `/v1/saves/dirs` de
  l'agent (422 : son refus, ex. chemin hors de son HOME, ou agent trop ancien ;
  502 : injoignable).
- **Tâches** : pull, push et suppression passent par la file (tâche `saves`,
  sans image) ; le détail par jeu et par cible est dans `params.result` de la tâche.
- **Écran** : l'onglet **Sauvegardes**, premier onglet, ouvert au démarrage
  (Dossiers et Aide restent mémorisés). Aide intégrée : page
  « Sauvegardes ».
  - Bandeau : une carte par client (icône, pastille verte joignable ou rouge
    injoignable, nombre de jeux) et une carte Manager. Un clic sur un client
    n'affiche que ses sauvegardes, sa colonne en premier (second clic : tout) ;
    le chevron à droite de la carte propose son icône et « Oublier » (pas pour
    le client d'essai). « Associer
    un client » prend le code `wsq://` ; sur le client :
    `wsquashfs-launcher --pair`, puis `wsquashfs-launcher --agent` pour
    qu'il reste joignable. Sans client, un encadré explique ces étapes.
  - Tableau en sections par plateforme : système Batocera de l'image du jeu
    (dossier sous `roms/` qui contient `<jeu>.wsquashfs` ou `.squashfs` ; à
    défaut un nom rapproché, sans casse ni suffixe entre parenthèses, s'il ne
    désigne qu'un système) — Jeux PC (`win`) d'abord, puis Arcade PC, Taito
    Type X²…, Switch, Dossiers, et « Autres (image introuvable) ». Champ
    `system` du statut ; parcours de `roms/` gardé 5 min. Titres seulement s'il
    y a plusieurs sections ; lignes triées par nom lisible. Un clic sur le titre replie ou
    rouvre la section (mémorisé dans le navigateur) ; « Tout sélectionner » ne
    prend que les lignes visibles. Vignette de chaque ligne : celle de l'image
    pour un jeu PC de la bibliothèque ; pour un jeu Switch, la jaquette de la
    fiche du `gamelist.xml` de `roms/switch` ou `roms/switch2` qui porte son
    TitleID (`GET /api/saves/art?game=switch:<TitleID>`) ; sinon les initiales.
  - Un client injoignable (éteint, en veille) n'a pas de colonne ; sa carte
    reste dans le bandeau, en rouge.
  - Tableau jeux × clients : ★ plus avancée, identique, en retard, — absente,
    seule copie, ? injoignable, avec taille · date ; colonne Manager ;
    ⚠ à vérifier. Filtres : Tous, Différences, Identiques, Sur le manager ; le
    filtre par client passe par le bandeau (ci-dessus).
  - Une ligne : « Sync X → Y » (la plus avancée vers les clients en retard)
    et son chevron : sync depuis un client précis, « Installer sur Z » (client
    qui n'a pas le jeu, choix explicite), copier vers le manager, sync depuis
    le manager (clients qui ont le jeu), et « Supprimer… ».
  - « Supprimer… » (menu de la ligne, ou bouton de la barre de sélection) ouvre
    un dialogue : une case par client joignable et une pour le manager, cochées
    si le jeu (ou un des jeux) y a une sauvegarde, sinon grisées ; « Supprimer »
    reste désactivé tant que rien n'est coché.
  - Sélection : source (la plus avancée, jeu par jeu ; un client ; le
    manager), cibles, « Sync » (récapitulatif avant envoi), « Copier vers le
    manager ». Une opération à la fois ; le résultat liste les échecs par jeu et
    par client.
  - Liste des images : une icône dans « Sauveg. » ouvre l'onglet filtré sur le
    jeu, dès qu'un client est associé.

Routes (toutes sous `/api/saves/`) :

| Méthode | Chemin | Corps / réponse |
|---|---|---|
| GET | `machines` | `{"machines":[{id,name,url,fingerprint,kind,removable,icon}]}` |
| POST | `machines` | `{"code":"wsq://…"}` → `{id,name,url,fingerprint,replaced}` ; 422 code illisible, déjà utilisé ou expiré, empreinte différente, agent trop ancien (version < 7) ; 502 injoignable |
| GET | `machines/{id}/dirs` | `{dirs:[{name,path}], eden}` : dossiers déclarés sur le client, dossier d'Eden (`""` sinon) |
| PUT | `machines/{id}/dirs` | `{"name","path"}` (chemin à partir du dossier personnel du client, ex. `.local/share/hookshot/save`) → même réponse ; 422 refus de l'agent (chemin hors HOME, nom invalide, agent < 8) ; 502 injoignable |
| DELETE | `machines/{id}/dirs?name=` | retire la déclaration (fichiers gardés) → même réponse ; 422 nom inconnu |
| PATCH | `machines/{id}` | `{"icon":"linux"\|"steamos"\|"batocera"\|null}` → `{id,icon,icon_set}` ; 400 icône ; 404 inconnue |
| DELETE | `machines/{id}` | `{"removed":id}` ; 404 inconnue ; 422 `game-host` |
| GET | `status[?id=<image>]` | `{machines:[{…,os,icon,icon_set}], games:[{game, reserve, check, check_by, machines:[{id,name,state,fingerprint,newest,size,message}]}]}` |
| POST | `pull` | `{"source":id, "games":[…]}` ou `{"source":id, "all":true}` → `{"task":id}` |
| POST | `push` | `{"source"?:id, "targets":[id…], "games":[…]}` ou `"all":true` → `{"task":id}` ; sans source : depuis le manager |
| POST | `delete` | `{"games":[…], "machines":[id…], "manager":bool}` → `{"task":id}` ; 422 : jeu invalide ou en double, client en double, ni client ni manager ; 404 client inconnu |

`"source":"auto"` (pull et push) : chaque jeu vient de son client « plus
avancée ». Push : seules les cibles « en retard » reçoivent le jeu ; une cible
« absente » ou déjà à la même version est sautée ; avec une source explicite,
toutes les cibles reçoivent (c'est ainsi qu'on installe sur un client) ; un jeu
identique, ou seulement sur le manager, n'est pas transféré.

`games` (noms de jeux, sans image requise : un jeu installé sur un seul client peut ne pas être dans la bibliothèque) et `all` s'excluent (422 sinon ; nom invalide : 422) ; source inconnue ou cible inconnue : 404 ;
source parmi les cibles, cible en double, jeu absent du manager ou manager
vide : 422. Le jeu d'une image est son nom sans `.wsquashfs` ni `.pc`.

### Synchro automatique à la sortie d'un jeu

Le manager écoute en HTTPS sur le port 8790 (`MACHINE_PORT`) et parle le même
protocole que l'agent de wsquashfs-launcher : un client qui le connaît lui
envoie sa partie **à la sortie d'un jeu**, quand il est joignable. Rien n'est
récupéré automatiquement au lancement ; un manager absent ou injoignable ne
gêne pas le jeu (une ligne d'information sur le client).

- **Réglage** : Réglages → « Synchro automatique des clients » → « Adresse du manager pour les clients »
  (`PUT /api/config/machine_url {"url"}`, `https://hôte:port` ou vide). Pré-remplie
  avec l'adresse de cette page et le port 8790 ; l'empreinte du certificat du
  manager est affichée à côté (`machine_fingerprint` dans `GET /api/config`).
- **Déclaration** : à l'association d'un client (agent version 9 ou plus), puis
  à chaque état de la synchro si le client ne connaît pas encore ce manager
  (`PUT /v1/saves/manager` sur l'agent). Le manager crée un jeton par client et
  n'en garde que l'empreinte SHA-256 (`manager_token_sha256` dans `config.json`).
  Un client plus ancien n'est pas déclaré.
- **Client** : `wsquashfs-launcher --sync auto on` (persistant, à faire une fois
  par client) ; `--sync auto off` l'arrête. La carte du client porte alors la
  pastille « auto ».
- **Ce que reçoit le manager** : la partie, archivée comme une version
  précédente (3 gardées). Pendant cet envoi, le manager ne renvoie rien à la
  client ; l'état de chaque client (dernière empreinte échangée) est dans
  `meta.json` de la réserve du jeu (`states`).
- **Mise à jour automatique** : toutes les `auto_push_interval` secondes (Réglages
  → Synchro automatique des clients, 120 par défaut, 0 : désactivée ; `config.json`), et juste après un
  envoi, le manager pousse sa version vers les clients dont la synchro auto est
  activée (`auto` de `GET /v1/info`) et qui sont en retard : celles dont l'état
  enregistré correspond encore à leur empreinte actuelle. Un client modifié
  depuis son dernier échange est « à départager » et n'est pas touchée ; une
  client occupé (jeu en cours) est retentée au passage suivant. Chaque
  poussée va au journal des tâches (« mise à jour auto »). Les clients dont la
  synchro auto est activée reçoivent donc aussi la version la plus récente du
  manager ; leur agent doit être lancé.
- **Routes du client** (port 8790 seulement, jamais l'interface) : `GET /v1/info`,
  `GET /v1/saves/list`, `GET|PUT /v1/saves/tar?game=`, `POST /v1/saves/state`.
  Authentification : `Authorization: Bearer <jeton>` (sinon 401) et en-tête
  `X-Wsq-Machine` (sinon 400). Les réponses d'erreur ont la même forme que
  celles de l'agent (`{"detail": …}`).

## Installer un jeu sur un client

Une image `.wsquashfs` de la bibliothèque s'envoie sur les clients associés
(les mêmes que l'onglet Sauvegardes), via leur agent (`/v1/games/*`, agent v6),
et s'en désinstalle. Rien n'est créé sur le client au-delà de l'image : le
raccourci de lancement reste à ajouter à la main.

- **Routes** : `GET /api/install/status` (par client : joignable, dossier des
  jeux, espace libre, jeux, envois partiels) ; `POST /api/install {ids, machines}`
  (une tâche par image) ; `POST /api/uninstall {names, machines}` (une tâche par
  nom de jeu) ; `POST /api/install/machines/{id}/dir {dir}`. Les tâches sont
  de type `install` ; le résultat par client est dans `params.result`.
- **Dossier des jeux** : deviné par l'agent, modifiable (`dir` vide : retour au
  dossier deviné). L'agent n'accepte qu'un dossier dans son HOME : sinon 400, avec
  son message. `set` indique si le dossier a été choisi à la main.
- **Envoi** : par blocs de 256 Mio (PUT à partir d'un décalage). Avant l'envoi,
  l'espace libre doit couvrir le reste de l'image plus 1 Gio, sinon rien n'est
  envoyé. L'empreinte SHA-256 est vérifiée par le client au commit.
- **Reprise** : une coupure ou un décalage différent relance l'envoi après 1, 2,
  4, 8, puis 16 s (5 reprises au plus). Le client dit ce qu'il a déjà reçu ;
  l'empreinte est recalculée depuis le début de l'image jusqu'à cet endroit. Une
  empreinte refusée au commit recommence un envoi complet, une fois.
- **Déjà installé** : même nom et même taille → réussite sans envoi. Une autre
  version → échec (la désinstaller d'abord).
- **Désinstallation** : jeu monté ou en cours (409) → échec pour ce client
  seulement ; pas installé → réussite sans effet.
- **Agent trop ancien** (404/405 sans détail) : `wsquashfs-launcher --update`
  sur le client, puis relancer son agent.

## Configuration

Variables `WSQUASHFS_MGR_*` (ou fichier `backend/.env`) :

| Variable | Défaut | Rôle |
|---|---|---|
| `ROMS_DIRS` | — | dossiers de roms de base (ex. `/roms`) ; le choix fait dans « Réglages » l'emporte |
| `SAVES_DIR` | `~/.local/share/wsquashfs/saves` | sauvegardes du lanceur |
| `LAUNCHER_CACHE` | `$WSQUASHFS_CACHE`, sinon `~/.cache/wsquashfs` | cache du lanceur : verrous par jeu (`locks/`) et montages, pour reconnaître un jeu en cours, même en préparation |
| `TMP_DIR` | — (à côté de l'image) | dossier de travail imposé ; par défaut `.wsquashfs-manager/` à côté de chaque image, même disque |
| `STATE_DIR` | `~/.local/state/wsquashfs-manager` | état de la file de tâches |
| `REBUILD_MODE` | `auto` | `stream` (sans montage ni extraction, squashfs-tools 4.7+), `extract` (repli) ou `auto` |
| `TASK_CONCURRENCY` | `1` | reconstructions simultanées |
| `OLD_RETENTION_DAYS` | — (manuelle) | suppression automatique des `.old` |
| `HOST`, `PORT`, `PASSWORD` | `127.0.0.1`, `8765`, — | accès (voir ci-dessous) |
| `MACHINE_PORT` | `8790` | HTTPS où les clients envoient leurs sauvegardes (`0` : désactivé) ; certificat auto-signé dans `STATE_DIR/tls/` |
| `MACHINE_HOST` | `0.0.0.0` | adresse d'écoute de ce port (les clients doivent l'atteindre) |
| `MACHINE_URL` | — | adresse du manager vue par les clients, ex. `https://192.168.1.10:8790` ; réglée aussi dans Réglages |
| `UI_URL` | `http://<hôte de MACHINE_URL>:<PORT>` | adresse de l'interface donnée aux clients (« Ouvrir le manager » de `wsquashfs-launcher --tray`) ; à régler derrière un proxy |

## Personnaliser l'apparence

L'interface charge `assets/app.css` (qui importe les feuilles de `assets/css/`, par couche), puis `custom.css` si ce fichier existe dans le dossier d'état
(`STATE_DIR` : `~/.local/state/wsquashfs-manager`, ou `WSQUASHFS_MGR_STATE_DIR`). Il est relu à chaque
chargement de la page : un rechargement suffit après modification.

Les règles de `custom.css` ne sont dans aucune couche CSS : elles l'emportent sur l'interface, quelle que
soit la spécificité. Exemple :

```css
/* custom.css : accent rose et boutons carrés */
:root {
  --accent: #e04f8a;
  --radius-pill: 12px;
}
```

Jetons principaux, définis dans `:root` (`assets/css/tokens.css`, couche `tokens`) :

- couleurs : `--accent`, `--accent2` (second accent), `--err`, `--bg`, `--surface`, `--s2`, `--s3`, `--text`, `--muted`, `--faint`
- rayons : `--radius-xs` à `--radius-xl`, `--radius-panel` (fenêtres), `--radius-pill` (boutons, pilules)
- transitions : `--dur-fast`, `--dur-base`, `--dur-med`, `--dur-slow`, `--ease-out`, `--ease-spring`
- tons des pastilles (classes `.t-*`) : `--tone-sage-bg/fg`, `--tone-accent-bg/fg`, `--tone-err-bg/fg`, `--tone-neutral-bg/fg`
- icônes : `--ico-xs`, `--ico-sm`, `--ico`, `--ico-lg`

## Création : dossiers → images

Vue « Dossiers » : les dossiers `*.pc` et `*.wine` des dossiers
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

1. **flux** (squashfs-tools 4.7 et plus) : `unsquashfs -pf -` décrit toute
   l'image, données comprises ; le manager en retire les chemins supprimés ou
   remplacés, ajoute les nouveaux (définitions « pseudo ») et
   `mksquashfs - … -pf -` reconstruit — ni montage FUSE ni extraction
   (Luigi's Mansion, 2,5 Go : 34 s contre 38 à 40 s avec l'ancien montage
   squashfuse) ; sinon **extraction** complète (≈ 2 × la taille décompressée).
   Le dossier de travail est `.wsquashfs-manager/` à côté de l'image (même
   disque), supprimé ensuite ;
2. la nouvelle image est construite en `<jeu>.wsquashfs.part` à côté de
   l'original, après contrôle de l'espace disque ;
3. vérification : table lisible, arborescence identique à celle attendue,
   fichiers écrits relus à l'octet près ;
4. échange : l'original devient `<jeu>.wsquashfs.old`, refusé si le jeu a été
   lancé ou l'image modifiée entre-temps.

L'`.old` n'est supprimé que sur « Supprimer l'ancienne version » (ou après `OLD_RETENTION_DAYS`) ;
« Restaurer l'ancienne version » le remet en place. Une image qui a déjà un `.old`
n'est pas reconstruite.

Les reconstructions passent par une file persistante (`STATE_DIR/tasks.json`) :
une tâche par image à la fois, priorité basse (`nice`/`ionice`), progression,
annulation ; une tâche interrompue par un redémarrage est remise en attente.

### Illustration (miniature des .wsquashfs)

Chaque reconstruction dépose l'illustration du jeu à la racine de l'image,
`.wsquashfs/cover` : jaquette d'EmulationStation (`gamelist.xml`), sinon
icône de l'exécutable de `CMD=`. Le fichier `.thumbnailer` installé par
`wsquashfs-launcher --setup` la montre dans Thunar, Nautilus, Nemo, Caja.

L'interface n'affiche que cette illustration intégrée : vignettes et fiche des
images lisent `.wsquashfs/cover` (`GET /api/image/art`), la vue Dossiers lit
`<dossier>/.wsquashfs/cover` (`GET /api/folders/art`). Sans elle, les initiales.
Lue sans monter l'image (`unsquashfs -cat`), mise en cache dans
`<dossier d'état>/art/embedded` ; l'absence y est gardée jusqu'à la prochaine
modification de l'image ou **Actualiser**.

Pour une image existante, **Illustration** (fiche) ou **Ajouter
l'illustration** (sélection) l'ajoute sans reconstruire : `mksquashfs` en
mode ajout écrit le fichier à la suite et ne réécrit que les tables
(Nosferatu, 180 Mo : 0,07 s), contenu et attributs étendus intacts. L'image
est modifiée en place, sans `.old` : refusé pendant une partie, et quand
`.wsquashfs/` existe déjà sans illustration (reconstruire alors). Une image
qui a déjà son illustration n'est pas touchée.
Pour remplacer l'illustration déjà intégrée, il faut une reconstruction : l'ancienne image est gardée en `.old`, et la tâche peut être programmée « cette nuit ». Le remplacement passe **toujours par un aperçu avec choix manuel** : `POST /api/mass/cover` avec `replace: true` n'accepte une image déjà illustrée que si `choices` donne le candidat choisi pour elle ; sans choix, elle est refusée dans `skipped` (« choisir l'illustration dans l'aperçu avant de remplacer »). Une image sans illustration s'ajoute comme avant, sans choix requis. L'interface n'envoie rien sans cet aperçu : la fiche (badge de l'illustration, menu « … ») ouvre un dialogue par image, la sélection un dialogue en lot (`js/art-choice.js`) ; le choix automatique est présélectionné.

**Aperçu** (`GET /api/image/art/candidates?id=<image>`) : aucune modification. Réponse :
`current` (`present`, `sha1`, `url` de l'illustration intégrée), `sgdb_configured`, `game` (`{id, name}` SteamGridDB ou `null`), `candidates` (au plus 24 grilles SteamGridDB, puis la jaquette d'EmulationStation `es` et l'icône de l'exécutable `icon` si elles existent ; chacune a `key`, `source`, `label`, `author`, `score`, `width`, `height`, `thumb`, `full` et `same_as_current`), `preferred` (la clé que le choix automatique prendrait) et, si SteamGridDB a échoué, `sgdb_error`. Les grilles sont triées : auteurs préférés d'abord (ordre des Réglages), puis note décroissante. `same_as_current` compare les octets au sha1 de l'illustration intégrée ; pour SteamGridDB, seulement si le plein format est déjà en cache. La liste SteamGridDB d'une image est gardée 5 minutes en mémoire (**Actualiser** la vide).

**Image d'un candidat** (`GET /api/image/art/candidate?id=<image>&key=<clé>&size=thumb|full`) : 404 si la clé n'est pas dans la liste de cette image, 502 si SteamGridDB est injoignable ou refuse la clé. Les images SteamGridDB sont mises en cache dans `<dossier d'état>/art/sgdb/candidates/<id de grille>-<size>.<ext>`. Seules les adresses de la liste de l'image sont suivies : la requête ne fournit jamais d'URL.

**Choix** : `POST /api/mass/cover` accepte `choices: {"<id image>": "<clé>"}` (clé `sgdb:<id de grille>`, `es` ou `icon`, sinon 422). La tâche porte `choice` et la reconstruction utilise exactement ce candidat, en pleine taille. Si le candidat ne peut plus être obtenu, la tâche échoue avec un message clair, sans repli sur une autre source. Si ses octets sont identiques à l'illustration intégrée, « Illustration déjà à jour : rien à faire ».

### Icônes et jaquettes SteamGridDB

SteamGridDB ne sert plus à l'affichage : il fournit l'illustration **ajoutée**
(reconstruction, « Ajouter l'illustration »), à partir de [SteamGridDB](https://www.steamgriddb.com)
**icônes rondes** (`kind=icon`) et **jaquettes 600×900** (`kind=cover`). Clé API : Profil › Préférences › API sur
steamgriddb.com, à coller dans **Réglages**. Sans clé, rien n'est demandé au site.

- **Auteurs préférés** (Réglages) : une image de l'un d'eux passe avant les
  autres, dans l'ordre de la liste ; sinon, la jaquette la mieux notée, ou
  l'icône la plus ronde.
- **Seuil de rondeur** (0 à 1, défaut 0,85) : une icône dont la rondeur n'atteint
  pas le seuil n'est pas retenue (la rondeur mesure la part du disque opaque et
  des coins transparents, après réduction à 64×64).
- **Cache** : `<dossier d'état>/art/sgdb` (`STATE_DIR`). L'illustration retenue
  y reste ; une absence est revalidée après 7 jours ; après une erreur réseau ou
  une clé refusée, aucune demande pendant 5 minutes. **Actualiser** la
  ludothèque revalide les absences.
- **Ordre des sources** (sélection automatique) : SteamGridDB (icône ronde ou
  jaquette) → jaquette d'EmulationStation → icône de l'exécutable de `CMD=`. Pour un
  remplacement, l'aperçu le remplace : c'est l'utilisateur qui choisit parmi les candidats.

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
posant `WSQUASHFS_MGR_ALLOW_NO_PASSWORD=1` : tout client du réseau peut
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
| POST | `/api/game_host/maintenance` | lanceur sur le client d'essai : corps `{"action", "id"?}` ; `list`, `umount`, `clean`, `saves` (sans jeu) ; `save` (jeu requis) ; `sync` (jeu facultatif) ; `sync-status` (sans jeu) |
| POST | `/api/mass/autorun[/preview]` · `/api/mass/files[/preview]` | masse : autorun, fichiers/registre |
| POST | `/api/mass/verify` · `/api/mass/cover` · `/api/mass/old/validate` · `/api/mass/saves/reset` | masse : intégrité, illustration (`choices` pour le remplacement, voir Illustration), `.old`, sauvegardes |
| GET | `/api/image/art/candidates?id=` · `/api/image/art/candidate?id=&key=&size=` | aperçu du remplacement de l'illustration : candidats et leurs images |
| GET | `/api/folders` · `/api/folders/analyze?id=&exe=` | dossiers à empaqueter, analyse |
| POST | `/api/folders/pack` · `/api/folders/delete` | empaquetage en batch, suppression des dossiers empaquetés |
| GET/POST | `/api/tasks[?image=]` · `/api/tasks/{id}` · `/api/tasks/{id}/cancel` | file |
| GET/POST/PATCH/DELETE | `/api/saves/machines` · `/api/saves/status[?id=]` · `/api/saves/pull` · `/api/saves/push` · `/api/saves/delete` | sauvegardes entre clients (voir ci-dessus) |
| PUT | `/api/config/sgdb` · POST `/api/config/sgdb/test` | clé et réglages SteamGridDB (la clé n'est jamais renvoyée) ; essai de la clé |
| GET/POST | `/api/install/status` · `/api/install` · `/api/uninstall` · `/api/install/machines/{id}/dir` | installer ou désinstaller un jeu sur les clients (voir ci-dessus) |

## Structure

```
backend/app/
├── main.py              # API FastAPI, démarrage de la file
├── config.py            # réglages (WSQUASHFS_MGR_*)
├── models.py
└── services/
    ├── scan.py          # scan + cache, lecture sans montage (unsquashfs)
    ├── autorun.py       # lecture/écriture ligne à ligne, validations, opérations
    ├── rebuild.py       # reconstruction en flux / extraction, vérification, .old
    ├── tasks.py         # file de tâches persistante
    ├── operations.py    # tâches : autorun, fichiers/registre, vérification (aperçu = exécution)
    ├── registry.py      # édition des .reg Wine (user.reg, system.reg)
    ├── analyze.py       # analyse d'un dossier, PE, choix de l'exécutable, proposition
    └── rules.py         # moteur de règles YAML (génération, étape 5)
backend/rules/rules.yaml # règles « fichier → réglage », éditables
frontend/index.html      # interface (sans build) : charge assets/app.css et le module assets/js/main.js
frontend/assets/app.css  # point d'entrée CSS : couches tokens, base, components, views, puis imports
frontend/assets/css/    # tokens.css, base.css, components.css, views/<vue>.css (un fichier par vue)
frontend/assets/js/     # modules ES natifs (sans build) : main.js démarre, chaque module déclare sa responsabilité
                        # en tête ; core.js (état, outils), api.js, library.js, detail/, saves*.js, settings.js …
```

## Décisions en attente (SPEC § 7)

1. Web ou bureau — **web** retenu de fait.
2. Où elle tourne — **conteneur Docker séparé** retenu (voir « Docker »),
   sans FUSE ni privilège (reconstruction en flux).
3. Rétention des `.old` : manuelle par défaut, `OLD_RETENTION_DAYS` sinon.
4. (Plus tard) fichier `<jeu>.wsquashfs.autorun` à côté de l'image, lu par le
   lanceur, ignoré par Batocera.

## Dépendances système

`squashfs-tools` 4.7 ou plus récent (mksquashfs/unsquashfs ; compilé dans l'image).
Wine n'est pas nécessaire : l'analyse des exécutables (`pefile`) et du
registre se fait en Python.
