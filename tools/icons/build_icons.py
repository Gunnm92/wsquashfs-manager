"""Vectorise les sources choisies (src/*.png, via vec.py) et assemble frontend/assets/js/aicons-data.js,
avec le rôle de chaque partie (classes p-<rôle>, animées par css/aicons.css).
Usage : python build_icons.py   (nécessite vtracer et pillow)"""
import json, pathlib, re, subprocess, sys
S = pathlib.Path(__file__).parent
OUT = S.parent.parent / "frontend/assets/js/aicons-data.js"
# nom dans le manager : (source, épaisseur du trait en unités, {indice de partie: rôle}) ; rôle "" = partie fixe
ICONS = {
    "linux":     ("k_tux_3", 1.7,       {1: "beak"}),
    "steamos":   ("k_deck_1", 1.5,      {2: "screen", 1: "btn", 3: "btn", 4: "stick", 5: "stick"}),
    "steambox":  ("k_box_3", 1.9,       {1: "logo", 2: "logo", 3: "led"}),
    "batocera":  ("k_arcade_2", 1.8,    {1: "shine", 2: "btn-1", 3: "btn-2"}),
    "server":    ("k_server_3", 1.9,    {1: "led-1", 3: "led-2"}),
    "xbox":      ("k_xbox_0", 1.5,      {1: "btn-y", 3: "btn-x", 4: "btn-b", 7: "btn-a"}),
    "dualsense": ("k_dualsense_3", 1.5, {3: "btn-tri", 5: "btn-sq", 6: "btn-ci", 7: "btn-cr"}),
    "sync":      ("k_sync_3", 1.9,      {0: "arc", 1: "arc"}),
    "install":   ("k_install_3", 1.9,   {0: "arrow"}),
    "eject":     ("k_eject_3", 1.9,     {0: "arrow"}),
    "save":      ("k_save_3", 1.7,      {1: "shutter", 2: "label", 3: "label", 4: "label", 5: "label"}),
    "trash":     ("k_trash_0", 1.8,     {0: "lid"}),
}
data = {}
(S / "build").mkdir(exist_ok=True)
for name, (src, target, roles) in ICONS.items():
    subprocess.run([sys.executable, str(S / "vec.py"), str(S / "src" / f"{src}.png"), str(S / "build" / f"{src}.json"), str(target)], check=True)
    parts = json.load(open(S / "build" / f"{src}.json"))
    data[name] = [[roles.get(i, ""), re.sub(r"-?\d+\.\d+", lambda m: f"{float(m.group()):.1f}".rstrip("0").rstrip("."), p["d"])] for i, p in enumerate(parts)]
OUT.write_text("// aicons-data.js — GÉNÉRÉ (Krea 2 + vtracer) : [rôle de la partie, tracé] par icône, viewBox 0 0 24 24.\n"
               "// Ne pas modifier à la main : régénérer avec le script de construction des icônes.\n\n"
               "export const AICONS = " + json.dumps(data, separators=(",", ":")) + ";\n")
print(OUT, OUT.stat().st_size, "octets")
