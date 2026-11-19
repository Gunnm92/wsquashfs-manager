"""Télécharge depuis l'API Iconify les icônes du manager qui viennent de jeux libres et écrit
frontend/assets/js/aicons-iconify.js (les autres icônes animées viennent de Flux, voir build_icons.py).
- line-md (MIT, Vjacheslav Trushkin) : animées en SMIL (le trait se dessine), variante « -loop » pendant le travail.
- Lucide (ISC), Tabler (MIT), Iconoir (MIT), MingCute (Apache 2.0) : fixes ; aicons.js et aicons.css
  leur ajoutent le même dessin du trait au survol (pathLength="1" sur chaque forme au trait).
Usage : python3 iconify.py   (sans dépendance)"""
import json, pathlib, re, urllib.request
OUT = pathlib.Path(__file__).parent.parent.parent / "frontend/assets/js/aicons-iconify.js"
LICENSES = {"line-md": "MIT", "lucide": "ISC", "tabler": "MIT", "iconoir": "MIT", "mingcute": "Apache-2.0", "ri": "Apache-2.0"}
# nom dans le manager (= icône du sprite ou client remplacé) : (icône, variante en boucle pendant le travail)
ICONS = {
    # actions et interface : line-md, animées
    "install":   ("line-md:download-outline", "line-md:download-outline-loop"),
    "eject":     ("line-md:upload-outline", "line-md:upload-outline-loop"),
    "trash":     ("line-md:trash", None),
    "settings":  ("line-md:cog", "line-md:cog-loop"),
    "tasks":     ("line-md:confirm-circle", None),
    "shield":    ("line-md:security", None),
    "sync":      ("line-md:rotate-270", None),
    "save":      ("line-md:folder-arrow-down", None),
    # clients, manettes, essai, reconstruction : jeux au trait
    "linux":     ("mingcute:linux-line", None),
    "steambox":  ("tabler:brand-steam", None),
    "batocera":  ("lucide:joystick", None),
    "server":    ("lucide:server", None),
    "xbox":      ("tabler:brand-xbox", None),
    "dualsense": ("ri:playstation-line", None),
    "flask":     ("lucide:flask-round", None),
    "hammer":    ("lucide:hammer", None),
}
wanted = {}
for pair in ICONS.values():
    for full in filter(None, pair):
        prefix, name = full.split(":")
        wanted.setdefault(prefix, set()).add(name)
got = {}
for prefix, names in wanted.items():
    req = urllib.request.Request(f"https://api.iconify.design/{prefix}.json?icons={','.join(sorted(names))}", headers={"User-Agent": "icon-fetch"})
    data = json.load(urllib.request.urlopen(req, timeout=30))
    for name in names:
        assert name in data["icons"], f"{prefix}:{name} introuvable"
        ic = data["icons"][name]
        w, h = ic.get("width", data.get("width", 24)), ic.get("height", data.get("height", 24))
        assert (w, h) == (24, 24), f"{prefix}:{name} n'est pas en 24×24"
        got[f"{prefix}:{name}"] = ic["body"]


def drawable(body):
    """Formes au trait d'une icône fixe : pathLength="1" pour que la CSS puisse dessiner le trait."""
    return re.sub(r"<(path|circle|rect|line|polyline|polygon|ellipse)\b(?![^>]*pathLength)", r'<\1 pathLength="1"', body)


out = {}
for key, (icon, loop) in ICONS.items():
    animated = icon.startswith("line-md:")
    stroked = 'stroke="currentColor"' in got[icon]          # MingCute dessine en formes pleines : pas de trait à dessiner
    draw = not animated and stroked
    body = drawable(got[icon]) if draw else got[icon]
    out[key] = {"src": icon, "body": body, **({"loop": got[loop]} if loop else {}), **({"draw": True} if draw else {})}
used = sorted({v["src"].split(":")[0] for v in out.values()})
OUT.write_text("// aicons-iconify.js — GÉNÉRÉ par tools/icons/iconify.py (API Iconify), viewBox 0 0 24 24.\n"
               + "".join(f"// {p} : licence {LICENSES[p]}\n" for p in used)
               + "\nexport const ICONIFY = " + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";\n")
print(OUT, OUT.stat().st_size, "octets")
