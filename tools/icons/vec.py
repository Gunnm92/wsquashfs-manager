"""PNG d'icône (traits noirs sur blanc) → parties SVG en viewBox 0 0 24 24 (marge Lucide : contenu dans 2..22).
Chaque composante connexe du trait d'origine devient une partie, épaissie et tracée à part : les parties
restent distinctes (animables) même si l'épaississement les fait se toucher.
Usage : vec.py entrée.png sortie.json [épaisseur visée en unités, Lucide ≈ 2.4] [px par unité]"""
import json, re, sys, tempfile
from collections import deque
import vtracer
from PIL import Image, ImageFilter

src, dst = sys.argv[1], sys.argv[2]
TARGET = float(sys.argv[3]) if len(sys.argv) > 3 else 1.9
PX = int(sys.argv[4]) if len(sys.argv) > 4 else 12
N = 24 * PX

im = Image.open(src).convert("L")
bw = im.point(lambda v: 0 if v < 128 else 255)
x0, y0, x1, y1 = bw.point(lambda v: 255 - v).getbbox()
side = max(x1 - x0, y1 - y0) * 24 / 20                            # contenu dans 20 unités sur 24
cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
box = (round(cx - side / 2), round(cy - side / 2), round(cx + side / 2), round(cy + side / 2))
canvas = Image.new("L", (round(side), round(side)), 255)
canvas.paste(bw.crop((max(0, box[0]), max(0, box[1]), min(im.width, box[2]), min(im.height, box[3]))),
             (max(0, -box[0]), max(0, -box[1])))
small = canvas.resize((N, N), Image.LANCZOS).point(lambda v: 0 if v < 140 else 255)


def stroke_width(img):
    """Épaisseur moyenne du trait en unités : 2 × surface / périmètre."""
    px = img.load()
    area = per = 0
    for y in range(1, N - 1):
        for x in range(1, N - 1):
            if px[x, y] == 0:
                area += 1
                if px[x - 1, y] or px[x + 1, y] or px[x, y - 1] or px[x, y + 1]:
                    per += 1
    return 2 * area / max(per, 1) / PX


# Nombre de dilatations d'un pixel pour atteindre l'épaisseur visée (mesuré sur l'icône entière)
w0, grow, probe = stroke_width(small), 0, small
while stroke_width(probe) < TARGET and grow < 2 * PX:
    grow += 1
    probe = probe.filter(ImageFilter.MinFilter(3))

# Composantes connexes (8-voisinage) du trait d'origine
px = small.load()
label = [[0] * N for _ in range(N)]
comps = []
for y in range(N):
    for x in range(N):
        if px[x, y] == 0 and not label[y][x]:
            cid = len(comps) + 1
            q, pts = deque([(x, y)]), []
            label[y][x] = cid
            while q:
                a, b = q.popleft()
                pts.append((a, b))
                for da in (-1, 0, 1):
                    for db in (-1, 0, 1):
                        u, v = a + da, b + db
                        if 0 <= u < N and 0 <= v < N and px[u, v] == 0 and not label[v][u]:
                            label[v][u] = cid
                            q.append((u, v))
            comps.append(pts)
comps = [c for c in comps if len(c) >= PX * PX * 0.4]                # miettes ignorées


def trace(mask):
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    mask.convert("RGB").save(tmp)
    vtracer.convert_image_to_svg_py(tmp, tmp + ".svg", colormode="binary", mode="spline", filter_speckle=6,
                                    corner_threshold=60, length_threshold=4.0, splice_threshold=45, path_precision=2)
    out = []
    for d, tx, ty in re.findall(r'<path d="([^"]+)"[^>]*?transform="translate\(([-\d.]+),([-\d.]+)\)"', open(tmp + ".svg").read()):
        tx, ty = float(tx), float(ty)
        res, nums = [], []
        for t in re.findall(r"[A-Za-z]|-?\d*\.?\d+(?:e-?\d+)?", d):
            if t.isalpha():
                res.append(t)
                continue
            nums.append(float(t))
            if len(nums) == 2:
                res.append(f"{(nums[0] + tx) / PX:.2f} {(nums[1] + ty) / PX:.2f}")
                nums = []
        out.append(re.sub(r" ?([A-Za-z]) ?", r"\1", " ".join(res)))
    return out


parts = []
for pts in comps:
    mask = Image.new("L", (N, N), 255)
    mp = mask.load()
    for a, b in pts:
        mp[a, b] = 0
    for _ in range(grow):
        mask = mask.filter(ImageFilter.MinFilter(3))
    xs = [a for a, _ in pts]; ys = [b for _, b in pts]
    d = "".join(trace(mask))
    assert not any(c in d for c in "HVAhvacls"), "commande non gérée"
    parts.append({"d": d, "bbox": [round(min(xs) / PX, 2), round(min(ys) / PX, 2), round(max(xs) / PX, 2), round(max(ys) / PX, 2)]})
json.dump(parts, open(dst, "w"))
print(f"trait {w0:.2f} → +{grow}px ; {len(parts)} parties", [p["bbox"] for p in parts], sum(len(p["d"]) for p in parts), "car.")
