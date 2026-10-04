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
| 7. Connaissances | base par jeu (nom, empreinte, exécutable imposé), ajout en un clic ; lancement d'essai à faire |

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

Les systèmes se cochent dans l'interface (bouton « Réglages »), parmi ceux
du dossier de base `WSQUASHFS_MGR_ROMS_DIRS` (`/roms` dans le conteneur) ; ce
choix l'emporte sur la variable. On peut aussi donner `roms/` entier ou un
autre chemin : le système est déduit comme le fait le lanceur. Un serveur resté sur une ancienne version répond
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

Paramètres supplémentaires du template : `--pid=host` (voir les jeux lancés
dans SteamBox et ne pas reconstruire leur image pendant une partie),
`--cap-add=SYS_ADMIN --security-opt apparmor=unconfined` (montages FUSE).
Le conteneur démarre en root, prépare `/data`, puis passe à PUID/PGID.
Le travail sur une image se fait à côté d'elle (`.wsquashfs-manager/`, même
disque), jamais dans l'appdata.

## Configuration

Variables `WSQUASHFS_MGR_*` (ou fichier `backend/.env`) :

| Variable | Défaut | Rôle |
|---|---|---|
| `ROMS_DIRS` | — | dossiers de roms de base (ex. `/roms`) ; le choix fait dans « Réglages » l'emporte |
| `SAVES_DIR` | `~/.local/share/wsquashfs/saves` | sauvegardes du lanceur |
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
| POST | `/api/mass/autorun[/preview]` · `/api/mass/files[/preview]` | masse : autorun, fichiers/registre |
| POST | `/api/mass/verify` · `/api/mass/old/validate` · `/api/mass/saves/reset` | masse : intégrité, `.old`, sauvegardes |
| GET | `/api/folders` · `/api/folders/analyze?id=&exe=` | dossiers à empaqueter, analyse |
| POST | `/api/folders/pack` · `/api/folders/delete` | empaquetage en batch, suppression des dossiers empaquetés |
| GET/POST | `/api/tasks[?image=]` · `/api/tasks/{id}` · `/api/tasks/{id}/cancel` | file |

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
