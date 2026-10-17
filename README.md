# wsquashfs-manager

Application web de gestion des images `.wsquashfs` (jeux Windows empaquetés au
format Batocera) : ludothèque, édition de l'`autorun.cmd`, montée de version,
génération automatique de l'autorun, traitement en masse.

Le manager s'appuie sur les conventions de
[wsquashfs-launcher](https://github.com/Gunnm92/wsquashfs-launcher) (qui reste
tel quel) et réutilise ses outils (`--info`, `--pack`) quand c'est possible.

Voir [SPEC.md](SPEC.md) pour la spécification complète.

## Structure

```
wsquashfs-manager/
├── SPEC.md              # spécification (source de vérité)
├── backend/             # FastAPI : scan, autorun, file de tâches
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py    # dossiers roms/, sauvegardes, temp, concurrence
│   │   ├── models/
│   │   ├── routes/
│   │   ├── services/    # scan, autorun, rebuild, génération
│   │   └── tasks/       # file de tâches (reconstructions)
│   ├── rules/           # règles YAML « fichier → réglage » (éditables)
│   │   └── rules.yaml
│   └── tests/
├── frontend/            # UI légère (HTMX ou Vue)
└── deploy/              # service systemd / conteneur
```

## Accès

Par défaut l'API n'écoute que sur `127.0.0.1:<port>`. Pour l'ouvrir au réseau,
poser **à la fois** `WSQUASHFS_MGR_HOST=0.0.0.0` et
`WSQUASHFS_MGR_PASSWORD=…` (jeton à passer dans l'en-tête `x-wsfs-token`) :
l'application lit et réécrit les images.

Les images sont désignées par leur identifiant `<système>/<nom>`
(`GET /api/image?id=arcade/gticlub`), deux systèmes pouvant contenir une image
du même nom.

## Édition de l'autorun

Depuis la fiche d'une image : formulaire des clés connues ou texte brut
(synchronisés), validations et aperçu du diff, puis reconstruction en tâche de
fond. L'image est montée (squashfuse), le nouvel autorun superposé
(fuse-overlayfs) et `mksquashfs` construit `<jeu>.wsquashfs.part` à côté ;
après vérification (table lisible, liste des fichiers attendue, contenu de
l'autorun), l'original devient `<jeu>.wsquashfs.old` jusqu'à « Valider » ou
« Revenir à l'ancienne ». Sans FUSE, l'image est extraite puis reconstruite.
Fins de ligne, encodage (UTF-8 ou cp1252), commentaires et ordre des lignes
sont conservés.

La file de tâches est enregistrée dans `WSQUASHFS_MGR_DATA_DIR`
(`~/.local/share/wsquashfs-manager`) : une tâche interrompue par un
redémarrage est remise en attente.

## Actions en masse

La bibliothèque se filtre (système, type, runner, manettes, sans version, a une
`.old`), se trie et permet une sélection multiple. Sur la sélection :

- opérations d'autorun appliquées dans l'ordre : poser / modifier / retirer une
  clé, remplacer par un modèle (`{name}`, `{system}` et les clés actuelles,
  `{CMD}`, `{DIR}`…) ; aperçu image par image (diff, validations, images
  bloquées) et espace disque par système de fichiers — chaque `.old` est gardé
  jusqu'à validation — puis une tâche de reconstruction par image ;
- valider ou revenir aux `.old` ;
- réinitialiser les sauvegardes (`saves/<jeu>` renommé en `.avant-<date>`) ;
- vérifier l'intégrité (table de l'image, autorun lisible), en tâche de fond.

## Décisions en attente (SPEC § 7)

1. Web ou bureau — **web** recommandé.
2. Où elle tourne : conteneur SteamBox ou conteneur séparé.
3. Rétention des `.old` : manuelle ou après N jours.
4. (Plus tard) fichier `<jeu>.wsquashfs.autorun` à côté de l'image, lu par le
   lanceur, ignoré par Batocera.

## Dépendances système

`squashfs-tools` (mksquashfs/unsquashfs), `squashfuse`, `fuse-overlayfs`.
Wine n'est pas nécessaire : l'analyse des exécutables et du registre se fait
en Python, et les fins de ligne de l'autorun sont gérées par l'application.
