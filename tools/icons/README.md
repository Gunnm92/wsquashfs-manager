# Icônes animées du manager

Icônes des clients (Linux, SteamOS, SteamBox, Batocera, serveur, manettes Xbox et DualSense) et des
actions groupées (synchroniser, installer, désinstaller, copier vers le manager, supprimer).
Résultat : `frontend/assets/js/aicons-data.js` (tracés par partie), rendu par `aicons.js`,
animé par `css/aicons.css` (monochrome au repos, touche de couleur au survol).

## Chaîne

1. **Génération** (`comfy.py`) : Krea 2 Turbo + LoRA `krea2_style_reference` dans ComfyUI, avec
   `style_ref.png` (les icônes Lucide de l'interface) comme référence de style ; prompts dans `kjobs.json`.
   `COMFY_URL=http://127.0.0.1:8188 python comfy.py kjobs.json style_ref.png` → 4 propositions par icône.
2. **Choix** : copier la meilleure proposition dans `src/` (512 px suffisent).
3. **Construction** (`build_icons.py`, nécessite `vtracer` et `pillow`) : vectorisation par `vec.py`
   (chaque composante du trait devient une partie, épaissie vers l'épaisseur de trait indiquée),
   puis attribution des rôles (`p-led`, `p-lid`, `p-arrow`…) par indice de partie.
   Après un changement de source, vérifier les indices (`build/*.json`, boîtes englobantes).
