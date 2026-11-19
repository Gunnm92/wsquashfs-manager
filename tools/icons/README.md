# Icônes animées du manager

`icon(nom)` (js/core.js) prend l'icône animée si elle existe, sinon l'icône Lucide du sprite. Une icône
animée porte le nom de l'icône du sprite ou du client qu'elle remplace (`settings`, `shield`, `linux`…).
Rendu par `frontend/assets/js/aicons.js`, animé par `css/aicons.css`.

## 1. Jeux libres via Iconify : `iconify.py` → `aicons-iconify.js`

La table `ICONS` du script dit quelle icône prendre pour chaque nom ; `python3 iconify.py` les télécharge
depuis l'API Iconify (sans dépendance). Parcourir les jeux : https://icon-sets.iconify.design/

- **line-md** (MIT) pour les actions et l'interface : animées en SMIL, le trait se dessine. Au repos
  `aicons.js` les fige dessinées ; elles rejouent au survol ; une variante `-loop` (si indiquée) tourne
  pendant le travail (`withBusy`). Les `id` des masques sont rendus uniques à chaque création.
- **Lucide** (ISC), **Tabler** (MIT) pour les clients, les manettes, l'essai et la
  reconstruction : fixes, le script ajoute `pathLength="1"` à leurs traits et la CSS les dessine au survol.
  **MingCute** (Apache 2.0, Linux) et **RemixIcon** (Apache 2.0, PlayStation) sont en formes pleines : pas de dessin du trait.

## 2. Flux 2 Klein : `build_icons.py` → `aicons-data.js`

Pour ce qui n'existe dans aucun jeu libre (aujourd'hui la console SteamOS).

1. **Génération** dans ComfyUI par la CLI `icon-studio` : `icones.json` fixe la charte (icône au trait
   façon Lucide, noir sur transparent) et le sujet ; `icon-studio jeu icones.json` → 4 propositions dans
   `gen/` (ignoré par git). Quand un sujet résiste, le mode « modifier » à partir d'une image existante
   marche mieux. Demander des parties **séparées** : seules les parties détachées peuvent être animées.
2. **Choix** : aplatir la meilleure proposition en noir sur blanc 512 px dans `src/f_<nom>.png`.
3. **Construction** (`build_icons.py`, nécessite `vtracer` et `pillow`) : vectorisation par `vec.py`
   (chaque composante du trait devient une partie), puis rôles (`p-screen`, `p-stick`…) par indice de partie.
