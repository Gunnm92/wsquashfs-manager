"""Icônes sans équivalent dans les jeux libres (aujourd'hui la console SteamOS) : vectorise les sources choisies (src/*.png, via vec.py) et assemble frontend/assets/js/aicons-data.js,
avec le rôle de chaque partie (classes p-<rôle>, animées par css/aicons.css).
Usage : python build_icons.py   (nécessite vtracer et pillow)"""
import json, pathlib, re, subprocess, sys
S = pathlib.Path(__file__).parent
OUT = S.parent.parent / "frontend/assets/js/aicons-data.js"
# nom dans le manager : (source, épaisseur du trait en unités, {indice de partie: rôle}) ; rôle "" = partie fixe
ICONS = {
    "steamos":   ("f_deck", 1.6,      {1: "screen", 2: "stick", 3: "stick"}),
}
data = {}
(S / "build").mkdir(exist_ok=True)
for name, (src, target, roles) in ICONS.items():
    subprocess.run([sys.executable, str(S / "vec.py"), str(S / "src" / f"{src}.png"), str(S / "build" / f"{src}.json"), str(target)], check=True)
    parts = json.load(open(S / "build" / f"{src}.json"))
    data[name] = [[roles.get(i, ""), re.sub(r"-?\d+\.\d+", lambda m: f"{float(m.group()):.1f}".rstrip("0").rstrip("."), p["d"])] for i, p in enumerate(parts)]
OUT.write_text("// aicons-data.js — GÉNÉRÉ (Flux 2 Klein + vtracer) : [rôle de la partie, tracé] par icône, viewBox 0 0 24 24.\n"
               "// Ne pas modifier à la main : régénérer avec le script de construction des icônes.\n\n"
               "export const AICONS = " + json.dumps(data, separators=(",", ":")) + ";\n")
print(OUT, OUT.stat().st_size, "octets")
